"""OCR 工具组：文本识别、可搜索 PDF、敏感信息、扫描质检、自动书签。

核心引擎在 core.ocr；这里只负责 PDF 页循环、文件读写和结果整形。
界面上的新功能都标 Beta，先保证结果真实、失败信息能看懂。
"""

import hashlib
import json
import logging
import os
import re
import time
import unicodedata

import pymupdf

from api._io import atomic_output, close_pdf, save_pdf_doc
from api._range import select_pages
from api.redact import _classify
from core import jobctl
from core import ocr
from core.paths import OCR_CACHE_DIR
from core.i18n import t as _text

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NUMBER_SPAN = re.compile(r"\d[\d \u3000-]{8,24}\d|\d{11,19}[Xx]?|\d{17}[\dXx]")
_HEADING = re.compile(r"^(第[一二三四五六七八九十0-9百]+[章节篇部]|[0-9]+[.、]|附录|目录|前言|结语|参考文献)")
_JA_TEXT = re.compile(r'[\u3040-\u30ff]')
_KO_TEXT = re.compile(r'[\uac00-\ud7af\u1100-\u11ff]')
_CJK_TEXT = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
# 西里尔、希腊这些字母 PDF 的 14 套内置字体盖不住，只能临时嵌一份系统 TTF。
_TTF_CANDIDATES = (r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\times.ttf',
                   r'C:\Windows\Fonts\calibri.ttf')


def _layer_font(page, text, wide_font=''):
    """给这一行挑 PDF 里能写出这些字的字体，返回（字体名, 这一页已嵌的 TTF 名）。

    用错字体的后果是「字层静默掉字」：文件正常、看不出来，用户搜不到才发现。
    """
    if _JA_TEXT.search(text):
        return 'japan', wide_font
    if _KO_TEXT.search(text):
        return 'korea', wide_font
    if _CJK_TEXT.search(text):
        return 'china-s', wide_font
    if all(ord(ch) < 0x180 for ch in text):
        return 'helv', wide_font
    if not wide_font:
        for candidate in _TTF_CANDIDATES:
            if os.path.isfile(candidate):
                page.insert_font(fontname='tkwide', fontfile=candidate)
                wide_font = 'tkwide'
                break
    return wide_font or 'helv', wide_font


def _render_page(doc, index, dpi):
    page = doc.load_page(index)
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    image = ocr._decode_png(pixmap.tobytes("png"))
    return page, pixmap, image


def _preprocess(image):
    """扫描优化：纠偏 + 按对比度自适应增强。**不裁边、不丢彩色**。

    三个旧毛病一次修掉：
    ① 旧版裁内容边，但上报的 width/height 还是原图的，识别框和页宽对不上，
       栏检测/表格定位全偏——坐标一致性比裁那点边重要，保持整幅尺寸。
    ② 旧版无脑转灰度：这份原件的标题是**蓝色**的，转灰度后蓝对白对比度大跌，
       彩色标题反而认不出（实测 dpi200 下命中从 12/12 掉到 11/12）。保留彩色。
    ③ 旧版无脑 medianBlur+CLAHE：干净印刷件再 blur 会糊小字、再 CLAHE 放大底噪。
       只有真低对比才增强，且只动亮度通道（LAB 的 L），颜色不变。
    """
    import cv2
    import numpy as np

    # 灰度图（单通道）先补成三通道：后面 LAB / 彩色纠偏都按三通道写，
    # 不补的话 cvtColor 直接抛异常，一张灰度扫描件就把整页识别弄崩。
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    out = image
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    points = cv2.findNonZero(cv2.threshold(gray, 0, 255,
                                           cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1])
    if points is not None and len(points) > 50:
        angle = cv2.minAreaRect(points)[-1]
        if angle > 45:
            angle -= 90
        if angle < -45:
            angle += 90
        if 0.2 < abs(angle) < 10:
            h, w = out.shape[:2]
            matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            out = cv2.warpAffine(out, matrix, (w, h), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE)
    if float(np.std(gray)) < 60:
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
        out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    return out


# ==================== 逐页结果缓存 ====================
# 一个人拿到扫描件的常见动作顺序是：先看识别结果、再导 TXT、觉得不对换个档再来一遍、
# 最后导 Word。每一遍都在把同样的页重新 OCR 一次——一页 1～3 秒，一百页就是几分钟的白等。
# 缓存按「文件 + 页 + 影响结果的那几个参数」建键，参数一变键就变，不会拿旧结果糊弄新请求。
# 版面判据一改（框线分表格、填空下划线）就必须把这个号加一：缓存里存的是整型的 blocks，
# 旧号命中的话新判据一行都不会生效——用户看到的还是升级前那份错版面。
PAGE_CACHE_VERSION = 3
PAGE_CACHE_DIR = os.path.join(OCR_CACHE_DIR, 'pages')
# 上限是「页」不是字节：240 页 ≈ 一本厚扫描书的一次完整识别，按实测一页 JSON 20～60 KB
# 算，最多也就十几 MB。再大就该用户自己清缓存了，不该让一个临时缓存吃掉 %LOCALAPPDATA%。
PAGE_CACHE_LIMIT = 240
# 扫描件出图精度：150dpi 下这份双栏答案解析的小字会漏认（人工转录句 11/12），
# 200dpi 全中且平均置信更高（.9769→.9816）。再往上（240）收益很小、像素却翻倍，
# 200 是准确度与速度的平衡点。数字页不走这条路（直接抽字层），不受影响。
SCAN_DPI = 200


def _cache_key(path, number, quality, lang, preprocess, dpi=0):
    """能算出键就算缓存；算不出（文件刚被移走）就返回空串当没这回事。

    dpi 也在键里：换了出图精度就是另一份识别结果，拿旧 dpi 的缓存糊弄新请求
    会让「提精度」这个动作看起来毫无效果。
    """
    try:
        stat = os.stat(path)
    except OSError:
        return ''
    raw = '|'.join([os.path.abspath(str(path)), str(int(stat.st_mtime)), str(stat.st_size),
                    str(number), str(quality), str(lang or ''), str(bool(preprocess)),
                    str(int(dpi or 0))])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _cache_read(key):
    """命中回这一页的结构，没命中回 None。

    读不动一律当没命中（半截文件、上一个版本的形状、只读目录），
    缓存坏了不该让识别功能报错——大不了重跑一遍。
    """
    if not key:
        return None
    target = os.path.join(PAGE_CACHE_DIR, key + '.json')
    try:
        with open(target, 'r', encoding='utf-8') as handle:
            box = json.load(handle)
        payload = box.get('payload')
        if box.get('v') != PAGE_CACHE_VERSION or not isinstance(payload, dict):
            return None
        os.utime(target, None)        # 摸一下就算「最近用过」，淘汰按这个时间排
    except (OSError, ValueError):
        return None
    payload['cached'] = True
    # 这一遍没花时间，报的时间就不能是上一遍的：不然用户会以为缓存没生效
    payload['elapsed_ms'] = 0
    return payload


