"""页面版式：加页码（F14）、加页眉页脚（F15）、裁剪（F16）、缩放（F17）。

坐标全部在后端算：前端只传语义（九宫格名、幅面名、页码范围），这是 6.0.4 定下的
口径（方案 §4.3）。写中文文字统一走 api/_text.py（含旋转页的坐标换算与字体裁剪），
这里只管「摆在哪儿」。
"""

import logging
import os
import time

from ._geom import lookup_size
from ._io import close_pdf, save_pdf_doc
from ._range import select_pages
from ._text import (LINE_RATIO, make_font_ex, style_fonts, subset_fonts,
                    write_line, write_runs_line)
from core.latex_inline import has_markup, parse as parse_inline
from core.i18n import t as _text

# 九宫格：前端传语义名，值是（贴哪条横边, 对齐 0/1/2）。
# align 是 0/1/2 = 左/中/右，文字在这个横条里怎么靠边由它决定。
_POSITIONS = {
    "top-left": ("top", 0),
    "top-center": ("top", 1),
    "top-right": ("top", 2),
    "middle-left": ("middle", 0),
    "middle-center": ("middle", 1),
    "middle-right": ("middle", 2),
    "bottom-left": ("bottom", 0),
    "bottom-center": ("bottom", 1),
    "bottom-right": ("bottom", 2),
    "左上": ("top", 0),
    "上中": ("top", 1),
    "右上": ("top", 2),
    "左": ("middle", 0),
    "中": ("middle", 1),
    "右": ("middle", 2),
    "左下": ("bottom", 0),
    "下中": ("bottom", 1),
    "右下": ("bottom", 2),
}

# 只说「靠左/居中/靠右」时按页脚处理：加页码的用户常这么描述位置
_ALIGN_ONLY = {"left": ("bottom", 0), "center": ("bottom", 1), "right": ("bottom", 2)}


def _place(opt, key, default):
    """认位置名，返回 (横边, align)；认不出来返回错误信息。"""
    name = str(opt.get(key) or default).strip()
    place = _POSITIONS.get(name) or _POSITIONS.get(name.lower()) or _ALIGN_ONLY.get(name.lower())
    if not place:
        return None, f"不认识的位置名: {name}"
    return place, None


# 罗马数字按减法式记法拆：先大后小，4/9/40/90/400/900 这四类是「前一位减后一位」的写法。
_ROMAN_TABLE = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)

# 页码数字样式（F14）：界面下拉框传语义名，和 _POSITIONS 一个口径。
_NUMBER_STYLES = ("arabic", "roman", "roman_lower")


def _roman(value, lower=False):
    """
    阿拉伯数字转罗马数字（F14 的「罗马数字」页码样式）。

    只在 1–3999 之间转：罗马数字没有 0，四位以上要靠上划线才能精确表示，我们没有
    那种字形。超出范围就把原数字照常返回——页脚上出现「4000」只是不好看，
    出现一个自造的 MMMM 是错的数，用户会照着它去对页码。
    正文用大写（i/i 那种排版不正式），序言/目录那一叠前置页习惯用小写，两种都给。
    """
    num = int(value)
    if num < 1 or num > 3999:
        return str(num)
    parts = []
    for unit, symbol in _ROMAN_TABLE:
        while num >= unit:
            parts.append(symbol)
            num -= unit
    text = "".join(parts)
    return text.lower() if lower else text


def _fill(template, page_no, total, number_style="arabic"):
    """
    把 {page} / {total} 换成实际数字。

    number_style 只管 {page}：罗马数字说的是「这本书自己的页序」（序言 i、ii、iii），
    {total} 是整册的物理页数，套上罗马只会让人数不清到底多少页，所以不动。
    """
    shown = page_no
    if number_style == "roman":
        shown = _roman(page_no)
    elif number_style == "roman_lower":
        shown = _roman(page_no, lower=True)
    return str(template).replace("{page}", str(shown)).replace("{total}", str(total))


def _number(value, name, low, high, default):
    """把前端传来的字符串数字转成浮点并夹进合法区间；不合法就报人话。"""
    if value is None or value == "":
        return default, None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None, f"{name}必须是数字"
    if not low <= num <= high:
        return None, f"{name}要在 {low:g}–{high:g} 之间"
    return num, None


