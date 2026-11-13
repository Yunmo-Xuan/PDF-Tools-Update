import logging
import os

from ._io import close_pdf, save_pdf_doc, unique_output_path
from core.i18n import t as _text


# 压缩级别 -> (图片重采样阈值 dpi, 目标 dpi, JPEG 质量)
_LEVELS = {
    "light": (300, 200, 80),
    "medium": (225, 150, 65),
    "heavy": (150, 96, 50),
}


class CompressMixin:
    def compress_pdf(self, pdf_path, output_path, level="medium"):
        """
        压缩 PDF，同时保留文字层、矢量图形、书签、链接与元数据。

        旧实现把每一页光栅化成位图再拼成新 PDF，导致文字全部变成图片、
        无法再选中或搜索，书签/链接/元数据全部丢失，而且对纯文字文档
        常常越压越大（"light" 用 200dpi 尤其明显）。

        现在只做三件不破坏内容的事：
          1. 对超过阈值的嵌入图片重采样并转 JPEG；
          2. 子集化嵌入字体；
          3. 去重对象、清理无用数据、压缩流。
        文字与矢量始终原样保留。
        """
        import pymupdf as fitz

        dpi_threshold, dpi_target, quality = _LEVELS.get(level, _LEVELS["medium"])

        try:
            original_size = os.path.getsize(pdf_path)
        except OSError:
            original_size = 0

        doc = fitz.open(pdf_path)
        # 加密文件打不开页面，往下走只会得到「PDF 没有页面」这种误导话；
        # 批量压缩里更糟——用户会以为那个文件本来就是空的。
        if doc.needs_pass:
            close_pdf(doc)
            return {"success": False, "message": _text('api_compress_encrypted')}
        if doc.page_count == 0:
            close_pdf(doc)
            return {"success": False, 'message': _text('api_pdf_empty')}

        try:
            # 1) 图片重采样：只动图片，文字与矢量不受影响
            rewrite = getattr(doc, "rewrite_images", None)
            if rewrite is None:
                logging.warning("当前 PyMuPDF 不支持 rewrite_images，本次只做无损重写")
            else:
                try:
                    rewrite(
                        dpi_threshold=dpi_threshold,
                        dpi_target=dpi_target,
                        quality=quality,
                        lossy=True,
                        lossless=True,
                    )
                except Exception:
                    logging.exception("图片重采样失败，继续做无损重写")

            # 2) 字体子集化，进一步减小体积
            try:
                doc.subset_fonts()
            except Exception:
                logging.exception("字体子集化失败，跳过")
        except Exception:
            close_pdf(doc)
            raise

        # 3) 无损重写：清理重复对象并压缩流（save_pdf_doc 内部会关闭文档）
        save_pdf_doc(
            doc,
            output_path,
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            clean=True,
        )

        try:
            new_size = os.path.getsize(output_path)
        except OSError:
            new_size = 0

        result = {
            "success": True,
            "original_size": original_size,
            "new_size": new_size,
        }
        if original_size:
            result["ratio"] = round(new_size / original_size, 4)
        if original_size and new_size >= original_size:
            result["message"] = _text('api_compress_already_best')

        logging.info(f"压缩完成: {pdf_path} -> {output_path} ({original_size} -> {new_size} 字节)")
        return result

    def batch_compress(self, file_list, level="medium"):
        """批量压缩：每个文件在它旁边出一份「原名_压缩.pdf」，原文件一律不动。

        返回形状和 batch_split / batch_convert 保持一致（results/total/failed），
        界面上那句「几个文件失败」的判据才不用按操作分叉。这里额外汇总
        original_size / new_size：批量压缩的意义就是省体积，用户要看一共小了多少
        才知道这一趟没白跑，只看「完成 12 个文件」是不够的。
        """
        files = [f for f in (file_list or []) if f]
        if not files:
            return {"success": False, 'message': _text('api_need_pdf')}
        if level not in _LEVELS:
            # 界面传进来的档位对不上时按中档走，而不是 KeyError 把整批打断
            logging.warning("未知的压缩档位 %r，按中档处理", level)
            level = "medium"

        results = []
        for f in files:
            out = unique_output_path(os.path.splitext(f)[0] + "_压缩.pdf")
            if not os.path.isfile(f):
                # 列表是界面攒出来的，中间用户把文件挪走很常见；先查一次，
                # 免得 MuPDF 的英文报错直接进状态行
                results.append({"success": False, "file": f, "output_path": "",
                                "message": _text('api_file_moved_missing') % f})
                continue
            try:
                res = self.compress_pdf(f, out, level)
            except Exception as e:
                # 单个文件坏掉（加密、内容流语法错）不许带走整批：记下失败接着做下一个
                logging.exception("batch_compress 处理 %s 失败", f)
                res = {"success": False, "message": str(e) or e.__class__.__name__}
            res["file"] = f
            res["output_path"] = out if res.get("success") else ""
            results.append(res)

        done = [r for r in results if r.get("success")]
        failed = [r for r in results if not r.get("success")]
        return {
            "success": not failed,
            "results": results,
            "total": len(results),
            "failed": len(failed),
            "original_size": sum(r.get("original_size") or 0 for r in done),
            "new_size": sum(r.get("new_size") or 0 for r in done),
            "ineffective": sum(
                1 for r in done
                if r.get("original_size") and (r.get("new_size") or 0) >= r["original_size"]),
        }
