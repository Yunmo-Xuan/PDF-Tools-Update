"""一页 PDF → 结构化 JSON：「PDF 编辑」的数据层地基。

要做 WPS 那种「点进一行字改两个字」的编辑，前端拿到的不能是一张 base64 图，
而得是「这一页上每块东西在哪、长什么样、叫什么名字」：文字块/行/字 span 的框、
字体字号颜色，图片的 xref 和指纹，矢量框线的位置粗细。这里产出的就是这一份。

三条口径（改代码前先读）：

1. **id 稳定可寻**。span id 形如 `p<页号>_b<块序>_l<行序>_s<span 序>`，
   块序 = 文字块（PyMuPDF 出块顺序）→ 图片块（get_image_info 顺序）→ 矢量块（seqno 顺序），
   全是确定性编号，不用 uuid：同一份文件同一页解析十遍，同一个 span 拿到的 id 一模一样，
   编辑才敢按 id 找回来（core/page_edit.py 的 locate_span 就是反向解析这个串）。
   注意「同一份文件」是前提：写回之后是新文件，页面内容变了，id 可能落到别的字上，
   所以 apply 之后必须重新取一次页面模型，不许拿旧 id 连改两轮。

2. **对外一律给渲染坐标（左上原点、y 向下、已按 /Rotate 转正）**，和查看页那张图同一套。
   PyMuPDF 的文字（get_text）、图片（get_image_info）、矢量（get_cdrawings）三套接口
   给的**都是没转角度那一套坐标**（相对未旋转页面左上角），而拍出来/画出来的图是转好的：
   OCR 那条链已经踩过一次（见 api/ocr.py 的 _page_rules 注释）——
   400×200 的页面转 90°，未转坐标里 y=150 的一条横线，拍出来是 x=50 的一条竖线；
   不套 `page.rotation_matrix` 就是整页错位，框和字对不上。
   所以本模块每个框都过一遍 rotation_matrix 再交出去（rotation=0 时是单位矩阵，跳过）。
   未转坐标也留在内部记录里（键名带 `_raw`），core/page_edit.py 写回用的正是那一套：
   add_redact_annot / insert_text 认的就是未转坐标，两边各自自洽、绝不混用。

3. **异常输入不许往上抛**。空页、纯扫描件（没有文字层）、只有图片的页、加密页、
   坏文件一律返回字典：能解析的给 `success: True` + `blocks`（可能为空）+
   `has_text_layer: False`；解析不了的给 `success: False` + `error` 错误码，
   由 api/page_model.py 翻成界面文案（本模块不 import i18n，错误码是纯数据）。
"""

import hashlib
import logging
import time

import pymupdf

MODEL_VERSION = 1
"""页面模型的结构版本号。字段增删（含义变了不算）必须把这个号加一，
前端/缓存按它判断手里那份 JSON 还作不作数——和 api/ocr.py 的 PAGE_CACHE_VERSION 同一个意思。"""

UNIT = 'pt'
COORD_SPACE = 'display'
"""对外坐标系的名字：页面渲染坐标（左上原点、y 向下、rotation 已生效）。"""

# 一页能装多少块是有人管的：扫描册一页几百张切片图、几千条矢量（本机实测那份 80 页
# 学案：一页 194 张图、2430 条路径），全塞进 JSON 就是几 MB，桥层要超时、界面要卡死。
# 超出上限就截断并如实报 truncated，让前端知道「这页东西太多，只给了前 N 个」。
MAX_TEXT_BLOCKS = 400
MAX_IMAGE_BLOCKS = 200
MAX_DRAWING_BLOCKS = 400
MAX_ITEMS_PER_DRAWING = 12

# PyMuPDF 给的行方向（未转坐标里）→ 写回时 insert_text 的 rotate 参数。
# 实测：rotate=90 出来的行 dir 是 (0,-1)，rotate=270 是 (0,1)（字往上/往下走）。
DIR_TO_ROTATE = {(1.0, 0.0): 0, (0.0, -1.0): 90, (-1.0, 0.0): 180, (0.0, 1.0): 270}

# span flags 的位含义（PyMuPDF 文档定的这四个 bit），拆成布尔给前端直接点。
_FLAG_BITS = (('superscript', 1), ('italic', 2), ('serif', 4), ('monospaced', 8), ('bold', 16))


