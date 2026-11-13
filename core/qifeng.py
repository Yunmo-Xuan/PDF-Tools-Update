r"""骑缝印（跨页印章）的切片与排布：纯函数层。

业务一句话：把一枚印章图**竖着切成 n 条**，第 i 条贴在第 i 页的**视觉右边缘**，
把书页翻开来并排贴着缝看，就是一枚完整的章（合同、发票、装订卷宗的常见做法）。

这一层只有算术，没有磁盘、没有 PDF、没有 Pillow 之外的依赖：
`core/qifeng.py` 里所有函数都吃普通值 / PIL 图片对象，吐普通 dict，
所以 `tests/qifeng_check.py` 可以拿它单独把数学钉死，桥层只负责「收参数、写 PDF、读回来核」。

## 三套坐标（口径先定死，别学参考实现把两套混着一个输入框用）

本层的 rect 一律是 4 元组 `(x0, y0, x1, y1)`，且 `x0 < x1`、`y0 < y1`：

1. **display（显示空间）**：左上原点、y 向下、单位 pt。**就是用户眼睛看到的那一页**，
   旋转 90/270 的页在这里宽高已经互换（视觉宽 = 存储高）。界面画预览框、
   读回来核对「贴在视觉哪条边」，用的都是这一套。
2. **page_top（PyMuPDF 页面坐标）**：左上原点、y 向下、单位 pt，但**未旋转**、
   且以 CropBox 的左上角为 (0,0)（PyMuPDF 已经把 CropBox 的偏移归一化掉了，实测确认）。
   `Page.insert_image()` 收的就是这一套。
3. **pdf_origin（PDF 原生坐标）**：**左下原点**、y 向上、单位 pt。谁要绕过 PyMuPDF
   直接往内容流里写 `cm`/`Do`，或者要「距页底多少 mm」这种给用户的读数，用这一套。

1↔2 的换算就是参考实现踩坑的那一步（它自己在文档里写「PDF 原点在左下、WinForms 在左上 →
位置比例要 1-y」）：纵向先按**左上**算出 `y_top`，再 `y_pdf = 页高 - y_top - 章高`。
本层把两件事分开算，`place_rect()` 同时把三套都算好返回，桥层和界面各取所需。

## 从参考实现学来的、以及刻意改掉的地方

* 学：竖切成条、每组限片数（60 页全切成 60 片时每片细到出锯齿/重叠失真，按 20 页一组、
  每组独立重切一枚，页边拼出的图案在组界处重新开始）。
* 学：旋转 90/270 必须交换可绘制宽高，否则扫描件位置全错。
* 改：它那套「每组片数」公式 `G = pages/base + 1` 在 `pages` 正好是 `base` 整数倍时**多开一组**
  （60 页 / 每组 20 → 它算出 4 组各 15 片，而不是 3 组各 20 片）。本层按「连续分块、
  每块至多 group_size 页、最后一块拿余数」分，60/20 → 正好 3 组各 20 片，可预期、可口述。
* 改：它把 `qfzbl` 没赋值时 `TryParse` 留 0 的默认值直接当「贴最底」用。本层默认值是
  **50（垂直居中）**，非数字输入一律退回默认并出声，不会掉到页边。
* 改：它的「章宽」输入框一个值两种语义（mm 与 %）交叉共用。本层拆成
  `width_pt` 与 `width_percent` 两个参数，**同时给就报错**，不猜。
* 改：它的 `subImages` 整除余数直接丢掉（472px 切 20 片 → 只用了 460px，右侧少 12px，
  且各片等宽却贴等宽位置，缝上必然错位）。本层余数**吃进最后一条**，n 条拼回去宽度 == 原图。
"""

MM_TO_PT = 72.0 / 25.4
"""毫米→磅的精确值（2.834645669291339）。参考实现用的是 2.842，偏大约 0.26%。"""

DEFAULT_WIDTH_MM = 42.0
"""默认章宽：公章常见直径 42mm（约 119.06pt）。骑缝章按「整枚拼起来的宽度」给。"""

DEFAULT_WIDTH_PT = DEFAULT_WIDTH_MM * MM_TO_PT

DEFAULT_GROUP_SIZE = 20
"""默认每组 20 页：一枚章最多被切成 20 条，再多就开始出锯齿/接缝失真。"""

DEFAULT_Y_PERCENT = 50.0
"""纵向位置默认**居中**。0 = 贴顶、100 = 贴底（界面/预览语义，左上原点）。"""

