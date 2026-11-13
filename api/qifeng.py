r"""骑缝印（跨页印章）的桥接方法：一枚章图竖切成条，逐页贴到视觉右边缘。

切片与排布的数学**全在 `core/qifeng.py`**（纯函数、不碰磁盘不碰 PDF），这一层只做五件事：

1. 把参数收干净（页码范围、每组页数、纵向百分比、章宽的 pt / 百分比两种语义分开）；
2. 读 PDF 与印章图，把加密、只读权限、空文档、图片读不出这些分支挡在写盘之前；
3. 逐页 `insert_image`，进度与取消走 `core/jobctl.py` 的槽位（一页一格、每页之间查取消）；
4. 写**新文件**（绝不原地覆盖源文件），没点名位置就在源文件旁边按项目口径起名字；
5. **写完重新打开、逐页读回核对**：章在不在预期的那页、矩形的右缘是不是贴着视觉右边缘、
   高宽与条序对不对——不一致就 `verified: False` + `success: False`，不用 success 糊过去。

## 三条口径

* **不假装成功**。核对不过就是把 `verified: False` 交回去，并列出对不上的页与原因码
  （`page_missing` / `edge_not_flush` / `rect_mismatch` / `count_mismatch` / `slice_px_mismatch`），
  界面能直接说清「第 3 页的条带比预期窄了 1.2pt」。
* **取消不是失败也不是成功**：已经贴好的页照常写出、照常读回核对，返回 `cancelled: True` +
  `pages_done`（只做了这些），一页都没做就什么都不写并如实报错，不留半个空壳文件。
* **一句话术都不写死**：全部走 `core/i18n` 的键。core 层只出错误码，翻译在这一层做。

## 界面接入（这一轮不做）

`api/__init__.py` 那边把 `QifengMixin` 加进 `Api` 的继承列表就能调；前端要显示进度条
还得把 `apply_cross_page_seal` 登记进 `assets/js/common.js` 的 `LONG_JOBS`，
`api/_decorators.py` 的 `_FALLBACKS` 也要补这一笔（清单写在 `tests/out/qifeng_report.txt`，
按要求这两处本文件都没动）。
"""

import io
import logging
import os

from core import jobctl
from core import qifeng as _qf
from core.i18n import t as _text

from ._io import close_pdf, same_path, save_pdf_doc, unique_output_path
from ._range import parse_page_range

# 印章图的上限：与 api/stamp.py 同一档顾虑——几十兆的扫描图解压后是几千万像素，
# 竖切成 20 条再各自编码会把内存吃穿。文件字节与像素两条都设闸。
_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_MAX_PIXELS = 40_000_000

# 读回核对的容差（pt）。0.6pt ≈ 0.2mm，比打印/拼合的误差还小，不会把真错位放过去。
_VERIFY_TOL_PT = 0.6
# 「这一页基本是张图」的判定：图片覆盖页面这个比例以上就当扫描件/大图页出声。
_SCAN_COVERAGE = 0.60
# 「没盖章的页不许多出图片」这一项最多扫多少页（每页都要过一次内容流，整本上万页时太贵）。
_UNTOUCHED_MAX_PAGES = 200
# 返回体里逐页核对明细最多带几条（失败/对不上的那些永远全带，不受这个数限制）。
_CHECKS_MAX = 30

# jobctl 槽位名与 LONG_JOBS 里的方法名保持同一个字符串：界面只认方法名。
_JOB_NAME = 'apply_cross_page_seal'


