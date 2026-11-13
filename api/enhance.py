"""拍照变清晰：界面上那一格的入口（算法在 core/enhance.py）。

和 OCR 那一组一样，这里只管「文件怎么进、成品怎么出、失败了怎么说」，
一像素都不在这层处理。三条入口：整本 PDF、一批图片、单页前后对比预览。

一次只握一页的全尺寸图：4 GB 的机器上，整本三百页的照片件要是先全部读进来
再处理，光像素就几十 GB；所以逐页渲染 → 逐页增强 → 逐页插回，中间不落临时文件。
"""

import base64
import logging
import os
import time

import pymupdf

from api._io import atomic_output, close_pdf, save_pdf_doc
from api._range import select_pages
from core import jobctl
from core import enhance
from core.i18n import t as _text

# 预览只给眼睛看、不参与成品：长边压到这么点，换一档效果才能立刻重画
PREVIEW_SIDE = 900


def _open_source(path):
    if not path or not os.path.isfile(path):
        return None, {'success': False, 'message': _text('api_file_not_found') % path}
    try:
        doc = pymupdf.open(path)
    except Exception as error:
        logging.exception('增强：打开 PDF 失败')
        return None, {'success': False, 'message': _text('api_pdf_open_failed') % error}
    if doc.needs_pass:
        close_pdf(doc)
        return None, {'success': False, 'message': _text('api_enhance_locked')}
    return doc, None


def _checked_mode(mode):
    """档位在这层先验掉，返回 (档名, 失败信息)。

    不能等到 core 里再抛：enhance_images 那种「一张坏图记一条、继续跑完」的写法
    会把异常吞在循环里，最后那句回给界面的 `normalize_mode(mode)` 才炸出去——
    用户看到的是整批失败加一句英文异常名，而正确的答复是「这一档不存在」。
    """
    try:
        return enhance.normalize_mode(mode), None
    except ValueError as error:
        return None, {'success': False, 'message': str(error)}


def _preview_dpi(page):
    """让预览那一页的长边落在 PREVIEW_SIDE 附近：按页幅反推该用多少 dpi。"""
    long_side = max(page.rect.width, page.rect.height) or 1.0
    return max(36, min(300, int(PREVIEW_SIDE * 72.0 / long_side)))


def _render(doc, index, dpi):
    """渲染一页并解成 BGR 数组；页宽页高由调用方自己从 page 上取。"""
    page = doc.load_page(index)
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    image = enhance.decode_png(pixmap.tobytes('png'))
    if image is None:
        raise RuntimeError('第 %d 页没能画成图' % (index + 1))
    return page, image


def _enhance_file(path, key, deskew, crop, work_side=None, perspective=False):
    """读一张文件 → 增强，返回 BGR；两条图片入口（散图 / 合册）共用这一份读法。"""
    image = enhance.read_image(path)
    side = work_side or max(image.shape[:2])
    return enhance.enhance_image(image, mode=key, deskew=bool(deskew),
                                 crop=bool(crop), work_side=side,
                                 perspective=bool(perspective))


def _data_url(png_bytes):
    return 'data:image/png;base64,' + base64.b64encode(png_bytes).decode('ascii')