def _num(value, digits=2):
    """坐标一律收成 float 并圆到 2 位小数：JSON 变小、比较稳定，0.01pt 的差没人看得出。"""
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


def _box(raw):
    """把「四元组或 Rect」收成 [x0, y0, x1, y1]（顺序统一、保证 x1>=x0 / y1>=y0）。"""
    if raw is None:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        if hasattr(raw, 'x0'):
            values = (raw.x0, raw.y0, raw.x1, raw.y1)
        else:
            values = tuple(raw)
        if len(values) != 4:
            return [0.0, 0.0, 0.0, 0.0]
        x0, y0, x1, y1 = (float(v) for v in values)
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0, 0.0]
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [_num(x0), _num(y0), _num(x1), _num(y1)]


def _apply_matrix(box, matrix):
    """把一个框的四个角点乘进 matrix，再取外接框。matrix 是 None 就原样给回。"""
    if matrix is None:
        return box
    x0, y0, x1, y1 = box
    corners = []
    for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1)):
        corners.append((x * matrix.a + y * matrix.c + matrix.e,
                        x * matrix.b + y * matrix.d + matrix.f))
    return _box((min(c[0] for c in corners), min(c[1] for c in corners),
                 max(c[0] for c in corners), max(c[1] for c in corners)))


def _point_matrix(point, matrix):
    """一个点（比如基线起点）乘 matrix；matrix 为 None（没转角度）时只圆整。"""
    x, y = float(point[0]), float(point[1])
    if matrix is None:
        return [_num(x), _num(y)]
    return [_num(x * matrix.a + y * matrix.c + matrix.e),
            _num(x * matrix.b + y * matrix.d + matrix.f)]


def _vector_matrix(vector, matrix):
    """行方向这种「向量」只转不平移（matrix 的 e/f 两项丢掉）。"""
    x, y = float(vector[0]), float(vector[1])
    if matrix is None:
        return [_num(x, 4), _num(y, 4)]
    return [_num(x * matrix.a + y * matrix.c, 4), _num(x * matrix.b + y * matrix.d, 4)]


def _color_hex(rgb):
    """PyMuPDF 的 (r,g,b) 浮点色 → '#rrggbb'；None 原样返回（矢量「不描边」就是 None）。"""
    if rgb is None:
        return None
    try:
        channels = [max(0, min(255, int(round(float(c) * 255)))) for c in rgb]
    except (TypeError, ValueError):
        return None
    if len(channels) != 3:
        return None
    return '#%02x%02x%02x' % tuple(channels)