def _cache_write(key, payload):
    if not key:
        return
    try:
        os.makedirs(PAGE_CACHE_DIR, exist_ok=True)
        stored = dict(payload)
        stored.pop('cached', None)
        with open(os.path.join(PAGE_CACHE_DIR, key + '.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump({'v': PAGE_CACHE_VERSION, 'payload': stored}, handle,
                      ensure_ascii=False)
        _cache_trim()
    except (OSError, TypeError, ValueError):
        # 写不进去（盘满、目录只读、结构里混进了不能序列化的东西）只是下次还要重算，
        # 不值得把这次的识别结果一起丢掉。
        logging.exception('OCR 页缓存写入失败')


def _cache_trim():
    """超上限就按最后使用时间删最老的，删到剩上限为止。"""
    try:
        names = [n for n in os.listdir(PAGE_CACHE_DIR) if n.endswith('.json')]
        if len(names) <= PAGE_CACHE_LIMIT:
            return
        dated = []
        for name in names:
            try:
                dated.append((os.path.getmtime(os.path.join(PAGE_CACHE_DIR, name)), name))
            except OSError:
                continue
        dated.sort()
        for _mtime, name in dated[:len(names) - PAGE_CACHE_LIMIT]:
            try:
                os.remove(os.path.join(PAGE_CACHE_DIR, name))
            except OSError:
                pass
    except OSError:
        pass


def _ocr_page(doc, index, quality, preprocess, dpi=SCAN_DPI, lang=None):
    page, pixmap, image = _render_page(doc, index, dpi)
    if preprocess:
        image = _preprocess(image)
    result = ocr._run_image(image, quality, {
        "page_index": index, "page_count": len(doc), "dpi": dpi,
        "width": pixmap.width, "height": pixmap.height,
        "preprocessed": bool(preprocess),
        # 这一页本来有多少内嵌文字：OCR 是把页面拍成图再认，本来就带文字层的页
        # 用「导出文字」直接抽出来更准更快，界面据此提醒用户。
        "text_chars": len(page.get_text("text").strip()),
    }, lang)
    if result.get("success"):
        # 页面上画出来的框线与下划线（换算成图片像素，和识别框同一套坐标）：
        # 有它就能按真框线分表格、也能把「挖空」的下划线带进导出结果。
        result["rules"] = _page_rules(page, dpi / 72.0)
    return result


# 图片也能直接识别：OCR 这一栏的「选择文件」不再只收 PDF。
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp')


def _is_image(path):
    return str(path or '').lower().endswith(IMAGE_SUFFIXES)


def _ocr_image_source(path, quality, lang, preprocess):
    """把一张图片当一页来识别（尺寸就是像素尺寸，后面排版按这个比例走）。"""
    image = ocr._read_image(path)
    if preprocess:
        image = _preprocess(image)
    height, width = image.shape[0], image.shape[1]
    return ocr._run_image(image, quality, {
        "width": width, "height": height, "image_path": path,
        "preprocessed": bool(preprocess),
    }, lang)


def _layout_blocks(items, width, rules):
    """版面重建的唯一入口：矢量框线先切真表格，剩下的排成正文行（含填空下划线）。

    OCR 那一页和数字页的字层两条路都从这里走；判据只留一份，改一处不会漏另一处。
    """
    horiz = (rules or {}).get('h') or []
    vert = (rules or {}).get('v') or []
    grids = _rule_grids(horiz, vert, float(width or 1))
    return _page_blocks(items, width, grids, bool(horiz or vert), _blank_lines(rules, grids))


def _page_payload(number, item, source="ocr"):
    """把一次整页识别整形成界面与导出共用的那一页数据（blocks 已分正文/表格）。"""
    lines = item.get("lines") or []
    width = item.get("width")
    return {
        "page": number,
        "source": source,
        "text": item.get("text") or "",
        "lines": lines,
        "blocks": _layout_blocks(_items_of(lines), width, item.get("rules")),
        "elapsed_ms": item.get("elapsed_ms", 0),
        "text_chars": item.get("text_chars", 0),
        "width": width,
        "height": item.get("height"),
    }


# 一页有多少内嵌文字才算「这页本来就是数字页」。20 字不是随手定的：
# 可搜索 PDF 那边「已有文字层就跳过」用的也是这条线，两处口径必须一致，
# 否则同一页在一个入口算数字页、在另一个入口算扫描页。
TEXT_LAYER_MIN = 20


def _text_layer_chars(page):
    """这一页内嵌了多少字（去掉首尾空白再数）——「这页是不是数字页」就看这个数。"""
    return len((page.get_text("text") or "").strip())


def _embedded_lines(page):
    """把 PDF 自带的文字层整成 OCR 那一行的形状（文本 + 四点框 + 置信度）。

    坐标用 PDF 点而不是像素，调用方把 width/height 也按点给：版面重建只看相对位置，
    真正的错法是同一页里一半点一半像素。score 一律 1.0 —— 内嵌文字是文件里写着的字，
    不是猜出来的，不该跟着识别结果一起被打「低于 0.6 算可疑」的折扣。
    """
    lines = []
    for block in page.get_text("dict").get("blocks") or []:
        if block.get("type") != 0:
            continue        # type 1 是图片，文字层里没有可读的字
        for line in block.get("lines") or []:
            text = ''.join(str(span.get("text") or "") for span in line.get("spans") or [])
            if not text.strip():
                continue
            x0, y0, x1, y1 = line.get("bbox") or (0, 0, 0, 0)
            lines.append({"text": text.strip(), "score": 1.0,
                          "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]})
    return lines


def _embedded_payload(number, page):
    """数字页走「直接抽」这条快路：不调 OCR，形状和 OCR 那一页完全一样。"""
    lines = _embedded_lines(page)
    width, height = float(page.rect.width), float(page.rect.height)
    raw = page.get_text("text") or ""
    # 这一路的坐标本来就是 PDF 点，所以矢量线按 1:1 取（OCR 那条要乘 dpi/72）
    return {
        "page": number, "source": "text", "text": raw.strip(), "lines": lines,
        "blocks": _layout_blocks(_items_of(lines), width, _page_rules(page, 1.0)),
        "elapsed_ms": 0,
        "text_chars": _text_layer_chars(page), "width": width, "height": height,
    }


def _display_width(text):
    """按终端/等宽排版算占几格：中日韩全角字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
               for ch in str(text or ''))


def _render_table(grid):
    """把重建出来的表格排成对齐的文本块（列宽按内容，全角按两格算）。"""
    columns = max(len(row) for row in grid)
    widths = [max(_display_width(row[index]) if index < len(row) else 0 for row in grid)
              for index in range(columns)]
    lines = []
    for row in grid:
        cells = []
        for index in range(columns):
            value = row[index] if index < len(row) else ''
            cells.append(value + ' ' * max(0, widths[index] - _display_width(value)))
        lines.append('  '.join(cells).rstrip())
    return '\n'.join(lines)


def _render_page_text(blocks):
    """一页的读法：正文一行一条，表格整块对齐铺开——和界面上预览看到的是同一份东西。"""
    parts = []
    for kind, body in blocks:
        parts.append(_render_table(body) if kind == 'table' else body)
    return '\n'.join(parts)


def _render_txt(pages):
    rows = []
    for page in pages:
        rows.append(_text('export_page_marker', n=page['page']))
        rows.append(_render_page_text(page['blocks']))
        rows.append('')
    return '\n'.join(rows)


def _open_pdf(path, action):
    if not path or not os.path.isfile(path):
        return None, {"success": False, "message": _text('api_ocr_pdf_missing') % path}
    try:
        doc = pymupdf.open(path)
    except Exception as error:
        logging.exception("OCR 打开 PDF 失败")
        return None, {"success": False, "message": _text('api_pdf_open_failed') % error}
    if doc.needs_pass:
        close_pdf(doc)
        return None, {"success": False, "message": _text('api_pdf_locked_doing') % action}
    return doc, None


def _pages(doc, page_range):
    return select_pages(page_range, doc.page_count)


def _box_rect(box):
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return min(xs), min(ys), max(xs), max(ys)
    except Exception:
        return None


def _items_of(lines):
    """把识别行收成 (x0, y0, x1, y1, text)；没有框的行对排版和表格都没用，丢掉。"""
    items = []
    for line in lines or []:
        rect = _box_rect(line.get('box'))
        text = str(line.get('text') or '').strip()
        if rect and text:
            items.append((rect[0], rect[1], rect[2], rect[3], text))
    items.sort(key=lambda item: ((item[1] + item[3]) / 2.0, item[0]))
    return items


def _y_overlap(item, band):
    """这一格与这一段（y0,y1）在竖直方向盖住多少像素。"""
    return max(0.0, min(item[3], band[3]) - max(item[1], band[1]))


def _visual_rows(items):
    """按「竖直方向互相盖住大半」把格子并成一行一行（表格和正文都用这一套）。

    不能只比 y 起点：同一行里「总成绩」这种高格和「2025 年」这种矮格起点就差好几像素，
    按起点分组会把一行拆成两行。这里按重叠高度判定，并要求横向不重叠（上下叠的字块不算同行）。
    """
    rows = []
    bands = []
    for item in items:
        target = None
        for index, row in enumerate(rows):
            band = bands[index]
            height = min(item[3] - item[1], band[3] - band[1])
            if height <= 0 or _y_overlap(item, band) < height * 0.55:
                continue
            # 横向要比的是「和这一行里每一格」的重叠，不是整行的外接框：
            # 一行铺满整页宽，拿外接框比会判「任何格子都和它重叠」，
            # 于是同一排的字全被挤到下一行（第一版就是这么把 8 列表头拆成两行的）。
            clash = any(
                max(0.0, min(item[2], cell[2]) - max(item[0], cell[0]))
                > 0.5 * min(item[2] - item[0], cell[2] - cell[0])
                for cell in row)
            if not clash:
                target = index
                break
        if target is None:
            rows.append([item])
            bands.append((item[0], item[1], item[2], item[3]))
        else:
            rows[target].append(item)
            band = bands[target]
            bands[target] = (min(band[0], item[0]), min(band[1], item[1]),
                             max(band[2], item[2]), max(band[3], item[3]))
    return [sorted(row, key=lambda cell: cell[0]) for row in rows]


def _column_cuts(row, gap):
    """一行里格子之间的空隙（x 轴投影），用来当列分界。"""
    edges = sorted((cell[0], cell[2]) for cell in row)
    cuts, right = [], edges[0][1]
    for left, this_right in edges[1:]:
        if left - right > gap:
            cuts.append((right + left) / 2.0)
        right = max(right, this_right)
    return cuts


def _row_partners(left, right):
    """两侧「真的是同一行」的比例：这是分栏和表格的分水岭。

    旧版只比「竖直方向盖住大半」，密集双栏散文也会命中——两栏各自排满行，
    随便一条左栏行在右栏几乎总能找到一条高度重叠的行，于是双栏被当成表格、不分栏，
    导出就左右交错（他那份答案解析正是这么坏的）。
    现在加一条硬杠：不仅重叠要够，**两条行的中心还得几乎在同一水平线上**
    （差不超过行高的三成）。表格的每一行左右格子是真的对齐的；散文两栏的行网格
    各自独立、错开是随机的，凑不出这么高的对齐率。
    """
    if not left or not right:
        return 1.0
    hits = 0
    for item in left:
        height = max(1.0, item[3] - item[1])
        center = (item[1] + item[3]) / 2.0
        for other in right:
            other_h = max(1.0, other[3] - other[1])
            other_c = (other[1] + other[3]) / 2.0
            overlap = min(item[3], other[3]) - max(item[1], other[1])
            if (overlap > 0.7 * min(height, other_h)
                    and abs(center - other_c) <= 0.3 * min(height, other_h)):
                hits += 1
                break
    return hits / float(len(left))


def _column_groups(items, page_width):
    """找一条「没有正文框跨过的竖直空白带」当栏缝，分成左右两栏各自读。

    不分栏的话双栏稿会按 y 一行读过去，左右两栏的字在同一行里串成一句胡话。
    判据三条：缝要够宽、两侧都要有足够多的行、两侧的行网格不能是表格那种逐行对齐。
    通栏的标题/页脚（宽度超过页宽六成）不参与缝的判定——它们横跨两栏，
    会把任何缝都「跨过」；它们归到左栏那一组，靠 y 排在最前（标题）或最后（页脚）。
    """
    width = max(1.0, float(page_width or 1))
    if len(items) < 6:
        return [items]
    body = [item for item in items if (item[2] - item[0]) < width * 0.6]
    if len(body) < 6:
        return [items]
    # 扫线统计「每个 x 上有几个框跨着」。栏缝不是「一个框都没有」——居中的标题
    # 往往正好压在中线上（它不够通栏宽、排除不掉），会把缝堵死；允许极少数框跨缝，
    # 缝两侧照样各有几十行，那才是真两栏。
    tol = max(1, len(body) // 40)
    events = []
    for item in body:
        events.append((item[0], 1))
        events.append((item[2], -1))
    events.sort()
    gutters = []
    count = 0
    band_start = None
    prev_x = None
    for x, delta in events:
        if prev_x is not None and x > prev_x:
            if count <= tol:
                if band_start is None:
                    band_start = prev_x
            else:
                if band_start is not None:
                    gutters.append((band_start, prev_x))
                    band_start = None
        count += delta
        prev_x = x
    if band_start is not None and prev_x is not None:
        gutters.append((band_start, prev_x))
    split = None
    for g0, g1 in sorted(gutters, key=lambda pair: pair[1] - pair[0], reverse=True):
        # 缝宽门槛不能太狠：这份答案解析两栏之间实测只有 13px（150dpi），
        # 1.5% 页宽会把它拦掉。防误判靠后面「两侧行数 + 行对齐率」，不靠缝宽。
        if g1 - g0 < max(4.0, width * 0.008):
            continue
        mid = (g0 + g1) / 2.0
        left = [item for item in body if (item[0] + item[2]) / 2.0 < mid]
        right = [item for item in body if (item[0] + item[2]) / 2.0 >= mid]
        if len(left) < 3 or len(right) < 3:
            continue
        if min(len(left), len(right)) < len(body) * 0.2:
            continue
        if _row_partners(left, right) >= 0.6:
            continue        # 逐行对齐 → 是表格不是分栏
        split = mid
        break
    if split is None:
        return [items]
    left_all, right_all = [], []
    for item in items:
        # 跨缝的框（居中标题这类）归左栏：它的 y 在最前/最后，靠栏内排序自然落到
        # 整页开头或结尾；按中心分会把它甩到右栏中间去。
        if (item[2] - item[0]) >= width * 0.6 or item[0] < split < item[2] \
                or (item[0] + item[2]) / 2.0 < split:
            left_all.append(item)
        else:
            right_all.append(item)
    return [left_all, right_all]


# 一条画出来的横线盖住这一格空隙的六成以上，就认定它是「填空的横线」而不是排版留白
BLANK_COVER = 0.6
BLANK_SPAN = 0.5      # 同一条线压在某一格字底下的长度超过半格（且不少于一个字符），就不是填空
BLANK_MIN_OVERLAP = 8.0   # px @150dpi：约三分之一汉字，防止识别框稍微糊出去一点就否掉真填空
# 填空线是「写在字底下的那条线」，离这一行的底边不会远；思维导图里的箭头杆、
# 方框的上边线都在行的中段甚至上沿。他那份原件量下来：真填空最深 -5.5px（行高 25～29），
# 箭头杆 -20.9px、框线上沿 -12～-21px，所以卡在行高四成（且不少于 6）这条线上分得开。
BLANK_LOW = 0.4
BLANK_LOW_FLOOR = 6.0
BLANK_CHAR = '\uff3f'      # ＿ 全角下划线：在 Word / TXT / 预览里本身就是一条线


def _blank_under(underlines, bottom, left, right, spans=(), median=0.0):
    """空隙 [left, right] 里到底压着一条画出来的横线（要落在这一行的基线附近才算）。

    还要看这条线是不是从**字底下**穿过去的：横幅标题下面那条色带、表格外框的底边，
    都是一路压着字走过来的，照单当成填空就会在「对接教材」这种标题前面凭空多出一串 ＿。
    真正的填空线两头都停在字边缘上，不会吃掉相邻那一格的大半宽度。
    """
    if not underlines or right - left <= 0:
        return False
    ceiling = bottom - max(BLANK_LOW_FLOOR, median * BLANK_LOW)
    for y, x0, x1 in underlines:
        if not (ceiling <= y <= bottom + 10.0):
            continue
        if min(x1, right) - max(x0, left) < (right - left) * BLANK_COVER:
            continue
        if any(min(x1, hi) - max(x0, lo) >= max(BLANK_MIN_OVERLAP, (hi - lo) * BLANK_SPAN)
               for lo, hi in spans):
            continue
        return True
    return False


def _render_line(row, page_width, underlines=None):
    """同一行里的几格按它们的实际空隙排回去（前导缩进 + 格间空隙），不是拼成一句。

    格与格之间如果压着一条画出来的横线，那一段就不是空白而是**填空**，用 ＿ 写出来。
    以前一律写成空格，导出的 Word 里挖空整条消失——学生拿这份讲义是要在横线上填答案的，
    线没了这份文件就不能用。
    """
    width = max(1.0, float(page_width or 1))
    heights = sorted(max(1.0, cell[3] - cell[1]) for cell in row)
    median = heights[len(heights) // 2]
    bottom = max(cell[3] for cell in row)
    unit = max(4.0, median * 0.55)
    spans = [(cell[0], cell[2]) for cell in row]
    parts = [row[0][4]]
    last_x = row[0][2]
    for cell in row[1:]:
        gap = max(0.0, cell[0] - last_x)
        if _blank_under(underlines, bottom, last_x, cell[0], spans, median):
            parts.append(BLANK_CHAR * max(2, min(24, int(round(gap / unit)))) + cell[4])
        else:
            parts.append(' ' * max(1, min(12, int(round(gap / unit)))) + cell[4])
        last_x = cell[2]
    # 行首那一截也可能是填空（「1. ＿＿＿ 留着齐腰长发」：编号和正文之间就是那条线）
    indent_px = row[0][0]
    indent = max(0, min(40, int(round(indent_px / width * 60))))
    if underlines and indent_px > unit:
        left_edge = max(0.0, indent_px - unit * max(2, min(24, indent)))
        if _blank_under(underlines, bottom, left_edge, indent_px, spans, median):
            return (BLANK_CHAR * max(2, min(24, int(round((indent_px - left_edge) / unit))))
                    + ''.join(parts))
    return ' ' * indent + ''.join(parts)



# ==================== 页面上「画出来的线」 ====================
# 两个用途：
#   ① 框线围出来的格子就是真表格。以前只按「一行里横向有几段空隙」猜表格，
#      结果他那份预习学案里带下划线空格的正文行全被数成了表（一页猜出 5 张，真表只有 1 张）。
#   ② 填空下划线（挖空）只有在线的数据里存在：识别像素看不出来「这行字底下画了一条线」，
#      所以导出的 Word 里一条下划线都没有。
# 判据看的是路径包围盒的形状，不是描边：iTextSharp / WPS 这类导出器把线画成又细又长的
# **填充**路径（fill 有值、color 是 None、width 是 None），按「有没有描边颜色」去筛会
# 一条都抓不到（这一版第一稿就是这么错的，八页量下来 0 条线）。
RULE_MAX_THICK = 1.6      # pt：比这个厚就不是线，是色块的边
RULE_MIN_H = 14.0         # pt：短于此的横线是连字符、破折号、项目符号
RULE_MIN_V = 8.0          # pt：短于此的竖线是括号、间隔点
# 围一张表至少要有：两条竖线当左右框 + 三条横线（表头一道、每行一道底线）→ 至少两行格子
GRID_MIN_COLS = 3         # 竖线根数（含左右框）
GRID_MIN_ROWS = 3         # 横线根数（含上下框）
GRID_MIN_WIDTH = 60.0     # px：窄于此的不是表格，是段落左侧的装饰竖条
GRID_MAX_ROW = 130.0      # px：两行框线之间超过这个间距就不是同一张表（是上下两个区块）
LINE_TOLERANCE = 2.5      # px：同一条线常被拆成几段画，y/x 差这么点就当一条
# 阅读顺序键里「栏带」的步长：要比任何一页的像素高度都大，左栏的最后一个 y
# 也排在右栏第一个 y 前面。150~300 dpi 下一页最高也就几千像素，1e7 足够且不会溢出排序。
COLUMN_BAND = 1e7


def _path_rect(path):
    """取一条矢量路径的外接框：(x0, y0, x1, y1)。

    `get_cdrawings()` 给的是裸的四元组，`get_drawings()` 给的是 Rect——两处都要认，
    不然换快速接口这件事会把整页排版直接炸掉（他那份原件一页两千多条路径，慢的那一个
    每页 250～570 ms，快的这个 64～196 ms，量出来的线一根不多一根不少）。
    退化只按「长宽都没有」判：Rect.is_empty 是「宽或高为零」，一条画成零高度的细线
    会被它当成空框扔掉，而那正是我们要找的横线。
    """
    rect = path.get('rect') if isinstance(path, dict) else None
    if rect is None:
        return None
    if isinstance(rect, tuple):
        if len(rect) != 4:
            return None
        x0, y0, x1, y1 = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    else:
        x0, y0, x1, y1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
    if x1 <= x0 and y1 <= y0:
        return None
    return x0, y0, x1, y1


def _page_rules(page, scale):
    """取页面上的水平线与垂直线，换算成图片像素（和识别框同一套坐标）。

    读不动就回 None：矢量线只是锦上添花的判据，不该让一份识别结果因为读不到它而失败。
    """
    try:
        if hasattr(page, 'get_cdrawings'):
            paths = page.get_cdrawings(extended=False)
        else:
            paths = page.get_drawings()
    except Exception:
        logging.exception('读取页面矢量线失败（这一页只按几何判据排版）')
        return None
    # 转了角度的页面，路径坐标还是「没转」那一套（mediabox），拍出来的图却是转好的：
    # 一条横线在 90° 页面上拍出来是竖线。不先套一次旋转矩阵，围出来的格子、认出来的
    # 填空线全是错位的东西（实测 400×200 转 90°：横线 y=150 应当变成竖线 x=50）。
    matrix = page.rotation_matrix if page.rotation else None
    horiz, vert = [], []
    for path in paths or []:
        box = _path_rect(path)
        if box is None:
            continue
        if matrix is not None:
            corners = [((x * matrix.a + y * matrix.c + matrix.e),
                        (x * matrix.b + y * matrix.d + matrix.f))
                       for x, y in ((box[0], box[1]), (box[0], box[3]),
                                    (box[2], box[1]), (box[2], box[3]))]
            box = (min(c[0] for c in corners), min(c[1] for c in corners),
                   max(c[0] for c in corners), max(c[1] for c in corners))
        x0, y0, x1, y1 = box
        if (y1 - y0) <= RULE_MAX_THICK and (x1 - x0) >= RULE_MIN_H:
            horiz.append([round((y0 + y1) / 2.0 * scale, 1), x0 * scale, x1 * scale])
        elif (x1 - x0) <= RULE_MAX_THICK and (y1 - y0) >= RULE_MIN_V:
            vert.append([round((x0 + x1) / 2.0 * scale, 1), y0 * scale, y1 * scale])
    if not horiz and not vert:
        return None
    return {'h': horiz, 'v': vert}


def _merge_lines(lines, tolerance=LINE_TOLERANCE):
    """把「同一位置被拆成几段画的线」并成一条：[(pos, a, b), …] 按 pos 排好、合并重叠。"""
    out = []
    for pos, a, b in sorted(lines):
        if out and abs(out[-1][0] - pos) <= tolerance:
            last = out[-1]
            out[-1] = [round((last[0] * 2 + pos) / 3.0, 1), min(last[1], a), max(last[2], b)]
            continue
        out.append([pos, a, b])
    return out


def _rule_grids(horiz, vert, page_width):
    """用框线围出表格区域：[{x0,y0,x1,y1,rows:[y…],cols:[x…]}]，坐标是图片像素。

    只认「两条竖线当左右框 + 若干条横线贯穿它们」的那种结构。灰底提示框、下划线、
    段落左侧的装饰竖条都围不出来——它们缺贯穿的竖线，或者横线之间不连续。
    """
    if len(horiz) < GRID_MIN_ROWS or len(vert) < GRID_MIN_COLS:
        return []
    rows_all = _merge_lines([[line[0], line[1], line[2]] for line in horiz])
    cols_all = _merge_lines([[line[0], line[1], line[2]] for line in vert])
    found = []
    for i, left in enumerate(cols_all):
        for right in cols_all[i + 1:]:
            span = right[0] - left[0]
            if span < GRID_MIN_WIDTH:
                continue
            # 竖线是按 x 排过序的：一旦这对已经宽过一页，后面那些只会更宽，直接换下一根左框。
            # 不这么收，一页几百根竖线（工程图、密集网格底纹）就会把 pair 数顶到十几万，
            # 一整页卡在这儿不动。
            if span > page_width * 0.98:
                break
            # 横跨这两条竖线的横线（左右各留一点容差：框线粗时会差一两像素）
            crossing = sorted(h[0] for h in rows_all
                              if h[1] <= left[0] + 4 and h[2] >= right[0] - 4
                              and left[1] - 4 <= h[0] <= left[2] + 4)
            if len(crossing) < GRID_MIN_ROWS:
                continue
            run = [crossing[0]]
            for y in crossing[1:] + [None]:
                if y is not None and y - run[-1] <= GRID_MAX_ROW:
                    run.append(y)
                    continue
                if len(run) >= GRID_MIN_ROWS:
                    # 竖线要「纵向盖住这一段行线」才算这张表的列框：
                    # 比的是竖线的 y 跨度与 run 的 y 区间（第一版在这里拿竖线的 x 去比
                    # 行线的 y，条件永远不成立，一页也围不出表格）
                    inside = [c[0] for c in cols_all
                              if c[1] <= run[-1] + 4 and c[2] >= run[0] - 4
                              and left[0] - 2 <= c[0] <= right[0] + 2]
                    if len(inside) >= GRID_MIN_COLS:
                        found.append({'x0': left[0], 'y0': run[0], 'x1': right[0],
                                      'y1': run[-1], 'rows': run, 'cols': sorted(inside)})
                run = [y] if y is not None else []
    # 大框套小框（外圈加每行一道线）会重复报，留外圈、丢内圈
    keep = []
    for box in sorted(found, key=lambda b: -((b['x1'] - b['x0']) * (b['y1'] - b['y0']))):
        if any(b['x0'] <= box['x0'] + 1 and b['y0'] <= box['y0'] + 1
               and b['x1'] >= box['x1'] - 1 and b['y1'] >= box['y1'] - 1 for b in keep):
            continue
        keep.append(box)
    return keep


def _in_grid(item, grid, pad=1.0):
    """一个识别框的中心落不落在这张表里。"""
    cx = (item[0] + item[2]) / 2.0
    cy = (item[1] + item[3]) / 2.0
    return grid['x0'] - pad <= cx <= grid['x1'] + pad and grid['y0'] - pad <= cy <= grid['y1'] + pad


def _grid_table(items, grid):
    """把落在表里的识别行按「行线 + 列线」分格子：这是真框线给的坐标，不用猜。"""
    rows, cols = grid['rows'], grid['cols']
    grid_cells = [['' for _ in range(max(1, len(cols) - 1))] for _ in range(max(1, len(rows) - 1))]
    rest = []
    for item in items:
        if not _in_grid(item, grid):
            rest.append(item)
            continue
        cy = (item[1] + item[3]) / 2.0
        cx = (item[0] + item[2]) / 2.0
        row_index = sum(1 for y in rows if cy > y) - 1
        col_index = sum(1 for x in cols if cx > x) - 1
        if not (0 <= row_index < len(grid_cells)) or not (0 <= col_index < len(grid_cells[0])):
            continue          # 压线的那一格（中心正好落在框线上）丢掉比塞错格子好
        cell = grid_cells[row_index][col_index]
        grid_cells[row_index][col_index] = ((cell + ' ' + item[4]).strip()
                                            if cell else item[4])
    body = [row for row in grid_cells if any(cell.strip() for cell in row)]
    # 一条标题色带（「能力迁移」那种）也会被线围成格子，但它只有一行有字——不是表。
    # 判据看的是**结构**：至少两行有字 + 至少两列格子。第二列可以整列是空的，
    # 因为「我的发现 / 错题收集 ｜ ＿＿」这种填写表右半本来就要留白给学生写
    # （第一版要求「两列都有字」，把这份原件第 4 页那张真表误杀了）。
    columns = max((len(row) for row in body), default=0)
    if len(body) < 2 or columns < 2:
        return [], items
    return body, rest


def _shared_boundaries(run, gap, merge):
    """跨行对得齐的那些列缝。

    「这一行里有空隙」不是表格的证据——带填空下划线的正文行、续行缩进的段落都有空隙。
    真正的表格是**每一行的缝都在同一个 x 上**，所以一道缝要被至少六成行同时具备才算列分界。
    """
    if len(run) < 2:
        return []
    votes = [_column_cuts(row, gap) for row in run]
    boundaries = []
    for cut in sorted({float(int(round(value))) for group in votes for value in group}):
        hits = sum(1 for group in votes if any(abs(value - cut) <= merge for value in group))
        if hits < max(2, int(len(run) * 0.6)):
            continue
        if boundaries and cut - boundaries[-1] <= merge:
            continue
        boundaries.append(cut)
    return boundaries


def _geometric_blocks(items, page_width, allow_tables=True, underlines=None):
    """没有框线可依据时的那套猜法：先分栏、再并视觉行，列对得齐的连续几行才算表。

    以前只要求「连续 ≥2 行、每行 ≥2 格」，太松：一份带填空下划线的正文
    （「1. ugly (adj) — ＿＿ (n 丑陋)」）每行都被切成两三格，一页猜出 5 张表，
    而那份原件里真正的表格只有 1 张。现在加三条硬杠：
      ① 列缝要跨行对得齐（`_shared_boundaries`）；
      ② 多数行的格子数要一致（表格每行都是同样的几列，散文续行不是）；
      ③ 格子不能大片是空的（真表格基本每格都有字，大片空格就是猜错了）。
    每一项带一个排序键，供上层和框线表格一起按阅读顺序排。
    **排序键里带「栏带」序号**：分栏之后左栏整栏读完才读右栏。旧版只带 y，
    上层 `_page_blocks` 再按 y 全局一排序，就把刚分好的两栏重新交错回一行行——
    他那份双栏答案解析导出左右串句，根子就在这一步。
    """
    blocks = []

    def emit(band, y, kind, body):
        blocks.append((band * COLUMN_BAND + y, kind, body))

    for band, group in enumerate(_column_groups(items, page_width)):
        rows = _visual_rows(group)
        if not rows:
            continue
        gap = max(6.0, page_width * 0.012)
        merge = max(gap, page_width * 0.02)
        index = 0
        while index < len(rows):
            row = rows[index]
            # 单格那一行（标题、备注、续行）先按文字走：把它当表头会凭空多出一行空格子
            if not allow_tables or len(row) < 2:
                emit(band, row[0][1], 'text', _render_line(row, page_width, underlines))
                index += 1
                continue
            run = [row]
            cursor = index + 1
            while cursor < len(rows) and len(rows[cursor]) >= 2:
                run.append(rows[cursor])
                cursor += 1
            boundaries = _shared_boundaries(run, gap, merge) if len(run) >= 2 else []
            counts = [len(one) for one in run]
            mode = max(set(counts), key=counts.count)
            regular = mode >= 2 and counts.count(mode) >= max(2, int(len(run) * 0.6))
            grid = []
            if boundaries and regular:
                columns = len(boundaries) + 1
                for one in run:
                    cells = [''] * columns
                    for cell in sorted(one, key=lambda item: item[0]):
                        center = (cell[0] + cell[2]) / 2.0
                        slot = max(0, min(columns - 1,
                                          sum(1 for edge in boundaries if center > edge)))
                        cells[slot] = ((cells[slot] + ' ' + cell[4]).strip()
                                       if cells[slot] else cell[4])
                    grid.append(cells)
                filled = sum(1 for cells in grid for cell in cells if cell.strip())
                if filled < len(grid) * columns * 0.6:
                    grid = []     # 大片空格子的「表」看着就是排版坏了，宁可当散文
            if not grid:
                for one in run:
                    emit(band, one[0][1], 'text', _render_line(one, page_width, underlines))
                index = cursor
                continue
            emit(band, run[0][0][1], 'table', grid)
            index = cursor
    return blocks


def _blank_lines(rules, grids):
    """把「不是表格框线」的水平线挑出来：那些就是填空与下划线。"""
    horiz = (rules or {}).get('h') or []
    if not horiz:
        return []
    out = []
    for y, x0, x1 in horiz:
        if any(g['y0'] - 3 <= y <= g['y1'] + 3 and g['x0'] - 3 <= x0 and x1 <= g['x1'] + 3
               for g in grids or []):
            continue
        out.append([y, x0, x1])
    return out


def _page_blocks(items, page_width, grids=None, has_rules=False,
                  underlines=None):
    """把一格一格的识别结果排成页面块：[('text', 一行文字) / ('table', 二维表格)]。

    有框线就先按框线分格（`grids` 由 `_rule_grids` 从页面的矢量线里围出来），
    表格外面的内容才走几何猜法——而且**这一页已经有框线表格时不再猜表**，
    否则剩下的正文行会被再猜出一堆假表格（他反馈的「不是表格的全成了表格」就是这条）。
    框线一条都没有（照片、纯位图扫描件）时，退回收紧后的几何判据。
    用户在预览里看到的版面就是这份东西，导出 TXT/Word/Excel 也照这份走。
    """
    width = max(1.0, float(page_width or 1))
    blocks = []
    rest = list(items or [])
    for grid in sorted(grids or [], key=lambda box: box['y0']):
        inside = [item for item in rest if _in_grid(item, grid)]
        if not inside:
            continue
        rest = [item for item in rest if not _in_grid(item, grid)]
        body, _leftover = _grid_table(inside, grid)
        if body:
            blocks.append((grid['y0'], 'table', body))
    # 这一页有矢量线可查、却一张格子都没围出来 → 它本来就没有表格（第 2 页那种
    # 「两列排的填空清单」以前被猜成了表）。只有完全读不到线（照片、纯位图扫描件）时，
    # 才轮到几何猜法上场。
    if rest:
        blocks.extend(_geometric_blocks(rest, width,
                                        allow_tables=not grids and not has_rules,
                                        underlines=underlines))
    blocks.sort(key=lambda row: row[0])
    return [(kind, body) for _y, kind, body in blocks]



def _tables_from_items(items, page_width):
    """只要表格那部分（给探针和「有没有表格」的判断用）。"""
    return [block for kind, block in _page_blocks(items, page_width) if kind == 'table']


class OcrMixin:
    def ocr_status(self, with_engine=False):
        """OCR 是否可用、模型与依赖版本；with_engine=True 会真的建一次引擎。"""
        return ocr.status(bool(with_engine))

    def ocr_warmup(self, quality="fast", lang=""):
        return ocr.warmup(str(quality or "fast"), lang or None)

    def ocr_image(self, image_path, quality="fast", lang=""):
        return ocr.ocr_image(str(image_path or ""), str(quality or "fast"), lang or None)

    def ocr_pdf_page(self, pdf_path, page_index=0, dpi=150, quality="fast", lang=""):
        return ocr.ocr_pdf_page(
            str(pdf_path or ""), page_index=page_index, dpi=dpi,
            quality=str(quality or "fast"), lang=lang or None)

    def ocr_pdf_text(self, pdf_path, page_range="", quality="fast", preprocess=False,
                     layout=False, lang=""):
        """识别 PDF 或图片，返回逐页文本、行框与页面块；不写文件。"""
        pages, error, cancelled = self._ocr_source_pages(pdf_path, page_range, quality,
                                                         lang, preprocess)
        if error:
            return error
        chars = sum(len((page["text"] or "").replace("\n", "")) for page in pages)
        for page in pages:
            # layout 这一档现在只决定「预览里给不给版面」：blocks 是顺手算出来的，
            # 关掉排版时退回逐行原文，行为和以前一致。
            page["layout_text"] = (_render_page_text(page["blocks"]) if layout
                                   else page["text"])
        return {
            "success": True, "pages": pages, "page_count": len(pages),
            "cancelled": cancelled,
            "chars": chars,
            "elapsed_ms": sum(page["elapsed_ms"] for page in pages),
            "quality": quality, "lang": ocr.normalize_lang(lang),
            "preprocessed": bool(preprocess),
            "tables": sum(1 for page in pages
                          for kind, _body in page["blocks"] if kind == "table"),
            "text_layer_pages": sum(1 for page in pages if page.get("source") == "text"),
            "cached_pages": sum(1 for page in pages if page.get("cached")),
            "text": "\n\n".join("%s\n%s" % (_text('export_page_marker', n=page["page"]),
                                            page["text"])
                                for page in pages),
        }

    def _ocr_source_pages(self, path, page_range, quality, lang, preprocess):
        """PDF 按页、图片按一页，产出同一种逐页结构（blocks 已把正文和表格分开）。

        第三个返回值 cancelled 表示「用户中途点了取消」：这时候前面几页的结果照旧返回，
        调用方据此说明停在哪一页，而不是把已经识别出来的东西一起丢掉。
        """
        if not path or not os.path.isfile(path):
            return [], {'success': False, 'message': _text('api_file_not_found') % path}, False
        if _is_image(path):
            # 图片入口也走同一份缓存：「先识别看看 → 再导 Word → 再导 Excel」这三步
            # 挑的是同一张照片，不缓存就是把同一张图重认三遍。
            key = _cache_key(path, 1, quality, lang, preprocess)
            hit = _cache_read(key)
            if hit is not None:
                return [hit], None, False
            item = _ocr_image_source(path, quality, lang, preprocess)
            if not item.get("success"):
                return [], item, False
            payload = _page_payload(1, item)
            _cache_write(key, payload)
            return [payload], None, False
        doc, error = _open_pdf(path, _text('api_ocr_act_recognize'))
        if error:
            return [], error, False
        try:
            pages, range_error = _pages(doc, page_range)
            if range_error:
                return [], {"success": False, "message": range_error}, False
            jobctl.start('ocr', len(pages))
            payloads = []
            cancelled = False
            for number in pages:
                if jobctl.should_stop():
                    cancelled = True
                    break
                jobctl.set_progress(len(payloads))
                page = doc.load_page(number - 1)
                # 数字页（本来就带文字层）直接抽，不拍成图再认一遍：更准，也快一个量级。
                # 旋转页留在 OCR 这条路上——可搜索 PDF 那一边也是「旋转页不写字层」，
                # 两处对同一页的判定得一致，否则一个入口当数字页、另一个当扫描页。
                if not page.rotation and _text_layer_chars(page) >= TEXT_LAYER_MIN:
                    payloads.append(_embedded_payload(number, page))
                    jobctl.advance()
                    continue
                key = _cache_key(path, number, quality, lang, preprocess, SCAN_DPI)
                hit = _cache_read(key)
                if hit is not None:
                    payloads.append(hit)
                    jobctl.advance()
                    continue
                payloads.append(_page_payload(number,
                                              _ocr_page(doc, number - 1, quality, preprocess,
                                                        dpi=SCAN_DPI, lang=lang)))
                _cache_write(key, payloads[-1])
                jobctl.advance()
            return payloads, None, cancelled
        except Exception as error:
            logging.exception("OCR 识别失败")
            return [], {"success": False, "message": "%s: %s" % (type(error).__name__, error)}, False
        finally:
            jobctl.finish()
            try:
                close_pdf(doc)
            except Exception:
                pass

    def _export(self, output_path, pdf_path, page_range, quality, preprocess, lang, writer, label):
        """三个导出共用的那条路：先识别，再写临时文件，落盘成功才算成。"""
        if not output_path:
            return {"success": False, 'message': _text('api_need_save_path')}
        result = self.ocr_pdf_text(pdf_path, page_range, quality, preprocess, False, lang)
        if not result.get("success"):
            return result
        if result.get("cancelled") and not result.get("pages"):
            # 一页都没识别完就被取消：写一个空壳 txt/docx 出去比不写更容易让人误用
            return {"success": False, "cancelled": True, "pages": 0, "chars": 0}
        try:
            with atomic_output(output_path) as handle:
                writer(handle, result)
            return {
                "success": True, "file": output_path, "pages": result["page_count"],
                "chars": result["chars"], "tables": result["tables"],
                "cancelled": bool(result.get("cancelled")),
                "elapsed_ms": result["elapsed_ms"],
            }
        except Exception as error:
            logging.exception("OCR 写 %s 失败" % label)
            return {"success": False, "message": "%s: %s" % (type(error).__name__, error)}

    def ocr_export_text(self, pdf_path, page_range="", quality="fast",
                        preprocess=False, output_path="", layout=False, lang=""):
        """识别并写入 TXT：正文按版面、表格按制表符对齐，逐页分隔。"""
        return self._export(
            output_path, pdf_path, page_range, quality, preprocess, lang,
            lambda handle, result: handle.write(_render_txt(result["pages"]).encode("utf-8")),
            "文本")

    def ocr_export_docx(self, pdf_path, output_path="", page_range="", quality="fast",
                        preprocess=False, lang=""):
        """识别并写成 Word：正文一段一段，表格用真正的 Word 表格（带框线）。"""
        def write(document, result):
            from docx import Document
            from docx.shared import Pt

            book = Document()
            for index, page in enumerate(result["pages"]):
                if index:
                    book.add_page_break()
                book.add_heading(_text('export_page_heading', n=page["page"]), level=2)
                for kind, body in page["blocks"]:
                    if kind != "table":
                        book.add_paragraph(body)
                        continue
                    columns = max(len(row) for row in body)
                    table = book.add_table(rows=len(body), cols=columns)
                    table.style = "Table Grid"
                    for row_index, row in enumerate(body):
                        for col_index in range(columns):
                            cell = table.cell(row_index, col_index)
                            cell.text = row[col_index] if col_index < len(row) else ""
                            for paragraph in cell.paragraphs:
                                for run in paragraph.runs:
                                    run.font.size = Pt(9)
            book.save(document)

        return self._export(output_path, pdf_path, page_range, quality, preprocess, lang,
                            write, "Word")

    def ocr_export_xlsx(self, pdf_path, output_path="", page_range="", quality="fast",
                        preprocess=False, lang=""):
        """识别并写成 Excel：每页一张工作表，表格按格子铺开并加框线。"""
        def write(document, result):
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Border, Font, Side
            from openpyxl.utils import get_column_letter

            edge = Side(style="thin", color="FF999999")
            box = Border(left=edge, right=edge, top=edge, bottom=edge)
            book = Workbook()
            book.remove(book.active)
            for page in result["pages"]:
                sheet = book.create_sheet(_text('ocr_page_sheet').format(page["page"]))
                row = 1
                for kind, body in page["blocks"]:
                    if kind != "table":
                        sheet.cell(row, 1, body).alignment = Alignment(vertical="center")
                        row += 1
                        continue
                    columns = max(len(r) for r in body)
                    for values in body:
                        for column in range(columns):
                            cell = sheet.cell(row, column + 1,
                                              values[column] if column < len(values) else "")
                            cell.border = box
                            cell.font = Font(size=9)
                        row += 1
                    row += 1
                for column in range(1, 30):
                    sheet.column_dimensions[get_column_letter(column)].width = 14
            book.save(document)

        return self._export(output_path, pdf_path, page_range, quality, preprocess, lang,
                            write, "Excel")

    def ocr_searchable_pdf(self, pdf_path, output_path="", page_range="", quality="fast",
                           lang=""):
        """给扫描页加不可见文字层；已有文字层的页原样保留。"""
        if not output_path:
            return {"success": False, 'message': _text('api_need_save_path')}
        doc, error = _open_pdf(pdf_path, _text('api_ocr_act_searchable'))
        if error:
            return error
        inserted_pages = skipped_text = skipped_rotated = chars = 0
        cancelled = False
        try:
            pages, range_error = _pages(doc, page_range)
            if range_error:
                return {"success": False, "message": range_error}
            started = time.perf_counter()
            jobctl.start('ocr_searchable', len(pages))
            try:
                for number in pages:
                    if jobctl.should_stop():
                        cancelled = True
                        break
                    jobctl.set_progress(inserted_pages + skipped_text + skipped_rotated)
                    page = doc.load_page(number - 1)
                    if page.rotation:
                        skipped_rotated += 1
                        continue
                    if len(page.get_text("text").strip()) >= 20:
                        skipped_text += 1
                        continue
                    pixmap = page.get_pixmap(dpi=200, alpha=False)
                    image = ocr._decode_png(pixmap.tobytes("png"))
                    result = ocr._run_image(image, quality, {}, lang or None)
                    scale = 72.0 / 200.0
                    wrote = 0
                    wide_font = ''          # 每页各嵌一份宽字符 TTF，页与页之间不共用句柄
                    for line in result.get("lines") or []:
                        rect = _box_rect(line.get("box"))
                        text = str(line.get("text") or "").strip()
                        if not rect or not text:
                            continue
                        x0, y0, x1, y1 = rect
                        size = max(4.0, min(36.0, (y1 - y0) * scale * 0.82))
                        try:
                            fontname, wide_font = _layer_font(page, text, wide_font)
                            page.insert_text((x0 * scale, y1 * scale - 0.5), text,
                                             fontname=fontname, fontsize=size,
                                             render_mode=3, color=(0, 0, 0), overlay=True)
                            wrote += len(text)
                        except Exception:
                            logging.exception("OCR 文字层写入失败: 第 %d 页", number)
                    if wrote:
                        inserted_pages += 1
                        chars += wrote
            finally:
                jobctl.finish()
            if cancelled and not inserted_pages:
                # 一个字层都没写成就落盘，交出去的只是源文件的副本——不如不写
                return {"success": False, "cancelled": True, "pages": 0, "inserted_pages": 0,
                        "chars": 0}
            save_pdf_doc(doc, output_path, source=pdf_path, garbage=3, deflate=True)
            return {
                "success": True, "file": output_path,
                "pages": inserted_pages + skipped_text + skipped_rotated,
                "cancelled": cancelled,
                "inserted_pages": inserted_pages, "skipped_text_pages": skipped_text,
                "skipped_rotated_pages": skipped_rotated, "chars": chars,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as error:
            logging.exception("生成可搜索 PDF 失败")
            return {"success": False, "message": "%s: %s" % (type(error).__name__, error)}
        finally:
            try:
                close_pdf(doc)
            except Exception:
                pass

    def ocr_sensitive_info(self, pdf_path, page_range="", quality="fast", lang=""):
        """对扫描页做 OCR 后找手机号/身份证/银行卡/邮箱；只报告，不改文件。"""
        result = self.ocr_pdf_text(pdf_path, page_range, quality, False, False, lang)
        if not result.get("success"):
            return result
        hits = []
        for page in result["pages"]:
            for line in page["lines"]:
                text = str(line.get("text") or "")
                for match in _EMAIL.finditer(text):
                    hits.append({"page": page["page"], "kind": "email",
                                 "text": match.group(0), "box": line.get("box"),
                                 "score": line.get("score")})
                for match in _NUMBER_SPAN.finditer(text):
                    kind, confidence = _classify(match.group(0))
                    if kind:
                        hits.append({"page": page["page"], "kind": kind,
                                     "text": match.group(0).strip(), "box": line.get("box"),
                                     "score": line.get("score"),
                                     "confidence": confidence})
        return {"success": True, "hits": hits, "count": len(hits),
                "pages": result["page_count"], "elapsed_ms": result["elapsed_ms"]}

    def ocr_scan_report(self, pdf_path, page_range="", dpi=100):
        """OpenCV 扫描质检：空白、模糊、过暗、对比度、倾斜角。"""
        import cv2
        import numpy as np

        doc, error = _open_pdf(pdf_path, _text('api_ocr_act_check'))
        if error:
            return error
        try:
            pages, range_error = _pages(doc, page_range)
            if range_error:
                return {"success": False, "message": range_error}
            dpi = max(72, min(200, int(dpi or 100)))
            report = []
            cancelled = False
            jobctl.start('ocr_scan_report', len(pages))
            try:
                for number in pages:
                    if jobctl.should_stop():
                        cancelled = True
                        break
                    jobctl.set_progress(len(report))
                    _page, pixmap, image = _render_page(doc, number - 1, dpi)
                    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    mean = float(gray.mean())
                    std = float(gray.std())
                    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                    ink = float((gray < 200).mean())
                    edges = cv2.Canny(gray, 50, 150)
                    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                                            minLineLength=gray.shape[1] // 4, maxLineGap=20)
                    angles = []
                    if lines is not None:
                        for x1, y1, x2, y2 in lines[:, 0]:
                            angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                            if abs(angle) <= 20:
                                angles.append(angle)
                    skew = float(np.median(angles)) if angles else 0.0
                    flags = []
                    if ink < 0.002:
                        flags.append("blank")
                    if lap < 60:
                        flags.append("blur")
                    if mean < 70:
                        flags.append("dark")
                    if std < 25:
                        flags.append("low_contrast")
                    if abs(skew) > 1.5:
                        flags.append("skew")
                    report.append({
                        "page": number, "width": pixmap.width, "height": pixmap.height,
                        "dpi": dpi, "ink_ratio": round(ink, 4), "mean": round(mean, 1),
                        "std": round(std, 1), "laplacian": round(lap, 1),
                        "skew": round(skew, 2), "flags": flags,
                    })
                    jobctl.advance()
            finally:
                jobctl.finish()
            return {"success": True, "pages": report, "page_count": len(report),
                    "cancelled": cancelled,
                    "flagged": sum(1 for item in report if item["flags"])}
        except Exception as error:
            logging.exception("扫描质检失败")
            return {"success": False, "message": "%s: %s" % (type(error).__name__, error)}
        finally:
            try:
                close_pdf(doc)
            except Exception:
                pass

    def ocr_auto_bookmarks(self, pdf_path, output_path="", page_range="", quality="fast",
                           lang=""):
        """识别大字号/章节式标题，生成书签；给了输出路径才落盘。"""
        result = self.ocr_pdf_text(pdf_path, page_range, quality, False, False, lang)
        if not result.get("success"):
            return result
        candidates = []
        for page in result["pages"]:
            heights = [_box_rect(line.get("box")) for line in page["lines"]]
            heights = [(b[3] - b[1]) for b in heights if b]
            median = sorted(heights)[len(heights) // 2] if heights else 0
            for line in page["lines"]:
                text = str(line.get("text") or "").strip()
                rect = _box_rect(line.get("box"))
                if not text or not rect or len(text) > 40:
                    continue
                height = rect[3] - rect[1]
                score = line.get("score") or 0
                if len(text) < 2 or score < 0.6:
                    continue
                if _HEADING.match(text) or (median and height >= median * 1.25):
                    level = 1 if median and height >= median * 1.55 else 2
                    candidates.append({"level": level, "title": text, "page": page["page"]})
        deduped, seen = [], set()
        for item in candidates:
            key = (item["page"], item["title"])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        if output_path and deduped:
            doc, error = _open_pdf(pdf_path, _text('api_ocr_act_bookmarks'))
            if error:
                return error
            try:
                doc.set_toc([[item["level"], item["title"], item["page"]]
                             for item in deduped])
                save_pdf_doc(doc, output_path, source=pdf_path, garbage=3, deflate=True)
            except Exception as error:
                logging.exception("OCR 自动书签写盘失败")
                return {"success": False, "message": "%s: %s" % (type(error).__name__, error)}
            finally:
                try:
                    close_pdf(doc)
                except Exception:
                    pass
        return {"success": True, "bookmarks": deduped, "count": len(deduped),
                "cancelled": bool(result.get("cancelled")),
                "file": output_path, "pages": result["page_count"]}
