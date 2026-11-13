"""涂黑（F18）与敏感信息检测（F19）。

涂黑是唯一「真删内容」的手段：add_redact_annot 标范围 + apply_redactions 执行，
文字对象从内容流里移除，不是盖一层色块（方案 §4.4）。所以两步之后必须读回成品
再查一遍，确认原串搜不到了才敢说成功。

检测只报命中、绝不动文件：命中清单交给用户在界面上勾，勾完再交给涂黑。
身份证/手机/银行卡的判定用纯标准库正则，不引第三方（7.0.1 不加依赖是硬约束）。
"""

import logging
import os
import re
import time

from ._io import close_pdf, save_pdf_doc
from ._range import select_pages
from core.i18n import t as _text

# 一次调用最多扫多少页、攒多少条命中：桥是同步的，超时或返回几十万字都会卡住界面
BUDGET_MS = 3000
MAX_HITS = 2000

# 一整串数字（允许中间的空格/连字符分组，末尾允许身份证的校验码 X）。
# 这里只负责「框出候选」，到底是身份证、手机还是银行卡由 _classify 判，
# 免得三种写法各写一遍正则、各漏一种分隔习惯。
_RUN_RE = re.compile(r"(?<![\dXx])\d(?:[\d -]{9,22})[Xx]?(?![\dXx])")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"

# 涂黑时对「重叠到的图片」的三种处理（实测常量值 0/1/2）
IMAGE_MODES = {
    0: "忽略重叠图",              # PDF_REDACT_IMAGE_NONE
    1: "删掉整张图",              # PDF_REDACT_IMAGE_REMOVE
    2: "只把重叠部分涂白",        # PDF_REDACT_IMAGE_PIXELS
}


def _is_valid_id18(digits):
    """18 位身份证：出生日期合法 + GB 11643 校验位重算。"""
    if len(digits) != 18 or not digits[:17].isdigit():
        return False
    year, month, day = int(digits[6:10]), int(digits[10:12]), int(digits[12:14])
    if not 1900 <= year <= time.localtime().tm_year:
        return False
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return False
    total = sum(int(digits[i]) * _ID_WEIGHTS[i] for i in range(17))
    return digits[17].upper() == _ID_CHECK_CHARS[total % 11]


def _is_valid_id15(digits):
    """15 位老号：只有两位年份，没有校验位，所以只当「提示」不算命中。"""
    if len(digits) != 15 or not digits.isdigit():
        return False
    year, month, day = int(digits[6:8]), int(digits[8:10]), int(digits[10:12])
    return 1900 <= year + 1900 <= time.localtime().tm_year and 1 <= month <= 12 and 1 <= day <= 31


def _luhn_ok(digits):
    """银行卡 Luhn 校验（从右往左偶数位加倍，超过 9 减 9）。"""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _groups(raw):
    """按空格/连字符拆出的分组长度，用来卡住「两串无关数字被拼成一个假号码」。"""
    return [len(part) for part in re.split(r"[ \u3000-]+", raw.strip()) if part]


def _classify(raw):
    """
    把一串（可能带分组的）数字判成 (类别, 把握度)，判不出来返回 (None, None)。

    分组长度这一关不是洁癖：整页数字表里「2019 13812345678」按纯位数放行就会
    凑出一个假银行卡号（方案 §4.5 说的误报控制）。
    """
    digits = re.sub(r"[^0-9Xx]", "", raw)
    parts = _groups(raw)
    spaced = len(parts) > 1

    # 手机：1[3-9] 开头共 11 位；带分组时只认 3-4-4 这种常见写法
    if len(digits) == 11 and digits[0] == "1" and digits[1] in "3456789":
        if not spaced or parts in ([3, 4, 4], [3, 3, 5], [4, 7], [3, 8]):
            return "phone", "high"
    # 身份证 18 位：校验位过不了的一律不报
    if len(digits) == 18 and not spaced or (len(digits) == 18 and max(parts) <= 8):
        if _is_valid_id18(digits):
            return "id_card", "high"
    if len(digits) == 15 and not spaced and _is_valid_id15(digits):
        return "id_card_15", "notice"
    # 银行卡：13–19 位 + Luhn；带分组时按 4 位一组（6222 0212 3456 7890）
    if 13 <= len(digits) <= 19 and digits.isdigit() and _luhn_ok(digits):
        if not spaced or all(size == 4 for size in parts):
            return "bank_card", "suspect"
    return None, None


def _line_items(words):
    """
    把一页的 words 拼成「行」。

    words 每条形如 (x0, y0, x1, y1, text, block_no, line_no, word_no)，
    同一 (block_no, line_no) 的算一行；每个字符记住它来自哪个词，
    命中之后就能把矩形精确落到「这几个词」上，而不是整行涂掉。
    """
    lines = {}
    for word in words:
        lines.setdefault((word[5], word[6]), []).append(word)
    items = []
    for group in lines.values():
        group.sort(key=lambda w: (w[7], w[0]))
        text = ""
        owner = []
        for index, word in enumerate(group):
            if index:
                text += " "
                owner.append(index - 1)
            text += word[4]
            owner.extend([index] * len(word[4]))
        items.append((text, owner, group))
    return items