def _message(code, detail=None, path=''):
    """错误码 → 人话。认得出的复用既有键，剩下的是本轮新键（清单见交付报告）。"""
    info = detail if isinstance(detail, dict) else {}
    # ---- 复用既有键（口径必须和 page_model / stamp 那几处一致）----
    if code == 'not_found':
        return _text('api_file_not_found') % (path or info.get('path') or '')
    if code == 'unreadable':
        # detail 可能是异常串（打不开），也可能是 {'reason': 现成的话}（页尺寸读不出来那一路）
        return _text('pm_unreadable', detail=info.get('reason') or str(detail or ''))
    if code == 'needs_password':
        return _text('api_encrypted_need_password')
    if code == 'bad_password':
        return _text('pm_bad_password')
    if code == 'read_only':
        return _text('pm_read_only')
    if code == 'empty_document':
        return _text('pm_empty_document')
    if code == 'must_end_pdf':
        return _text('api_pages_must_end_pdf')
    if code == 'output_is_source':
        return _text('pm_output_is_source')
    if code == 'write_failed':
        return _text('pm_write_failed', detail=str(detail or ''))
    if code == 'verify_failed':
        return _text('pm_verify_failed')
    if code == 'image_missing':
        return _text('api_stamp_image_missing')
    if code == 'image_too_big':
        return _text('api_stamp_image_too_big')
    if code == 'image_bad':
        return _text('api_stamp_image_bad')
    # ---- 本轮新键（还没进十本字典，先列在报告里）----
    if code == 'output_dir_missing':
        return _text('qf_output_dir_missing', directory=info.get('directory') or '')
    if code == 'output_dir_unwritable':
        return _text('qf_output_dir_unwritable', directory=info.get('directory') or '')
    if code == 'image_unreadable':
        return _text('qf_image_unreadable', detail=str(info.get('reason') or detail or ''))
    if code == 'page_range_none':
        return _text('qf_page_range_none', spec=info.get('spec') or '',
                     total=info.get('total', ''))
    if code == 'bad_group_size':
        return _text('qf_bad_group_size', value=str(info.get('value', '')))
    if code == 'bad_width':
        return _text('qf_bad_width', width_pt=info.get('width_pt', ''),
                     width_percent=info.get('width_percent', ''))
    if code == _qf.ERR_WIDTH_CONFLICT:
        return _text('qf_width_conflict', width_pt=info.get('width_pt', ''),
                     width_percent=info.get('width_percent', ''))
    if code == _qf.ERR_NARROW:
        return _text('qf_slice_too_narrow', width=info.get('width', ''),
                     parts=info.get('parts', ''))
    if code == 'too_wide':
        return _text('qf_too_wide', need=info.get('need', ''), page_w=info.get('page_w', ''))
    if code == 'too_tall':
        return _text('qf_too_tall', need=info.get('need', ''), page_h=info.get('page_h', ''))
    if code == 'percent_overflow':
        return _text('qf_percent_overflow', percent=info.get('percent', ''))
    if code == 'cancelled':
        return _text('qf_cancelled_none')
    return _text('qf_unexpected', code=str(code or ''))


def _warn_text(code, info):
    """出声（warnings）的话术：不是错误，但用户得知道自己拿到的不是我以为的那个值。"""
    if code == 'group_size_clamped':
        return _text('qf_note_group_clamped', requested=info.get('requested', ''),
                     used=info.get('used', ''), pages=info.get('pages', ''))
    if code == 'pages_not_adjacent':
        return _text('qf_note_not_adjacent', gaps=info.get('gaps', ''))
    if code == 'page_range_partial':
        return _text('qf_note_page_range_partial', spec=info.get('spec') or '',
                     total=info.get('total', ''))
    if code == 'scanned_pages':
        return _text('qf_note_scanned', pages=info.get('pages', ''))
    if code == 'rotated_pages':
        return _text('qf_note_rotated', pages=info.get('pages', ''))
    if code == 'width_default':
        return _text('qf_note_width_default', mm=info.get('mm', ''),
                     pt=round(_qf.DEFAULT_WIDTH_PT, 1))
    if code == 'overlap_clamped':
        return _text('qf_note_overlap_clamped', ratio=info.get('ratio', ''),
                     used=info.get('used', ''))
    if code == 'page_sizes_differ':
        return _text('qf_note_page_sizes_differ', sizes=info.get('sizes', ''))
    if code == 'y_default':
        return _text('qf_note_y_default', percent=int(_qf.DEFAULT_Y_PERCENT))
    if code == 'y_clamped':
        return _text('qf_note_y_clamped', percent=info.get('percent', ''))
    return _text(code) if code else ''


def _fail(code, detail=None, path='', **extra):
    out = {'success': False, 'verified': False, 'error': code,
           'message': _message(code, detail, path), 'file': ''}
    out.update(extra)
    return out


def _beside(pdf_path, name, ext):
    """源文件同目录拼派生名字（api/page_model.py、api/translate.py 同一口径）。"""
    return os.path.join(os.path.dirname(os.path.abspath(str(pdf_path))),
                        os.path.splitext(os.path.basename(str(pdf_path)))[0] + name + ext)


