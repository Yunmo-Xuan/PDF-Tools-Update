"""往页面里写矢量文字的公共底座：页码（F14）、页眉页脚（F15）、TXT 转 PDF（F12）共用。

为什么不用 page.insert_text / insert_textbox：
  * 它们对内置中文字体 china-s 每次调用都要重做一遍 CID 编码，本机实测 8.4 ms/页，
    3000 页的书光是加页码就要 25 秒，越过方案 §4.13 给桥层同步调用定的 5 秒上限；
  * TextWriter 实测 1.0 ms/页（同一份 pm_3000 夹具 3.1 秒写完），而且字宽用
    font.text_length 量出来的就是渲染出来的，对齐和「放不下」的判断才有依据——
    CID 引用法的实际宽度取决于阅读器替换的那套中文字体，同一行字在不同机器上能差一倍。

代价是 TextWriter 会把字体嵌进文件，所以写完要 doc.subset_fonts() 把没用到的字形裁掉。
裁完的体积与 CID 引用法实测持平（pm_3000 加页码：TextWriter+裁剪 +502 KB，
insert_textbox 的 CID 写法 +522 KB），渲染逐字节相同，用户看不出差别。
"""

import logging
import re

# PyMuPDF 内置简体中文字体（含常用汉字与拉丁字母，一套覆盖页码/页眉的全部写法）
FONT_NAME = "china-s"
# 行高 = 字号 × 1.6：中文 1.0 会挤成一团，页码/页眉用的横条按这个高度留
LINE_RATIO = 1.6
# 基线在横条里的位置 = 上沿往下 1.1 个字号（实测中英文与数字的墨迹都完整落在条内）
BASELINE_RATIO = 1.1


def make_font(fitz):
    """取内置中文字体对象，整本循环复用（每次 new 都要把字库读进内存）。

    老行为一个字都不变：给没接「选字体」的那一路（涂黑改写等）继续用内置 china-s。
    要用户自选字体走 make_font_ex。
    """
    return fitz.Font(FONT_NAME)


def make_font_ex(fitz, ref=None):
    """(字体对象, 退回说明的字典键)。ref 是界面选的字体引用（族名或路径），
    为空就等价于内置 china-s；ref 给了但读不出来 → 内置字体 + FONT_FALLBACK_NOTE，
    调用方把那把键过 core.i18n 翻成界面语言告诉用户「字体没换成」。

    字体对象仍然只造一次、整本循环复用——和 make_font 同一动机。
    """
    from core import fonts as local_fonts
    return local_fonts.load_fitz_font(fitz, ref)


def text_width(font, text, size_pt):
    """这段文字按 size_pt 排出来占多宽（pt）。"""
    return font.text_length(text, fontsize=size_pt)


def text_matrix(fitz, page):
    """
    把「屏幕上看到的那一页」的坐标换算成写内容时用的内部坐标。

    页码位置前端按可视方向给（用户眼里的右下角），可 TextWriter 不认 /Rotate：
      * 0°/180°：乘 derotation_matrix 就够了；
      * 90°/270°：还不够。TextWriter 内部先按可视高度做 y 翻转，又按「可视高−可视宽」
        平移一次来补宽高互换，两处叠加的结果是文字被左右镜像、并且整条跑到页面外——
        表现是「接口报成功、文件也没坏，但页面上找不到页码」。
        再乘一个绕可视宽度中线的左右翻转才正好抵消。
    这个组合在 4 种页面尺寸 × 4 个旋转角 × 有无 CropBox 偏移共 96 组实测里，
    渲染出的墨迹与 0° 页面的正立渲染逐像素对得上（方案 §4.10 要求 F14–F17 认旋转）。
    """
    matrix = page.derotation_matrix
    if page.rotation not in (90, 270):
        return matrix
    rect = page.rect
    mirror = fitz.Matrix(-1, 0, 0, 1, rect.width, 0)
    flip = fitz.Matrix(1, 0, 0, -1, 0, rect.height)
    shift = fitz.Matrix(1, 0, 0, 1, 0, rect.height - rect.width)
    return mirror * flip * matrix * shift


