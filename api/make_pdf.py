"""从别的东西造出 PDF（F11 图片→PDF、F12 TXT→PDF）。

这两个能力都是「从零建文档」，所以走 PyMuPDF：一条 new_page + 插入的路，
不需要 pypdf（方案 §4.1）。中文写入统一走 api/_text.py，用户机器上没装中文字体
也能出字——这条坑在 core/fonts.py 里记过。
"""

import io
import logging
import os

from ._geom import is_fit, lookup_size
from ._io import save_pdf_doc, unique_output_path
from ._text import (make_font_ex, runs_measure, runs_write, style_fonts, subset_fonts,
                    wrap_line, wrap_runs, write_at)
from core.i18n import t as _text
from core.latex_inline import has_markup, parse as parse_inline

# 正文行距 = 字号 × 1.5：中文 1.0 会挤得看不清，2.0 又浪费纸。
# 和 _text.LINE_RATIO（页码横条的高度比 1.6）不是一回事，别合并。
_LINE_SPACING = 1.5


def _read_text_file(txt_path):
    """
    读文本文件内容，自动认编码。

    Windows 记事本存的中文 .txt 大多是 GBK（叫 ANSI），按 UTF-8 硬读会直接抛
    UnicodeDecodeError，用户看到的是「导出失败」而不是「编码不对」。
    顺序：UTF-8 带 BOM → UTF-8 → GBK → 最后按 UTF-8 容错替换，保证一定有结果。
    """
    with open(txt_path, "rb") as f:
        raw = f.read()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    logging.info(f"文本编码无法识别，按 UTF-8 容错读取: {txt_path}")
    return raw.decode("utf-8", errors="replace")


# EXIF 里方向标记的 tag 号；Pillow 用整数存，写死比 import ExifTags 少一层依赖。
_EXIF_ORIENTATION = 274


def _exif_orientation(img):
    """
    取图片的 EXIF 方向标记（F11），没有或读不懂都返回 1（＝不用转）。

    手机和相机竖着拍的照片，像素常常还是按横着存，靠这个 1–8 的标记让看图软件
    转正。PDF 里没有「方向标记」这个概念，直接引用原文件就会得到一张侧躺的
    扫描页；插入前的尺寸也会差一个 90°，「按原图成页」那条路会跟着错。
    EXIF 残缺是很常见的事（截屏、微信传过的图），不能因为读不动就把一张好图丢掉。
    """
    try:
        return int(img.getexif().get(_EXIF_ORIENTATION) or 1)
    except Exception:
        logging.info("EXIF 方向标记读不出来，按不转处理")
        return 1