class EnhanceMixin:
    def enhance_status(self):
        """OpenCV 在不在、三档效果叫什么、工作尺寸多少——界面灰态和验收都看这一句。"""
        info = enhance.status()
        info['success'] = bool(info.get('available'))
        return info

    def enhance_pdf(self, pdf_path, output_path='', page_range='', mode=enhance.DEFAULT_MODE,
                    deskew=False, crop=False, dpi=150, perspective=False):
        """把整本（或选中的几页）拍照件逐页增强后另存成新 PDF；原文件一个字都不动。

        页幅一律沿用原页的宽高：增强出来的图本身可能被压小或裁过边，
        按原页幅插回去，用户翻页时尺寸感才不会变。
        """
        if not output_path:
            return {'success': False, 'message': _text('api_need_save_path')}
        key, mode_error = _checked_mode(mode)
        if mode_error:
            return mode_error
        doc, error = _open_source(pdf_path)
        if error:
            return error
        target = None
        try:
            pages, range_error = select_pages(page_range, doc.page_count)
            if range_error:
                return {'success': False, 'message': range_error}
            dpi = max(72, min(300, int(dpi or 150)))
            started = time.perf_counter()
            target = pymupdf.open()
            jobctl.start('enhance_pdf', len(pages))
            cancelled = False
            try:
                for number in pages:
                    if jobctl.should_stop():
                        cancelled = True
                        break
                    jobctl.set_progress(target.page_count)
                    page, image = _render(doc, number - 1, dpi)
                    # 工作尺寸跟着用户点的精度走：渲染出来多大就尽量保留多大，
                    # 只受 core 那个内存上限（长边 2400）截断。写死 1600 的话，
                    # 勾了 300dpi 的那一页会在核心里被压小，用户多等的几秒全白花。
                    result = enhance.enhance_image(image, mode=key, deskew=bool(deskew),
                                                   crop=bool(crop), perspective=bool(perspective),
                                                   work_side=max(image.shape[:2]))
                    placed = target.new_page(width=page.rect.width, height=page.rect.height)
                    # 用 JPEG 不用 PNG：增强完的纸面大片是同一个白，PNG 逐像素存，
                    # 实测两页照片件 1.7 MB；换 JPEG(88) 后字没糊、体积小一个量级。
                    placed.insert_image(placed.rect, stream=enhance.encode_jpeg(result))
                    del image, result
            finally:
                jobctl.finish()
            done_pages = target.page_count
            if cancelled and not done_pages:
                # 一页都没做完就别写半成品文件：留一个空 PDF 在用户电脑上比不写更糟
                return {'success': False, 'cancelled': True, 'pages': 0,
                        'message': _text('api_enhance_cancelled')}
            # save_pdf_doc 负责关文档；output_path 恰好就是源文件时它会走临时文件那条路
            save_pdf_doc(target, output_path, source=pdf_path, garbage=3, deflate=True)
            target = None
            return {'success': True, 'file': output_path, 'pages': done_pages,
                    'cancelled': cancelled, 'mode': key,
                    'elapsed_ms': int((time.perf_counter() - started) * 1000)}
        except Exception as error:
            logging.exception('增强 PDF 失败')
            return {'success': False, 'message': '%s: %s' % (type(error).__name__, error)}
        finally:
            for handle in (doc, target):
                if handle is not None:
                    try:
                        close_pdf(handle)
                    except Exception:
                        pass

    def enhance_images(self, image_paths, output_dir='', mode=enhance.DEFAULT_MODE,
                       deskew=False, crop=False, to_pdf='', perspective=False):
        """一批照片各自增强成同格式的图片（给了 to_pdf 就合成一份 PDF）。

        名字里加 _增强，扩展名跟着实际写出的格式走；原图永远不动——这一档的用法是
        「拍完存一份能打印的」，把用户唯一的原件改没了是不可接受的。
        """
        paths = [str(item) for item in (image_paths or []) if str(item or '').strip()]
        if not paths:
            return {'success': False, 'message': _text('api_enhance_pick_images')}
        key, mode_error = _checked_mode(mode)
        if mode_error:
            return mode_error
        folder = str(output_dir or '').strip()
        if folder and not to_pdf:
            # 用户点定的目录就该存在；不存在时与其在 atomic_output 里抛一句
            # 「The system cannot find the path specified」，不如替他建好
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as error:
                return {'success': False, 'message': _text('api_enhance_outdir_failed') % error}
        started = time.perf_counter()
        written, failed = [], 0
        cancelled = False
        jobctl.start('enhance_images', len(paths), 'file')
        try:
            if to_pdf:
                return self._enhance_to_pdf(paths, to_pdf, key, deskew, crop, started,
                                            perspective)
            for index, path in enumerate(paths):
                if jobctl.should_stop():
                    cancelled = True
                    break
                jobctl.set_progress(index)
                try:
                    if not os.path.isfile(path):
                        raise FileNotFoundError('文件不存在: %s' % path)
                    target_dir = folder or os.path.dirname(path)
                    stem, ext = os.path.splitext(os.path.basename(path))
                    result = _enhance_file(path, key, deskew, crop, perspective=perspective)
                    # 编码那一步会告回来「实际写成了什么格式」：bmp/tiff/webp 这些
                    # OpenCV 那边不认或没必要认的，一律换成 .jpg，文件名跟着改，
                    # 否则就是「后缀是 bmp、里面装的是 JPEG」。
                    data, final_ext = enhance.encode_for(ext, result)
                    if not data:
                        raise RuntimeError('这张图编码失败')
                    target = os.path.join(target_dir, '%s_增强%s' % (stem, final_ext))
                    with atomic_output(target) as handle:
                        handle.write(data)
                    written.append(target)
                    del result
                except Exception:
                    # 一张坏图不该拖垮整批：记一笔继续，最后一起报（同 make_pdf 的 failed）
                    failed += 1
                    logging.exception('增强图片失败: %s', path)
        finally:
            jobctl.finish()
        return {'success': bool(written), 'files': written, 'count': len(written),
                'failed': failed, 'cancelled': cancelled, 'mode': key,
                'message': '' if written else _text('api_enhance_all_failed'),
                'elapsed_ms': int((time.perf_counter() - started) * 1000)}

    def _enhance_to_pdf(self, paths, output_path, key, deskew, crop, started,
                        perspective=False):
        """照片逐张增强后合成一份 PDF：拍多页文件的人要的是「一份能打印的 PDF」，
        不是一堆散图。页幅按图片像素尺寸给（和「图片转 PDF」里「按图片尺寸」那一档一致），
        这样同一批照片在两个入口里出来的页面大小是一样的。
        """
        if not str(output_path or '').strip():
            return {'success': False, 'message': _text('api_need_save_path')}
        doc = pymupdf.open()
        written = failed = 0
        cancelled = False
        try:
            for index, path in enumerate(paths):
                if jobctl.should_stop():
                    cancelled = True
                    break
                jobctl.set_progress(index)
                try:
                    result = _enhance_file(path, key, deskew, crop, perspective=perspective)
                    height, width = result.shape[:2]
                    page = doc.new_page(width=width, height=height)
                    page.insert_image(page.rect, stream=enhance.encode_jpeg(result))
                    written += 1
                    del result
                except Exception:
                    failed += 1
                    logging.exception('合成 PDF 时这张图没进来: %s', path)
            if not written:
                return {'success': False, 'cancelled': cancelled, 'pages': 0,
                        'failed': failed,
                        'message': _text('api_enhance_none_written')}
            save_pdf_doc(doc, output_path, source=paths[0], garbage=3, deflate=True)
            doc = None
            return {'success': True, 'file': output_path, 'pages': written,
                    'failed': failed, 'cancelled': cancelled, 'mode': key,
                    'elapsed_ms': int((time.perf_counter() - started) * 1000)}
        finally:
            if doc is not None:
                try:
                    close_pdf(doc)
                except Exception:
                    pass

    def enhance_preview(self, pdf_path, page_index=0, mode=enhance.DEFAULT_MODE,
                        deskew=False, crop=False, perspective=False):
        """出一页的「原来 / 处理后」两张小图给界面对比；什么都不写盘。"""
        key, mode_error = _checked_mode(mode)
        if mode_error:
            return mode_error
        doc, error = _open_source(pdf_path)
        if error:
            return error
        try:
            total = doc.page_count
            try:
                index = int(page_index)
            except (TypeError, ValueError):
                index = 0
            index = max(0, min(total - 1, index))
            _page, image = _render(doc, index, _preview_dpi(doc.load_page(index)))
            result = enhance.enhance_image(image, mode=key, deskew=bool(deskew),
                                           crop=bool(crop), perspective=bool(perspective))
            return {'success': True, 'page_index': index, 'page_count': total,
                    'mode': key,
                    'width': image.shape[1], 'height': image.shape[0],
                    'before': _data_url(enhance.encode_png(image)),
                    'after': _data_url(enhance.encode_png(result))}
        except Exception as error:
            logging.exception('增强预览失败')
            return {'success': False, 'message': '%s: %s' % (type(error).__name__, error)}
        finally:
            try:
                close_pdf(doc)
            except Exception:
                pass