def _parse_number(value):
    """认真取一个数：认不出来给 None（而不是 0）。

    区分「没填」和「填了个 0」是这一层的责任——参考实现就是把它混为一谈，
    才出现「勾了骑缝章但弹窗没确认 → TryParse 留 0 → 章掉到页面最底」。
    """
    if isinstance(value, bool):
        return None
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace('％', '%').replace('%', '')
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def _normalize_overlap(value):
    """`overlap` 收 bool 或 0..0.5 的比例 → (交给 core 的比例, 要出声的清单)。

    * `True` → `core.qifeng.DEFAULT_OVERLAP_RATIO`（每条右端多带 10% 条宽的源列与相邻条重复，
      拼合时缝上不留白；代价是接缝处图案重复一小段，整体宽度与高度都不变）；
    * `False` / 空 / 认不出来 → 0（不压缝），认不出来那一路出声；
    * 负数 → 0 并出声；超过 `MAX_OVERLAP_RATIO` → 夹到上限并出声。
    """
    if isinstance(value, bool):
        return (_qf.DEFAULT_OVERLAP_RATIO if value else 0.0), []
    parsed = _parse_number(value)
    if parsed is None:
        if value in (None, ''):
            return 0.0, []
        return 0.0, [{'code': 'overlap_clamped',
                      'text': _warn_text('overlap_clamped', {'ratio': value, 'used': 0}),
                      'ratio': str(value), 'used': 0}]
    if parsed < 0:
        return 0.0, [{'code': 'overlap_clamped',
                      'text': _warn_text('overlap_clamped', {'ratio': parsed, 'used': 0}),
                      'ratio': parsed, 'used': 0}]
    if parsed > _qf.MAX_OVERLAP_RATIO:
        return _qf.MAX_OVERLAP_RATIO, [{'code': 'overlap_clamped',
                                        'text': _warn_text('overlap_clamped',
                                                            {'ratio': parsed,
                                                             'used': _qf.MAX_OVERLAP_RATIO}),
                                        'ratio': parsed, 'used': _qf.MAX_OVERLAP_RATIO}]
    return parsed, []


def _requested_bounds(spec):
    """从「1-5, 8, 11-13」里只取头尾两个数（不展开区间，免得 1-999999999 把内存吃穿）。"""
    lo = hi = None
    for part in str(spec or '').replace('，', ',').split(','):
        part = part.strip()
        if not part:
            continue
        pieces = part.split('-', 1) if '-' in part else [part]
        for piece in pieces:
            try:
                n = int(float(piece.strip()))
            except (TypeError, ValueError):
                continue
            lo = n if lo is None else min(lo, n)
            hi = n if hi is None else max(hi, n)
    return lo, hi


def _png_bytes(image):
    """PIL 图 → PNG 字节（内存里编，不落盘）。带 alpha 的原样保留（PyMuPDF 会拆成图 + SMask）。"""
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


