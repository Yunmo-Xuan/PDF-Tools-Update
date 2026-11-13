import logging
import os

from ._io import atomic_output, unique_output_path
from ._range import parse_page_range
from core.i18n import t as _text


class SplitMergeMixin:
    def _parse_page_range(self, range_str, total):
        # 解析逻辑收进 api/_range.py：文本导出、图片提取、涂黑、书签拆分都要认
        # 「1-5, 8」这种写法，各处自己解析迟早会出现「拆分认得、导出认不得」的口径差。
        return parse_page_range(range_str, total)

    def split_pdf(self, pdf_path, mode, every_n=1, range_str=""):
        from pypdf import PdfReader, PdfWriter
        try:
            reader = PdfReader(pdf_path)
            total = len(reader.pages)
            if total == 0:
                return {"success": False, 'message': _text('api_pdf_empty')}

            base_name = os.path.splitext(os.path.basename(pdf_path))[0]
            output_dir = os.path.dirname(pdf_path)
            files = []

            if mode == "every_n":
                try:
                    n = int(every_n)
                except (TypeError, ValueError):
                    return {"success": False, 'message': _text('api_split_n_positive')}
                if n < 1:
                    return {"success": False, 'message': _text('api_split_n_positive')}

                for start in range(0, total, n):
                    writer = PdfWriter()
                    for i in range(start, min(start + n, total)):
                        writer.add_page(reader.pages[i])
                    out = os.path.join(output_dir, f"{base_name}_part{start + 1}.pdf")
                    with atomic_output(out) as f:
                        writer.write(f)
                    files.append(out)

            elif mode == "range":
                pages = self._parse_page_range(range_str, total)
                if not pages:
                    return {"success": False, "message": _text('api_split_range_empty')}
                writer = PdfWriter()
                for p in pages:
                    writer.add_page(reader.pages[p - 1])
                out = unique_output_path(os.path.join(output_dir, f"{base_name}_extracted.pdf"))
                with atomic_output(out) as f:
                    writer.write(f)
                files.append(out)

            else:
                return {"success": False, "message": _text('api_split_mode_bad') % mode}

            logging.info(f"拆分完成: {pdf_path}, 生成 {len(files)} 个文件")
            return {"success": True, "files": files}
        except Exception as e:
            logging.exception(f"split_pdf 失败: {pdf_path}")
            return {"success": False, "message": str(e)}

    def merge_pdfs(self, file_list, output_path):
        from pypdf import PdfReader, PdfWriter
        try:
            files = [f for f in (file_list or []) if f]
            if not files:
                return {"success": False, "message": _text('api_merge_pick_pdfs')}

            missing = [f for f in files if not os.path.isfile(f)]
            if missing:
                return {"success": False, "message": _text('api_file_not_found') % missing[0]}

            logging.info(f"合并 {len(files)} 个 PDF 到 {output_path}")
            writer = PdfWriter()
            for f in files:
                reader = PdfReader(f)
                for page in reader.pages:
                    writer.add_page(page)

            if len(writer.pages) == 0:
                return {"success": False, "message": _text('api_merge_no_pages')}

            # 输出路径可能与某个输入文件相同，原子写入可避免边读边截断
            with atomic_output(output_path) as f:
                writer.write(f)
            logging.info(f"合并完成: {output_path} ({len(writer.pages)} 页)")
            return {"success": True, "pages": len(writer.pages)}
        except Exception as e:
            logging.exception("merge_pdfs 失败")
            return {"success": False, "message": str(e)}