def write_at(fitz, font, page, x, baseline_y, text, size_pt, color=None):
    """在 (x, baseline_y) 写一行字，坐标按可视方向给。color 省略时是黑色。"""
    writer = fitz.TextWriter(page.rect)
    writer.append((x, baseline_y), text, font=font, fontsize=size_pt)
    # 不传 color 就一个字都不多写：传 (0,0,0) 会让内容流里凭空多一行 "0 0 0 rg"，
    # 老文件的页码成品和新文件的对不上，回归里那种逐字节比较会误报
    kwargs = {} if color is None else {"color": color}
    writer.write_text(page, matrix=text_matrix(fitz, page), **kwargs)


def write_line(fitz, font, page, rect, text, size_pt, align, color=None):
    """
    在横条 rect 里按 align（0 左 / 1 中 / 2 右）写一行字。返回 (是否放下, 实测宽度)。

    「放不下」用实测宽度对横条宽度判断：宁可报「这个位置留白不足，请调大边距」，
    也不把字压到正文上去（方案 §4.10）。
    """
    width = text_width(font, text, size_pt)
    if width > rect.width:
        return False, width
    if align == 0:
        x = rect.x0
    elif align == 2:
        x = rect.x1 - width
    else:
        x = rect.x0 + (rect.width - width) / 2.0
    write_at(fitz, font, page, x, rect.y0 + size_pt * BASELINE_RATIO, text, size_pt, color)
    return True, width


def wrap_line(font, line, size_pt, limit, cache=None):
    """
    按真实字宽把一行切成不超过 limit 的若干小段。

    不这么做的后果：TXT 里一行有几十字（没换行的段落）时，超出页宽的部分会被直接
    写到纸外面，用户发现右边缺字。一个字一个字量太慢，量过的字宽记进 cache
    （中英混排时常见字只量一次，3000 行也不会退化成 3000×字数 次测量）。
    """
    if not line:
        return [""]
    if cache is None:
        cache = {}
    out = []
    current = ""
    used = 0.0
    for ch in line:
        char_w = cache.get(ch)
        if char_w is None:
            try:
                char_w = font.text_length(ch, fontsize=size_pt)
            except Exception:
                # 量不了（个别生僻字符）就按最宽的中文估，宁可换行早一点，
                # 也不能把字写到纸外
                char_w = size_pt
            cache[ch] = char_w
        if used + char_w > limit and current:
            out.append(current)
            current, used = ch, char_w
        else:
            current += ch
            used += char_w
    if current:
        out.append(current)
    return out


def subset_fonts(doc):
    """
    裁掉嵌进来的字体中没用到的字形。

    裁剪失败（缺 fontTools 之类）只影响体积，不影响这份文件能不能看，
    所以记一条日志继续保存，别把用户的页码功能报成失败。
    """
    try:
        doc.subset_fonts()
    except Exception:
        logging.exception("字体裁剪失败，按原样保存（文件会偏大）")


# ==================== 行内 LaTeX 标记：一段文字里混着多种样式 ====================
#
# 只有 `core/latex_inline.has_markup()` 为真才走这里；没有标记的行仍然走上面的
# write_at / write_line，成品逐字节不变（这是硬要求，验收里单独钉一条）。

# 用户选的那支字体没有对应样式时，拉丁字母退到 Base-14 的这几面
STYLE_BASE14 = {'bf': 'tibo', 'it': 'tiit', 'bi': 'tibi', 'tt': 'cour',
                'sf': 'helv', 'rm': 'tiro'}
# 找用户字体样式时按这些名字试（不同厂商叫法不一，试到哪个算哪个）
STYLE_TRY = {'bf': ('Bold', 'Bold Regular', 'Heavy', 'Black'),
             'it': ('Italic', 'Oblique', 'Light Italic'),
             'bi': ('Bold Italic', 'Bold Oblique'),
             'tt': ('Regular',),
             'sf': ('Regular',)}