DEFAULT_OVERLAP_RATIO = 0.10
"""overlap=True 时的压缝比例：每条右端多带 10% 条宽的源列（相邻条重复一小段，缝上不留白）。"""

MAX_OVERLAP_RATIO = 0.5
"""压缝上限：一条里重复的内容不许超过它自己的一半，否则整条就被重复吃掉了。"""

SWAP_ROTATIONS = (90, 270)

# 错误码是纯数据，话术一律留给 api 层翻（core 不 import i18n，命令行才还能自己决定怎么说）
ERR_IMAGE = 'image_bad'
ERR_PARTS = 'bad_parts'
ERR_NARROW = 'slice_too_narrow'
ERR_WIDTH_CONFLICT = 'width_conflict'
ERR_ZERO_SIZE = 'zero_page'


def _to_int(value, default=0):
    """宽容取整：None / 空串 / 认不出来 → default；小数按截断。不抛异常。"""
    try:
        if value is None or value == '':
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _to_float(value, default=0.0):
    """宽容取浮点：NaN / inf / 认不出来 → default（参考实现就是栽在「认不出来留 0」上）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float('inf'), float('-inf')):
        return float(default)
    return number


def _clamp(value, low, high):
    return max(low, min(high, value))


def normalize_rotation(rotation):
    """把页面旋转角收成 0/90/180/270 之一，返回 (角度, 是否被规整过)。

    /Rotate 只允许 90 的整数倍；实际文件里见过 45（做水印的）与 -90、90+360。
    规整是**向下取到最近的 90 倍数**（45° 当 0° 处理：这类页本来就靠矩阵自己转，
    章贴着右边缘放比按 90 猜一个方向更接近用户看到的右边缘）。
    """
    raw = _to_int(rotation, 0)
    r = raw % 360
    if r % 90 != 0:
        return (r // 90) * 90 % 360, True
    return r, False


def visual_size(page_w, page_h, rotation=0):
    """存储宽高 + 旋转角 → 视觉宽高。90/270 交换（扫描件最常见的错位源）。"""
    w = max(0.0, _to_float(page_w, 0.0))
    h = max(0.0, _to_float(page_h, 0.0))
    r, _snapped = normalize_rotation(rotation)
    return (w, h) if r not in SWAP_ROTATIONS else (h, w)


def to_pdf_origin(rect, page_h):
    """同一矩形从「左上原点」翻到「PDF 左下原点」：y_pdf = 页高 - y_top。

    传进来的是**未旋转**页面空间的矩形（page_top 那套），单位 pt。
    """
    x0, y0, x1, y1 = rect
    h = _to_float(page_h, 0.0)
    return (float(x0), h - float(y1), float(x1), h - float(y0))


def to_page_rect(rect, page_w, page_h, rotation=0):
    """display（视觉、左上）→ page_top（未旋转、左上，PyMuPDF 插图片用的那一套）。

    四角映射与本模块 `to_display_rect()` 严格互逆；两条都被
    `tests/qifeng_check.py` 拿 PyMuPDF 的 `Page.derotation_matrix` 逐角对过数。
    """
    x0, y0, x1, y1 = [float(v) for v in rect]
    w = _to_float(page_w, 0.0)   # 存储宽（未旋转）
    h = _to_float(page_h, 0.0)   # 存储高（未旋转）
    r, _snapped = normalize_rotation(rotation)
    if r == 90:
        return (y0, h - x1, y1, h - x0)
    if r == 180:
        return (w - x1, h - y1, w - x0, h - y0)
    if r == 270:
        return (w - y1, x0, w - y0, x1)
    return (x0, y0, x1, y1)


def to_display_rect(rect, page_w, page_h, rotation=0):
    """page_top（未旋转、左上，读回来的那一套）→ display（视觉、左上）。"""
    x0, y0, x1, y1 = [float(v) for v in rect]
    w = _to_float(page_w, 0.0)
    h = _to_float(page_h, 0.0)
    r, _snapped = normalize_rotation(rotation)
    if r == 90:
        return (h - y1, x0, h - y0, x1)
    if r == 180:
        return (w - x1, h - y1, w - x0, h - y0)
    if r == 270:
        return (y0, w - x1, y1, w - x0)
    return (x0, y0, x1, y1)


def seal_width(visual_w, width_pt=0.0, width_percent=0.0, default_pt=DEFAULT_WIDTH_PT):
    """整枚章的宽度（pt），两种语义分开，**同时给就报错**。

    * `width_pt > 0`      → 绝对宽度（pt）：印出来多大就是多大，与页面尺寸无关。
    * `width_percent > 0` → 占**视觉页宽**的百分比：不同开本的页拼起来比例一致。
    * 两个都不 > 0        → 退回 `default_pt`（42mm），并出声（`used_default`）。
    * 两个都 > 0          → `width_conflict`：参考实现把 mm 与 % 塞进同一个输入框，
      同一个数两种解释差好几倍，这种歧义宁可报错也不猜。

    → `{'success', 'width_pt', 'width_mode', 'used_default', 'visual_w', 'error', 'detail'}`
    """
    pt = _to_float(width_pt, 0.0)
    pct = _to_float(width_percent, 0.0)
    vw = max(0.0, _to_float(visual_w, 0.0))
    if pt > 0 and pct > 0:
        return {'success': False, 'error': ERR_WIDTH_CONFLICT,
                'detail': {'width_pt': pt, 'width_percent': pct}}
    if pt <= 0 and pct <= 0:
        return {'success': True, 'width_pt': _to_float(default_pt, DEFAULT_WIDTH_PT),
                'width_mode': 'pt', 'used_default': True, 'visual_w': vw}
    if pt > 0:
        return {'success': True, 'width_pt': pt, 'width_mode': 'pt',
                'used_default': False, 'visual_w': vw}
    return {'success': True, 'width_pt': vw * pct / 100.0, 'width_mode': 'percent',
            'used_default': False, 'visual_w': vw, 'width_percent': pct}


def overlap_pixels(ratio, base_width):
    """把「压缝比例」翻成「源列数」：每条右端多带几列（与相邻条重复）。

    - `ratio` 接受 bool / 数字：True → `DEFAULT_OVERLAP_RATIO`，数字 → 自己那一档，
      负数与非数字 → 0（不压缝），上限 `MAX_OVERLAP_RATIO`。
    - 上限还受条宽管着：重复的列数不许吃到 `base_width` 整条，最多 `base_width - 1`。
    - 只切一条（`parts == 1`，即 group_size=1 或单页）时没有相邻缝 → 0。
    """
    if isinstance(ratio, bool):
        value = DEFAULT_OVERLAP_RATIO if ratio else 0.0
    else:
        value = _to_float(ratio, 0.0)
    if value <= 0:
        return 0
    value = min(value, MAX_OVERLAP_RATIO)
    base = _to_int(base_width, 0)
    if base < 2:
        return 0
    return int(_clamp(round(base * value), 0, base - 1))


def slice_plan(width, parts, overlap_px=0):
    """切片数学：宽 width 的图切成 parts 条，**整除余数吃进最后一条**。

    返回 `{'success', 'width', 'parts', 'base', 'remainder', 'overlap_px',
           'widths': [...], 'ranges': [(x0, x1)], 'sum_widths', 'sum_source', 'tiled'}`

    口径（都被 tests 钉着）：
    * 不压缝时 `Σ widths == width`、`ranges` 首尾相接 —— 拼回去既不少一像素也不重叠；
    * `base = width // parts`、`remainder = width % parts`，`widths[-1] = base + remainder`；
    * 压缝（overlap_px > 0）时**只把右端加长**，左边界的划分不变，所以最后一条不再右伸，
      `Σ widths = width + (parts-1) * overlap_px`，重复的正是相邻条交界那一小段；
      放不下的（最后一条右边没东西可借）自然不吃；
    * `parts <= 0` 不抛不除零：当作 1 条（整张图）并留 `normalized` 出声；
    * `parts > width`（图太窄、页太多）时无法保证每条至少 1 列 → `slice_too_narrow`。
    """
    w = _to_int(width, 0)
    raw = _to_int(parts, 0)
    n = raw if raw >= 1 else 1
    o = max(0, _to_int(overlap_px, 0))
    if w < 1:
        return {'success': False, 'error': ERR_ZERO_SIZE, 'detail': {'width': w}}
    if n > w:
        return {'success': False, 'error': ERR_NARROW,
                'detail': {'width': w, 'parts': n}}
    base = w // n
    rem = w % n
    widths = [base] * (n - 1) + [base + rem] if n > 1 else [w]
    ranges = []
    cursor = 0
    for i, one in enumerate(widths):
        x0 = cursor
        x1 = x0 + one
        cursor = x1
        if o and i < n - 1:
            x1 = min(w, x1 + o)
        ranges.append((x0, x1))
    drawn = [b - a for a, b in ranges]
    return {'success': True, 'width': w, 'parts': n, 'base': base, 'remainder': rem,
            'overlap_px': o, 'widths': widths, 'ranges': ranges,
            'sum_widths': sum(widths), 'sum_source': sum(drawn), 'tiled': (cursor == w),
            'normalized': raw != n}


def slice_image(img, parts, overlap_px=0):
    """用 Pillow 把图片竖切成 n 条竖条（纯内存，不落盘、不碰 PDF）。

    `img` 是任何带 `.size` / `.crop()` 的 PIL 图片对象；本函数不改它的模式，
    要带透明通道请在调用前 `convert('RGBA')`（桥层就是这么做的）。

    → `{'success', 'parts', 'overlap_px', 'image_width', 'image_height', 'sum_source',
        'slices': [{'part', 'x0', 'x1', 'width', 'image'}]}`

    保证：`overlap_px=0` 时 n 条拼回去的宽度**等于原图宽度**（余数在最后一条里），
    条与条共享的是「边界坐标」而不是像素列 —— 既不丢一列也不重一列。
    """
    size = getattr(img, 'size', None)
    if not size:
        return {'success': False, 'error': ERR_IMAGE, 'detail': {'reason': 'no_size'}}
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError):
        return {'success': False, 'error': ERR_IMAGE, 'detail': {'reason': 'bad_size'}}
    if width < 1 or height < 1:
        return {'success': False, 'error': ERR_IMAGE, 'detail': {'width': width,
                                                                 'height': height}}
    n = _to_int(parts, 0)
    if n < 1:
        n = 1                      # 不抛不除零：当作整张一条
    sp = slice_plan(width, n, overlap_px)
    if not sp.get('success'):
        return sp
    slices = []
    for i, (x0, x1) in enumerate(sp['ranges']):
        slices.append({'part': i + 1, 'x0': x0, 'x1': x1, 'width': x1 - x0,
                       'image': img.crop((x0, 0, x1, height))})
    return {'success': True, 'parts': sp['parts'], 'overlap_px': sp['overlap_px'],
            'image_width': width, 'image_height': height, 'sum_source': sp['sum_source'],
            'remainder': sp['remainder'], 'slices': slices}


def group_sizes(page_count, group_size):
    """连续分块的块长表：每块至多 group_size 页，最后一块拿余数。

    `page_count <= 0` → 空表；`group_size` 被夹到 `[1, page_count]`（0/负数 → 1，
    比页数还大 → 页数，即「整本一枚章」）。返回 `(块表, 生效的 group_size)`。
    """
    total = max(0, _to_int(page_count, 0))
    raw = _to_int(group_size, DEFAULT_GROUP_SIZE)
    if total == 0:
        return [], 0
    size = raw
    if size < 1:
        size = 1
    if size > total:
        size = total
    out = []
    left = total
    while left > 0:
        out.append(min(size, left))
        left -= out[-1]
    return out, size


def plan(page_count, group_size=DEFAULT_GROUP_SIZE):
    """每一页拿到哪一条：第几片 / 共几片 / 属于第几组。纯算术，不碰 PDF。

    → `{'success', 'page_count', 'group_size_requested', 'group_size', 'group_count',
        'rows': [{'index', 'page', 'group', 'part', 'parts'}],
        'groups': [{'group', 'pages', 'parts'}], 'sum_parts', 'covers_all', 'notes'}`

    口径：
    * 第 i 组切 `parts = 该组页数` 条，组内第 k 页拿第 k 条 —— 每组**独立重切一枚完整的章**，
      所以每组的第 1 页拿到的永远是第 1 片（组界处图案重新开始）；
    * 不变式 `Σ parts == page_count`（每页恰好一条，不多不少）；
    * `page_count == 0` → 空 rows、空 groups、`group_count = 0`，不崩不除零；
    * `pages == 1` → 1 组 1 片（整枚章在那一页上，本来就没有缝）；
    * `group_size > pages` → 收敛成一页组都不开：只有 1 组、片数 = 页数；
    * `group_size == 1` → 每组 1 页 1 片：**每页都盖一整枚章**（退化成普通盖章，
      这是这条参数的确定语义，不是「报错」也不是「按页数」）；
    * `group_size <= 0` 在这里是**规整成 1** 并留 note 出声（桥层拿它当输入错误，直接报错）。
    """
    total = max(0, _to_int(page_count, 0))
    raw = _to_int(group_size, DEFAULT_GROUP_SIZE)
    sizes, size = group_sizes(total, group_size)
    notes = []
    if total == 0:
        notes.append('no_pages')
        return {'success': True, 'page_count': 0, 'group_size_requested': raw,
                'group_size': 0, 'group_count': 0, 'rows': [], 'groups': [],
                'sum_parts': 0, 'covers_all': True, 'notes': notes}
    if raw != size:
        notes.append('group_size_clamped')
    rows = []
    groups = []
    page = 0
    for gidx, n in enumerate(sizes, start=1):
        chunk = []
        for part in range(1, n + 1):
            rows.append({'index': page, 'page': page + 1, 'group': gidx,
                         'part': part, 'parts': n})
            chunk.append(page + 1)
            page += 1
        groups.append({'group': gidx, 'pages': chunk, 'parts': n})
    return {'success': True, 'page_count': total, 'group_size_requested': raw,
            'group_size': size, 'group_count': len(groups), 'rows': rows,
            'groups': groups, 'sum_parts': sum(g['parts'] for g in groups),
            'covers_all': page == total, 'notes': notes}


def plan_for_pages(pages, group_size=DEFAULT_GROUP_SIZE):
    """和 `plan()` 同一套数学，但喂的是「要盖章的页码升序表」（1-based、可跳号）。

    页码范围里只挑 3~10 页盖骑缝章时，切片数按**被选的页数**算而不是全文页数，
    拼起来才是完整一枚；`page` 字段回填真实页码，桥层直接按它取页。
    选出 0 页时与 `plan(0, …)` 同形（空表）。页码不连续时留 note 出声
    （缝对面不是相邻页，图案物理上拼不到一起，这不是错误但用户该知道）。
    """
    numbers = []
    for item in (pages or []):
        value = _to_int(item, 0)
        if value > 0 and value not in numbers:
            numbers.append(value)
    numbers.sort()
    result = plan(len(numbers), group_size)
    for row in result['rows']:
        row['page'] = numbers[row['index']]
    for group in result['groups']:
        group['pages'] = [numbers[i - 1] for i in group['pages']]
    result['page_count'] = len(numbers)
    if len(numbers) > 1:
        gaps = [i for i in range(1, len(numbers)) if numbers[i] != numbers[i - 1] + 1]
        if gaps:
            result['notes'].append('pages_not_adjacent')
            result['adjacency_gaps'] = len(gaps)
    return result


def y_from_top(y_percent, visual_h, seal_h):
    """纵向百分比（0=贴顶、100=贴底）→ 章**顶边距视觉页顶**的 pt 数。

    可活动余量是 `visual_h - seal_h`（章比页面高时为 0，此时章贴顶放、由调用方看 flags）。
    这条公式就是参考实现文档里那条「1-y」的另一半：
      左上原点：`y_top = (H - h) * p/100`
      左下原点：`y_pdf = (H - h) * (1 - p/100)`  ==  `H - (y_top + h)`
    两者都被 tests 钉着（p=0 贴顶、p=100 贴底、p=50 居中）。
    """
    p = _clamp(_to_float(y_percent, DEFAULT_Y_PERCENT), 0.0, 100.0)
    room = max(0.0, _to_float(visual_h, 0.0) - max(0.0, _to_float(seal_h, 0.0)))
    return room * p / 100.0


def place_rect(page_w, page_h, rotation=0, piece_px=0, total_piece_px=0,
               image_w=0, image_h=0, width_pt=0.0, width_percent=0.0,
               y_percent=DEFAULT_Y_PERCENT):
    """某一页某一条的贴放矩形。纯函数，输入普通数、输出三套坐标 + 尺寸账。

    参数（语义各管各的，不许交叉）：
      `page_w` / `page_h`  **未旋转**（存储）的页面宽高，pt —— 桥层给 `Page.cropbox` 的宽高。
      `rotation`           页面 /Rotate，0/90/180/270（其它值按 `normalize_rotation` 规整）。
      `piece_px`           这一条在原图上占的**源列数**（含压缝多带的部分）。
      `total_piece_px`     这一组所有条的源列数之和（不压缝时 == 原图宽）。
      `image_w`/`image_h`  原图像素宽高（定章的高宽比）。
      `width_pt`           整枚章的绝对宽度，pt。**与 width_percent 互斥。**
      `width_percent`      整枚章的宽度占**视觉页宽**的百分比。**与 width_pt 互斥。**
      `y_percent`          纵向位置，0=贴顶 / 100=贴底 / 50=居中（左上原点语义）。

    账怎么算：
      视觉宽高 = 存储宽高（90/270 交换）；
      整枚章宽 = width_pt 或 视觉页宽 × width_percent；整枚章高 = 章宽 × 图高/图宽；
      本条显示宽 = 整枚章宽 × piece_px / total_piece_px —— 各条按**源列数比例**分配，
      所以 Σ 各条显示宽 == 整枚章宽（压缝时重复列被摊薄，整体宽度不变、高不变）；
      横向永远 `x1 = 视觉页宽`（贴视觉右边缘，这就是骑缝印的全部意义）；
      纵向 `y_top = (视觉高 - 章高) × y_percent/100`，超出可活动余量就夹并留 flags。

    → `{'success', 'visual': {...}, 'size': {...}, 'rect_display': (...),
        'rect_page': (...), 'rect_pdf': (...), 'flush_right', 'flags', 'error', 'detail'}`
    """
    sw = _to_float(page_w, 0.0)
    sh = _to_float(page_h, 0.0)
    rot, snapped = normalize_rotation(rotation)
    if sw <= 0 or sh <= 0:
        return {'success': False, 'error': ERR_ZERO_SIZE,
                'detail': {'page_w': sw, 'page_h': sh}}
    vw, vh = visual_size(sw, sh, rot)

    width = seal_width(vw, width_pt, width_percent)
    if not width.get('success'):
        width['visual'] = {'stored_w': sw, 'stored_h': sh, 'rotation': rot,
                           'visual_w': vw, 'visual_h': vh}
        return width

    iw = _to_float(image_w, 0.0)
    ih = _to_float(image_h, 0.0)
    total = _to_float(total_piece_px, 0.0)
    piece = max(0.0, _to_float(piece_px, 0.0))
    if iw <= 0 or ih <= 0:
        return {'success': False, 'error': ERR_IMAGE,
                'detail': {'image_w': iw, 'image_h': ih}}
    if total <= 0:
        return {'success': False, 'error': ERR_PARTS, 'detail': {'total_piece_px': total}}

    full_w = max(0.0, width['width_pt'])
    full_h = full_w * ih / iw
    piece_raw = full_w * piece / total if piece > 0 else 0.0
    piece_w = piece_raw
    piece_h = full_h

    y_top = y_from_top(y_percent, vh, full_h)
    p_used = _clamp(_to_float(y_percent, DEFAULT_Y_PERCENT), 0.0, 100.0)
    if full_h > vh:
        # 章比页面还高：没有可活动余量，贴顶放（余量公式会给出负数，那是不行的）
        y_top = 0.0
    if piece_w > vw:
        # 条宽超过页宽（单页组 + 超大章）：贴右边缘放不下的那部分会出框，夹进来并出声
        piece_w = vw

    x1 = vw
    x0 = vw - piece_w
    display = (x0, y_top, x1, y_top + piece_h)
    page_top = to_page_rect(display, sw, sh, rot)
    pdf = to_pdf_origin(page_top, sh)
    flags = {'rotation_snapped': snapped, 'seal_taller_than_page': full_h > vh,
             'seal_wider_than_page': full_w > vw, 'piece_wider_than_page': piece_raw > vw,
             'y_clamped_bottom': y_top + piece_h > vh + 1e-9,
             'zero_piece': piece_w <= 0}
    return {'success': True,
            'visual': {'stored_w': sw, 'stored_h': sh, 'rotation': rot,
                       'visual_w': vw, 'visual_h': vh, 'swapped': rot in SWAP_ROTATIONS},
            'size': {'full_width_pt': full_w, 'full_height_pt': full_h,
                     'full_width_mm': full_w / MM_TO_PT,
                     'piece_width_pt': piece_w, 'piece_height_pt': piece_h,
                     'piece_px': piece, 'total_piece_px': total,
                     'width_mode': width['width_mode'], 'used_default': width['used_default'],
                     'image_w': iw, 'image_h': ih},
            'y': {'percent': p_used, 'from_top_pt': y_top,
                  'from_bottom_pt': max(0.0, vh - (y_top + piece_h))},
            'rect_display': display, 'rect_page': page_top, 'rect_pdf': pdf,
            'flush_right': True, 'flags': flags}
