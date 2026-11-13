"""提取页面里的内嵌图片（F13）。

两种口径的差别必须让用户知道（方案 §4.9）：
  original —— 从文件里原样取出图片对象，不重编码。无损、最快，但取的是
              「当初塞进去的那张图」，页面旋转/裁剪的效果不会体现。
  render   —— 按 DPI 重新渲染整页存成图片。所见即所得，但等于又压一遍，
              量更大、慢一些。
"""

import logging
import os

from ._range import select_pages
from .viewer import _clamp_dpi

# extract_image 拿到的这些「格式」只是 PDF 内部的编码名，外面没有对应的
# 文件容器（jpx 是 JPEG2000 的裸流、ccitt/jbig2 是传真/二值图编码），
# 直接落盘得到的文件哪个看图软件都打不开。这几种统一转成 PNG 交出去。
_NEEDS_PNG = ("jpx", "jb2", "jbig2", "ccitt", "fax", "sbx", "raw", "")


def _unique_dir(path):
    """程序自动建的目录不许撞掉用户已有的目录（同名时往后加 _2、_3）。"""
    candidate = path
    index = 2
    while os.path.exists(candidate) and os.listdir(candidate):
        candidate = f"{path}_{index}"
        index += 1
    return candidate


def _compose_alpha(doc, xref, smask_xref):
    """
    把底图和它的透明遮罩合成一张带 alpha 的 PNG。

    合成失败（mask 尺寸或色彩空间和底图对不上，损坏文件会这样）时退回底图：
    少一张图的透明度，总比整批提取全部失败要好。
    """
    import pymupdf as fitz
    pix = fitz.Pixmap(doc, xref)
    if pix.colorspace and pix.colorspace.n == 4:
        # CMYK 底图不能直接和灰度 mask 拼，先转成 RGB
        pix = fitz.Pixmap(fitz.csRGB, pix)
    try:
        return fitz.Pixmap(pix, fitz.Pixmap(doc, smask_xref))
    except Exception:
        logging.exception("合成透明通道失败，按底图输出 xref=%s", xref)
        return pix


class ExtractMixin:
    def extract_images(self, pdf_path, page_range="", mode="original", output_dir="", dpi=150):
        """
        把页面内嵌图导出到目录，返回 {success, dir, count, files}。

        output_dir 留空时落在 PDF 旁边的「<文件名>_图片」里——和「PDF→图片」
        那套的命名一致，用户在同一个文件夹里就能找到。
        """
        import pymupdf as fitz
        # 失败也要把 count/files 带上：纯文字册子走进「所选范围内没有内嵌图片」是常态，
        # 前端拿到结果直接 .map，键缺失就是整块面板空白而日志里只有一条 exception
        empty = {"success": False, "dir": "", "count": 0, "files": []}
        try:
            mode = (mode or "original").lower()
            if mode not in ("original", "render"):
                return dict(empty, message=f"不支持的提取方式: {mode}")

            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    return dict(empty, message="该 PDF 已加密，请先解密后再提取图片")
                pages, error = select_pages(page_range, doc.page_count)
                if error:
                    return dict(empty, message=error)
                base = os.path.splitext(os.path.basename(pdf_path))[0]

                if not output_dir:
                    target = os.path.join(os.path.dirname(os.path.abspath(pdf_path)),
                                          f"{base}_图片")
                else:
                    target = output_dir
                target = _unique_dir(target)
                os.makedirs(target, exist_ok=True)

                files = []
                duplicates = 0
                if mode == "render":
                    # 渲染整页：page.rect 已经算进 /Rotate 与 CropBox，
                    # 所以这样出来的就是用户在页面上看到的朝向与范围。
                    out_dpi = _clamp_dpi(dpi, 150)
                    for page_no in pages:
                        pix = doc[page_no - 1].get_pixmap(dpi=out_dpi, alpha=False)
                        path = os.path.join(target, f"{base}_p{page_no}.png")
                        pix.save(path)
                        files.append(path)
                else:
                    seen = set()
                    for page_no in pages:
                        for item in doc[page_no - 1].get_images(full=True):
                            xref = item[0]
                            # 同一张图被多页引用（套红头、页眉 logo）很常见：
                            # 只出一次，否则 1000 页的册子能抽出 1000 个同一个文件。
                            if xref in seen:
                                duplicates += 1
                                continue
                            seen.add(xref)
                            info = doc.extract_image(xref)
                            ext = (info.get("ext") or "").lower()
                            if ext in _NEEDS_PNG:
                                path = os.path.join(target, f"{base}_p{page_no}_{len(files) + 1}.png")
                                with open(path, "wb") as f:
                                    f.write(fitz.Pixmap(doc, xref).tobytes("png"))
                            else:
                                if info.get("smask"):
                                    # 带透明通道的图：alpha 存在另一个对象里，
                                    # 原样写出去就只剩没有透明的底图（logo 会变黑底）。
                                    path = os.path.join(
                                        target, f"{base}_p{page_no}_{len(files) + 1}.png")
                                    with open(path, "wb") as f:
                                        f.write(_compose_alpha(doc, xref, info["smask"]).tobytes("png"))
                                else:
                                    path = os.path.join(
                                        target, f"{base}_p{page_no}_{len(files) + 1}.{ext}")
                                    with open(path, "wb") as f:
                                        f.write(info["image"])
                            files.append(path)
            finally:
                doc.close()

            if not files:
                # 没图不是失败得含糊：扫描件整页是一张图（能抽到），
                # 纯文字册子才是真的一张内嵌图都没有。
                return dict(empty, message="所选范围内没有内嵌图片")

            logging.info(f"图片提取完成: {pdf_path} 方式={mode} 页数={len(pages)} "
                         f"共 {len(files)} 张（重复引用略过 {duplicates} 次）-> {target}")
            return {"success": True, "dir": target, "count": len(files),
                    "files": files, "duplicates": duplicates}
        except Exception as e:
            logging.exception("extract_images 失败")
            return dict(empty, message=str(e))