def style_fonts(fitz, ref=None):
    """(样式→字体 的字典, 说明键列表)。

    `rm` 与中文都用用户选的那支（没选就是内置 china-s，和老行为一致）；
    `bf/it/tt/sf` 先试同一族的对应样式，试不到才退 Base-14，并记一条
    `api_latex_style_base14` —— 退面要让用户知道，不然他会以为
    「我选的字体加粗了」而成品其实是 Times-Bold。
    """
    from core import fonts as local_fonts

    notes = []
    base, base_note = local_fonts.load_fitz_font(fitz, ref)
    if base_note:
        notes.append(base_note)
    out = {'rm': base, 'cjk': base}
    ref_clean = str(ref or '').strip()
    for style in ('bf', 'it', 'bi', 'tt', 'sf'):
        font = None
        if ref_clean:
            for name in STYLE_TRY.get(style, ()):
                cand, cand_note = local_fonts.load_fitz_font(
                    fitz, '%s|%s' % (ref_clean.split('|')[0], name))
                if cand is not None and cand_note is None:
                    font = cand
                    break
        if font is None:
            font = fitz.Font(STYLE_BASE14[style])
            if ref_clean:
                notes.append('api_latex_style_base14')
        out[style] = font
    return out, notes


def _split_script(text):
    """按中日韩/其余切段：中文永远用用户/内置那支，拉丁才允许换 Base-14 面。"""
    from core.latex_sym import is_cjk

    segs = []
    buf = ''
    cur = None
    for ch in text:
        key = is_cjk(ch)
        if cur is None:
            cur = key
        if key != cur:
            segs.append((cur, buf))
            buf, cur = ch, key
        else:
            buf += ch
    if buf:
        segs.append((bool(cur), buf))
    return segs


def face_for(fonts, style, text):
    """这段文字该用哪支字体。"""
    if text and _split_script(text)[0][0]:
        return fonts.get('cjk') or fonts.get('rm')
    return fonts.get(style) or fonts.get('rm')


def runs_measure(fonts, items, size):
    """(宽, 基线以上高, 基线以下深)。分式与根号会把高/深撑大。"""
    width = 0.0
    height = size * 0.72
    depth = size * 0.22
    for it in items or []:
        k = it.get('k')
        if k == 'txt':
            text = it.get('v') or ''
            w = 0.0
            for cjk, seg in _split_script(text):
                font = (fonts.get('cjk') or fonts.get('rm')) if cjk \
                    else (fonts.get(it.get('style') or 'rm') or fonts.get('rm'))
                w += font.text_length(seg, fontsize=size * 0.72) if it.get('shift') \
                    else font.text_length(seg, fontsize=size)
            width += w
            if it.get('shift') == 'sup':
                height = max(height, size * 0.45 + size * 0.72 * 0.72)
            elif it.get('shift') == 'sub':
                depth = max(depth, size * 0.22 + size * 0.5)
        elif k == 'space':
            width += (it.get('em') or 0.0) * size
        elif k == 'frac':
            num_w, num_h, num_d = runs_measure(fonts, it.get('num'), size * 0.85)
            den_w, den_h, den_d = runs_measure(fonts, it.get('den'), size * 0.85)
            width += max(num_w, den_w) + size * 0.24
            height = max(height, size * 0.26 + size * 0.18 + num_h + num_d)
            depth = max(depth, abs(size * 0.26 - size * 0.18 - den_d) + den_h)
        elif k == 'sqrt':
            body_w, body_h, body_d = runs_measure(fonts, it.get('body'), size)
            width += size * 0.75 + body_w
            height = max(height, body_h + size * 0.12)
            depth = max(depth, body_d)
    return width, height, depth


