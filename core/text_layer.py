r"""查看页的「可选文字层」：把一页拆成一个个能框住、能选中的文字单元。

为什么要单独一个模块，而不是直接拿 core/page_model.py 那份页面模型：页面模型是给
「编辑」用的，一页里还带着图片 xref、指纹、矢量路径、颜色、稳定 id——那是几 MB 的
JSON。查看页要把它铺满整个可见窗口（一次 24 页），桥层会被压死。这里只留选取文字
真正需要的六件事：框、字、字号、行方向、行号、切分方式。

四条口径（改代码前先读）：

1. **对外一律给渲染坐标**（左上原点、y 向下、/Rotate 已转正），和查看页那张图同一套，
   前端拿 `页宽 px / 页宽 pt` 一个系数就能把框铺上去，缩放时不必回后端重取。
   PyMuPDF 的 get_text 给的是**未转**那一套，每个框都要过 `page.rotation_matrix`
   （和 page_model 同一个坑、同一个解法，见那里的模块注释与 tests/redact_check.py 的实测）。

2. **中日韩整串只交一个框，前端按字数等分**。全角字等宽，等分就是准的——这句话不是
   想当然：验收里拿 rawdict 的逐字真框和等分结果逐字比，本机实测最大偏差 0.004 pt。
   这样做只为了一件事：一页 597 字的中文册子，逐字交就是 597 个框（约 40 KB），
   按串交是 13 个框（3 KB），差的是十几倍桥载荷。
   **等分的起点角由 `r` 决定**，不是永远从框左上角往右排：竖排向上那一列第一个字在框底
   （r=270 从左下角往上、r=180 从右下角往左）。这条写死在契约里，前端与验收按同一张表算。

3. **异常输入不许往上抛**。空页、纯扫描件（没有文字层）、加密页、坏文件、越界页码
   一律返回字典；取不出真文字（字体没 ToUnicode，读回来全是 `?`）时如实标
   `encoding_suspect`，前端据此**不铺层**——选出一串问号比选不中更糟。

4. **同一份文件同一页解析十遍结果一模一样**。单元顺序就是阅读顺序（块 → 行 → 行内字符），
   前端照这个顺序进 DOM，浏览器跨 span 选出来的文本才是通顺的一句话。
"""

import logging
import math
import unicodedata

# 换算与圆整直接复用页面模型那套审过的实现，不在这里另写一份旋转矩阵乘法。
from core.page_model import _apply_matrix, _box, _num, _vector_matrix

LAYER_VERSION = 1
"""文字层的结构版本号：字段含义变了就加一，前端与缓存按它判断手里那份还作不作数。"""

UNIT = 'pt'
COORD_SPACE = 'display'
"""坐标系名字，和 page_model 同一个：页面渲染坐标（左上原点、y 向下、rotation 已生效）。"""

# 一页给多少个单元、多少字是有人管的：极端册子一页几万个字，全塞进 JSON 就是几 MB，
# 桥层要超时、界面要卡死。超出上限就截断并如实报 truncated。
MAX_UNITS_PER_PAGE = 2000
MAX_CHARS_PER_PAGE = 30000

# 读回来是这些字符，说明字体没有 ToUnicode，字其实没取出来（本机 book_1000.pdf 就是这样）。
_PLACEHOLDERS = ('?', '￼', '\ufffd')

_SPLIT_CJK = 'c'
"""全角串：前端按字数等分成逐字框。"""
_SPLIT_WORD = 'w'
"""拉丁词：前端按框宽做横向缩放，让字形正好铺满这个框。"""


def is_wide(ch):
    """这个字符是不是「全角」——按 Unicode 的 East_Asian_Width 判，不按具体语种硬列区间。

    'W'（宽）与 'F'（全角）都算：中日韩汉字、假名、谚文、全角标点都落在里面；
    'A'（含糊，如 ± ·）按窄处理，它跟着拉丁走词那条路更稳。
    """
    return unicodedata.east_asian_width(ch) in ('W', 'F')


def _display_rotation(direction, matrix):
    """行方向 → 前端要转的角度（屏幕坐标、顺时针、0~360 度）。

    必须先把方向换成渲染坐标再算角度：/Rotate 90 那一页，字在未转坐标里是横排
    (1, 0)，摆到屏幕上其实是竖排 (0, 1)。拿未转的方向算，前端就把整页竖着排的字
    横着铺了（本机实测：那份旋转页算出来是 0°，正是这个错）。
    也不能照抄 page_model 的 DIR_TO_ROTATE——那张表是 insert_text 的 rotate 参数，
    走的是 PDF「y 向上」的口径，和 CSS 的顺时针角度差一个方向。
    """
    dx, dy = _vector_matrix(direction, matrix)
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    angle = math.degrees(math.atan2(dy, dx)) % 360.0
    # 轴对齐那四档收成整数，免得 89.99999 传到前端去
    for snap in (0.0, 90.0, 180.0, 270.0, 360.0):
        if abs(angle - snap) < 0.01:
            return snap % 360.0
    return round(angle, 1)


