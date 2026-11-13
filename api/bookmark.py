r"""书签（F22）：读书签、导出/导入 JSON、按书签拆分文件。只动书签树，不动页面内容。

三个实测出来的坑决定了这里大半的写法（方案 §4.8）：
  1. PyMuPDF 的 set_toc 对页码越界是静默夹取而不是报错（单页文档写 page=2 读回 1，
     1000 页文档末条写 99999 读回 1000）。所以必须在写之前自己按 page_count 拦，
     并且重新打开成品逐条核对——success 不算数。
  2. set_toc 要求首条 1 级、相邻两条最多深一级，违反时抛的是 "bad hierarchy level
     in row N" 这种没人看得懂的英文。所以先把层级压成合规形状，并如实报改了几条。
  3. 记事本「UTF-8」存出来的文件带 BOM，json.loads 炸在第一个字符上，用户只看到
     "Expecting value"。所以 BOM 要单独认出来并说人话。

拆分走 pypdf 复制页，和 split_merge.py 同一口径：拆出来的页与原文同源，/Rotate 原样带着。
"""

import json
import logging
import os
import re
import time

from core.i18n import t as _text

from ._io import atomic_output, close_pdf, save_pdf_doc, unique_output_path

# 问题清单最多列几条：再多界面就铺不开了，剩下的用「…共 N 条」收尾
_MAX_LISTED = 5
# 文件名里标题的长度上限（前面的两位序号不计），再长资源管理器只显示省略号
_NAME_CAP = 60
# 书签标题当文件名用是拆分的常态，但 Windows 这九个字符加控制符不许出现。序号前缀
# 让 CON/NUL 这类保留名不算问题：Windows 只拦裸的 CON 和 CON.pdf，不拦 01_CON.pdf。
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 记事本「UTF-8」默认加 BOM，json.loads 炸在第一个字符上，用户只看到 "Expecting
# value: line 1 column 1"。这两句是替他省下折腾时间的
_BOM_FILE = ("书签文件开头有 BOM（记事本存「UTF-8」时默认就会加）。请用记事本「另存为」，编码选 UTF-8，"
             "并把「ANSI/UTF-8 with BOM」换成不带 BOM 的写法后再导入")
_BOM_PASTE = ("粘贴进来的书签内容开头有 BOM（记事本存「UTF-8」时默认会加，BOM 也跟着被粘过来了）。"
              "请删掉开头那个看不见的字符，或改用「选择文件」导入无 BOM 的 UTF-8 文件")


def _opts(opt):
    """选项块：字典直接用，JSON 字符串顺手认一下（前端有时直接 stringify）。

    pagelayout.py 里有个同类的 _as_opt，但那是模块私有函数——为了三行代码去改别人
    正在改的文件不划算，这里留一份口径完全一样的。
    """
    if opt is None:
        return {}
    if isinstance(opt, dict):
        return opt
    if isinstance(opt, str):
        try:
            loaded = json.loads(opt)
        except ValueError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def _to_int(value):
    """容忍 "3" / 3.0 这类来自 JSON 和表单的写法；取不出整数就返回 None（不猜）。"""
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num != int(num):  # NaN / 带小数
        return None
    return int(num)


def _beside(pdf_path, name, ext):
    """源文件同目录拼一个派生名字：<stem>_书签.json / <stem>_导入书签.pdf / <stem>_按书签拆分。"""
    return os.path.join(os.path.dirname(os.path.abspath(pdf_path)),
                        os.path.splitext(os.path.basename(pdf_path))[0] + name + ext)


def _not_there(pdf_path):
    """没选文件 / 文件不在，就地给一句人话，而不是把 FileNotFound 的英文原文甩给用户。"""
    if not pdf_path or not os.path.isfile(pdf_path):
        return {"success": False, "message": _text('api_bm_pdf_missing') % (pdf_path or _text('api_bm_no_file_chosen'))}
    return None