def runs_write(fitz, fonts, page, x, baseline_y, items, size, color=None, matrix=None):
    """在 (x, baseline_y) 画一串 run，返回画完之后最靠右的那条边。

    `matrix` 传 `text_matrix(fitz, page)` 那种换算（旋转页要它），不给就不换算。
    颜色沿用老规矩：不传就一个字都不多写，免得内容流里凭空多一行 `0 0 0 rg`
    把「同一份输入两次成品逐字节一致」的回归判据搞坏。
    """
    writer = fitz.TextWriter(page.rect)
    ops = []
    _collect_runs(fonts, items, size, x, baseline_y, ops)
    for op in ops:
        if op[0] == 'txt':
            writer.append((op[1], op[2]), op[5], font=op[3], fontsize=op[4])
    kwargs = {} if color is None else {'color': color}
    writer.write_text(page, matrix=matrix or fitz.Matrix(1, 0, 0, 1, 0, 0), **kwargs)
    for op in ops:
        if op[0] == 'rule':
            page.draw_line((op[1], op[2]), (op[3], op[2]), width=max(op[4], 0.4),
                           color=color or (0, 0, 0))
    return _ops_right(ops)


def write_runs_line(fitz, fonts, page, rect, items, size, align, color=None):
    """在横条 rect 里按 align 画一串 run。返回 (是否放得下, 实测宽度)。

    和 `write_line` 同一套「放不下就先判断再说」的规矩：宽度用 `runs_measure`
    量出来的实测值，不许按字符数估——页码/页眉占的是页边距里的留白，
    压到正文上比报一句「放不下」糟糕得多（方案 §4.10）。
    """
    width, _height, _depth = runs_measure(fonts, items, size)
    if width > rect.width:
        return False, width
    if align == 0:
        x = rect.x0
    elif align == 2:
        x = rect.x1 - width
    else:
        x = rect.x0 + (rect.width - width) / 2.0
    runs_write(fitz, fonts, page, x, rect.y0 + size * BASELINE_RATIO, items, size,
               color=color, matrix=text_matrix(fitz, page))
    return True, width


def wrap_runs(fonts, items, size, limit):
    """把一串 run 按实测宽度切成若干行（每行还是 run 序列）。

    TXT 排 PDF 那条路要用：一行可能长过页宽，老代码 `wrap_line` 只认单一字体的纯文本，
    带标记的行里没有「纯文本」这个东西了，所以断行也得跟着按 run 走。
    断点规则照 `wrap_line`：西文按词、中日韩按字，绝不把字写到纸外。
    """
    lines = []
    cur = []
    used = 0.0

    def push_line():
        if cur:
            lines.append(list(cur))
            cur.clear()

    for it in items or []:
        k = it.get('k')
        if k == 'txt':
            for cjk, seg in _split_script(it.get('v') or ''):
                font = (fonts.get('cjk') or fonts.get('rm')) if cjk \
                    else (fonts.get(it.get('style') or 'rm') or fonts.get('rm'))
                scale = 0.72 if it.get('shift') else 1.0
                tsize = size * scale
                if cjk:
                    # 中日韩：一个字一个字试，超宽就换行
                    for ch in seg:
                        w = font.text_length(ch, fontsize=tsize)
                        if used + w > limit and cur:
                            push_line()
                            used = 0.0
                        cur.append(dict(it, v=ch))
                        used += w
                else:
                    # 西文：按空格切词，词内部不拆
                    parts = re.split(r'(\s+)', seg)
                    for part in parts:
                        if not part:
                            continue
                        w = font.text_length(part, fontsize=tsize)
                        if part.isspace():
                            cur.append(dict(it, v=part))
                            used += w
                            continue
                        if used + w > limit and cur:
                            push_line()
                            used = 0.0
                        cur.append(dict(it, v=part))
                        used += w
        elif k == 'space':
            w = (it.get('em') or 0.0) * size
            if used + w > limit and cur:
                push_line()
                used = 0.0
            cur.append(it)
            used += w
        else:
            # 分式/根号是不可再分的整体：放不下也只能整块占一行
            w = runs_measure(fonts, [it], size)[0]
            if used + w > limit and cur:
                push_line()
                used = 0.0
            cur.append(it)
            used += w
    if cur:
        lines.append(cur)
    return lines or [[]]