def _color(value, name="颜色"):
    """
    把界面给的颜色换成 fitz 的 (r, g, b) 浮点三元组；没给就返回 None（黑色）。

    认 #rrggbb / #rgb（取色器给的就是这个）、也认三个数字。三个数字时只要有一个
    大于 1 就整套按 0–255 读，否则按 0–1 读。
    认不出来要报错而不是悄悄退回黑色：用户选了红色却印出黑字，只会以为是功能坏了。
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        raw = value.strip().lstrip("#")
        if not raw:
            return None, None
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) != 6:
            return None, f"{name}要用 #rrggbb 这种十六进制写法"
        try:
            return tuple(int(raw[i:i + 2], 16) / 255.0 for i in (0, 2, 4)), None
        except ValueError:
            return None, f"{name}里有不是十六进制的字符: {value}"
    try:
        values = [float(v) for v in value]
    except (TypeError, ValueError):
        return None, f"{name}要给三个数字（红、绿、蓝）"
    if len(values) != 3:
        return None, f"{name}要给三个数字（红、绿、蓝）"
    if max(values) > 1:
        if not all(0 <= v <= 255 for v in values):
            return None, f"{name}的每个分量要在 0–255 之间"
        return tuple(v / 255.0 for v in values), None
    if not all(0 <= v <= 1 for v in values):
        return None, f"{name}的每个分量要在 0–1 之间"
    return tuple(values), None


def _as_opt(opt):
    """表单块必须是字典；顺手认 JSON 字符串（前端有时直接 stringify）。"""
    if opt is None:
        return {}
    if isinstance(opt, dict):
        return opt
    if isinstance(opt, str):
        import json
        try:
            loaded = json.loads(opt)
        except ValueError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def _band_rect(fitz, page_rect, band_height, margin, vertical):
    """
    算出这一行文字该占的横条。

    宽度取「页宽减两边距」而不是文字实测宽度：横条给宽一点，长文字才有放下的余地，
    放不下的判断交给 _text.write_line 用实测宽度做（负余量的写法只对 insert_textbox 有效）。
    """
    if vertical == "top":
        top = page_rect.y0 + margin
    elif vertical == "middle":
        top = page_rect.y0 + (page_rect.height - band_height) / 2.0
    else:
        top = page_rect.y1 - margin - band_height
    return fitz.Rect(page_rect.x0 + margin, top, page_rect.x1 - margin, top + band_height)


def _parse_rect(rect_pt):
    """
    把前端给的裁剪框整理成 (x0, y0, x1, y1) 四个浮点。

    兼容列表/元组、字典、逗号字符串三种写法：界面从画布上拖出来的框和手打的
    框都走这一个入口，免得前端为不同控件拼不同格式。
    """
    if isinstance(rect_pt, dict):
        values = (rect_pt.get("x0"), rect_pt.get("y0"), rect_pt.get("x1"), rect_pt.get("y1"))
    elif isinstance(rect_pt, str):
        values = rect_pt.replace("，", ",").replace("[", "").replace("]", "").split(",")
    else:
        try:
            values = tuple(rect_pt)
        except TypeError:
            values = None
    if not values or len(values) != 4:
        return None, "裁剪框要给 4 个数字（左上 x、左上 y、右下 x、右下 y，单位 pt）"
    try:
        x0, y0, x1, y1 = (float(v) for v in values)
    except (TypeError, ValueError):
        return None, "裁剪框的 4 个值必须都是数字"
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None, "裁剪框太小（至少 1×1 pt），请检查右下坐标是否真的大于左上坐标"
    return (x0, y0, x1, y1), None


# ========== 表单解析：一件工具一份，真做与预览共用同一套校验 ==========
# 校验只写一遍是有原因的：预览那一路（preview_page_effect）必须和「开始加页码」
# 给同样的脸色——不然预览得好好的，点下去才报「字号要在 6–72 之间」。


def _number_spec(opt):
    """加页码（F14）的表单 → (spec, 错误说明)。校验顺序和原来逐条读 opt 时一致。"""
    place, error = _place(opt, "position", "bottom-center")
    if error:
        return None, error
    size_pt, error = _number(opt.get("font_size"), "字号", 6, 72, 10)
    if error:
        return None, error
    margin, error = _number(opt.get("margin"), "边距", 0, 200, 28.35)
    if error:
        return None, error
    start, error = _number(opt.get("start"), "起始页码", -99999, 99999, 1)
    if error:
        return None, error
    color, error = _color(opt.get("color"), "页码颜色")
    if error:
        return None, error
    # 数字样式要在开文档之前挡掉：罗马数字是页码的「写法」不是「算法」，
    # 拼错了跑到第 30 页才报错，用户已经等了好几秒。
    number_style = str(opt.get("number_style") or "arabic").strip().lower()
    if number_style not in _NUMBER_STYLES:
        return None, (f"不认识的数字样式: {number_style}"
                      f"（可选 {' / '.join(_NUMBER_STYLES)}）")
    return {
        "place": place,
        "template": opt.get("template") or "{page}",
        "start": start,
        "number_style": number_style,
        "font_size": size_pt,
        "margin": margin,
        "color": color,
        "page_range": opt.get("page_range"),
        # 本机字体族名/路径（⑨）；空串 = 内置中文字体，行为与 7.0.3 完全一致
        "font": str(opt.get("font") or "").strip(),
        # 按不按 LaTeX 标记解析这一行：默认关。开着才把 \textbf{}、$\alpha$ 当标记，
        # 关着时模板里出现反斜杠也只是普通字符——水印/页码里写「C:\temp」不该被吃掉。
        "latex_markup": bool(opt.get("latex_markup")),
    }, None


def _hf_spec(opt):
    """页眉页脚（F15）的表单 → (spec, 错误说明)。"""
    header = str(opt.get("header") or "")
    footer = str(opt.get("footer") or "")
    even_header = opt.get("even_header")
    even_footer = opt.get("even_footer")
    if not (header.strip() or footer.strip()
            or str(even_header or "").strip() or str(even_footer or "").strip()):
        return None, "页眉和页脚都是空的，没有可写的内容"

    align_name = str(opt.get("align") or "center").strip().lower()
    align = {"left": 0, "center": 1, "right": 2}.get(align_name)
    if align is None:
        return None, f"不认识的对齐方式: {align_name}"
    size_pt, error = _number(opt.get("font_size"), "字号", 6, 72, 9)
    if error:
        return None, error
    margin, error = _number(opt.get("margin"), "边距", 0, 200, 28.35)
    if error:
        return None, error
    color, error = _color(opt.get("color"), "页眉页脚颜色")
    if error:
        return None, error
    return {
        "header": header, "footer": footer,
        "even_header": even_header, "even_footer": even_footer,
        "align": align, "font_size": size_pt, "margin": margin, "color": color,
        "page_range": opt.get("page_range"),
        # 本机字体族名/路径（⑨），同 _number_spec 的口径
        "font": str(opt.get("font") or "").strip(),
        # 按不按 LaTeX 标记解析，同 _number_spec：默认关，写了反斜杠也不动老行为
        "latex_markup": bool(opt.get("latex_markup")),
    }, None


def _crop_spec(rect_pt):
    """裁剪（F16）的「幅面名或四个数」→ (preset, rect, 错误说明)。"""
    # 空着的 rect_pt 不能当成「不挑幅面」：lookup_size("") 按口径是 A4，
    # 那样用户漏填裁剪框会得到一句「第 1 页比  还小」
    preset = lookup_size(rect_pt) if isinstance(rect_pt, str) and rect_pt.strip() else None
    if preset is not None:
        return preset, None, None
    rect, error = _parse_rect(rect_pt)
    if error:
        if isinstance(rect_pt, str) and rect_pt.strip():
            # 字符串又拼不出 4 个数字，多半是幅面名打错了：报错要指着他打的那个词说，
            # 而不是「要给 4 个数字」——那样用户会以为是界面控件坏了
            return None, None, (f"不认识的裁剪范围「{rect_pt.strip()}」：可以是幅面名"
                                f"（A4/A3/Letter，带「横向」也行），也可以是 4 个 pt 数字")
        return None, None, error
    return None, rect, None


def _scale_spec(target):
    """缩放（F17）的目标 → (幅面尺寸, 百分比因子, 错误说明)，后两个只会有一个有值。"""
    raw = str(target or "").strip()
    size = lookup_size(raw)
    if size is not None:
        return size, None, None
    digits = raw.replace("%", "").replace("％", "").replace(",", ".").strip()
    try:
        percent = float(digits)
    except ValueError:
        return None, None, (f"不认识的缩放目标: {target}"
                            f"（可以是 A4/A3/Letter，也可以是百分比）")
    if not 10 <= percent <= 400:
        return None, None, "按百分比缩放要在 10%–400% 之间"
    return None, percent / 100.0, None


# ========== 单页落笔：真做那条循环里跑的就是这几段，预览也跑这几段 ==========


def _apply_number(fitz, font, page, page_no, total, spec, fonts=None):
    """在这一页上写页码。返回 (是否放得下, 实测宽度)。

    模板里带 LaTeX 标记（`\\textbf{}`、`$\\alpha$` 这类）时走 runs 那条路；
    没标记时一个字都不改地走原来的 `write_line`——回归判据要的是逐字节一致。
    """
    band = spec["font_size"] * LINE_RATIO
    rect = _band_rect(fitz, page.rect, band, spec["margin"], spec["place"][0])
    text = _fill(spec["template"], int(page_no - 1 + spec["start"]), total,
                 spec["number_style"])
    if spec.get("latex_markup") and has_markup(text):
        items, _notes = parse_inline(text)
        if fonts is None:
            fonts, _fn = style_fonts(fitz, spec.get("font"))
        return write_runs_line(fitz, fonts, page, rect, items, spec["font_size"],
                               spec["place"][1], spec["color"])
    return write_line(fitz, font, page, rect, text, spec["font_size"],
                      spec["place"][1], spec["color"])


def _apply_header_footer(fitz, font, page, page_no, total, spec, fonts=None):
    """写一页的页眉和/或页脚；放不下时返回给人看的那句原因，都放下了返回 None。"""
    # 奇偶按物理页序算（第 2、4、6…页算偶数页），和用户翻纸时的单双页直觉一致
    is_even = page_no % 2 == 0
    top_text = (spec["even_header"] if is_even and spec["even_header"] is not None
                else spec["header"])
    bottom_text = (spec["even_footer"] if is_even and spec["even_footer"] is not None
                   else spec["footer"])
    band = spec["font_size"] * LINE_RATIO
    for text, vertical in ((top_text, "top"), (bottom_text, "bottom")):
        if not text:
            continue
        rect = _band_rect(fitz, page.rect, band, spec["margin"], vertical)
        filled = _fill(text, page_no, total)
        # 页眉和页脚占的是页边距里的留白：放不下就报是哪一条，
        # 而不是把字叠到正文上（方案 §4.10 的留白检查）
        if spec.get("latex_markup") and has_markup(filled):
            items, _notes = parse_inline(filled)
            if fonts is None:
                fonts, _fn = style_fonts(fitz, spec.get("font"))
            ok, _width = write_runs_line(fitz, fonts, page, rect, items,
                                         spec["font_size"], spec["align"], spec["color"])
        else:
            ok, _width = write_line(fitz, font, page, rect, filled,
                                    spec["font_size"], spec["align"], spec["color"])
        if not ok:
            where = "页眉" if vertical == "top" else "页脚"
            return f"第 {page_no} 页的{where}放不下，请调大边距或减小字号"
    return None


def _apply_crop(fitz, page, page_no, preset, rect, rect_desc=""):
    """把一页裁到指定幅面或指定框（坐标按屏幕上看到的那一页算）。不合法时返回那句原因。"""
    visible = page.rect
    if preset is not None:
        # 差不到 1 pt 就别当错误：标准 A4 是 595.28×841.89，而扫描件和
        # 很多排版软件写的是 595×842，为 0.28 pt 弹一句「比 A4 还小」没人看得懂
        if visible.width < preset[0] - 1.0 or visible.height < preset[1] - 1.0:
            return (f"第 {page_no} 页比 {rect_desc} 还小，裁不出这个幅面（该页可视范围 "
                    f"{visible.width:.1f}×{visible.height:.1f} pt）")
        width = min(preset[0], visible.width)
        height = min(preset[1], visible.height)
        dx = (visible.width - width) / 2.0
        dy = (visible.height - height) / 2.0
        box = fitz.Rect(visible.x0 + dx, visible.y0 + dy,
                        visible.x0 + dx + width, visible.y0 + dy + height)
    else:
        box = fitz.Rect(*rect)
        if not (box.x0 >= visible.x0 - 0.01 and box.y0 >= visible.y0 - 0.01
                and box.x1 <= visible.x1 + 0.01 and box.y1 <= visible.y1 + 0.01):
            return (f"裁剪框超出第 {page_no} 页（该页可视范围 "
                    f"{visible.width:.1f}×{visible.height:.1f} pt）")
    # 上面放 0.01 pt 容差，是为了不为零点几 pt 的误差弹一句没人看得懂的错；可 MuPDF 写
    # CropBox 时是零容忍的，浮点末位超出就抛 "CropBox not in MediaBox"。而「读取本页尺寸
    # 并填满」填回的是 get_page_size 四舍五入到两位的数（这页真宽 595.29998779，填回 595.3），
    # 于是「填满 → 开始裁剪」这一串最正常的操作会失败，还会把英文报错摆到界面上。
    # 收进可视范围是这里该有的语义：超出部分本来就不可能看得见，不该当成用户的错。
    box.x0 = max(box.x0, visible.x0)
    box.y0 = max(box.y0, visible.y0)
    box.x1 = min(box.x1, visible.x1)
    box.y1 = min(box.y1, visible.y1)
    if page.rotation:
        # derotation_matrix 把「看到的」坐标映射回未旋转的 PDF 内部坐标。
        # 乘完 Rect 会自动归一化，所以不用管反向框。
        box = box * page.derotation_matrix
        # 换算回来再对 MediaBox 取一次交集：MuPDF 校验的是未旋转的那一套坐标，
        # 矩阵乘法自己也会带来浮点末位误差。
        box &= page.mediabox
    try:
        page.set_cropbox(box)
    except ValueError:
        # 走到这里说明这一页自己的页面框就记得不合规矩，不是用户填错了坐标。
        return (f"第 {page_no} 页自身的页面范围记录异常，写不进裁剪框（该页可视范围 "
                f"{visible.width:.1f}×{visible.height:.1f} pt）")
    return None


def _apply_scale(fitz, out, doc, index, size, factor):
    """把 doc 的那一页贴进 out 的一张新纸（等比、四周留白），返回新建的那一页。"""
    visible = doc[index].rect
    width, height = size if size is not None else (visible.width * factor,
                                                   visible.height * factor)
    new_page = out.new_page(width=width, height=height)
    # 满幅贴 + keep_proportion=True：等比适配自动取 min(宽比, 高比)，
    # 比例不合的页面居中留白而不是被压扁
    new_page.show_pdf_page(fitz.Rect(0, 0, width, height), doc, index, keep_proportion=True)
    return new_page


class PageLayoutMixin:
    def get_page_size(self, pdf_path, page_index=0):
        """
        取某一页「看得见的」尺寸（F16/F17 界面用）。

        裁剪框和缩放目标都是 pt，可用户不知道自己的 PDF 到底是 595×842 还是
        612×792：不给这个数，裁剪页就只能盲填，填大了得到一句「裁剪框超出第 1 页」。
        返回的是 page.rect（含 /Rotate 与 CropBox），也就是屏幕上那一页的样子，
        和 crop_pages 校验用的坐标是同一套，两边才不会各说各话。
        """
        import pymupdf as fitz
        try:
            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    return {"success": False, "message": _text('api_pl_encrypted_plain')}
                total = doc.page_count
                if not total:
                    return {"success": False, 'message': _text('api_pdf_empty')}
                try:
                    index = int(page_index)
                except (TypeError, ValueError):
                    index = 0
                index = max(0, min(index, total - 1))
                rect = doc[index].rect
                return {"success": True, "page": index + 1, "page_count": total,
                        "width": round(rect.width, 2), "height": round(rect.height, 2),
                        "rotation": doc[index].rotation}
            finally:
                close_pdf(doc)
        except Exception as e:
            logging.exception("get_page_size 失败")
            return {"success": False, "message": str(e)}

    def add_page_numbers(self, pdf_path, output_path="", opt=None):
        """
        在页面上加页码（F14）。

        opt 支持的键：
          position   九宫格名，默认 bottom-center
          template   文字模板，{page} 是当前页码、{total} 是总页数，默认 "{page}"。
                     「第 {page} 页 / 共 {total} 页」这类样式就是拼出来的
          number_style 数字样式：arabic（默认）/ roman / roman_lower，只换算 {page}，
                     {total} 照旧是阿拉伯数字（整册多少页写成罗马没法数）
          start      第一页显示的数字，默认 1
          font_size  字号 pt，默认 10
          color      文字颜色，#rrggbb 或三个数字，默认黑色
          margin     距边留白 pt，默认 28.35（1 厘米）
          page_range 哪些页加页码，默认全部
          font       本机字体族名（如「微软雅黑」/ "Microsoft YaHei"，可加"|样式"），
                     空 = 内置中文字体；选的字体读不出来会退回内置并在 message 里说明

        页码按物理位置算：只给第 5–8 页加，显示的数字也是 5、6、7、8；
        start 是把整套数字平移（封面不占号就填 0）。
        """
        import pymupdf as fitz
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}
            opt = _as_opt(opt)
            if opt is None:
                return {"success": False, "message": _text('api_pl_number_opt_bad')}

            spec, error = _number_spec(opt)
            if error:
                return {"success": False, "message": error}

            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    close_pdf(doc)
                    return {"success": False, "message": _text('api_pl_number_locked')}
                total = doc.page_count
                pages, error = select_pages(spec["page_range"], total)
                if error:
                    close_pdf(doc)
                    return {"success": False, "message": error}

                font, font_note = make_font_ex(fitz, spec["font"])
                # 样式字体只在整本循环外算一次；模板里没标记就一个都不准备
                _nf, _nnote = (style_fonts(fitz, spec.get("font"))
                               if spec.get("latex_markup") and has_markup(spec["template"])
                               else (None, None))
                # 说明按「填好页码的那一行」取一次：页码本身不影响标记结构，
                # 每页都解析一遍再拼说明只会白白重复几千次
                _sample = _fill(spec["template"], int(spec["start"]), total,
                                spec["number_style"])
                _nnotes = (parse_inline(_sample)[1]
                           if spec.get("latex_markup") and has_markup(_sample) else [])
                for page_no in pages:
                    # page.rect 是「用户看到的那一页」（含 /Rotate 与 CropBox），
                    # 位置按可视方向算，写进去之前再由 text_matrix 换算回内部坐标
                    ok, _width = _apply_number(fitz, font, doc[page_no - 1], page_no,
                                               total, spec, fonts=_nf)
                    if not ok:
                        close_pdf(doc)
                        return {"success": False,
                                "message": _text('api_pl_number_no_room') % page_no}
                subset_fonts(doc)
            except Exception:
                close_pdf(doc)
                raise

            save_pdf_doc(doc, output_path, deflate=True)
            logging.info(f"页码添加完成: {pdf_path} 共 {len(pages)} 页 / 起始 {spec['start']:g} -> {output_path}")
            result = {"success": True, "file": output_path, "pages": len(pages)}
            # 用户点了别的字体却没换成，必须出声（⑨）；没退回就不塞 message
            if font_note:
                result["message"] = _text(font_note)
            # LaTeX 标记里没生效的部分同样要出声（认不出的命令、样式退到自带字体等）
            _markup_msgs = [_text(n['key'], **(n.get('args') or {})) for n in _nnotes]
            if _markup_msgs:
                result["message"] = (result.get("message") or '') + \
                    ('\n' if result.get("message") else '') + '\n'.join(_markup_msgs)
                result["latex_notes"] = [n['key'] for n in _nnotes]
            return result
        except Exception as e:
            logging.exception("add_page_numbers 失败")
            return {"success": False, "message": str(e)}

    def add_header_footer(self, pdf_path, output_path="", opt=None):
        """
        加页眉和/或页脚（F15）。

        opt 支持的键：
          header / footer        文字，支持 {page}、{total}；留空的那项就不加
          even_header / even_footer  偶数页用的文字（不给就和奇数页一样）
          align                  left / center / right，默认 center
          font_size              字号 pt，默认 9
          color                  文字颜色，#rrggbb 或三个数字，默认黑色
          margin                 距边留白 pt，默认 28.35
          page_range             哪些页加，默认全部
          font                   本机字体族名，同 add_page_numbers 的口径

        奇偶按物理页序算（第 2、4、6…页算偶数页），和用户翻纸时的单双页直觉一致。
        """
        import pymupdf as fitz
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}
            opt = _as_opt(opt)
            if opt is None:
                return {"success": False, "message": _text('api_pl_header_opt_bad')}

            spec, error = _hf_spec(opt)
            if error:
                return {"success": False, "message": error}

            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    close_pdf(doc)
                    return {"success": False, "message": _text('api_pl_header_locked')}
                total = doc.page_count
                pages, error = select_pages(spec["page_range"], total)
                if error:
                    close_pdf(doc)
                    return {"success": False, "message": error}

                font, font_note = make_font_ex(fitz, spec["font"])
                _hf_markup = bool(spec.get("latex_markup")) and any(
                    has_markup(spec.get(key)) for key in
                    ("header", "footer", "even_header", "even_footer"))
                _ff, _fnote = (style_fonts(fitz, spec.get("font"))
                               if _hf_markup else (None, None))
                _hsample = _fill(spec.get("header") or spec.get("footer") or '', 1, total)
                _hnotes = parse_inline(_hsample)[1] if _hf_markup else []
                for page_no in pages:
                    error = _apply_header_footer(fitz, font, doc[page_no - 1], page_no,
                                                 total, spec, fonts=_ff)
                    if error:
                        close_pdf(doc)
                        return {"success": False, "message": error}
                subset_fonts(doc)
            except Exception:
                close_pdf(doc)
                raise

            save_pdf_doc(doc, output_path, deflate=True)
            logging.info(f"页眉页脚添加完成: {pdf_path} 处理 {len(pages)} 页 -> {output_path}")
            result = {"success": True, "file": output_path, "pages": len(pages)}
            if font_note:
                result["message"] = _text(font_note)
            _markup_msgs = [_text(n['key'], **(n.get('args') or {})) for n in _hnotes]
            if _markup_msgs:
                result["message"] = (result.get("message") or '') + \
                    ('\n' if result.get("message") else '') + '\n'.join(_markup_msgs)
                result["latex_notes"] = [n['key'] for n in _hnotes]
            return result
        except Exception as e:
            logging.exception("add_header_footer 失败")
            return {"success": False, "message": str(e)}

    def crop_pages(self, pdf_path, output_path="", rect_pt=None, page_range=""):
        """
        裁剪页面（F16）：只改 CropBox，不动内容流。

        裁掉的区域内容流一个字节都没少，把 CropBox 放回 MediaBox 就又看得见；但正常
        阅读器和「复制文字」都按 CropBox 走，页面上就是看不见了——所以界面要写清：
        要真删请用涂黑（F18），别指望裁剪保密。这句话代码里也留着，免得后来人
        「顺手优化」成删内容。

        rect_pt 是用户看到的坐标（pt），列表/字典/逗号字符串都认；
        也可以直接给幅面名（"A4"、"A4横向"）按那个尺寸居中裁。
        旋转页要先把可视坐标换算回 PDF 内部坐标，否则裁出来的位置和屏幕上差 90°。
        """
        import pymupdf as fitz
        started = time.perf_counter()
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}

            preset, rect, error = _crop_spec(rect_pt)
            if error:
                return {"success": False, "message": error}

            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    close_pdf(doc)
                    return {"success": False, "message": _text('api_pl_crop_locked')}
                pages, error = select_pages(page_range, doc.page_count)
                if error:
                    close_pdf(doc)
                    return {"success": False, "message": error}

                for page_no in pages:
                    # 报错里那句「比 A4 还小」用的是用户自己填的那个词，原样带回去
                    error = _apply_crop(fitz, doc[page_no - 1], page_no, preset, rect,
                                        str(rect_pt))
                    if error:
                        close_pdf(doc)
                        return {"success": False, "message": error}
            except Exception:
                close_pdf(doc)
                raise

            save_pdf_doc(doc, output_path, garbage=3, deflate=True)
            size_bytes = os.path.getsize(output_path)
            logging.info(f"页面裁剪完成: {pdf_path} 裁了 {len(pages)} 页 -> {output_path} "
                         f"{size_bytes} 字节 / {time.perf_counter() - started:.2f}s")
            return {"success": True, "file": output_path, "pages": len(pages)}
        except Exception as e:
            logging.exception("crop_pages 失败")
            return {"success": False, "message": str(e)}

    def scale_pages(self, pdf_path, output_path="", target="A4", page_range=""):
        """
        把页面统一到目标幅面，或按比例缩放（F17）。

        target 是幅面名（"A4"/"A3"/"Letter"/"A4横向"）时新建那种纸，原页等比贴进去、
        四周留白——拉伸填满会把版面弄变形，所以不给这个选项（方案 §4.10）。
        target 是数字时按百分比缩放（"150" 和 "150%" 都是放大 1.5 倍）。

        贴图用 show_pdf_page 而不是改写原内容流：原页的字号、路径、图片编码一个
        字节都不动，只是换了张纸放上去。旋转页它自己会摆正（实测）。

        page_range 只决定「哪些页重排」，范围外的页原样搬进成品——用户勾「只缩放第
        2–5 页」是想改这几页，不是想把别的页删掉（和裁剪同一口径）。
        """
        import pymupdf as fitz
        started = time.perf_counter()
        try:
            if not output_path:
                return {"success": False, 'message': _text('api_need_save_path')}

            size, factor, error = _scale_spec(target)
            if error:
                return {"success": False, "message": error}

            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    close_pdf(doc)
                    return {"success": False, "message": _text('api_pl_scale_locked')}
                pages, error = select_pages(page_range, doc.page_count)
                if error:
                    close_pdf(doc)
                    return {"success": False, "message": error}

                total = doc.page_count
                picked = set(pages)
                # 把整册切成「连续要缩放的段」和「连续原样保留的段」：后者用一次
                # insert_pdf 搬过去，逐页 insert_pdf 的话 3000 页里有 2999 页没选中时
                # 会一页一个调用地慢（3000 次），分块只要两三次
                runs = []
                index = 1
                while index <= total:
                    hit = index in picked
                    end = index
                    while end + 1 <= total and ((end + 1) in picked) == hit:
                        end += 1
                    runs.append((index, end, hit))
                    index = end + 1

                out = fitz.open()
                try:
                    for first, last, scaling in runs:
                        if not scaling:
                            out.insert_pdf(doc, from_page=first - 1, to_page=last - 1)
                            continue
                        for page_no in range(first, last + 1):
                            _apply_scale(fitz, out, doc, page_no - 1, size, factor)
                    out_pages = out.page_count
                except Exception:
                    close_pdf(out)
                    raise
                # 先把源文档关掉再存：insert_pdf / show_pdf_page 已经把要用的对象整个复制进
                # out 了，源文件不再被需要。而「存回原文件」这一路在 Windows 上要求源文件
                # 没被占用（见 api/_io.py 的 save_pdf_doc），不关就覆盖安装不了用户自己的文件。
                close_pdf(doc)
                # 只 deflate、不做 garbage：成品是刚重建出来的，本来就没有无用对象，
                # garbage=3 的「找重复对象」在 3000 页上要 19.02s，还省不出体积
                # （实测 2,449,623 字节，比不整理的 2,089,743 还大）。
                # source=pdf_path：另存为指回原文件时，out 正握着那个文件的句柄，
                # 必须走临时文件 + 原子替换，否则 Windows 上直接写回会 Permission denied。
                save_pdf_doc(out, output_path, source=pdf_path, deflate=True)
            except Exception:
                close_pdf(doc)
                raise

            size_bytes = os.path.getsize(output_path)
            logging.info(f"页面缩放完成: {pdf_path} 目标={target} 缩放 {len(pages)} 页 / 共 {total} 页 -> "
                         f"{output_path} {size_bytes} 字节 / {time.perf_counter() - started:.2f}s")
            return {"success": True, "file": output_path, "pages": out_pages,
                    "scaled": len(pages)}
        except Exception as e:
            logging.exception("scale_pages 失败")
            return {"success": False, "message": str(e)}

    def preview_page_effect(self, tool, pdf_path, page_index=0, opt=None, dpi=120):
        """
        单页效果预览（F14–F17）：把某一页按当前表单改一遍再出图，只出图、不落盘。

        tool 取 number / header / crop / scale，对应上面那四件工具；opt 用各自工具
        原来那套字段名（裁剪给 rect_pt、缩放给 target，都装在 opt 里），前端不必拼两种表单。

        为什么值得单独走一路：这四件工具改的都是「页面上看不见摸不着」的东西——
        页码会不会被正文压住、页眉够不够宽、裁剪框裁掉了哪几个字、换幅面后四周留白多少，
        光看数字填不出来。预览走的是和真做同一段落笔代码（_apply_number 等），
        所以「预览里看到的样子」和「点开始之后得到的样子」不会是两套。

        只做一页、且改的是内存里的文档句柄：不写临时文件、不调 save、原文件一个字节不动。
        """
        import pymupdf as fitz
        from .viewer import _clamp_dpi, _encode_page
        try:
            if not pdf_path or not os.path.exists(pdf_path):
                return {"success": False, "message": _text('api_pl_pick_pdf')}
            name = str(tool or "").strip().lower()
            if name not in ("number", "header", "crop", "scale"):
                return {"success": False, "message": _text('api_pl_tool_unknown') % tool}
            opt = _as_opt(opt)
            if opt is None:
                return {"success": False, "message": _text('api_pl_preview_opt_bad')}

            if name == "number":
                spec, error = _number_spec(opt)
                page_range = spec["page_range"] if spec else opt.get("page_range")
            elif name == "header":
                spec, error = _hf_spec(opt)
                page_range = spec["page_range"] if spec else opt.get("page_range")
            elif name == "crop":
                preset, rect, error = _crop_spec(opt.get("rect_pt"))
                spec = {"preset": preset, "rect": rect}
                page_range = opt.get("page_range")
            else:
                size, factor, error = _scale_spec(opt.get("target"))
                spec = {"size": size, "factor": factor}
                page_range = opt.get("page_range")
            if error:
                return {"success": False, "message": error}

            dpi = _clamp_dpi(dpi, 120)
            try:
                index = int(page_index)
            except (TypeError, ValueError):
                index = 0

            doc = fitz.open(pdf_path)
            out = None
            try:
                if doc.needs_pass:
                    return {"success": False, "message": _text('api_pl_preview_locked')}
                total = doc.page_count
                if not total:
                    return {"success": False, 'message': _text('api_pdf_empty')}
                index = max(0, min(index, total - 1))
                page_no = index + 1

                pages, error = select_pages(page_range, total)
                if error:
                    return {"success": False, "message": error}
                # 页码范围是这件工具的一部分，预览必须跟着它：
                # 用户填了「只给第 5–8 页加页码」却在看第 2 页，那这一页就该是原样——
                # 否则预览骗他说没页码，真做时第 2 页确实也没页码，两头对不上更难懂
                in_range = page_no in pages
                note = "" if in_range else f"第 {page_no} 页不在页码范围里，这一页不会有变化"

                target_page = doc[index]
                font_note = ""
                if in_range:
                    if name == "number":
                        font, note_key = make_font_ex(fitz, spec["font"])
                        ok, _width = _apply_number(fitz, font, target_page,
                                                   page_no, total, spec)
                        if not ok:
                            note = (f"第 {page_no} 页在这个位置放不下页码，"
                                    f"请调大边距或换个位置")
                        elif note_key:
                            font_note = _text(note_key)
                    elif name == "header":
                        font, note_key = make_font_ex(fitz, spec["font"])
                        note = _apply_header_footer(fitz, font, target_page,
                                                    page_no, total, spec) or ""
                        if not note and note_key:
                            font_note = _text(note_key)
                    elif name == "crop":
                        note = _apply_crop(fitz, target_page, page_no, spec["preset"],
                                           spec["rect"], str(opt.get("rect_pt"))) or ""
                    else:
                        # 缩放换的是「纸」，改不了原来那一页本身：贴进一张临时新文档，
                        # 出图从那一张纸上出。临时文档只活到这一次调用结束。
                        out = fitz.open()
                        target_page = _apply_scale(fitz, out, doc, index,
                                                   spec["size"], spec["factor"])

                rect = target_page.rect
                return {"success": True, "tool": name, "image": _encode_page(target_page, dpi),
                        "page": page_no, "page_count": total,
                        "width": round(rect.width, 2), "height": round(rect.height, 2),
                        "applied": in_range and not note,
                        # 字体没换成不是「没应用」，但预览也要出声：note 空着才放这句
                        "note": note or font_note, "dpi": dpi}
            finally:
                if out is not None:
                    close_pdf(out)
                close_pdf(doc)
        except Exception as e:
            logging.exception(f"preview_page_effect 失败: {tool} {pdf_path} page={page_index}")
            return {"success": False, "message": str(e)}