def _describe(items):
    """把有问题的书签写成「第 3 条「第一章」→ 第 99999 页」，超过 5 条就收尾。

    page 是 -1 表示这条书签压根不指向本文的某一页（实测 get_toc 对「打开网址」这类
    外链书签就是返回 -1，不是 0 也不是越界的大数），这种书签当切点没有意义，得单说。
    """
    head = ["第 %d 条「%s」%s" % (i + 1, it["title"],
                                  "→ 第 %s 页" % it["page"] if it["page"] >= 1 else
                                  "（页码 %s，指向文档外）" % it["page"])
            for i, it in enumerate(items[:_MAX_LISTED])]
    if len(items) > _MAX_LISTED:
        head.append("…共 %d 条" % len(items))
    return "；".join(head)


def _read_rows(bookmarks):
    """书签输入统一成列表：接受 Python 列表、JSON 字符串、.json 文件路径三种写法。

    界面既有「选择文件」也有粘贴框，所以字符串要自己判断像不像 JSON——像就解析，
    不像就当路径。返回值是 (列表, 错误信息)。
    """
    if not bookmarks:
        return None, "没有可导入的书签，请先选择书签文件或粘贴书签内容"
    if isinstance(bookmarks, (list, tuple)):
        return list(bookmarks), None
    if isinstance(bookmarks, dict):
        return None, "书签内容要是一个列表（[{...}, {...}]），现在只有一个对象"
    if not isinstance(bookmarks, str):
        return None, "不认识的书签输入类型: %s" % type(bookmarks).__name__
    text = bookmarks.strip()
    where = "书签内容"
    if text.startswith("\ufeff"):  # 粘贴板一样能把 BOM 带进来
        return None, _BOM_PASTE
    if text[:1] not in ("[", "{"):  # 不像 JSON 就当文件路径读，字节先过一遍 BOM/编码
        where = "书签文件 %s 里的内容" % bookmarks
        if not os.path.isfile(bookmarks):
            return None, "书签文件不存在: %s" % bookmarks
        try:
            with open(bookmarks, "rb") as handle:
                raw = handle.read()
        except OSError as e:
            return None, "读不了书签文件 %s: %s" % (bookmarks, e)
        if raw.startswith(b"\xef\xbb\xbf"):
            return None, _BOM_FILE
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "书签文件不是 UTF-8 编码，请用记事本「另存为」UTF-8（无 BOM）后再导入"
    try:
        loaded = json.loads(text)
    except ValueError as e:
        return None, "%s不是合法的 JSON: %s" % (where, e)
    if isinstance(loaded, dict):  # 整份文件只写了一条书签，也认
        loaded = [loaded]
    if not isinstance(loaded, list):
        return None, "书签内容要是一个列表（[{...}, {...}]），现在是 %s" % type(loaded).__name__
    return loaded, None


def _validate(rows, page_count):
    """逐条体检，返回 (合法条目, 页码越界条目, 致命错误列表)。

    只有「页码越界」能被 skip_invalid 丢掉；层级/标题/结构问题属于输入本身写错了，
    静默丢弃等于把用户的书签改没，所以一律致命。认不出的键（备注之类）直接忽略。
    """
    clean, out_of_range, errors = [], [], []
    for index, raw in enumerate(rows):
        where = "第 %d 条" % (index + 1)
        if not isinstance(raw, dict):
            errors.append("%s不是一条书签（应为对象，实际是 %s）" % (where, type(raw).__name__))
            continue
        level = 1 if raw.get("level") is None else _to_int(raw.get("level"))  # 没写层级当天花板
        if level is None or level < 1:
            errors.append("%s的层级「%s」不合法：层级必须是不小于 1 的整数，0 和负数都不行"
                          % (where, raw.get("level")))
            continue
        if raw.get("page") is None:
            errors.append("%s没有页码" % where)
            continue
        page = _to_int(raw.get("page"))
        if page is None:
            errors.append("%s的页码「%s」不是整数" % (where, raw.get("page")))
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            errors.append("%s的标题是空的，书签必须有名字" % where)
            continue
        item = {"level": level, "title": title, "page": page}
        (out_of_range if not 1 <= page <= page_count else clean).append(item)
    return clean, out_of_range, errors


def _normalize_levels(rows):
    """把层级压成 set_toc 认的形状：首条 1 级，相邻两条最多深一级。返回 (新列表, 改动条数)。

    只做「变浅」不做「变深」：set_toc 只禁止跨级下潜，压浅了顶多目录扁一点，不会丢
    书签，也不会把英文异常甩给用户。
    """
    out, prev, changed = [], 0, 0
    for row in rows:
        level = min(row["level"], prev + 1)
        changed += level != row["level"]
        out.append({"level": level, "title": row["title"], "page": row["page"]})
        prev = level
    return out, changed


