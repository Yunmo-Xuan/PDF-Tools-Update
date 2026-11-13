"""把 PDF 的内容导成纯文本或单文件 HTML（F09 / F10）。

和「格式转换」页的 pdf_to_word 分工不同：这里产出的是「拿走就能读」的成品，
不追求可编辑，所以全部走 PyMuPDF 的渲染侧抽取——pypdf 抽文本会丢排版、
也取不到内嵌图（方案 §4.1 的引擎分工）。
"""

import logging
import os
import re

from ._io import atomic_output
from ._range import select_pages
from core.i18n import t as _text

# 关掉「内嵌图片」时靠它剥掉 fitz 写的 <img src="data:image/...;base64,...">。
# base64 串里带换行，所以必须允许跨行匹配；base64 字母表不含 >，[^>] 不会多吃。
_IMG_RE = re.compile(r"<img\b[^>]*>", re.S)

# PyMuPDF 把中文写成 &#x4e2d; 这样的数字实体。浏览器显示没问题，但一本 3000 页
# 的中文书因此要胖三倍，用户在记事本里打开更是完全没法读。这里只把「非 ASCII」
# 的实体还原成真字（文件本身声明了 UTF-8）；&lt; &#60; 这类一律不动——
# 解出 < 就等于让 PDF 正文往 HTML 里塞标签。
_HEX_REF_RE = re.compile(r"&#x([0-9a-fA-F]{1,6});")


def _restore_cjk(text):
    """把非 ASCII 的十六进制字符实体换回真字符。"""
    def repl(match):
        code = int(match.group(1), 16)
        return chr(code) if code > 127 else match.group(0)
    return _HEX_REF_RE.sub(repl, text)