def _coverage(infos, area):
    """这一页最大的一张图占页面多少比例（0..1）；没有图就是 0。"""
    best = 0.0
    for row in infos or []:
        try:
            box = row['bbox']
            w = max(0.0, float(box[2]) - float(box[0]))
            h = max(0.0, float(box[3]) - float(box[1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if area > 0:
            best = max(best, min(1.0, w * h / area))
    return best


class QifengMixin:
    """骑缝印（跨页印章）。数学在 core/qifeng.py，这里只管读、写、核。"""

    def apply_cross_page_seal(self, pdf_path, image_path, page_range='', group_size=20,
                              y_percent=50, width_pt=0, width_percent=0, overlap=False,
                              password='', output_path=''):
        """给 PDF 的每页右边缘贴一条章，翻页拼起来是一枚完整的章。**永远写到新文件**。

        参数（两种章宽语义**分开**，同时给就报错，不猜）：
          `pdf_path` / `image_path`  源 PDF 与印章图（png/jpg/bmp，任何 Pillow 认得出的）。
          `page_range`               「1-5, 8」这种；空 / all / 全部 = 整本。越界的页跳过并出声。
          `group_size`               每组多少页（= 一枚章最多被切成几条），默认 20；
                                     60 页 → 3 组各 20 条，每组独立重切一枚，组界处图案重新开始。
                                     必须 ≥ 1，0 / 负数 / 认不出来 → `bad_group_size`。
          `y_percent`                纵向位置：0 = 贴顶、100 = 贴底、50 = 居中（默认）。
                                     认不出来按 50 处理并出声（不掉到页底）。
          `width_pt`                 整枚章的宽度，单位 pt（绝对尺寸，与页面大小无关）。
          `width_percent`            整枚章的宽度占视觉页宽的百分比（1..100）。
                                     两个都不给 → 默认 42mm（≈119.1pt）并出声。
          `overlap`                  压缝：True = 每条右端多带 10% 条宽的源列与相邻条重复，
                                     拼合时缝上不留白；也可以直接给 0..0.5 的比例。
          `password`                 加密件的口令。没给口令 → `needs_password`。
          `output_path`              保存位置。留空 → 源文件旁 `<原名>_骑缝章.pdf`，撞名加序号；
                                     与源文件同一个路径 → `output_is_source`（绝不原地覆盖）。

        返回（成功形状）：
          `{success, verified, file, pages, message, page_count, group_size, group_count,
            groups, seal, y_percent, overlap, image, rotated_pages, pages_scanned,
            warnings, checks, mismatches, cancelled, pages_done, untouched_checked}`
          `checks` 是逐页读回核对的明细（页码 / 第几片 / 预期与实际矩形 / 差多少 pt），
          对不上的页**一定**在里面，通过的页最多带 `_CHECKS_MAX` 条。
        慢接口：逐页写，进度走 jobctl（一页一格），每页之间查取消；中途取消交回已写出的页。
        """
        import pymupdf as fitz
        from PIL import Image

        path = str(pdf_path or '').strip()
        if not path or not os.path.isfile(path):
            return _fail('not_found', {'path': path}, path)

        # ---------- 参数 ----------
        try:
            gs = int(float(group_size))
            if isinstance(group_size, bool):
                raise ValueError
        except (TypeError, ValueError):
            return _fail('bad_group_size', {'value': group_size}, path)
        if gs < 1:
            return _fail('bad_group_size', {'value': group_size}, path)

        y_parsed = _parse_number(y_percent)
        if y_parsed is None:
            # 认不出来一律按「居中」而不是「贴底」——参考实现那个 TryParse 留 0 的坑就在这儿
            y_used = _qf.DEFAULT_Y_PERCENT
        else:
            y_used = max(0.0, min(100.0, y_parsed))

        pt_raw = _parse_number(width_pt) or 0.0
        pct_raw = _parse_number(width_percent) or 0.0
        if pt_raw < 0 or pct_raw < 0:
            # 负宽不是「没填」：填错了就说清，不许悄悄退回默认那档
            return _fail('bad_width', {'width_pt': pt_raw, 'width_percent': pct_raw}, path)
        if pt_raw > 0 and pct_raw > 0:
            # 两种语义同时给：参考实现把 mm 与 % 塞进一个输入框，同一个数两种解释差几倍。
            # 这里不猜，直接报错让界面分两个控件收。
            return _fail(_qf.ERR_WIDTH_CONFLICT, {'width_pt': pt_raw,
                                                 'width_percent': pct_raw}, path)
        warnings = []
        if y_parsed is None:
            warnings.append({'code': 'y_default', 'text': _warn_text('y_default', {}),
                             'percent': _qf.DEFAULT_Y_PERCENT, 'raw': str(y_percent or '')})
        elif y_used != y_parsed:
            warnings.append({'code': 'y_clamped',
                             'text': _warn_text('y_clamped', {'percent': y_used}),
                             'raw': y_parsed, 'used': y_used})
        width_mode = 'default'
        if pt_raw <= 0 and pct_raw <= 0:
            width_mode = 'pt'
            warnings.append({'code': 'width_default',
                             'text': _warn_text('width_default', {'mm': _qf.DEFAULT_WIDTH_MM}),
                             'mm': _qf.DEFAULT_WIDTH_MM, 'pt': round(_qf.DEFAULT_WIDTH_PT, 2)})
        elif pt_raw > 0:
            width_mode = 'pt'
        else:
            width_mode = 'percent'
            if pct_raw > 100:
                return _fail('percent_overflow', {'percent': pct_raw}, path)

        # ---------- 印章图 ----------
        img_path = str(image_path or '').strip()
        if not img_path or not os.path.isfile(img_path):
            return _fail('image_missing', {'path': img_path}, path)
        try:
            if os.path.getsize(img_path) > _IMAGE_MAX_BYTES:
                return _fail('image_too_big', {'path': img_path}, path)
        except OSError:
            return _fail('image_missing', {'path': img_path}, path)
        try:
            stamp = Image.open(img_path)
            stamp.load()
        except Exception as error:
            logging.exception('apply_cross_page_seal: 印章图读不出来')
            return _fail('image_unreadable', {'reason': '%s: %s' % (type(error).__name__, error)},
                         path)
        image_w, image_h = int(stamp.width), int(stamp.height)
        if image_w < 1 or image_h < 1:
            return _fail('image_bad', {'width': image_w, 'height': image_h}, path)
        if image_w * image_h > _IMAGE_MAX_PIXELS:
            return _fail('image_too_big', {'pixels': image_w * image_h}, path)
        stamp = stamp.convert('RGBA')

        # ---------- PDF：先过加密 / 权限 / 空文档三道门 ----------
        doc = None
        try:
            doc = fitz.open(path)
        except Exception as error:
            logging.exception('apply_cross_page_seal: PDF 打不开 %s', path)
            return _fail('unreadable', '%s: %s' % (type(error).__name__, error), path)
        try:
            if doc.needs_pass:
                if not doc.authenticate(str(password or '')):
                    close_pdf(doc)
                    doc = None
                    return _fail('needs_password' if not str(password or '') else 'bad_password',
                                 {}, path)
            if not (doc.permissions & fitz.PDF_PERM_MODIFY):
                close_pdf(doc)
                doc = None
                return _fail('read_only', {}, path)
            page_count = doc.page_count
            if page_count <= 0:
                close_pdf(doc)
                doc = None
                return _fail('empty_document', {}, path)

            # ---------- 页码范围 → 切片计划 ----------
            spec = str(page_range or '').strip()
            if not spec or spec.lower() in ('all', '全部', '全部页面'):
                targets = list(range(1, page_count + 1))
            else:
                targets = parse_page_range(spec, page_count)
                if not targets:
                    close_pdf(doc)
                    doc = None
                    return _fail('page_range_none', {'spec': spec, 'total': page_count}, path)
                lo, hi = _requested_bounds(spec)
                if (lo is not None and lo < 1) or (hi is not None and hi > page_count):
                    warnings.append({
                        'code': 'page_range_partial',
                        'text': _warn_text('page_range_partial', {'spec': spec,
                                                                 'total': page_count}),
                        'spec': spec, 'requested': [lo, hi], 'total': page_count})

            plan = _qf.plan_for_pages(targets, gs)
            if plan.get('group_size_requested') != plan.get('group_size'):
                warnings.append({
                    'code': 'group_size_clamped',
                    'text': _warn_text('group_size_clamped',
                                       {'requested': gs, 'used': plan['group_size'],
                                        'pages': len(targets)}),
                    'requested': gs, 'used': plan['group_size'], 'pages': len(targets)})
            if 'pages_not_adjacent' in (plan.get('notes') or []):
                warnings.append({'code': 'pages_not_adjacent',
                                 'text': _warn_text('pages_not_adjacent',
                                                    {'gaps': plan.get('adjacency_gaps', 0)}),
                                 'gaps': plan.get('adjacency_gaps', 0)})

            # ---------- 输出位置（写在动笔之前，免得白跑一趟）----------
            out = str(output_path or '').strip()
            if not out:
                out = unique_output_path(_beside(path, _text('qf_output_suffix'), '.pdf'))
            else:
                if os.path.splitext(out)[1].lower() != '.pdf':
                    close_pdf(doc)
                    doc = None
                    return _fail('must_end_pdf', {'path': out}, path)
                if same_path(out, path):
                    close_pdf(doc)
                    doc = None
                    return _fail('output_is_source', {'path': out}, path)
            directory = os.path.dirname(os.path.abspath(out))
            if not os.path.isdir(directory):
                close_pdf(doc)
                doc = None
                return _fail('output_dir_missing', {'directory': directory}, path)
            if not os.access(directory, os.W_OK):
                close_pdf(doc)
                doc = None
                return _fail('output_dir_unwritable', {'directory': directory}, path)

            # ---------- 预检：页面尺寸 / 章放不下 / 条数超过图宽 ----------
            # 全部检查写完才开始改文档——任何一条不过就一个字都不写（照 page_edit 的口径）。
            geometry = []
            sizes = set()
            for row in plan['rows']:
                page = doc[row['page'] - 1]
                crop_w = float(page.cropbox.width) or float(page.rect.width)
                crop_h = float(page.cropbox.height) or float(page.rect.height)
                rotation = int(page.rotation or 0)
                vw, vh = _qf.visual_size(crop_w, crop_h, rotation)
                sizes.add((round(vw, 1), round(vh, 1)))
                geometry.append({'row': row, 'crop_w': crop_w, 'crop_h': crop_h,
                                 'rotation': rotation, 'visual_w': vw, 'visual_h': vh})
            if len(sizes) > 1:
                warnings.append({'code': 'page_sizes_differ',
                                 'text': _warn_text('page_sizes_differ', {'sizes': len(sizes)}),
                                 'sizes': sorted(sizes)})

            for item in geometry:
                if item['visual_w'] < 1 or item['visual_h'] < 1:
                    close_pdf(doc)
                    doc = None
                    return _fail('unreadable', {'reason': _text('qf_bad_page_size',
                                                                page=item['row']['page'])},
                                 path)
            # ---------- 压缝先归一化一次：bool / 数字都认，负数与超限都出声 ----------
            overlap_ratio, notes = _normalize_overlap(overlap)
            warnings.extend(notes)

            # 每种「本组条数」切一次，组与组之间复用（每组都是同一枚章重切一遍）
            slice_cache = {}
            overlap_used_px = []
            for group in plan['groups']:
                parts = group['parts']
                if parts in slice_cache:
                    continue
                if parts > image_w:
                    close_pdf(doc)
                    doc = None
                    return _fail(_qf.ERR_NARROW, {'width': image_w, 'parts': parts}, path)
                base = image_w // parts
                o = _qf.overlap_pixels(overlap_ratio, base)
                overlap_used_px.append(o)
                sp = _qf.slice_plan(image_w, parts, o)
                if not sp.get('success'):
                    close_pdf(doc)
                    doc = None
                    return _fail(sp.get('error'), sp.get('detail'), path)
                cropped = _qf.slice_image(stamp, parts, o)
                if not cropped.get('success'):
                    close_pdf(doc)
                    doc = None
                    return _fail(cropped.get('error'), cropped.get('detail'), path)
                for sl in cropped['slices']:
                    sl['png'] = _png_bytes(sl['image'])
                slice_cache[parts] = {'plan': sp, 'slices': cropped['slices'],
                                      'total_source': sp['sum_source']}

            if width_mode == 'percent':
                check = _qf.seal_width(min(g['visual_w'] for g in geometry), 0, pct_raw)
            elif pt_raw > 0:
                check = _qf.seal_width(min(g['visual_w'] for g in geometry), pt_raw, 0)
            else:
                check = _qf.seal_width(min(g['visual_w'] for g in geometry),
                                       _qf.DEFAULT_WIDTH_PT, 0)
            full_w = check['width_pt']
            full_h = full_w * image_h / image_w
            min_h = min(g['visual_h'] for g in geometry)
            if full_h > min_h:
                close_pdf(doc)
                doc = None
                return _fail('too_tall', {'need': round(full_h, 1),
                                         'page_h': round(min_h, 1)}, path)

            # 每页的贴放矩形（display / page_top / pdf 三套都给）
            for item in geometry:
                parts = item['row']['parts']
                cache = slice_cache[parts]
                sl = cache['slices'][item['row']['part'] - 1]
                placed = _qf.place_rect(item['crop_w'], item['crop_h'], item['rotation'],
                                        piece_px=sl['width'], total_piece_px=cache['total_source'],
                                        image_w=image_w, image_h=image_h,
                                        width_pt=(pt_raw if width_mode == 'pt' else 0),
                                        width_percent=(pct_raw if width_mode == 'percent' else 0),
                                        y_percent=y_used)
                if not placed.get('success'):
                    close_pdf(doc)
                    doc = None
                    return _fail(placed.get('error'), placed.get('detail'), path)
                flags = placed['flags']
                if flags['seal_taller_than_page']:
                    close_pdf(doc)
                    doc = None
                    return _fail('too_tall', {'need': round(placed['size']['full_height_pt'], 1),
                                             'page_h': round(item['visual_h'], 1)}, path)
                if flags['piece_wider_than_page']:
                    close_pdf(doc)
                    doc = None
                    return _fail('too_wide', {'need': round(placed['size']['piece_width_pt'], 1),
                                             'page_w': round(item['visual_w'], 1)}, path)
                item['placed'] = placed
                item['slice'] = sl

            # ---------- 基线（读回核对要用）：这些页原本有几张图、最大一张盖多少页 ----------
            baseline = {}
            scanned = 0
            for item in geometry:
                page = doc[item['row']['page'] - 1]
                infos = page.get_image_info(xrefs=True)
                area = item['visual_w'] * item['visual_h']
                coverage = _coverage(infos, area)
                baseline[item['row']['page']] = len(infos)
                if coverage >= _SCAN_COVERAGE:
                    scanned += 1
            if scanned:
                warnings.append({'code': 'scanned_pages',
                                 'text': _warn_text('scanned_pages', {'pages': scanned}),
                                 'pages': scanned})
            untouched_baseline = None
            if page_count <= _UNTOUCHED_MAX_PAGES:
                untouched_baseline = {}
                for index in range(page_count):
                    number = index + 1
                    if number in baseline:
                        continue
                    untouched_baseline[number] = len(doc[index].get_image_info())

            rotated = sorted({item['row']['page'] for item in geometry if item['rotation']})
            if rotated:
                warnings.append({'code': 'rotated_pages',
                                 'text': _warn_text('rotated_pages', {'pages': len(rotated)}),
                                 'pages': len(rotated), 'page_list': rotated[:20]})

            # ---------- 逐页写（进度 + 取消）----------
            written = []
            cancelled = False
            expected = {}
            try:
                jobctl.start(_JOB_NAME, len(geometry))
                for item in geometry:
                    if jobctl.should_stop():
                        cancelled = True
                        break
                    number = item['row']['page']
                    expected[number] = item
                    page = doc[number - 1]
                    rect = item['placed']['rect_page']
                    page.insert_image(fitz.Rect(rect[0], rect[1], rect[2], rect[3]),
                                      stream=item['slice']['png'], keep_proportion=False,
                                      alpha=True)
                    written.append(number)
                    jobctl.advance()
                jobctl.set_progress(len(written))
            except Exception as error:
                logging.exception('apply_cross_page_seal: 逐页贴章失败')
                close_pdf(doc)
                doc = None
                return _fail('write_failed', '%s: %s' % (type(error).__name__, error), path)
            finally:
                jobctl.finish()

            if cancelled and not written:
                # 一页都没贴成：不写出半成品，源文件也不动
                close_pdf(doc)
                doc = None
                return _fail('cancelled', {'pages': 0, 'planned': len(geometry)}, path)

            try:
                save_pdf_doc(doc, out)
            except OSError as error:
                logging.exception('apply_cross_page_seal: 写出失败 %s', out)
                code = 'output_dir_unwritable' if getattr(error, 'errno', None) in (13, 22) \
                    else 'write_failed'
                doc = None
                return _fail(code, {'directory': directory, 'reason': str(error)},
                             path, **{'file': ''})
            doc = None        # save_pdf_doc 负责关闭

            if untouched_baseline is None:
                untouched_note = _text('qf_note_untouched_skipped', pages=page_count)
            else:
                untouched_note = ''

            verified, mismatches, checks = self._verify_cross_page_seal(
                out, written, expected, baseline, untouched_baseline, _JOB_NAME)

            result = {
                'success': bool(verified),
                'verified': bool(verified),
                'file': out,
                'pages': len(written),
                'page_count': page_count,
                'cancelled': bool(cancelled),
                'pages_done': len(written),
                'planned_pages': len(geometry),
                'group_size': plan['group_size'],
                'group_count': plan['group_count'],
                'groups': plan['groups'],
                'seal': {'image_width': image_w, 'image_height': image_h,
                         'width_mode': width_mode, 'width_pt': round(full_w, 3),
                         'width_mm': round(full_w / _qf.MM_TO_PT, 2),
                         'height_pt': round(full_h, 3),
                         'width_percent': (pct_raw if width_mode == 'percent' else None),
                         'max_parts': max(g['parts'] for g in plan['groups']),
                         'overlap_px': max(overlap_used_px) if overlap_used_px else 0,
                         'overlap_ratio': overlap_ratio},
                'y_percent': y_used,
                'image': {'width': image_w, 'height': image_h, 'path': img_path},
                'rotated_pages': rotated,
                'pages_scanned': scanned,
                'untouched_checked': (0 if untouched_baseline is None
                                      else len(untouched_baseline)),
                'warnings': warnings,
                'mismatches': mismatches,
                'checks': checks[:_CHECKS_MAX] if verified else checks,
            }
            if untouched_note:
                result['warnings'].append({'code': 'untouched_skipped', 'text': untouched_note,
                                           'pages': page_count})
            if verified:
                result['message'] = (_text('qf_done_cancelled', pages=len(written),
                                           planned=len(geometry))
                                     if cancelled else
                                     _text('qf_done', pages=len(written),
                                          groups=plan['group_count'],
                                          parts=result['seal']['max_parts']))
            else:
                result['success'] = False
                result['error'] = 'verify_failed'
                result['message'] = _text('qf_verify_failed_pages',
                                         pages=len(mismatches),
                                         detail='; '.join(str(m.get('page', ''))
                                                          for m in mismatches[:5]))
            logging.info('骑缝印: %s -> %s（%d 页 / %d 组 / 核对=%s / 取消=%s）',
                         path, out, len(written), plan['group_count'], verified, cancelled)
            return result
        finally:
            if doc is not None:
                close_pdf(doc)

    # ---------- 读回核对 ----------
    def _verify_cross_page_seal(self, out_path, written, expected, baseline,
                                untouched_baseline, _job=''):
        """重新打开**写出来的那个文件**，逐页核对章在不在预期的页、贴没贴视觉右边缘。

        每一页查五件事，任一不过就 `verified: False`（判据全是「读回来的是什么」，
        不看写入函数自己怎么说）：
          1. 这一页的图片数 == 写之前的数量 + 1（`count_mismatch`：多贴/漏贴）；
          2. 有一张图的实际矩形（换算到**视觉坐标**）与预期矩形对得上，容差 0.6pt
             （`rect_mismatch` / `page_missing`）；
          3. 实际矩形的右缘 == 视觉页宽（`edge_not_flush`：贴错边就等于没有骑缝）；
          4. 存进去的位图宽高 == 那一条切片的像素宽高（`slice_px_mismatch`：条序/切片坏了）；
          5. 没被点名的页图片数不许变（`untouched_mismatch`，整本超过 200 页时不查，
             并在 warnings 里说明「没逐页核没盖到的页」）。
        """
        import pymupdf as fitz

        mismatches = []
        checks = []
        try:
            back = fitz.open(out_path)
        except Exception as error:
            logging.exception('读回核对打不开成品: %s', out_path)
            return False, [{'page': 0, 'why': 'unreadable',
                           'reason': '%s: %s' % (type(error).__name__, error)}], []
        try:
            numbers = list(written)
            for number in numbers:
                item = expected[number]
                placed = item['placed']
                want = placed['rect_display']
                if back.page_count <= number - 1:
                    mismatches.append({'page': number, 'why': 'page_missing',
                                       'detail': {'page_count': back.page_count}})
                    continue
                page = back[number - 1]
                crop_w = float(page.cropbox.width) or float(page.rect.width)
                crop_h = float(page.cropbox.height) or float(page.rect.height)
                rotation = int(page.rotation or 0)
                infos = page.get_image_info(xrefs=True)
                visual = _qf.visual_size(crop_w, crop_h, rotation)
                rows = []
                for row in infos:
                    got_display = _qf.to_display_rect(tuple(row['bbox']), crop_w, crop_h,
                                                     rotation)
                    rows.append((row, got_display))
                matched = [r for r in rows if _near_rect(r[1], want, _VERIFY_TOL_PT)]
                problems = []
                if len(infos) != baseline.get(number, 0) + 1:
                    problems.append({'why': 'count_mismatch',
                                     'detail': {'before': baseline.get(number),
                                                'after': len(infos)}})
                if not matched:
                    problems.append({'why': 'page_missing' if not rows else 'rect_mismatch',
                                     'detail': {'want': [round(v, 2) for v in want],
                                                'got': [[round(v, 2) for v in r[1]]
                                                        for r in rows[:4]]}})
                best = matched[0] if matched else (rows[0] if rows else None)
                if best is not None:
                    if abs(best[1][2] - visual[0]) > _VERIFY_TOL_PT:
                        problems.append({'why': 'edge_not_flush',
                                         'detail': {'right': round(best[1][2], 2),
                                                    'page_w': round(visual[0], 2)}})
                    ex = None
                    try:
                        ex = back.extract_image(best[0]['xref'])
                    except Exception:
                        ex = None
                    if ex:
                        want_px = (int(item['slice']['width']),
                                   int(item['slice']['image'].size[1]))
                        got_px = (int(ex.get('width') or 0), int(ex.get('height') or 0))
                        if got_px != want_px:
                            problems.append({'why': 'slice_px_mismatch',
                                             'detail': {'stored': list(got_px),
                                                        'want': list(want_px)}})
                checks.append({'page': number, 'part': item['row']['part'],
                               'parts': item['row']['parts'], 'group': item['row']['group'],
                               'rotation': rotation,
                               'expect': [round(v, 2) for v in want],
                               'got': [round(v, 2) for v in best[1]] if best else None,
                               'max_diff_pt': (round(max(abs(a - b) for a, b in
                                                         zip(best[1], want)), 3)
                                               if best else None),
                               'flush_right_offset_pt': (round(visual[0] - best[1][2], 3)
                                                        if best else None),
                               'ok': not problems})
                for problem in problems:
                    mismatches.append(dict({'page': number, 'part': item['row']['part']},
                                          **problem))
            for number, count in (untouched_baseline or {}).items():
                if number in expected:
                    continue
                if back.page_count < number:
                    mismatches.append({'page': number, 'why': 'page_count',
                                       'detail': {'page_count': back.page_count}})
                    continue
                after = len(back[number - 1].get_image_info())
                if after != count:
                    mismatches.append({'page': number, 'why': 'untouched_mismatch',
                                       'detail': {'before': count, 'after': after}})
            return (not mismatches), mismatches, checks
        finally:
            close_pdf(back)


def _near_rect(a, b, tol):
    try:
        return all(abs(float(a[i]) - float(b[i])) <= tol for i in range(4))
    except (TypeError, ValueError, IndexError):
        return False