def _toc_rows(doc):
    """把 doc.get_toc() 转成 [{'level','title','page'}]，页码保持 1 基。"""
    return [{"level": int(row[0]), "title": str(row[1]), "page": int(row[2])} for row in doc.get_toc(simple=True)]


def _read_tree(pdf_path, doing):
    """打开 → 读书签树 → 关掉，返回 ((条目, 页数), None) 或 (None, 失败结果)。

    get_bookmarks 和 split_by_bookmarks 只要书签树不要句柄，读完就放手；needs_pass
    必须先判，加密文档上调 get_toc 抛的是 "cannot initialize - document still
    encrypted"（实测）。doing 拼进加密那句，告诉用户是为哪一步要他先解密。
    """
    import pymupdf as fitz
    doc = fitz.open(pdf_path)
    try:
        if doc.needs_pass:
            return None, {"success": False, "message": _text('api_pdf_encrypted_doing') % doing}
        return (_toc_rows(doc), doc.page_count), None
    finally:
        close_pdf(doc)


def _safe_name(title):
    """书签标题 → 能落地的文件名（非法字符换空格、空白压一个、末尾点号和空格剪掉）。"""
    name = re.sub(r"\s+", " ", _FORBIDDEN.sub(" ", str(title or ""))).strip().rstrip(". ")
    if len(name) > _NAME_CAP:
        name = name[:_NAME_CAP].rstrip(". ")
    return name or "未命名书签"