# 版式由 PyMuPDF 逐页生成（每页一个带宽高的 div），这里只补外壳与页间留白。
# position:relative 是给页面内嵌图用的：fitz 把图写成 position:absolute，
# 没有这个锚点，扫描件的图会全部飘到文档左上角。
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
    body { margin: 0; padding: 16px; background: #f0f2f5;
           font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
    div[id^="page"] { position: relative; background: #fff; margin: 0 auto 16px;
                      box-shadow: 0 1px 4px rgba(0, 0, 0, .15); }
</style>
</head>
<body>
__BODY__
</body>
</html>"""


class TextExportMixin:
    def export_text(self, pdf_path, page_range="", output_path=""):
        """
        逐页取纯文本，页与页之间空一行，写成一个 .txt。

        页码范围留空表示整册（和拆分页同一个口径）。输出 UTF-8 无 BOM：
        Win10 之后的记事本认 UTF-8，而带 BOM 的文件在 python/命令行里读会多出一个
        不可见字符，反而更难用。
        """
        import pymupdf as fitz
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}
            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    return {"success": False, "message": _text('api_export_text_locked')}
                pages, error = select_pages(page_range, doc.page_count)
                if error:
                    return {"success": False, "message": error}
                chunks = [doc[i - 1].get_text("text") for i in pages]
            finally:
                doc.close()

            text = "\n\n".join(chunks)
            with atomic_output(output_path) as f:
                f.write(text.encode("utf-8"))
            logging.info(f"文本导出完成: {pdf_path} 共 {len(pages)} 页 / {len(text)} 字符 -> {output_path}")
            # chars 为 0 是扫描件（页面只有图没有字），不算失败：
            # 前端拿这个数字提示用户「这份文件没有可提取的文字」。
            return {"success": True, "file": output_path, "pages": len(pages),
                    "chars": len(text.strip())}
        except Exception as e:
            logging.exception("export_text 失败")
            return {"success": False, "message": str(e)}

    def export_html(self, pdf_path, output_path="", embed_images=True):
        """
        整册导成一个自包含的 .html。

        图片一律内联成 data URI（实测 PyMuPDF 的 html 输出本来就这么写），
        所以文件可以直接发给别人、双击就能看，不许留 file:// 路径——
        那种链接换台机器、甚至换个目录就断了。
        embed_images 关掉时只剥掉 <img>，版式与文字照旧，文件能小一个数量级
        （实测 300 页扫描册：开图约 76 MB，1.2 秒）。
        """
        import pymupdf as fitz
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}
            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    return {"success": False, "message": _text('api_export_html_locked')}
                parts = [doc[i].get_text("html") for i in range(doc.page_count)]
                total = doc.page_count
            finally:
                doc.close()

            body = _restore_cjk("".join(parts))
            images = body.count("<img")
            if not embed_images:
                body = _IMG_RE.sub("", body)
            page = (_HTML_TEMPLATE
                    .replace("__TITLE__", os.path.splitext(os.path.basename(pdf_path))[0])
                    .replace("__BODY__", body))

            with atomic_output(output_path) as f:
                f.write(page.encode("utf-8"))
            logging.info(f"HTML 导出完成: {pdf_path} 共 {total} 页 / 图片 {images} 张"
                         f"（内嵌={bool(embed_images)}）-> {output_path}")
            return {"success": True, "file": output_path, "pages": total, "images": images}
        except Exception as e:
            logging.exception("export_html 失败")
            return {"success": False, "message": str(e)}

    def export_latex(self, pdf_path, page_range="", output_path="", password=""):
        """把 PDF 导成 LaTeX (.tex)：页面模型 → core.latex_out.blocks_to_tex。

        形状与 export_text 对齐：`{success, file, pages, chars, notes}`；失败给
        `{success: False, message: <字典键>}`。绝不覆盖源 PDF 或已有文件（成品另存路径
        由调用方给；若成品路径 == 源路径直接拒绝）。逐页解析走 jobctl 进度槽，
        中途取消则交回已解析的页并把取消说明放进 notes。表格靠 PyMuPDF find_tables
        现算（page_model 不给表格块），图片只出注释占位不造假文件名。
        """
        import pymupdf as fitz
        from core import page_model as _pm
        from core import latex_out
        from core import jobctl
        from ._io import same_path
        try:
            if not output_path:
                return {"success": False, "message": _text('api_need_save_path')}
            path = str(pdf_path or "")
            if not os.path.isfile(path):
                return {"success": False, "message": _text('api_file_not_found') % path}
            if same_path(output_path, path):
                return {"success": False, "message": _text('pm_output_is_source')}

            doc = fitz.open(path)
            models = []
            tables_info = {}
            notes = []
            try:
                if doc.needs_pass:
                    if not doc.authenticate(str(password or "")):
                        return {"success": False,
                                "message": _text('api_encrypted_need_password')}
                pages, error = select_pages(page_range, doc.page_count)
                if error:
                    return {"success": False, "message": error}
                if not pages:
                    return {"success": False,
                            "message": _text('api_latex_no_pages')}
                jobctl.start("export_latex", len(pages))
                try:
                    for i in pages:
                        if jobctl.should_stop():
                            notes.append(_text('api_latex_cancelled'))
                            break
                        models.append(_pm.build_model(doc, i - 1))
                        extracted = _extract_tables(doc[i - 1])
                        if extracted:
                            tables_info[len(models) - 1] = extracted
                        jobctl.advance()
                finally:
                    jobctl.finish()
            finally:
                doc.close()

            if not models:
                return {"success": False,
                        "message": _text('api_latex_cancelled')}
            options = {
                "tables": tables_info,
                "header_note": _text('latex_header_note'),
                "no_table_note": _text('latex_no_table_note'),
                "image_note": _text('latex_image_note'),
                "math_note": _text('latex_math_note'),
            }
            tex = latex_out.blocks_to_tex(models, options=options)
            with atomic_output(output_path) as f:
                f.write(tex.encode("utf-8"))
            logging.info("LaTeX 导出完成: %s 共 %d 页 / %d 字符 -> %s",
                         path, len(models), len(tex), output_path)
            return {"success": True, "file": output_path, "pages": len(models),
                    "chars": len(tex), "notes": notes}
        except Exception:
            logging.exception("export_latex 失败")
            return {"success": False, "message": _text('api_latex_failed')}


def _extract_tables(page):
    """现算一页里的表格：返回 [{'rows': [[cell,...],...], 'rect': [x0,y0,x1,y1]}]。

    page_model 只给 text/image/drawing 三种块，没有表格结构，所以表格在 api 层用
    PyMuPDF find_tables 单独取（core/latex_out.py 是纯函数不碰 fitz）。
    find_tables 在部分版本上没有 tables 属性或抛异常——都按「没有表格」处理，
    导出的 .tex 头部会写明「未识别出表格结构」。
    """
    try:
        tf = page.find_tables()
    except Exception:
        logging.exception("export_latex: find_tables 失败（这页按无表格处理）")
        return []
    raw = getattr(tf, "tables", None)
    if raw is None:
        try:
            raw = list(tf)
        except Exception:
            return []
    out = []
    for t in raw or []:
        try:
            data = t.extract() or []
            rows = [["" if c is None else str(c) for c in r] for r in data]
            bbox = getattr(t, "bbox", None)
            rect = [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])] if bbox else None
            if any(any(cell for cell in r) for r in rows):
                out.append({"rows": rows, "rect": rect})
        except Exception:
            logging.exception("export_latex: 单个表格整形失败，跳过")
            continue
    return out
