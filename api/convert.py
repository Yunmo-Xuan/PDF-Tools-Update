import io
import logging
import os
from core.i18n import t as _text


class ConvertMixin:
    def convert_to_docx(self, pdf_path, output_path):
        try:
            from pdf2docx import Converter as DocxConverter
            logging.info(f"转换 Word: {pdf_path}")
            cv = DocxConverter(pdf_path)
            try:
                cv.convert(output_path)
            finally:
                cv.close()
            return {"success": True, "file": output_path}
        except Exception as e:
            logging.exception("convert_to_docx 失败")
            return {"success": False, "message": str(e)}

    def convert_to_xlsx(self, pdf_path, output_path):
        try:
            import openpyxl
            import pdfplumber

            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            table_count = 0
            with pdfplumber.open(pdf_path) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    for t_idx, table in enumerate(page.extract_tables()):
                        if not table:
                            continue
                        table_count += 1
                        ws = wb.create_sheet(title=f"Page{page_num}_Table{t_idx + 1}")
                        for row in table:
                            ws.append([cell if cell is not None else "" for cell in row])

            if table_count == 0:
                # 没有表格时仍然给一个工作表，让用户拿到的是能打开的文件而不是报错
                wb.create_sheet(title=_text('convert_no_table_sheet')).append(
                    [_text('convert_no_table_cell')])

            wb.save(output_path)
            return {"success": True, "file": output_path, "tables": table_count}
        except Exception as e:
            logging.exception("convert_to_xlsx 失败")
            return {"success": False, "message": str(e)}

    def convert_to_pptx(self, pdf_path, output_path):
        try:
            import pymupdf as fitz
            from pptx import Presentation
            from pptx.util import Inches

            doc = fitz.open(pdf_path)
            try:
                if doc.page_count == 0:
                    return {"success": False, 'message': _text('api_pdf_empty')}

                prs = Presentation()
                prs.slide_width = Inches(13.333)
                prs.slide_height = Inches(7.5)
                skipped_pages = []
                added = 0

                for i in range(doc.page_count):
                    page = doc[i]
                    pw, ph = page.rect.width, page.rect.height
                    if pw <= 0 or ph <= 0:
                        skipped_pages.append(i + 1)
                        continue

                    # 直接喂内存流，不再落地临时 PNG：
                    # 少一次磁盘往返，也不再依赖系统临时目录可写
                    # （临时目录不可写时旧实现会整个转换失败）
                    buf = io.BytesIO(page.get_pixmap(dpi=150).tobytes("png"))

                    slide = prs.slides.add_slide(prs.slide_layouts[6])
                    # 关键修复：原实现把图片强行拉满整张幻灯片，
                    # 非 16:9 的页面会被拉伸变形。这里等比缩放并居中。
                    factor = min(prs.slide_width / pw, prs.slide_height / ph)
                    draw_w = int(pw * factor)
                    draw_h = int(ph * factor)
                    slide.shapes.add_picture(
                        buf,
                        int((prs.slide_width - draw_w) / 2),
                        int((prs.slide_height - draw_h) / 2),
                        width=draw_w,
                        height=draw_h,
                    )
                    added += 1

                if skipped_pages:
                    logging.warning(
                        f"转 PPT 跳过 {len(skipped_pages)} 页（页面尺寸为 0）: {skipped_pages}")
                if added == 0:
                    # 全被跳过还硬存的话会得到一个 0 页的 pptx，
                    # 用户以为转换成功，打开才发现是空的。
                    return {"success": False,
                            "message": _text('api_ppt_no_valid_sizes')}

                prs.save(output_path)
            finally:
                doc.close()
            return {"success": True, "file": output_path, "slides": added}
        except Exception as e:
            logging.exception("convert_to_pptx 失败")
            return {"success": False, "message": str(e)}

    def convert_to_images(self, pdf_path, output_dir, fmt="png"):
        try:
            import pymupdf as fitz
            from PIL import Image

            fmt = (fmt or "png").lower()
            if fmt not in ("png", "jpeg", "jpg"):
                return {"success": False, "message": _text('api_image_fmt_unsupported') % fmt}

            doc = fitz.open(pdf_path)
            try:
                if doc.page_count == 0:
                    return {"success": False, 'message': _text('api_pdf_empty')}

                base = os.path.splitext(os.path.basename(pdf_path))[0]
                os.makedirs(output_dir, exist_ok=True)
                files = []
                for i in range(doc.page_count):
                    pix = doc[i].get_pixmap(dpi=200)
                    if fmt in ("jpeg", "jpg"):
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        path = os.path.join(output_dir, f"{base}_page_{i + 1}.jpg")
                        img.save(path, quality=90)
                    else:
                        path = os.path.join(output_dir, f"{base}_page_{i + 1}.png")
                        pix.save(path)
                    files.append(path)
            finally:
                doc.close()
            return {"success": True, "files": files}
        except Exception as e:
            logging.exception("convert_to_images 失败")
            return {"success": False, "message": str(e)}

    def batch_split(self, file_list, every_n):
        files = [f for f in (file_list or []) if f]
        if not files:
            return {"success": False, 'message': _text('api_need_pdf')}
        results = [self.split_pdf(f, "every_n", every_n=every_n) for f in files]
        failed = [r for r in results if not r.get("success")]
        return {
            "success": not failed,
            "results": results,
            "total": len(results),
            "failed": len(failed),
        }

    def batch_convert(self, file_list, fmt):
        files = [f for f in (file_list or []) if f]
        if not files:
            return {"success": False, 'message': _text('api_need_pdf')}

        results = []
        for f in files:
            if fmt in ("png", "jpeg", "jpg"):
                results.append(self.convert_to_images(f, os.path.dirname(f), fmt))
            else:
                base = os.path.splitext(os.path.basename(f))[0]
                out = os.path.join(os.path.dirname(f), f"{base}.{fmt}")
                if fmt == "docx":
                    results.append(self.convert_to_docx(f, out))
                elif fmt == "xlsx":
                    results.append(self.convert_to_xlsx(f, out))
                elif fmt == "pptx":
                    results.append(self.convert_to_pptx(f, out))
                elif fmt == "tex":
                    # 导 LaTeX 的入口本来就在内容提取页那一格，这里只是让它能一次吃一批：
                    # 成品同样另存为 <原名>.tex，源文件不动（export_latex 自己还挡了「输出==源文件」）。
                    # 必须按关键字给 output_path——它第二个位置参数是 page_range，直接传过去
                    # 会被当成页码范围，然后报「请先选择保存位置」。
                    results.append(self.export_latex(f, output_path=out))
                else:
                    results.append({"success": False, "message": _text('api_fmt_unsupported') % fmt})

        failed = [r for r in results if not r.get("success")]
        return {
            "success": not failed,
            "results": results,
            "total": len(results),
            "failed": len(failed),
        }
