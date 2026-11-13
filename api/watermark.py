import base64
import io
import logging

from ._io import close_pdf, save_pdf_doc
from core.i18n import t as _text

# 预览和成品必须走同一套画法，否则「水印预览」和真正导出的 PDF 会变成两套逻辑早晚对不上。
# 前端只负责说「多大、多密」，像素→pt 的几何全部在这里算。
DEFAULT_FONT_SIZE = {'diagonal': 20, 'centered': 40}
DEFAULT_SPACING = 200
FONT_SIZE_RANGE = (8, 120)
SPACING_RANGE = (40, 500)


def _clamped(value, low, high, default):
    """把前端传来的数字收进安全区间；非法值退回默认，不抛异常。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return int(max(low, min(high, number)))


def _rgb(color_hex):
    hex_str = str(color_hex or "").lstrip('#')
    if len(hex_str) >= 6:
        try:
            return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))
        except ValueError:
            return (255, 0, 0)
    return (255, 0, 0)


def _alpha(opacity):
    try:
        opacity = float(opacity)
    except (TypeError, ValueError):
        opacity = 0.3
    return int(max(0.0, min(1.0, opacity)) * 255)


def _params(diagonal, font_size, spacing):
    """字号和间距都没传时，沿用原来的默认（对角线 20pt / 居中 40pt / 间距 200pt）。"""
    size_default = DEFAULT_FONT_SIZE['diagonal'] if diagonal else DEFAULT_FONT_SIZE['centered']
    return (_clamped(font_size, FONT_SIZE_RANGE[0], FONT_SIZE_RANGE[1], size_default),
            _clamped(spacing, SPACING_RANGE[0], SPACING_RANGE[1], DEFAULT_SPACING))


def _build_watermark_png(text, rgb, alpha, font_size, diagonal, font=None, latex=False):
    r"""文字画成带透明通道的 PNG，对角线时整张转 45 度。返回 (字节, 宽, 高, 退回说明键)。

    font 是界面选的本机字体引用（族名/路径）；None 走原来那三级兜底。
    要它和 apply_watermark 用同一张字体，预览才会和成品一致。

    `latex` 是界面上那个「按 LaTeX 标记解析」的开关：**默认关**。
    不默认开的原因很实在——水印里出现反斜杠太常见（`C:\\temp`、日期里的 `\`），
    自动当标记会把用户的字吃掉；要标记效果请显式勾上。
    """
    from core.latex_inline import has_markup

    if latex and has_markup(text):
        # 带 LaTeX 标记的行另走一条绘制路；没开开关时一个字节都不改，老成品能逐字节对上
        return _build_watermark_png_runs(text, rgb, alpha, font_size, diagonal, font)
    from PIL import Image, ImageDraw
    from core.fonts import load_pil_font

    font_obj, font_note = load_pil_font(font_size, font)
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font_obj)
    tw = max(1, bbox[2] - bbox[0])
    th = max(1, bbox[3] - bbox[1])

    pad = 6
    txt_img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(txt_img).text((pad - bbox[0], pad - bbox[1]), text,
                                 font=font_obj, fill=(rgb[0], rgb[1], rgb[2], alpha))

    if diagonal:
        txt_img = txt_img.rotate(45, expand=True, resample=Image.BICUBIC)

    buf = io.BytesIO()
    txt_img.save(buf, format="PNG")
    return buf.getvalue(), txt_img.size[0], txt_img.size[1], font_note


def _styles_used(items):
    """这串 run 里用户**点名要过**哪些样式（分式/根号里的子串也要算进来）。

    数学段（`math` 标记）不计入：`$\\alpha$` 要的是那个希腊字母，不是斜体面，
    拿它去报「这台机器的字体没有斜体」属于瞎嚷嚷。
    """
    out = set()
    for it in items or []:
        if it.get('k') == 'txt' and not it.get('math'):
            out.add(it.get('style') or 'rm')
        for key in ('num', 'den', 'body'):
            if it.get(key):
                out |= _styles_used(it[key])
    return out


def _pil_style_faces(size, ref, used=None):
    """水印这一路的样式字体。

    PIL 没有 Base-14 那种「换一支自带面」的退路，所以：
      粗体 → 先试用户字体族里的 Bold 样式；没有就同一支字重画一遍偏移 0.6 px（真看得出粗）；
      斜体 / 等宽 / 无衬线 → 试族内对应样式，试不到就按原字体画，并记一条
      `api_latex_pil_style_limited` 说明——**不假装斜体成功了**。

    `used` 是这行文字里真的出现过的样式集合：没用到斜体却报一句「斜体没生效」
    属于瞎嚷嚷，会把用户的注意力带偏。
    """
    from core.fonts import load_pil_font

    base, note = load_pil_font(size, ref)
    notes = [note] if note else []
    faces = {'rm': base, 'cjk': base, 'bf': base, 'it': base, 'tt': base, 'sf': base}
    used = set(used or ())
    ref_clean = str(ref or '').strip().split('|')[0]
    want = {'bf': ('Bold', 'Heavy', 'Black'), 'it': ('Italic', 'Oblique'),
            'tt': ('Regular',), 'sf': ('Regular',)}
    limited = False
    for style, names in want.items():
        face = None
        if ref_clean:
            for name in names:
                cand, cand_note = load_pil_font(size, '%s|%s' % (ref_clean, name))
                if cand_note is None:
                    face = cand
                    break
        if face is not None and face is not base:
            faces[style] = face
            continue
        # 拿不到对应样式：粗体还能靠偏移重画看出区别，斜体/等宽不行，就得如实说
        faces[style] = base
        if style in ('it', 'tt', 'sf') and style in used:
            limited = True
    if limited:
        notes.append('api_latex_pil_style_limited')
    return faces, notes


def _build_watermark_png_runs(text, rgb, alpha, font_size, diagonal, font=None):
    """带 LaTeX 标记的水印文字：按 run 画到透明底图上，再走原来的贴图流程。"""
    from PIL import Image, ImageDraw

    from core.latex_inline import parse as parse_inline

    items, notes = parse_inline(text)
    faces, face_notes = _pil_style_faces(font_size, font, _styles_used(items))
    fill = (rgb[0], rgb[1], rgb[2], alpha)
    scale = 0.72

    def face_of(it):
        return faces.get(it.get('style') or 'rm', faces['rm'])

    def seg_w(font_obj, value, sz):
        box = font_obj.getbbox(value)
        return box[2] - box[0]

    def measure(nodes, sz):
        """(宽, 基线以上高, 基线以下深)。"""
        w = 0.0
        hi = sz * 0.78
        lo = sz * 0.25
        for it in nodes:
            k = it.get('k')
            if k == 'txt':
                sz2 = sz * (scale if it.get('shift') else 1.0)
                w += seg_w(face_of(it), it.get('v') or '', sz2)
                if it.get('shift') == 'sup':
                    hi = max(hi, sz * 0.45 + sz2 * 0.78)
                elif it.get('shift') == 'sub':
                    lo = max(lo, sz * 0.25 + sz2 * 0.6)
            elif k == 'space':
                w += (it.get('em') or 0.0) * sz
            elif k == 'frac':
                nw, nh, nd = measure(it.get('num') or [], sz * 0.85)
                dw, dh, dd = measure(it.get('den') or [], sz * 0.85)
                w += max(nw, dw) + sz * 0.24
                hi = max(hi, sz * 0.3 + nh + nd)
                lo = max(lo, dd + dh)
            elif k == 'sqrt':
                bw, bh, bd = measure(it.get('body') or [], sz)
                w += seg_w(faces['rm'], '√', sz) + bw
                hi = max(hi, bh + sz * 0.15)
                lo = max(lo, bd)
        return w, hi, lo

    total_w, ascent, descent = measure(items, font_size)
    pad = 6
    img = Image.new("RGBA", (int(total_w) + pad * 2, int(ascent + descent) + pad * 2),
                    (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    baseline = pad + ascent

    def paint(nodes, x0, sz, dy):
        x = x0
        for it in nodes:
            k = it.get('k')
            if k == 'txt':
                value = it.get('v') or ''
                if not value:
                    continue
                shift = it.get('shift')
                sz2 = sz * (scale if shift else 1.0)
                font_obj = face_of(it)
                ty = dy + (-sz * 0.45 if shift == 'sup'
                           else sz * 0.22 if shift == 'sub' else 0.0)
                start = x
                # 假粗：同一支字偏移再画一次，比悄悄换成别的字体更诚实
                draw.text((x, baseline + ty), value, font=font_obj, fill=fill)
                if it.get('style') == 'bf' and font_obj is faces['rm']:
                    draw.text((x + 0.6, baseline + ty), value, font=font_obj, fill=fill)
                w = seg_w(font_obj, value, sz2)
                x += w
                if it.get('ul'):
                    yy = baseline + ty + sz * 0.22
                    draw.line((start, yy, x, yy), fill=fill, width=max(1, int(sz * 0.05)))
                if it.get('ol'):
                    yy = baseline + ty - sz * 0.8
                    draw.line((start, yy, x, yy), fill=fill, width=max(1, int(sz * 0.05)))
            elif k == 'space':
                x += (it.get('em') or 0.0) * sz
            elif k == 'frac':
                num = it.get('num') or []
                den = it.get('den') or []
                nw = measure(num, sz * 0.85)[0]
                dw = measure(den, sz * 0.85)[0]
                box_w = max(nw, dw) + sz * 0.24
                axis = sz * 0.26
                gap = sz * 0.85 * 0.18
                paint(num, x + (box_w - nw) / 2.0, sz * 0.85, dy - axis - gap
                      - measure(num, sz * 0.85)[1])
                paint(den, x + (box_w - dw) / 2.0, sz * 0.85, dy + axis + gap)
                yy = baseline + dy - axis
                draw.line((x + sz * 0.06, yy, x + box_w - sz * 0.06, yy), fill=fill,
                          width=max(1, int(sz * 0.05)))
                x += box_w
            elif k == 'sqrt':
                body = it.get('body') or []
                bw, bh, bd = measure(body, sz)
                rad_w = seg_w(faces['rm'], '√', sz)
                draw.text((x, baseline + dy), '√', font=faces['rm'], fill=fill)
                paint(body, x + rad_w, sz, dy)
                yy = baseline + dy - bh - sz * 0.08
                draw.line((x + rad_w - sz * 0.04, yy, x + rad_w + bw, yy), fill=fill,
                          width=max(1, int(sz * 0.045)))
                x += rad_w + bw
        return x

    paint(items, pad, font_size, 0.0)
    if diagonal:
        img = img.rotate(45, expand=True, resample=Image.BICUBIC)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    # 一次只回一条说明键（调用方那格 message 就一个位置）：字体没换成比标记没生效更要紧
    note = (face_notes[0] if face_notes
            else (notes[0]['key'] if notes else None))
    return buf.getvalue(), img.size[0], img.size[1], note


def _stamp_on_page(page, img_bytes, img_w, img_h, diagonal, spacing):
    """按平铺网格（对角线）或正中（单张）把水印图摆到页面上，单位都是 pt。"""
    import pymupdf as fitz

    w, h = page.rect.width, page.rect.height
    if diagonal:
        for x in range(60, int(w) + spacing, spacing):
            for y in range(60, int(h) + spacing, spacing):
                rect = fitz.Rect(x - img_w / 2, y - img_h / 2,
                                 x + img_w / 2, y + img_h / 2)
                page.insert_image(rect, stream=img_bytes, alpha=True)
    else:
        rect = fitz.Rect((w - img_w) / 2, (h - img_h) / 2,
                         (w + img_w) / 2, (h + img_h) / 2)
        page.insert_image(rect, stream=img_bytes, alpha=True)


class WatermarkMixin:
    def apply_watermark(self, pdf_path, output_path, text, color_hex, opacity, diagonal,
                        font_size=None, spacing=None, font=None, latex=False):
        """
        文字水印（F9）。font 是界面选的本机字体引用（族名/路径，见 core/fonts.py），
        不传就走和以前一样的内置兜底；选了却读不出来会退回兜底并在 message 里说明。
        latex=True 才把文字按 LaTeX 标记解析（`\textbf{}`、`$\alpha$` 那一小套）。
        """
        import pymupdf as fitz
        try:
            text = str(text or "").strip()
            if not text:
                return {"success": False, 'message': _text('api_need_watermark_text')}

            font_size, spacing = _params(diagonal, font_size, spacing)
            img_bytes, img_w, img_h, font_note = _build_watermark_png(
                text, _rgb(color_hex), _alpha(opacity), font_size, bool(diagonal), font,
                bool(latex))

            doc = fitz.open(pdf_path)
            if doc.page_count == 0:
                close_pdf(doc)
                return {"success": False, 'message': _text('api_pdf_empty')}

            try:
                for page in doc:
                    _stamp_on_page(page, img_bytes, img_w, img_h, diagonal, spacing)
            except Exception:
                close_pdf(doc)
                raise

            save_pdf_doc(doc, output_path)

            logging.info(f"水印应用完成: {output_path} (字号 {font_size}pt, 间距 {spacing}pt)")
            if font_note:
                return {"success": True, "message": _text(font_note)}
            return {"success": True}
        except Exception as e:
            logging.exception("apply_watermark 失败")
            return {"success": False, "message": str(e)}

    def preview_watermark(self, pdf_path, text, color_hex, opacity, diagonal,
                          font_size=None, spacing=None, page_index=0, dpi=80,
                          font=None, latex=False):
        """
        把水印按同一套画法打在某一页（默认首页）上再渲染成 PNG，给前端当预览。

        只渲染、不写盘：预览要的是「导出后这页长什么样」，不是真的改文件。
        失败返回 success=False，前端据此显示提示而不是干等。
        font 和 apply_watermark 同一口径——预览和成品必须用同一张字体，不然
        「预览里挑的字体」和「导出来的水印」两套样子早晚对不上。
        """
        import pymupdf as fitz
        try:
            text = str(text or "").strip()
            if not text:
                return {"success": False, 'message': _text('api_need_watermark_text')}

            font_size, spacing = _params(diagonal, font_size, spacing)
            img_bytes, img_w, img_h, font_note = _build_watermark_png(
                text, _rgb(color_hex), _alpha(opacity), font_size, bool(diagonal), font,
                bool(latex))

            doc = fitz.open(pdf_path)
            try:
                total = doc.page_count
                try:
                    index = int(page_index)
                except (TypeError, ValueError):
                    index = 0
                if total == 0:
                    return {"success": False, 'message': _text('api_pdf_empty')}
                if index < 0 or index >= total:
                    return {"success": False, "message": _text('api_wm_page_range')}

                page = doc[index]
                _stamp_on_page(page, img_bytes, img_w, img_h, diagonal, spacing)
                try:
                    dpi = int(dpi)
                except (TypeError, ValueError):
                    dpi = 80
                pix = page.get_pixmap(dpi=max(30, min(dpi, 200)))
                result = {"success": True,
                          "image": base64.b64encode(pix.tobytes("png")).decode("utf-8"),
                          "page_index": index,
                          "page_count": total, "font_size": font_size, "spacing": spacing}
                if font_note:
                    result["message"] = _text(font_note)
                return result
            finally:
                close_pdf(doc)
        except Exception:
            logging.exception("preview_watermark 失败")
            return {"success": False, "message": _text('api_wm_preview_failed')}