def _int_color_to_hex(value):
    """span 的 color 是一个 int（0xff0000 这种），顺手也给个 '#rrggbb' 方便前端直接用。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return '#%06x' % (number & 0xFFFFFF)


def _digest_of(raw_bytes):
    """图片内容的短指纹（sha1 前 16 位）：同一张图不管被摆在哪一页、摆几次都是同一个串。"""
    try:
        return hashlib.sha1(raw_bytes).hexdigest()[:16]
    except Exception:
        return ''


def _cdrawings(page):
    """取一页的矢量路径（带样式）。

    优先 `get_cdrawings(extended=True)`：本机实测那份 80 页扫描学案，
    慢的 `get_drawings()` 一页 0.51s、快的这个 0.20s，条数和样式都不少（api/ocr.py
    的 _page_rules 同口径）。老版本没有这个接口、或读炸了就退回 get_drawings()；
    再炸就返回空——矢量框线只是编辑时的「底下有没有东西」参考，读不到不该让整页失败。
    """
    try:
        if hasattr(page, 'get_cdrawings'):
            return page.get_cdrawings(extended=True) or []
    except Exception:
        logging.exception('page_model: 快速矢量接口失败，退回 get_drawings')
    try:
        return page.get_drawings() or []
    except Exception:
        logging.exception('page_model: 矢量读取失败（这一页不给 drawing 块）')
        return []


def _image_digest(doc, xref, info):
    """这张图的指纹：先用 PyMuPDF 已经算好的 digest，没有就自己按原始流算一次。"""
    digest = info.get('digest')
    if isinstance(digest, (bytes, bytearray)) and digest:
        return digest.hex()
    if isinstance(digest, str) and digest:
        return digest[:64]
    try:
        raw = doc.xref_stream_raw(xref)
    except Exception:
        return ''
    return _digest_of(raw)


def _items_of(drawing):
    """把一条矢量路径的几何逐项收成可序列化的小字典，并把这支笔的粗细颜色抄给每一项。

    为什么要抄一份：PyMuPDF 是一支笔管整条路径（样式在路径级），而界面要点住「某一格
    框线」时拿的是 item。宁可多写几个字节，也不让前端回去查路径。
    """
    stroke = _color_hex(drawing.get('color'))
    fill = _color_hex(drawing.get('fill'))
    width = drawing.get('width')
    common = {'color': stroke, 'fill': fill,
              'width': _num(width, 3) if width is not None else None}
    out = []
    for item in (drawing.get('items') or [])[:MAX_ITEMS_PER_DRAWING]:
        try:
            kind = str(item[0])
            payload = {'kind': kind}
            payload.update(common)
            if kind == 'l' and len(item) >= 3:
                payload['from'] = [_num(item[1].x if hasattr(item[1], 'x') else item[1][0]),
                                   _num(item[1].y if hasattr(item[1], 'y') else item[1][1])]
                payload['to'] = [_num(item[2].x if hasattr(item[2], 'x') else item[2][0]),
                                 _num(item[2].y if hasattr(item[2], 'y') else item[2][1])]
            elif kind == 'c':
                payload['points'] = [[_num(p.x if hasattr(p, 'x') else p[0]),
                                      _num(p.y if hasattr(p, 'y') else p[1])]
                                     for p in item[1:5]]
            elif kind == 're' and len(item) >= 2:
                payload['rect'] = _box(item[1])
                if len(item) >= 3:
                    payload['quad_fill'] = int(item[2] or 0)
            else:
                # 认不出的形状（qu/空项/以后新增的）只留坐标，别硬编，也别把整页带崩
                points = []
                for p in item[1:]:
                    try:
                        points.append([_num(p.x if hasattr(p, 'x') else p[0]),
                                       _num(p.y if hasattr(p, 'y') else p[1])])
                    except (TypeError, ValueError, AttributeError):
                        continue
                if points:
                    payload['points'] = points
                else:
                    payload['detail'] = str(item)[:80]
            out.append(payload)
        except Exception:
            logging.exception('page_model: 单个矢量项整形失败，跳过')
            continue
    return out


def _collect(doc, page):
    """把一页拆成内部记录（坐标是**未转**那一套，键带 _raw），给上层两种用法：

      * 转成对外的渲染坐标给前端（build_model）；
      * 直接拿未转坐标写回（core/page_edit.py 走这条）。
    """
    rotation = int(getattr(page, 'rotation', 0) or 0) % 360
    text_blocks = []
    try:
        data = page.get_text('dict') or {}
    except Exception:
        logging.exception('page_model: 取文字层失败（这一页按没有字处理）')
        data = {}
    for raw_block in data.get('blocks') or []:
        if raw_block.get('type') != 0:
            continue        # type 1 是图片：图片块从 get_image_info 取（那边有 xref 和指纹）
        lines = []
        for raw_line in raw_block.get('lines') or []:
            spans = []
            for raw_span in raw_line.get('spans') or []:
                text = str(raw_span.get('text') or '')
                if not text.strip() and not text:
                    continue
                spans.append({
                    'text': text,
                    'bbox_raw': _box(raw_span.get('bbox')),
                    'origin_raw': [_num((raw_span.get('origin') or (0, 0))[0]),
                                   _num((raw_span.get('origin') or (0, 0))[1])],
                    'font': str(raw_span.get('font') or ''),
                    'size': _num(raw_span.get('size'), 3),
                    'color': int(raw_span.get('color') or 0),
                    'flags': int(raw_span.get('flags') or 0),
                    'ascender': _num(raw_span.get('ascender'), 4),
                    'descender': _num(raw_span.get('descender'), 4),
                })
            if not spans:
                continue
            direction = raw_line.get('dir') or (1.0, 0.0)
            lines.append({
                'bbox_raw': _box(raw_line.get('bbox')),
                'dir_raw': [_num(direction[0], 4), _num(direction[1], 4)],
                'spans': spans,
            })
        if lines:
            text_blocks.append({
                'bbox_raw': _box(raw_block.get('bbox')),
                'number': int(raw_block.get('number') or 0),
                'flags': int(raw_block.get('flags') or 0),
                'lines': lines,
            })
        if len(text_blocks) >= MAX_TEXT_BLOCKS:
            break

    images = []
    try:
        infos = page.get_image_info(xrefs=True) or []
    except Exception:
        logging.exception('page_model: 取图片清单失败（这一页不给 image 块）')
        infos = []
    truncated_images = max(0, len(infos) - MAX_IMAGE_BLOCKS)
    for info in infos[:MAX_IMAGE_BLOCKS]:
        xref = int(info.get('xref') or 0)
        transform = info.get('transform') or ()
        images.append({
            'bbox_raw': _box(info.get('bbox')),
            'xref': xref,
            'digest': _image_digest(doc, xref, info),
            'pix_width': int(info.get('width') or 0),
            'pix_height': int(info.get('height') or 0),
            'colorspace': str(info.get('cs-name') or info.get('colorspace') or ''),
            'bpc': int(info.get('bpc') or 0),
            'size_bytes': int(info.get('size') or 0),
            'has_mask': bool(info.get('has-mask')),
            'transform': [_num(v, 3) for v in transform],
            'number': int(info.get('number') or 0),
        })

    drawings = []
    paths = _cdrawings(page)
    # clip 项没有 rect（只是裁剪路径），既不能当块定位也不该占 id，直接不收
    paths = [d for d in paths if d.get('rect') is not None]
    paths.sort(key=lambda d: (int(d.get('seqno') or 0), str(d.get('type') or '')))
    truncated_drawings = max(0, len(paths) - MAX_DRAWING_BLOCKS)
    for drawing in paths[:MAX_DRAWING_BLOCKS]:
        rect = _box(drawing.get('rect'))
        stroke_width = drawing.get('width')
        drawings.append({
            'rect_raw': rect,
            'path_type': str(drawing.get('type') or ''),
            'stroke_color': _color_hex(drawing.get('color')),
            'fill_color': _color_hex(drawing.get('fill')),
            'width': _num(stroke_width, 3) if stroke_width is not None else None,
            'stroke_opacity': _num(drawing.get('stroke_opacity'), 3),
            'fill_opacity': _num(drawing.get('fill_opacity'), 3),
            'dashes': str(drawing.get('dashes') or ''),
            'even_odd': bool(drawing.get('even_odd')),
            'close_path': bool(drawing.get('closePath')),
            'seqno': int(drawing.get('seqno') or 0),
            'level': int(drawing.get('level') or 0) if isinstance(drawing.get('level'), int) else 0,
            'items': _items_of(drawing),
        })

    try:
        text_chars = len((page.get_text('text') or '').strip())
    except Exception:
        logging.exception('page_model: 取纯文本字数失败，按 0 处理')
        text_chars = 0

    return {
        'rotation': rotation,
        'matrix': page.rotation_matrix if rotation else None,
        'rect': page.rect,
        'mediabox': page.mediabox,
        'cropbox': page.cropbox,
        'text_blocks': text_blocks,
        'images': images,
        'drawings': drawings,
        'text_chars': text_chars,
        'truncated': {'text_blocks': max(0, len(data.get('blocks') or []) - MAX_TEXT_BLOCKS),
                      'images': truncated_images,
                      'drawings': truncated_drawings},
    }


def collect_page(doc, page):
    """`_collect` 的公开入口：同一页要连用几次（预检 + 定位 + 计数）时解析一次就够。"""
    return _collect(doc, page)


def block_id(page_number, block_index):
    """块 id：`p<1 基页号>_b<块序>`。"""
    return 'p%d_b%d' % (page_number, block_index)


def span_id(page_number, block_index, line_index, span_index):
    """span id：`p<1 基页号>_b<块序>_l<行序>_s<span 序>`——写回按这个串找回同一处字。"""
    return 'p%d_b%d_l%d_s%d' % (page_number, block_index, line_index, span_index)


def parse_span_id(value):
    """拆 span id → (页号(1 基), 块序, 行序, span 序)；不是这个形状就返回 None。"""
    parts = str(value or '').split('_')
    if len(parts) != 4 or not parts[0].startswith('p') or not parts[1].startswith('b') \
            or not parts[2].startswith('l') or not parts[3].startswith('s'):
        return None
    try:
        return (int(parts[0][1:]), int(parts[1][1:]), int(parts[2][1:]), int(parts[3][1:]))
    except ValueError:
        return None


def build_model(doc, page_index, page_number=None, collected=None, started=None):
    """把一页（内部记录）整成对外的页面模型字典。

    给前端的那份只有一种坐标系：**渲染坐标**。`blocks` 里三种块共用一个序列，
    顺序固定为文字块 → 图片块 → 矢量块，所以 id 里的块序对同一份文件永远指同一块。
    """
    page = doc.load_page(page_index)
    data = collected or _collect(doc, page)
    matrix = data['matrix']
    rotation = data['rotation']
    number = int(page_number if page_number is not None else page_index + 1)
    rect = data['rect']
    blocks = []
    text_count = image_count = drawing_count = 0

    for bi, block in enumerate(data['text_blocks']):
        lines = []
        for li, line in enumerate(block['lines']):
            spans = []
            for si, span in enumerate(line['spans']):
                flags = span['flags']
                spans.append({
                    'id': span_id(number, bi, li, si),
                    'text': span['text'],
                    'bbox': _apply_matrix(span['bbox_raw'], matrix),
                    'origin': _point_matrix(span['origin_raw'], matrix),
                    'font': span['font'],
                    'size': span['size'],
                    'color': span['color'],
                    'color_hex': _int_color_to_hex(span['color']),
                    'flags': flags,
                    'bold': bool(flags & 16),
                    'italic': bool(flags & 2),
                    'serif': bool(flags & 4),
                    'monospaced': bool(flags & 8),
                    'ascender': span['ascender'],
                    'descender': span['descender'],
                })
            direction = line['dir_raw']
            lines.append({
                'id': 'p%d_b%d_l%d' % (number, bi, li),
                'bbox': _apply_matrix(line['bbox_raw'], matrix),
                'dir': _vector_matrix(direction, matrix),
                'spans': spans,
            })
        text_count += 1
        blocks.append({
            'id': block_id(number, bi),
            'type': 'text',
            'bbox': _apply_matrix(block['bbox_raw'], matrix),
            'number': block['number'],
            'text': ''.join(s['text'] for l in block['lines'] for s in l['spans']),
            'line_count': len(block['lines']),
            'span_count': sum(len(l['spans']) for l in block['lines']),
            'lines': lines,
        })

    for k, image in enumerate(data['images']):
        image_count += 1
        blocks.append({
            'id': block_id(number, text_count + k),
            'type': 'image',
            'bbox': _apply_matrix(image['bbox_raw'], matrix),
            'xref': image['xref'],
            'digest': image['digest'],
            'pix_width': image['pix_width'],
            'pix_height': image['pix_height'],
            'colorspace': image['colorspace'],
            'bpc': image['bpc'],
            'size_bytes': image['size_bytes'],
            'has_mask': image['has_mask'],
            'transform': image['transform'],
        })

    for k, drawing in enumerate(data['drawings']):
        drawing_count += 1
        rect_box = drawing['rect_raw']
        # 细线的框在 PyMuPDF 里是「高度为零」的一条，膨胀半个笔宽再交出去，
        # 前端按这个框命中才不至于点不中（不改变原始 rect 的含义，只是给个可用范围）
        pad = (drawing['width'] or 0) / 2.0
        hit = _box((rect_box[0] - pad, rect_box[1] - pad,
                    rect_box[2] + pad, rect_box[3] + pad))
        blocks.append({
            'id': block_id(number, text_count + image_count + k),
            'type': 'drawing',
            'bbox': _apply_matrix(hit, matrix),
            'rect': _apply_matrix(rect_box, matrix),
            # PyMuPDF 把「描边/填充」这个信息就叫 type，可我们块级已经用了 type
            # （text/image/drawing）。这里改叫 path_type，值原样不动：'f' / 's' / 'fs'。
            'path_type': drawing['path_type'],
            'stroke': {'color': drawing['stroke_color'], 'width': drawing['width'],
                       'opacity': drawing['stroke_opacity'], 'dashes': drawing['dashes']},
            'fill': {'color': drawing['fill_color'], 'opacity': drawing['fill_opacity']},
            'even_odd': drawing['even_odd'],
            'close_path': drawing['close_path'],
            'seqno': drawing['seqno'],
            'items': [{**item,
                       'rect': _apply_matrix(item['rect'], matrix)}
                      if 'rect' in item else item for item in drawing['items']],
        })

    model = {
        'model_version': MODEL_VERSION,
        'page': number,
        'page_index': page_index,
        'page_count': doc.page_count,
        'width': _num(rect.width),
        'height': _num(rect.height),
        'rotation': rotation,
        'unit': UNIT,
        'coord': COORD_SPACE,
        'mediabox': _box(data['mediabox']),
        'cropbox': _box(data['cropbox']),
        'has_text_layer': data['text_chars'] > 0,
        'text_chars': data['text_chars'],
        'block_count': {'text': text_count, 'image': image_count, 'drawing': drawing_count},
        'truncated': dict(data['truncated']),
        'blocks': blocks,
    }
    if started is not None:
        model['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return model


def _error(code, **detail):
    result = {'success': False, 'error': code}
    if detail:
        result['detail'] = detail.get('detail') or detail
    return result


def page_model(pdf_path, page_index=0, password='', **options):
    """把一个 PDF 的第 page_index 页（0 基）解析成页面模型。

    返回 `{'success': True, ...模型字段...}` 或 `{'success': False, 'error': 错误码, ...}`；
    任何异常都吃在下面，不往桥层抛（前端拿到的一定是能 JSON 化的字典）。
    可选参数：password（加密文件）、以及 build_model 用不上的预留位（先收着不报错）。
    """
    import os
    started = time.monotonic()
    path = str(pdf_path or '')
    if not path or not os.path.isfile(path):
        return _error('not_found', detail=path)
    try:
        doc = pymupdf.open(path)
    except Exception as error:
        logging.exception('page_model: 打不开文件 %s', path)
        return _error('unreadable', detail='%s: %s' % (type(error).__name__, error))
    try:
        # 加密状态要在 authenticate 之前记：认证过后 doc.is_encrypted 就变 False 了，
        # 到那时再问只能报「这份没加密」，前端该提示输密码的时机就丢了。
        was_locked = bool(doc.needs_pass)
        if was_locked:
            if not doc.authenticate(str(password or '')):
                return _error('needs_password' if not password else 'bad_password')
        try:
            index = int(page_index)
        except (TypeError, ValueError):
            return _error('page_out_of_range', detail={'index': page_index, 'total': doc.page_count})
        total = doc.page_count
        if total <= 0:
            return _error('empty_document', detail={'total': total})
        if index < 0 or index >= total:
            return _error('page_out_of_range', detail={'index': index, 'total': total})
        model = build_model(doc, index, started=started)
        model['source'] = path
        model['encrypted'] = was_locked
        model['success'] = True
        return model
    except Exception as error:
        logging.exception('page_model: 解析失败 %s 第 %s 页', path, page_index)
        return _error('parse_failed', detail='%s: %s' % (type(error).__name__, error))
    finally:
        try:
            doc.close()
        except Exception:
            pass


def locate_span(page, span_id_value, page_number=None, collected=None):
    """按 span id 在这一页上找回它的**未转坐标**几何与样式（写回专用，不给前端）。

    返回 None 表示「这个 id 在这一页上不存在」——调用方必须如实报错，
    不能猜一个位置把字写上去。带回来的 `under` 是写回风险判断的原料：
    这块字底下压着哪些图片/矢量、旁边挨着哪些别的 span（id 列表，各留上限 20 个）。
    `collected` 传 `collect_page()` 的结果可以省一遍整页解析（同一页连查多个 id 时用）。
    """
    parsed = parse_span_id(span_id_value)
    if not parsed:
        return None
    number, bi, li, si = parsed
    if page_number is None:
        # 调用方没指定就自己核：id 里的页号（1 基）必须就是手里这一页，
        # 否则「p5 的 span」会被写到第 1 页上去——页页长得像，错了没人看得见。
        page_number = (int(page.number) + 1) if hasattr(page, 'number') else None
    if page_number is not None and int(page_number) != number:
        return None
    # PyMuPDF 的 Page 认 `page.parent`（Document），没有 .doc：_collect 只要拿得到
    # 文档就能读图片原始流算指纹，拿不到就退化成 PyMuPDF 自带的 digest。
    data = collected or _collect(getattr(page, 'parent', None), page)
    if bi >= len(data['text_blocks']):
        return None        # 这个下标落在图片块/矢量块上，不是文字，写不回
    block = data['text_blocks'][bi]
    if li >= len(block['lines']):
        return None
    line = block['lines'][li]
    if si >= len(line['spans']):
        return None
    span = line['spans'][si]
    box = span['bbox_raw']
    rect = pymupdf.Rect(box[0], box[1], box[2], box[3])
    # 判「底下有没有东西」用的容差：细线框高度为零、相邻 span 的框常常首尾相接，
    # 一律按往里缩 0.5pt 之后的实际重叠算，免得每个编辑都背一堆假 risk。
    probe = pymupdf.Rect(rect.x0 + 0.5, rect.y0 + 0.5, rect.x1 - 0.5, rect.y1 - 0.5)
    if probe.is_empty or probe.width <= 0 or probe.height <= 0:
        probe = rect
    area = max(0.0001, probe.get_area())

    images = []
    for k, image in enumerate(data['images']):
        ib = image['bbox_raw']
        hit = pymupdf.Rect(ib[0], ib[1], ib[2], ib[3])
        inter = probe & hit
        if not inter.is_empty and inter.get_area() / area > 0.01:
            images.append(block_id(number, k))
            if len(images) >= 20:
                break

    text_count = len(data['text_blocks'])
    drawings = []
    for k, drawing in enumerate(data['drawings']):
        pad = (drawing['width'] or 0) / 2.0 + 0.25
        db = drawing['rect_raw']
        hit = pymupdf.Rect(db[0] - pad, db[1] - pad, db[2] + pad, db[3] + pad)
        inter = probe & hit
        if not inter.is_empty and inter.get_area() / area > 0.01:
            drawings.append(block_id(number, text_count + len(data['images']) + k))
            if len(drawings) >= 20:
                break

    neighbors = []
    for other_bi, other_block in enumerate(data['text_blocks']):
        for other_li, other_line in enumerate(other_block['lines']):
            for other_si, other_span in enumerate(other_line['spans']):
                if (other_bi, other_li, other_si) == (bi, li, si):
                    continue
                ob = other_span['bbox_raw']
                hit = pymupdf.Rect(ob[0], ob[1], ob[2], ob[3])
                inter = probe & hit
                if not inter.is_empty and inter.get_area() / area > 0.01:
                    neighbors.append(span_id(number, other_bi, other_li, other_si))
                    if len(neighbors) >= 20:
                        break

    direction = tuple(line['dir_raw'])
    return {
        'id': str(span_id_value),
        'page_number': number,
        'page_index': page.number if hasattr(page, 'number') else None,
        'block_index': bi, 'line_index': li, 'span_index': si,
        'text': span['text'],
        'rect_raw': box,
        'origin_raw': span['origin_raw'],
        'dir_raw': line['dir_raw'],
        'rotate': DIR_TO_ROTATE.get(direction),
        'font': span['font'],
        'size': span['size'],
        'color': span['color'],
        'flags': span['flags'],
        'under': {'images': images, 'drawings': drawings, 'spans': neighbors},
    }


def iter_spans(model):
    """遍历模型里的每个 span（给验收脚本和前端全文搜索用的便利生成器）。"""
    for block in model.get('blocks') or []:
        if block.get('type') != 'text':
            continue
        for line in block.get('lines') or []:
            for span in line.get('spans') or []:
                yield block, line, span


def model_to_text(model):
    """把模型里的字按行拼回一段文本（顺序 = 块序 → 行序 → span 序），给核对和调试用。"""
    rows = []
    for _block, line, span in iter_spans(model):
        rows.append(span['text'])
    return '\n'.join(rows)