class BookmarkMixin:
    def get_bookmarks(self, pdf_path):
        """读书签树（F22）。空书签不是错误——界面要能客气地说「这份 PDF 没有书签」。"""
        started = time.perf_counter()
        try:
            refusal = _not_there(pdf_path)
            if refusal:
                return refusal
            tree, refusal = _read_tree(pdf_path, _text('api_bm_doing_read'))
            if refusal:
                return refusal
            bookmarks, page_count = tree
            logging.info("书签读取完成: %s 共 %d 页 / %d 条书签 / %.2fs"
                         % (pdf_path, page_count, len(bookmarks), time.perf_counter() - started))
            result = {"success": True, "bookmarks": bookmarks, "page_count": page_count}
            if not bookmarks:
                result["message"] = _text('api_bm_no_bookmarks')
            return result
        except Exception as e:
            logging.exception("get_bookmarks 失败")
            return {"success": False, "message": str(e)}

    def export_bookmarks(self, pdf_path, output_path=""):
        """书签导出成 JSON（UTF-8 无 BOM、ensure_ascii=False，用户打开就是中文）。"""
        started = time.perf_counter()
        try:
            refusal = _not_there(pdf_path)
            if refusal:
                return refusal
            tree, refusal = _read_tree(pdf_path, _text('api_bm_doing_export'))
            if refusal:
                return refusal
            bookmarks = tree[0]
            if not output_path:  # 程序自己起的名字不许撞掉用户已有的文件
                output_path = unique_output_path(_beside(pdf_path, "_书签", ".json"))
            # 用 utf-8 而不是 utf-8-sig 编码：后者会写 BOM，而 BOM 会把这份文件挡在
            # 下一次导入门外（连自己也 import 不了自己），实测记事本就是这么坑用户的
            payload = json.dumps(bookmarks, ensure_ascii=False, indent=2).encode("utf-8")
            with atomic_output(output_path) as handle:
                handle.write(payload)
            logging.info("书签导出完成: %s -> %s 共 %d 条 / %.2fs"
                         % (pdf_path, output_path, len(bookmarks), time.perf_counter() - started))
            return {"success": True, "file": output_path, "count": len(bookmarks)}
        except Exception as e:
            logging.exception("export_bookmarks 失败")
            return {"success": False, "message": str(e)}

    def import_bookmarks(self, pdf_path, output_path="", bookmarks=None, opt=None):
        """导入书签（F22）。bookmarks 认列表 / JSON 字符串 / .json 路径三种写法。

        opt：mode = replace（默认，整树覆盖）/ append（保留原有书签再接上新的，结果
        里如实说走了哪条）；skip_invalid = True 时丢弃页码越界的条目并报出。
        """
        import pymupdf as fitz
        started = time.perf_counter()
        try:
            refusal = _not_there(pdf_path)
            if refusal:
                return refusal
            opt = _opts(opt)
            if opt is None:
                return {"success": False, "message": _text('api_bm_import_opt_bad')}
            mode = str(opt.get("mode") or "replace").strip().lower()
            if mode not in ("replace", "append"):
                return {"success": False, "message": _text('api_bm_import_mode_unknown') % mode}
            rows, error = _read_rows(bookmarks)
            if error:
                return {"success": False, "message": error}

            doc, saved = fitz.open(pdf_path), False
            try:
                if doc.needs_pass:
                    return {"success": False, "message": _text('api_pdf_encrypted_doing') % _text('api_bm_doing_import')}
                page_count = doc.page_count
                clean, out_of_range, errors = _validate(rows, page_count)
                if errors:
                    return {"success": False,
                            "message": _text('api_bm_issue_count') % (len(errors), "；".join(errors[:_MAX_LISTED]))}
                # 越界拦在写之前：set_toc 不会报错，它只会悄悄把页码夹回文档里（见文件头）
                if out_of_range and not opt.get("skip_invalid"):
                    return {"success": False,
                            "message": _text('api_bm_out_of_range_import') % (len(out_of_range), page_count,
                                                                  _describe(out_of_range))}
                if not clean:
                    return {"success": False,
                            "message": (_text('api_bm_skip_all_empty') % len(out_of_range))
                            if out_of_range else _text('api_bm_none_to_import')}
                base, dropped_source = [], []
                if mode == "append":
                    # 原有书签过同一套体检：里面同样可能藏着指向文档外的条目，
                    # 直接拼进 set_toc 就会踩上「末条静默夹取」那个坑
                    base, dropped_source, source_errors = _validate(_toc_rows(doc), page_count)
                    if source_errors:
                        return {"success": False,
                                "message": _text('api_bm_existing_issue')
                                           % (len(source_errors), "；".join(source_errors[:_MAX_LISTED]))}
                expected, changed = _normalize_levels(base + clean)
                doc.set_toc([[r["level"], r["title"], r["page"]] for r in expected])
                if not output_path:
                    output_path = unique_output_path(_beside(pdf_path, "_导入书签", ".pdf"))
                save_pdf_doc(doc, output_path, garbage=3, deflate=True)  # 它负责关 doc
                saved = True
            finally:
                if not saved:
                    close_pdf(doc)

            # 成品读回核对（方案 §「成品读回核对」）：success 不算数，只有重新打开
            # 读到的书签树和预期逐条一致才算导入成功
            check = fitz.open(output_path)
            try:
                back = _toc_rows(check)
            finally:
                close_pdf(check)
            if back != expected:
                diff = next((i for i, pair in enumerate(zip(back, expected)) if pair[0] != pair[1]),
                            min(len(back), len(expected)))
                want = expected[diff] if diff < len(expected) else _text('api_bm_more_rows')
                got = back[diff] if diff < len(back) else _text('api_bm_fewer_rows')
                return {"success": False,
                        "message": _text('api_bm_verify_mismatch')
                                   % (output_path, diff + 1, want, got)}

            skipped = out_of_range + dropped_source
            logging.info("书签导入完成: %s 方式=%s 写入 %d 条 / 跳过 %d 条 / 层级压平 %d 条 -> %s / %.2fs"
                         % (pdf_path, mode, len(expected), len(skipped), changed, output_path,
                            time.perf_counter() - started))
            result = {"success": True, "file": output_path, "count": len(expected),
                      "skipped": skipped, "mode": mode, "levels_normalized": changed}
            if skipped:
                result["message"] = _text('api_bm_skipped_list') % (len(skipped), _describe(skipped))
            return result
        except Exception as e:
            logging.exception("import_bookmarks 失败")
            return {"success": False, "message": str(e)}

    def split_by_bookmarks(self, pdf_path, output_dir="", level=1, opt=None):
        """按第 level 层书签拆文件（F22）。

        第 i 条书签管到「下一条同层书签的前一页」，最后一条管到文档末页。
        opt["keep_outline"]=True 才把原书签树带进每个分册（默认不带：实测 pypdf 的
        append 默认 import_outline=True，会把整棵原书签塞进每个分册、页码全是错的）。
        """
        from pypdf import PdfReader, PdfWriter
        started = time.perf_counter()
        try:
            refusal = _not_there(pdf_path)
            if refusal:
                return refusal
            opt = _opts(opt)
            if opt is None:
                return {"success": False, "message": _text('api_bm_split_opt_bad')}
            level = _to_int(level)
            if level is None or level < 1:
                return {"success": False, "message": _text('api_bm_split_level_bad') % level}
            tree, refusal = _read_tree(pdf_path, _text('api_bm_doing_split'))
            if refusal:
                return refusal
            toc, page_count = tree
            if not toc:
                return {"success": False, "message": _text('api_bm_no_bookmarks_split')}
            marks = [r for r in toc if r["level"] == level]
            if not marks:  # 只说「没有书签」等于没说，得把真有哪几层报出来
                return {"success": False, "message": _text('api_bm_level_none')
                          % (level, "、".join(str(v) for v in sorted({r["level"] for r in toc})))}
            bad = [r for r in marks if not 1 <= r["page"] <= page_count]
            if bad:
                return {"success": False,
                        "message": _text('api_bm_out_of_range_split')
                                   % (len(bad), page_count, _describe(bad))}

            # 书签页码乱序很常见（手工加的多半从后往前加），不排就会拆出负长度区间。
            # 排完如实报「顺序动过」，而不是假装用户写的就是升序
            ordered = sorted(marks, key=lambda r: r["page"])
            reordered = [r["page"] for r in ordered] != [r["page"] for r in marks]
            cuts, duplicates = [], 0
            for row in ordered:
                if cuts and cuts[-1]["page"] == row["page"]:
                    duplicates += 1  # 同页两条会拆出一个 0 页的空分册，那种文件打不开
                    continue
                cuts.append(row)

            output_dir = output_dir or _beside(pdf_path, "_按书签拆分", "")
            os.makedirs(output_dir, exist_ok=True)
            reader = PdfReader(pdf_path)
            total_pages = len(reader.pages)
            keep_outline = bool(opt.get("keep_outline"))

            files, emitted = [], 0
            for index, row in enumerate(cuts):
                start = row["page"]
                end = cuts[index + 1]["page"] - 1 if index + 1 < len(cuts) else page_count
                # pypdf 6.20.1 实测：页码参数名是 pages（page_range 这个关键字不存在，
                # 传进去直接 TypeError）；值只认 (start, stop[, step]) 元组、整数列表或
                # PageRange，range 对象会被拒（「"pages" must be a tuple ... or a list」）。
                # 元组是 0 基左闭右开，stop 超过总页数会抛 IndexError，所以 end 先夹一下。
                writer = PdfWriter()
                writer.append(reader, pages=(start - 1, min(end, total_pages)), import_outline=keep_outline)
                target = unique_output_path(os.path.join(output_dir,
                                                         "%02d_%s.pdf" % (index + 1, _safe_name(row["title"]))))
                with atomic_output(target) as handle:
                    writer.write(handle)
                count = end - start + 1
                emitted += count
                files.append({"file": target, "title": row["title"], "start": start, "end": end, "pages": count})

            skipped_pages = cuts[0]["page"] - 1
            notes = []
            if skipped_pages:
                notes.append(_text('api_bm_lead_pages_note') % skipped_pages)
            if reordered:
                notes.append(_text('api_bm_reordered_note'))
            if duplicates:
                notes.append(_text('api_bm_dup_rows_note') % duplicates)
            logging.info("按书签拆分完成: %s 第 %d 层 %d 条 -> %d 个文件 / 共 %d 页 / 前置跳过 %d 页 / %.2fs"
                         % (pdf_path, level, len(cuts), len(files), emitted, skipped_pages,
                            time.perf_counter() - started))
            return {"success": True, "files": files, "skipped_pages": skipped_pages,
                    "reordered": reordered, "level": level,
                    "message": _text('api_bm_split_done')
                               % (level, len(files), emitted, ("；" + "；".join(notes)) if notes else "")}
        except Exception as e:
            logging.exception("split_by_bookmarks 失败")
            return {"success": False, "message": str(e)}