def _ops_right(ops):
    """这批指令画到哪儿为止。"""
    right = 0.0
    for op in ops:
        if op[0] == 'txt':
            right = max(right, op[1] + op[3].text_length(op[5], fontsize=op[4]))
        elif op[0] == 'rule':
            right = max(right, op[3])
    return right


def _collect_runs(fonts, items, size, x0, y0, ops):
    """把 run 摊成**绝对坐标**的绘制指令。

    指令只有两种形状，别处（含验收）就按这个形状读，别再发明第三种：
      `('txt', x, 基线y, font, 字号, 文字)`
      `('rule', 左x, y, 右x, 粗细)`
    """
    x = x0
    for it in items or []:
        k = it.get('k')
        if k == 'txt':
            text = it.get('v') or ''
            if not text:
                continue
            shift = it.get('shift')
            tsize = size * (0.72 if shift else 1.0)
            ty = y0 + (-size * 0.45 if shift == 'sup'
                       else size * 0.22 if shift == 'sub' else 0.0)
            start_x = x
            for cjk, seg in _split_script(text):
                font = (fonts.get('cjk') or fonts.get('rm')) if cjk \
                    else (fonts.get(it.get('style') or 'rm') or fonts.get('rm'))
                ops.append(('txt', x, ty, font, tsize, seg))
                x += font.text_length(seg, fontsize=tsize)
            if it.get('ul'):
                ops.append(('rule', start_x, ty + size * 0.22, x, size * 0.05))
            if it.get('ol'):
                ops.append(('rule', start_x, ty - size * 0.78, x, size * 0.05))
        elif k == 'space':
            x += (it.get('em') or 0.0) * size
        elif k == 'frac':
            num_size = size * 0.85
            num_ops = _collect_runs(fonts, it.get('num'), num_size, 0.0, 0.0, [])
            den_ops = _collect_runs(fonts, it.get('den'), num_size, 0.0, 0.0, [])
            num_w = _ops_right(num_ops)
            den_w = _ops_right(den_ops)
            box_w = max(num_w, den_w) + size * 0.24
            axis = size * 0.26
            gap = num_size * 0.18
            # 分子整体上移到横线之上、分母下移到横线之下：
            # 上移量按分子自己的墨迹高度算，写死一个系数会在大字号时压到横线上
            for op in num_ops:
                ops.append(_move(op, x + (box_w - num_w) / 2.0,
                                 y0 - axis - gap - _height_of(num_ops)))
            for op in den_ops:
                ops.append(_move(op, x + (box_w - den_w) / 2.0, y0 + axis + gap))
            ops.append(('rule', x + size * 0.06, y0 - axis,
                        x + box_w - size * 0.06, size * 0.05))
            x += box_w
        elif k == 'sqrt':
            body_ops = _collect_runs(fonts, it.get('body'), size, 0.0, 0.0, [])
            body_w = _ops_right(body_ops)
            rad_font = fonts.get('rm')
            rad_w = rad_font.text_length('√', fontsize=size)
            ops.append(('txt', x, y0, rad_font, size, '√'))
            for op in body_ops:
                ops.append(_move(op, x + rad_w, y0))
            ops.append(('rule', x + rad_w - size * 0.04, y0 - size * 0.82,
                        x + rad_w + body_w, size * 0.045))
            x += rad_w + body_w
    return ops


def _height_of(ops):
    """这批 txt 指令从基线往上最高到哪儿（分式分子上移量用它）。"""
    top = 0.0
    for op in ops:
        if op[0] == 'txt':
            top = max(top, op[4] * 0.75)
    return top


def _move(op, dx, dy):
    if op[0] == 'txt':
        return ('txt', op[1] + dx, op[2] + dy, op[3], op[4], op[5])
    return ('rule', op[1] + dx, op[2] + dy, op[3] + dx, op[4])