def _hit_rect(group, owner, start, end):
    """命中区间覆盖到哪几个词，返回它们的并集矩形。"""
    boxes = [group[i][:4] for i in sorted({owner[i] for i in range(start, min(end, len(owner)) + 1)})]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _inner_rect(fitz, page, box):
    """
    把界面上的可视矩形换算成这一页的 PDF 内部坐标（只有旋转页需要换）。

    add_redact_annot 和 get_text(clip=) 都按内部坐标算，而前端量到的是屏幕坐标，
    rot=90/270 的页上两者差一整条边（本机实测：可视 (60, 87.5) 的字内部读回 (87.5, 669.69)）。
    注记和「数这块里有几个字」必须用同一份换算结果，否则删掉的地方和被统计的地方不是同一处。
    """
    rect = fitz.Rect(*box)
    return rect * page.derotation_matrix if page.rotation else rect


def _scan_page(page, kinds):
    """
    扫一页，返回命中列表 [{page, rect, text, kind, confidence}]。

    rect 一律换算成「用户在屏幕上看到的坐标」：本机实测 get_text("words") 在旋转页上
    给的是 PDF 内部未旋转坐标（rot=90 页上可视 (60, 87.5) 的那串字，读回来是
    (87.5, 669.69)），而涂黑那边收的、以及界面画高亮框用的都是可视矩形。少换这一道，
    检测出来的框会整体偏 90°，勾着「看着对」的数字涂掉的是隔壁那行。
    """
    import pymupdf as fitz
    hits = []
    for text, owner, group in _line_items(page.get_text("words")):
        for match in _RUN_RE.finditer(text):
            kind, confidence = _classify(match.group(0))
            if not kind or (kinds and kind not in kinds):
                continue
            rect = _hit_rect(group, owner, match.start(), match.end() - 1)
            if page.rotation:
                rect = tuple(fitz.Rect(rect) * page.rotation_matrix)
            hits.append({"page": page.number + 1, "rect": [round(v, 2) for v in rect],
                         "text": match.group(0).strip(), "kind": kind,
                         "confidence": confidence})
    return hits