def _units_of_line(raw_line, matrix):
    """一行 → 若干可选单元（未转坐标先乘 matrix 换成渲染坐标）。

    空格不进单元，但要留痕：它后面第一个单元带 `g:1`，前端据此在两个框之间补一个空格。
    不补的话跨框选出来的是 "helloworld"——选得中却复制不通顺，等于只做了一半。
    """
    direction = raw_line.get('dir') or (1.0, 0.0)
    rot = _display_rotation(direction, matrix)
    units = []
    text_run = ''
    box_run = None
    text_size = None
    wide_run = ''
    wide_box = None
    wide_size = None
    pending_gap = False        # 上一个分隔符是空格，还没落到某个单元头上

    def emit(text, box, size, split):
        nonlocal pending_gap
        # 行首那个不补空格：前面没有别的框，补了就是句首多一个空格
        units.append({'t': text, 'b': box, 's': size, 'p': split,
                      'g': 1 if (pending_gap and units) else 0})
        pending_gap = False

    def close_text():
        nonlocal text_run, box_run, text_size
        if text_run and box_run is not None:
            emit(text_run, box_run, text_size, _SPLIT_WORD)
        text_run, box_run, text_size = '', None, None

    def close_wide():
        nonlocal wide_run, wide_box, wide_size
        if wide_run and wide_box is not None:
            emit(wide_run, wide_box, wide_size, _SPLIT_CJK)
        wide_run, wide_box, wide_size = '', None, None

    for span in raw_line.get('spans') or []:
        size = _num(span.get('size'), 2)
        for char in span.get('chars') or []:
            ch = char.get('c') or ''
            if not ch:
                continue
            bbox = _box(char.get('bbox'))
            if ch.isspace():
                # 空格是断词符，本身不进单元：跨框选出来的那句才不会被塞进多余空格
                pending_gap = True
                close_text()
                close_wide()
                continue
            if is_wide(ch):
                close_text()
                # 等分的前提是「全角 + 同一字号」；字号一变就断成两串，各按各的等分
                if wide_box is not None and wide_size != size:
                    close_wide()
                if wide_box is None:
                    wide_run, wide_box, wide_size = ch, list(bbox), size
                else:
                    wide_run += ch
                    wide_box = [min(wide_box[0], bbox[0]), min(wide_box[1], bbox[1]),
                                max(wide_box[2], bbox[2]), max(wide_box[3], bbox[3])]
                continue
            close_wide()
            if box_run is None:
                text_run, box_run, text_size = ch, list(bbox), size
            else:
                text_run += ch
                box_run = [min(box_run[0], bbox[0]), min(box_run[1], bbox[1]),
                           max(box_run[2], bbox[2]), max(box_run[3], bbox[3])]
    close_text()
    close_wide()

    out = []
    for unit in units:
        display = _apply_matrix(unit['b'], matrix)
        out.append({'t': unit['t'], 'x': display[0], 'y': display[1],
                    'w': _num(display[2] - display[0]), 'h': _num(display[3] - display[1]),
                    's': unit['s'], 'p': unit['p'], 'r': rot, 'g': unit['g']})
    return out


def _suspect_encoding(text):
    """取回来的字里有多少是占位符：过半就认定这一页其实没取到真文字。"""
    if not text:
        return False
    bad = sum(1 for c in text if c in _PLACEHOLDERS)
    return bad * 2 > len(text)


def build_layer(page, page_number=None):
    """一页 → 文字层字典（坐标已按 /Rotate 转正）。不抛异常，坏页给空 items。

    返回 {'page_index', 'page_number', 'width', 'height', 'rotation',
          'has_text', 'truncated', 'encoding_suspect', 'items': [...]}
    items 每项：t 文字 / x,y 框左上角(pt，可能为负——字压在页边外面) / w,h 框宽高 /
    s 字号 / p 切分方式('c' 全角按字等分、'w' 拉丁词按框宽缩放) / r 顺时针角度 /
    l 行序（前端在行号变化处补一个换行，跨行选出来才是通顺的一句）。
    """
    index = page.number
    number = int(page_number if page_number is not None else index + 1)
    rotation = int(getattr(page, 'rotation', 0) or 0) % 360
    matrix = page.rotation_matrix if rotation else None
    rect = page.rect
    result = {
        'page_index': index,
        'page_number': number,
        'width': _num(rect.width),
        'height': _num(rect.height),
        'rotation': rotation,
        'has_text': False,
        'truncated': False,
        'encoding_suspect': False,
        'items': [],
    }
    try:
        data = page.get_text('rawdict') or {}
    except Exception:
        logging.exception('text_layer: 取文字层失败（这一页按没有字处理）: page=%s', index)
        return result

    items = []
    chars = 0
    line_no = 0
    stopped = False
    for block in data.get('blocks') or []:
        if stopped:
            break
        if block.get('type') != 0:
            continue          # type 1 是图片：图片上没有可选的字
        for raw_line in block.get('lines') or []:
            for unit in _units_of_line(raw_line, matrix):
                chars += len(unit['t'])
                unit['l'] = line_no
                items.append(unit)
                if len(items) >= MAX_UNITS_PER_PAGE or chars >= MAX_CHARS_PER_PAGE:
                    result['truncated'] = True
                    stopped = True
                    break
            line_no += 1
            if stopped:
                break

    result['items'] = items
    result['has_text'] = bool(items)
    if items:
        result['encoding_suspect'] = _suspect_encoding(''.join(u['t'] for u in items))
    return result


def layer_of_document(doc, page_index):
    """从已打开的文档里取某一页的文字层；越界返回 None（调用方决定怎么报）。"""
    if page_index < 0 or page_index >= doc.page_count:
        return None
    return build_layer(doc[page_index], page_number=page_index + 1)