class MakePdfMixin:
    def images_to_pdf(self, images, page_size="a4", keep_aspect=True, output_path=""):
        """
        多张图片合成一个 PDF，每张一页，顺序就是列表顺序。

        page_size 传 "fit"/"原图" 时按图片自身像素成页（1 像素 = 1 pt，
        扫描件这样能保住原始分辨率）；否则用标准幅面，图片在页面里居中。
        keep_aspect 默认 True：等比放进页面，长宽不同的图不会被拉变形。
        """
        import pymupdf as fitz
        from PIL import Image, ImageOps
        try:
            paths = [str(p) for p in (images or []) if p]
            if not paths:
                return {"success": False, "message": _text('api_mk_pick_images')}
            missing = [p for p in paths if not os.path.isfile(p)]
            if missing:
                return {"success": False, "message": _text('api_mk_image_missing') % missing[0]}

            fit_mode = is_fit(page_size)
            size = None if fit_mode else lookup_size(page_size)
            if size is None and not fit_mode:
                return {"success": False, "message": _text('api_mk_size_unknown') % page_size}

            doc = fitz.open()
            failed = []
            for path in paths:
                try:
                    # 只读图片头拿尺寸，不解码整张图（几千万像素的照片也不会卡住）；
                    # 带 EXIF 方向标记的那几张例外，必须解码才能把图转正
                    with Image.open(path) as img:
                        orientation = _exif_orientation(img)
                        if orientation != 1:
                            # 转完 img.format 会变成 None，下面的 needs_convert 自然把它
                            # 判成「不能直接引用原文件」——正是要的效果：原文件还是躺着的
                            img = ImageOps.exif_transpose(img)
                        px_w, px_h = img.size
                        # PDF 里没有 bmp/tiff/gif/webp 的位置，也不许拿原格式硬塞：
                        # 统一转成内存里的 PNG（带透明）或 JPEG，再喂给 insert_image。
                        # 这三类之外的 jpg/png 直接引用原文件，一个字节都不重编码。
                        needs_convert = img.format not in ("JPEG", "PNG") or (
                            img.mode not in ("RGB", "L", "1", "CMYK"))
                        stream = None
                        if needs_convert:
                            buf = io.BytesIO()
                            has_alpha = img.mode in ("RGBA", "LA", "P") and (
                                "transparency" in img.info or img.mode in ("RGBA", "LA"))
                            if has_alpha:
                                img.save(buf, format="PNG")
                            else:
                                img.convert("RGB").save(buf, format="JPEG", quality=92)
                            stream = buf.getvalue()

                    if fit_mode:
                        rect = fitz.Rect(0, 0, px_w, px_h)
                    else:
                        rect = fitz.Rect(0, 0, size[0], size[1])
                    page = doc.new_page(width=rect.width, height=rect.height)
                    if stream is not None:
                        page.insert_image(rect, stream=stream, keep_proportion=bool(keep_aspect))
                    else:
                        page.insert_image(rect, filename=path, keep_proportion=bool(keep_aspect))
                except Exception:
                    # 一张坏图不该拖垮整本：记下来继续，最后一起报给用户
                    logging.exception(f"图片插入失败，跳过: {path}")
                    failed.append(os.path.basename(path))

            pages = doc.page_count
            if pages == 0:
                doc.close()
                return {"success": False,
                        "message": _text('api_mk_no_image_readable') + (_text('api_mk_failed_one') % failed[0] if failed else "")}

            if not output_path:
                output_path = unique_output_path(os.path.join(
                    os.path.dirname(paths[0]),
                    f"{os.path.splitext(os.path.basename(paths[0]))[0]}_图片合集.pdf"))
            # save_pdf_doc 会顺手关掉文档，所以页数要在它之前取
            save_pdf_doc(doc, output_path, garbage=3, deflate=True)
            logging.info(f"图片转 PDF 完成: {len(paths)} 张图 -> {output_path}"
                         f"（{pages} 页，跳过 {len(failed)} 张）")
            result = {"success": True, "file": output_path, "pages": pages}
            if failed:
                result["failed"] = failed
                result["message"] = _text('api_mk_skipped_images') % len(failed)
            return result
        except Exception as e:
            logging.exception("images_to_pdf 失败")
            return {"success": False, "message": str(e)}

    def text_to_pdf(self, txt_path, output_path="", font_size=11, margins=56.7, font="",
                    latex=False):
        """
        文本文件分页排成 A4 PDF（F12）。

        margins 单位是 pt，默认 56.7 pt 正好是 2 厘米。
        font 是界面选的本机字体引用（族名/路径，见 core/fonts.py）；空 = 内置中文字体，
        选的读不出来会退回内置并在 message 里说明（页码/页眉页脚同一口径）。
        latex=True 才把每行按 LaTeX 标记解析（`\textbf{}`、`$x^2$` 那一小套）；
        默认关——文本文件里有反斜杠太常见（路径、代码、LaTeX 源码本身），
        自动当标记会把内容改掉。
        排版靠逐行算 y 坐标手写，而不是把整篇塞给 insert_textbox：
        后者放不下时只会静默少写几行，用户要到打印时才发现尾部没了。
        """
        import pymupdf as fitz
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}
            if not os.path.isfile(txt_path):
                return {"success": False, "message": _text('api_file_not_found') % txt_path}

            try:
                size_pt = float(font_size)
            except (TypeError, ValueError):
                return {"success": False, "message": _text('api_mk_font_not_number')}
            if not 6 <= size_pt <= 72:
                return {"success": False, "message": _text('api_mk_font_range')}

            try:
                margin = float(margins)
            except (TypeError, ValueError):
                return {"success": False, "message": _text('api_mk_margin_not_number')}
            if margin < 0 or margin > 200:
                return {"success": False, "message": _text('api_mk_margin_range')}

            text = _read_text_file(txt_path)
            if not text.strip():
                return {"success": False, "message": _text('api_mk_text_empty')}

            width, height = lookup_size("a4")
            left, right = margin, width - margin
            top, bottom = margin, height - margin
            if right - left < size_pt * 4:
                return {"success": False, "message": _text('api_mk_margins_too_big')}

            doc = fitz.open()
            font_obj, font_note = make_font_ex(fitz, str(font or "").strip())
            # 样式字体只在整本循环外算一次；开关没开就一个都不准备（老行为原样）
            latex_on = bool(latex)
            lfonts = None
            latex_notes = []
            if latex_on:
                lfonts, lfnote = style_fonts(fitz, str(font or "").strip())
                if lfnote:
                    latex_notes.append(lfnote)
            line_h = size_pt * _LINE_SPACING
            # 缓存跨行复用：常见字只量一次，3000 行的文件也不会退化成 3000 次测量
            width_cache = {}
            page = None
            y = 0.0
            for raw_line in text.replace("\t", "    ").splitlines():
                raw_line = raw_line.rstrip("\r")
                if latex_on and has_markup(raw_line):
                    items, notes = parse_inline(raw_line)
                    for n in notes:
                        if n.get('key') and n['key'] not in latex_notes:
                            latex_notes.append(n['key'])
                    for line_items in wrap_runs(lfonts, items, size_pt, right - left):
                        need = runs_measure(lfonts, line_items, size_pt)
                        if page is None or y + need[1] + need[2] > bottom:
                            page = doc.new_page(width=width, height=height)
                            y = top
                        if line_items:
                            runs_write(fitz, lfonts, page, left, y + need[1],
                                       line_items, size_pt)
                        y += max(need[1] + need[2], line_h)
                    continue
                for line in wrap_line(font_obj, raw_line, size_pt,
                                      right - left, width_cache):
                    if page is None or y + line_h > bottom:
                        page = doc.new_page(width=width, height=height)
                        y = top
                    if line:
                        write_at(fitz, font_obj, page, left, y + size_pt, line, size_pt)
                    y += line_h

            pages = doc.page_count
            # 新建的文档没有源文件可参照，字体裁剪后嵌进去的那点字模才不会白占体积
            subset_fonts(doc)
            save_pdf_doc(doc, output_path, garbage=3, deflate=True)
            logging.info(f"文本转 PDF 完成: {txt_path} -> {output_path}（{pages} 页，"
                         f"字号 {size_pt} pt / 边距 {margin} pt）")
            result = {"success": True, "file": output_path, "pages": pages}
            if font_note:
                result["message"] = _text(font_note)
            # LaTeX 标记里没生效的部分（认不出的命令、样式退面）逐条出声，别只报「成功」
            if latex_notes:
                msgs = [_text(k) for k in latex_notes if k]
                if msgs:
                    result["message"] = (result.get("message") or '') + \
                        ('\n' if result.get("message") else '') + '\n'.join(msgs)
                result["latex_notes"] = latex_notes
            return result
        except Exception as e:
            logging.exception("text_to_pdf 失败")
            return {"success": False, "message": str(e)}