class RedactMixin:
    def apply_redaction(self, pdf_path, output_path="", targets=None, image_mode=2):
        """
        按矩形真删内容（F18）。

        targets 每项形如 {"page": 3, "rect": [x0, y0, x1, y1]}，坐标是用户在
        屏幕上看到的 pt（旋转页会先换算回 PDF 内部坐标）。
        image_mode 只管「矩形压到了图怎么办」：
          2 只把重叠部分涂白（扫描件涂身份证号必须用这个，默认）
          1 连整张图一起删（一页就是一张扫描图，选这个等于删掉整页）
          0 忽略图片，只删文字
        写完立刻重新打开成品逐条复查，读回还能搜到原字就不算成功。
        """
        import pymupdf as fitz
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}
            items = [t for t in (targets or []) if isinstance(t, dict)]
            if not items:
                return {"success": False, "message": _text('api_redact_no_targets')}
            try:
                mode = int(image_mode)
            except (TypeError, ValueError):
                mode = 2
            if mode not in IMAGE_MODES:
                return {"success": False, "message": _text('api_redact_mode_bad') % image_mode}

            by_page = {}
            for item in items:
                try:
                    page_no = int(item.get("page"))
                except (TypeError, ValueError):
                    return {"success": False, "message": _text('api_redact_page_invalid') % item.get('page')}
                if page_no < 1:  # 上界交给下面的 page_count 核对，这里不重复设一个假天花板
                    return {"success": False, "message": _text('api_redact_page_invalid') % item.get('page')}
                box = item.get("rect") or item.get("bbox")
                try:
                    x0, y0, x1, y1 = (float(v) for v in box)
                except (TypeError, ValueError):
                    return {"success": False, "message": _text('api_redact_rect_not4') % page_no}
                if x1 - x0 < 1 or y1 - y0 < 1:
                    return {"success": False, "message": _text('api_redact_rect_small') % page_no}
                by_page.setdefault(page_no, []).append((x0, y0, x1, y1))

            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    close_pdf(doc)
                    return {"success": False, "message": _text('api_redact_locked')}
                total = doc.page_count
                out_of_range = [p for p in by_page if not 1 <= p <= total]
                if out_of_range:
                    close_pdf(doc)
                    return {"success": False,
                            "message": _text('api_redact_page_missing') % (out_of_range[0], total)}

                removed = 0
                empty = 0
                for page_no, boxes in by_page.items():
                    page = doc[page_no - 1]
                    inner = [_inner_rect(fitz, page, b) for b in boxes]
                    # 先数一遍这块里有多少字：空矩形也计入「删掉了几处」就成了假数字
                    counts = [len(page.get_text("words", clip=r)) for r in inner]
                    for rect in inner:
                        page.add_redact_annot(rect, fill=(0, 0, 0))
                    # 一页只 apply 一次：逐条 apply 会把内容流反复重写好几遍
                    page.apply_redactions(images=mode)
                    removed += sum(1 for c in counts if c)
                    empty += sum(1 for c in counts if not c)
                save_pdf_doc(doc, output_path, garbage=3, deflate=True)
            except Exception:
                close_pdf(doc)
                raise

            # 读回核对：涂黑必须真的搜不到原字（方案 §4.4，只在页面上盖黑块不算通过）
            residual = 0
            check = fitz.open(output_path)
            try:
                for page_no, boxes in by_page.items():
                    page = check[page_no - 1]
                    for box in boxes:
                        residual += len(page.get_text("words", clip=_inner_rect(fitz, page, box)))
            finally:
                check.close()

            logging.info(f"涂黑完成: {pdf_path} 处理 {len(items)} 处 / {len(by_page)} 页，"
                         f"有内容 {removed} 处 / 空矩形 {empty} 处，图片方式={IMAGE_MODES[mode]} "
                         f"-> {output_path} {os.path.getsize(output_path)} 字节")
            if residual:
                return {"success": False, "removed": removed, "residual": residual,
                        "message": _text('api_redact_residual') % residual}
            return {"success": True, "file": output_path, "removed": removed, "empty": empty,
                    "pages": len(by_page), "image_mode": mode}
        except Exception as e:
            logging.exception("apply_redaction 失败")
            return {"success": False, "message": str(e)}

    def detect_sensitive_info(self, pdf_path, kinds=None, page_range="", start_index=0,
                              budget_ms=BUDGET_MS):
        """
        扫描身份证/手机/银行卡号，只返回命中清单（F19，不写文件）。

        分片 + 限时：实测逐页 words 抽取约 1 ms/页，3000 页整册要 3 s 以上，
        超过桥层同步调用的 5 s 软上限（方案 §4.13）。所以这里每次最多扫
        budget_ms 毫秒就返回，带上 next_index 让前端接着调——
        取消就是前端不再调下一次，服务端不留任何状态。

        返回 {success, hits, done, next_index, scanned_pages}。
        hits 每项含 page / rect(pt) / text / kind / confidence。
        kind=id_card_15 与 bank_card 只是「提示/疑似」，界面上不该默认勾选。
        """
        import pymupdf as fitz
        started = time.perf_counter()
        try:
            if isinstance(kinds, str):
                kinds = [k for k in kinds.replace("，", ",").split(",") if k]
            wanted = set(kinds or ())
            unknown = wanted - {"id_card", "id_card_15", "phone", "bank_card"}
            if unknown:
                return {"success": False, "hits": [], "done": True, "next_index": 0,
                        "message": _text('api_redact_kind_unknown') % ', '.join(sorted(unknown))}

            doc = fitz.open(pdf_path)
            hits = []
            try:
                if doc.needs_pass:
                    return {"success": False, "hits": [], "done": True, "next_index": 0,
                            "message": _text('api_detect_locked')}
                pages, error = select_pages(page_range, doc.page_count)
                if error:
                    return {"success": False, "hits": [], "done": True, "next_index": 0,
                            "message": error}
                try:
                    index = max(0, int(start_index))
                except (TypeError, ValueError):
                    index = 0
                if index >= len(pages):
                    return {"success": True, "hits": [], "done": True,
                            "next_index": len(pages), "scanned_pages": 0}

                scanned = 0
                truncated = False
                while index < len(pages):
                    page = doc[pages[index] - 1]
                    page_hits = _scan_page(page, wanted)
                    hits.extend(page_hits)
                    index += 1
                    scanned += 1
                    if len(hits) >= MAX_HITS:
                        truncated = True
                        break
                    if (time.perf_counter() - started) * 1000 >= max(200, float(budget_ms)):
                        truncated = True
                        break
            finally:
                doc.close()

            logging.info(f"敏感信息检测: {pdf_path} 本轮扫 {scanned} 页，累计命中 {len(hits)} 条，"
                         f"{'未扫完' if truncated else '已扫完'}")
            result = {"success": True, "hits": hits, "done": not truncated,
                      "next_index": index, "scanned_pages": scanned,
                      "truncated": truncated}
            if truncated and not hits:
                result["message"] = _text('api_detect_empty_chunk')
            return result
        except Exception as e:
            logging.exception("detect_sensitive_info 失败")
            return {"success": False, "hits": [], "done": True, "next_index": 0,
                    "message": str(e)}
