import base64
import io
import logging

from ._io import close_pdf, save_pdf_doc
from core.i18n import t as _text


# 缩放 100% 时，印章宽度占页面宽度的比例
_STAMP_BASE_WIDTH_RATIO = 0.30
# 印章再大也不超过页面的这个比例，保证永远不会出框
_STAMP_MAX_RATIO = 0.92
# 预览时读进内存的上限，避免误选一张几百兆的图把界面卡死
_STAMP_IMAGE_MAX_BYTES = 20 * 1024 * 1024
# 预览图长边的像素上限（原始文件不受影响，盖印时仍按原图分辨率输出）
_STAMP_PREVIEW_MAX_PIXELS = 2000


class StampMixin:
    def load_stamp_image(self, image_path):
        """
        把印章图片读成 data URL 给界面用。

        界面跑在 http://127.0.0.1 上，<img src="file:///..."> 会被 WebView2
        拦掉：预览里印章只剩一个空框，用户拖着框盖印，盖的却是看不见的图。
        """
        import os
        from PIL import Image
        try:
            if not image_path or not os.path.isfile(image_path):
                return {"success": False, "message": _text('api_stamp_image_missing')}
            if os.path.getsize(image_path) > _STAMP_IMAGE_MAX_BYTES:
                return {"success": False, "message": _text('api_stamp_image_too_big')}
            stamp = Image.open(image_path)
            stamp.load()
            if stamp.width <= 0 or stamp.height <= 0:
                return {"success": False, 'message': _text('api_stamp_image_bad')}
            preview = stamp.convert("RGBA")
            # 20 MB 的扫描图解压后可能是七八千万像素，RGBA 存成 PNG 再 base64
            # 就是一个上百 MB 的字符串，界面会直接卡死。预览用不到这个精度，
            # 盖印时 apply_stamp 仍然读原图。
            if max(preview.width, preview.height) > _STAMP_PREVIEW_MAX_PIXELS:
                preview.thumbnail((_STAMP_PREVIEW_MAX_PIXELS, _STAMP_PREVIEW_MAX_PIXELS),
                                  Image.LANCZOS)
            buf = io.BytesIO()
            preview.save(buf, format="PNG")
            return {
                "success": True,
                "data_url": "data:image/png;base64,"
                            + base64.b64encode(buf.getvalue()).decode("ascii"),
                "width": stamp.width,
                "height": stamp.height,
            }
        except Exception as e:
            logging.exception("load_stamp_image 失败")
            return {"success": False, "message": str(e)}

    def apply_stamp(self, pdf_path, output_path, stamp_image_path,
                    opacity=0.8, scale=1.0,
                    page_mode="all", page_range="",
                    position=None):
        from PIL import Image
        import pymupdf as fitz
        from pypdf import PdfReader
        try:
            reader = PdfReader(pdf_path)
            total = len(reader.pages)
            if total == 0:
                return {"success": False, 'message': _text('api_pdf_empty')}

            if page_mode == "all":
                pages = list(range(1, total + 1))
            else:
                pages = self._parse_page_range(page_range, total)
            if not pages:
                return {"success": False, "message": _text('api_stamp_no_pages')}

            try:
                scale = float(scale)
            except (TypeError, ValueError):
                scale = 1.0
            scale = max(0.05, min(scale, 10.0))

            try:
                opacity = float(opacity)
            except (TypeError, ValueError):
                opacity = 0.8

            stamp = Image.open(stamp_image_path).convert("RGBA")
            w, h = stamp.size
            if w <= 0 or h <= 0:
                return {"success": False, 'message': _text('api_stamp_image_bad')}

            alpha = int(max(0.0, min(1.0, opacity)) * 255)
            r, g, b, a = stamp.split()
            a = a.point(lambda x: min(x * alpha // 255, 255))
            stamp = Image.merge("RGBA", (r, g, b, a))

            buf = io.BytesIO()
            stamp.save(buf, format="PNG")
            stamp_bytes = buf.getvalue()

            doc = fitz.open(pdf_path)
            try:
                for pnum in pages:
                    page = doc[pnum - 1]
                    page_w, page_h = page.rect.width, page.rect.height

                    # 关键修复：原实现把图片的「像素」当成 PDF 的「点」直接使用，
                    # 一张 800px 宽的印章会变成 800 点（约 28cm），远大于页面。
                    # 现在以页宽为基准算尺寸：100% 缩放时约占页宽 30%，
                    # 与界面上预览看到的相对大小一致，任何分辨率都不会出框。
                    draw_w = page_w * _STAMP_BASE_WIDTH_RATIO * scale
                    draw_h = draw_w * (h / w)

                    limit = min(
                        page_w * _STAMP_MAX_RATIO / draw_w,
                        page_h * _STAMP_MAX_RATIO / draw_h,
                        1.0,
                    )
                    draw_w *= limit
                    draw_h *= limit

                    if position:
                        try:
                            x = float(position.get("x", 0.82)) * page_w
                            y = float(position.get("y", 0.82)) * page_h
                        except (TypeError, ValueError):
                            x = page_w - draw_w - 30
                            y = page_h - draw_h - 30
                    else:
                        x = page_w - draw_w - 30
                        y = page_h - draw_h - 30

                    # 保证印章完整落在页面内
                    x = max(0.0, min(x, page_w - draw_w))
                    y = max(0.0, min(y, page_h - draw_h))

                    rect = fitz.Rect(x, y, x + draw_w, y + draw_h)
                    page.insert_image(rect, stream=stamp_bytes, alpha=True)

            except Exception:
                close_pdf(doc)
                raise

            save_pdf_doc(doc, output_path)

            logging.info(
                f"印章应用完成: {output_path} "
                f"({len(pages)} 页, {draw_w:.0f}x{draw_h:.0f} 点)"
            )
            return {"success": True, "pages": len(pages)}
        except Exception as e:
            logging.exception("apply_stamp 失败")
            return {"success": False, "message": str(e)}

    def save_signature_canvas(self, png_base64, output_path):
        try:
            if not png_base64:
                return {"success": False, 'message': _text('api_sign_empty')}
            data = png_base64.split(",")[-1] if "," in png_base64 else png_base64
            raw = base64.b64decode(data)
            if not raw:
                return {"success": False, 'message': _text('api_sign_empty')}
            with open(output_path, "wb") as f:
                f.write(raw)
            return {"success": True, "file": output_path}
        except Exception as e:
            logging.exception("save_signature_canvas 失败")
            return {"success": False, "message": str(e)}
