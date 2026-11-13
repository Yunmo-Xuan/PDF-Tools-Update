"""把「按 span id 改文字」这最小一坨改动写回 PDF，写出**新文件**。

这是 core/page_model.py 的下游：前端拿页面模型点亮一句字、改完把
`{span_id: 新文本}` 递回来，这里负责去掉旧字、写上新字、再重新打开数一遍。

四条口径（写代码前先读，都是这个项目的老规矩）：

1. **绝不原地覆盖用户打开的那份**。输出路径和被打开的文件是同一个就当场拒绝
   （起名字是 api 层的事，按项目既有习惯 `<原名>_编辑.pdf` + 撞名加序号）。
2. **去旧字走「真删」而不是只盖白块**。`add_redact_annot` + `apply_redactions(
   images=NONE, graphics=LINE_ART_NONE)` 只把压在框里的**字**从内容流里去掉，
   底下的图片和框线一根都不动。为什么不拿「白矩形盖住」当主力：盖住的写法旧字
   还留在文件里——读回来是两句、搜索搜得到、复制出来还是老字，
   「接口报成功但其实什么都没改」正是这个项目反复提防的那类假成功。
   白漆只在「这块底下干净（没图没矢量）」时补上（redact 的 `fill=(1,1,1)`；
   本机实测 fill=None/False 完全不刷漆、fill=(1,1,1) 才刷白）。
   底下有图或有矢量就 `fill=False` 不刷漆并在返回里标 `risk`：
   扫描件上那句 OCR 字层底下压的就是原稿墨迹，白漆一盖就把人家的扫描图啃掉一块。
   涂黑这一步万一引擎不认（异常/返回 False），才退回「白块盖住」，
   并且照样标 risk——旧字还在文件里这件事必须让人知道。
3. **写完必须读回核对**：重新打开成品，逐条确认新文本在原位置、旧文本没了、
   这一页其余 span 一个字没变、页数没变。任何一条不满足就 `verified: False`
   并如实回 `verify_mismatch`，不许用 success 糊弄。
4. **只用项目里已有的内置字体**（helv/hebo/tiro/… 与 china-s/china-ss/japan/korea）。
   本机字体接入是另一条线，这里不做字体枚举。写之前先 `Font.has_glyph` 逐字量一遍，
   内置字体盖不住的字符宁可不写、如实报错，也绝不静默掉字
   （api/ocr.py 的 _layer_font 注释记的就是这个坑）。

坐标：本文件全部在**未转角度那一套坐标**里操作（`core/page_model.py` 内部记录 `_raw`）。
PyMuPDF 的 get_text / get_image_info / get_cdrawings 给的是这套，
add_redact_annot / insert_text / draw_rect 认的也是这套，两边自洽；
对外的渲染坐标只在模型层转一次，绝不混用。
"""

import logging
import os
import re
import tempfile
import time
from collections import Counter

import pymupdf

from core.page_model import (
    _int_color_to_hex,
    collect_page,
    locate_span,
    parse_span_id,
)

# 抽取到的字体名 → PyMuPDF 内置写入名。只有这一份小表，不做本机字体枚举。
# 值都是本机实测 insert_text 写得进、get_text 读得回的那几套
# （tiiu/cobl/coob/zapd 这些内置名在本版 PyMuPDF 要额外字体文件，写不进去，故不收）。
# 内置 CJK 三套抽出来分别叫 Heiti / Gothic / Dotum（本机实测 china-s 与 china-ss 同名）。
_BUILTIN_BY_FONT = {
    'helvetica': 'helv',
    'arial': 'helv',
    'helvetica-bold': 'hebo',
    'arial-bold': 'hebo',
    'times-roman': 'tiro',
    'times-new-roman': 'tiro',
    'times-bold': 'tibo',
    'times-new-roman-bold': 'tibo',
    'times-italic': 'titi',
    'times-bold-italic': 'tibi',
    'courier': 'cour',
    'courier-oblique': 'coit',
    'symbol': 'symb',
    'zapfdingbats': 'zadb',
    'heiti': 'china-s',
    'gothic': 'japan',
    'dotum': 'korea',
}
_LATIN_BUILTINS = {'helv', 'hebo', 'tiro', 'titi', 'tibi', 'cour', 'coit', 'symb', 'zadb'}
_WIDE_BUILTINS = {'china-s', 'china-ss', 'japan', 'korea'}
# 判「这句要不要用宽字体」：正则与 api/ocr.py 同一套口径
_KANA = re.compile(r'[\u3040-\u30ff\u31f0-\u31ff]')
_HANGUL = re.compile(r'[\uac00-\ud7af\u1100-\u11ff]')
_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
# 读回核对的位置容差（pt）：新写的字中心就该落在原 span 的框里，超出算写歪
VERIFY_TOLERANCE = 3.0
# fitz.Font() 每次都要把字库读进内存，按名字缓存一份（api/_text.make_font 同理）
_FONTS = {}


def _font_of(name):
    font = _FONTS.get(name)
    if font is None:
        font = _FONTS[name] = pymupdf.Font(name)
    return font


def _int_color_to_rgb(value):
    """span 的 color（int，如 0xff0000）→ insert_text 要的 (r,g,b) 浮点。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    return (((number >> 16) & 255) / 255.0, ((number >> 8) & 255) / 255.0,
            (number & 255) / 255.0)


def _error(code, **detail):
    result = {'success': False, 'error': code}
    if detail:
        result['detail'] = detail.get('detail', detail)
    return result


def missing_glyphs(fontname, text):
    """这套内置字体盖不住的字符（去重、按出现顺序）。空列表表示全都写得下。"""
    try:
        font = _font_of(fontname)
    except Exception:
        logging.exception('page_edit: 内置字体 %s 取不到', fontname)
        return sorted(set(str(text or '')))
    out = []
    for ch in str(text or ''):
        if ch in '\r\n\t \u3000':
            continue
        try:
            if font.has_glyph(ord(ch)):
                continue
        except (TypeError, ValueError):
            continue
        if ch not in out:
            out.append(ch)
    return out


def pick_font(text, orig_font='', flags=0):
    """给这句新文本挑一套写得进去的内置字体，返回 `(fontname, substituted, missing)`。

    * `substituted=True`：没能沿用原 span 那套字体（原文是嵌入子集字体、或按脚本换了
      一套宽字体）。成品里字的样子会和邻居不一样，这是实话，必须让前端看见。
    * `missing` 非空：连兜底的宽字体都盖不住这些字符。**这一处编辑要拒绝**——
      写下去就是「文件正常、字不见了」那种没人能当场发现的假成功。
    """
    wanted = _BUILTIN_BY_FONT.get(str(orig_font or '').strip().lower().replace(' ', '-'))
    if _KANA.search(text or ''):
        wide = 'japan'
    elif _HANGUL.search(text or ''):
        wide = 'korea'
    elif _CJK.search(text or ''):
        wide = 'china-s'
    else:
        wide = ''

    if wide:
        first = wanted if wanted in _WIDE_BUILTINS else wide
        candidates = [first, 'china-s', 'japan', 'korea']
    elif wanted in _LATIN_BUILTINS:
        candidates = [wanted, 'helv', 'china-s']
    else:
        # 原文是嵌入子集字体（形如 ABCDEF+SimHei）或认不出的名字：按 flags 猜一套最接近的
        bold = bool(int(flags or 0) & 16)
        serif = bool(int(flags or 0) & 4)
        candidates = ['hebo' if bold else 'helv', 'tibo' if serif else 'tiro', 'helv', 'china-s']

    for name in candidates:
        if name not in _LATIN_BUILTINS and name not in _WIDE_BUILTINS:
            continue
        if not missing_glyphs(name, text):
            return name, (wanted is None or name != wanted), []
    fallback = candidates[0]
    return fallback, True, missing_glyphs(fallback, text)


def _normalize(edits):
    """把改动集收成 `[(span_id, 新文本)]`；形状不对就地给错误码，绝不自作主张猜。

    两种写法都收：`{span_id: 新文本}`（要求的最小改动集）和
    `[{'id':…, 'text':…}]`（前端从模型直接映射出来的数组，顺序有意义）。
    新文本允许空串 = 删掉这句。
    """
    pairs = []
    if isinstance(edits, dict):
        for key, value in edits.items():
            if value is None or isinstance(value, (dict, list, tuple)):
                return None, 'bad_edits'
            pairs.append((str(key), str(value)))
    elif isinstance(edits, (list, tuple)):
        for item in edits:
            if not isinstance(item, dict) or 'id' not in item:
                return None, 'bad_edits'
            value = item.get('text')
            if value is None or isinstance(value, (dict, list, tuple)):
                return None, 'bad_edits'
            pairs.append((str(item['id']), str(value)))
    else:
        return None, 'bad_edits'
    return pairs, None


def _same_file(a, b):
    """两个路径是不是同一个文件（大小写、相对/绝对写法的差别都不算）。"""
    try:
        return (os.path.normcase(os.path.abspath(str(a)))
                == os.path.normcase(os.path.abspath(str(b))))
    except (TypeError, ValueError, OSError):
        return False


def _save(doc, output_path, password, was_locked):
    """写新文件：临时文件 + 原子替换（api/_io.save_pdf_doc 同一口径，但不反向依赖 api 层）。

    源文件是加密的、且我们用口令解开了 → 成品按同一口令重新加密回去：
    不能出现「改一句字顺手把人家密码剥了」。口令只有一个时用户口令=拥有者口令
    （api/encrypt.py 就是这个约定）。
    """
    target = os.path.abspath(str(output_path))
    directory = os.path.dirname(target) or os.getcwd()
    fd, tmp = tempfile.mkstemp(suffix='.tmp', dir=directory)
    os.close(fd)
    kwargs = {}
    if was_locked and password:
        kwargs.update({'encryption': pymupdf.PDF_ENCRYPT_AES_256,
                       'user_pw': str(password), 'owner_pw': str(password),
                       'permissions': doc.permissions})
    try:
        doc.save(tmp, **kwargs)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    try:
        os.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return target


def _spans_of_collected(collected):
    """从内部记录里拿 (文本, 未转 bbox, 未转基线起点)——计数与读回核对都用它，省一遍解析。"""
    rows = []
    for block in collected['text_blocks']:
        for line in block['lines']:
            for span in line['spans']:
                rows.append((span['text'], span['bbox_raw'], span['origin_raw']))
    return rows


def _counter_of(rows):
    return Counter(row[0] for row in rows)


def _find(rows, text, origin_ref):
    """在这一页的 span 里找「文本相等、且基线起点还在原位（±VERIFY_TOLERANCE）」的那一个。

    为什么按基线起点而不是按框中心：新句子比原句长时中心必然右移，
    那是文本变长了，不是写歪了。我们写回去用的就是原 span 的基线起点，
    所以「有没有落在该落的地方」只有这个点说了才算，横排竖排都一样。
    """
    best = None
    for row in rows:
        value, _box, origin = row
        if value != text:
            continue
        drift = max(abs(float(origin[0]) - float(origin_ref[0])),
                    abs(float(origin[1]) - float(origin_ref[1])))
        if drift > VERIFY_TOLERANCE:
            continue
        if best is None or drift < best['drift']:
            best = {'bbox': [round(float(v), 2) for v in _box], 'drift': round(drift, 2)}
    return best


def _squash(text):
    """去掉所有空白后再比对（给「写不下一行被引擎自动折了行」那种情况留一条退路）。"""
    return ''.join(str(text or '').split())


def _find_wrapped(rows, text):
    """span 级找不到时的退路：整页文字（去空白）里含不含这句。

    什么时候会走到这里：新句子比这一行剩下的位置长，`insert_text` 会自动折行，
    读回来就不是「一个完整的 span」而是被拆成几段。字是全的，位置也还在原来那一行起，
    所以算写进去了，但要在返回里标 `wrapped` 让前端看得见——
    而不是让它以为「和原来一样一行摆着」。
    """
    if not text:
        return False
    joined = _squash(''.join(row[0] for row in rows))
    return _squash(text) in joined


def _box_along_writing_direction(target):
    """原 span 沿书写方向占多长（未转坐标）：横排看 x、竖排看 y。"""
    box = target['rect_raw']
    direction = target.get('dir_raw') or [1.0, 0.0]
    if abs(float(direction[0])) >= abs(float(direction[1])):
        return max(0.0, box[2] - box[0])
    return max(0.0, box[3] - box[1])


def apply_text_edits(pdf_path, edits, output_path, password=''):
    """按 `{span_id: 新文本}` 把改动写进一份**新** PDF。

    成功：`{'success': True, 'file':…, 'verified': True, 'edits':[…], …}`；
    读回核对不合格：`success: False` + `error: 'verify_mismatch'` + `problems`（文件已写出，
    路径照旧带回去，绝不假装成功）；其余失败：`{'success': False, 'error': 错误码, …}`。
    本函数不抛异常给桥层。

    **整批全有或全无**：只要有一个 id 找不到、或某句新文本里有内置字体盖不住的字符，
    这一批一个字都不写、直接回错误（照 api/pages.py 的 _read_slots 口径：
    只处理其中合法的那部分并回报成功，等于静默丢改动）。
    """
    started = time.monotonic()
    path = os.path.abspath(str(pdf_path or ''))
    if not path or not os.path.isfile(path):
        return _error('not_found', detail=path)
    out = str(output_path or '').strip()
    if not out:
        return _error('need_save_path')
    if not out.lower().endswith('.pdf'):
        return _error('must_end_pdf', detail=out)
    if _same_file(path, out):
        return _error('output_is_source', detail=out)
    pairs, bad = _normalize(edits)
    if bad:
        return _error(bad)
    if not pairs:
        return _error('edits_empty')

    try:
        stat_before = os.stat(path)
    except OSError:
        stat_before = None

    try:
        doc = pymupdf.open(path)
    except Exception as error:
        logging.exception('page_edit: 打不开文件 %s', path)
        return _error('unreadable', detail='%s: %s' % (type(error).__name__, error))

    saved = False
    try:
        was_locked = bool(doc.needs_pass)
        if was_locked:
            if not doc.authenticate(str(password or '')):
                return _error('needs_password' if not password else 'bad_password')
        if not (doc.permissions & pymupdf.PDF_PERM_MODIFY):
            return _error('read_only_permissions')
        total = doc.page_count

        # ---------- 第一遍：只预检（不写任何东西）----------
        # 每页只解析一次（_collect 一遍要读文字层+图片清单+矢量，扫描页可达 0.7s），
        # 预检、算「别的 span 有没有变」的基线计数都复用这一份。
        collected = {}
        plan = []
        not_found = []
        unsupported = []
        for raw_id, new_text in pairs:
            parsed = parse_span_id(raw_id)
            if not parsed:
                not_found.append(raw_id)
                continue
            number, _bi, _li, _si = parsed
            index = number - 1
            if index < 0 or index >= total:
                not_found.append(raw_id)
                continue
            page = doc.load_page(index)
            if index not in collected:
                collected[index] = collect_page(doc, page)
            target = locate_span(page, raw_id, number, collected=collected[index])
            if target is None:
                not_found.append(raw_id)
                continue
            fontname, substituted, absent = pick_font(new_text, target['font'], target['flags'])
            if absent:
                unsupported.append({'id': raw_id, 'chars': ''.join(absent)[:40]})
                continue
            plan.append({'index': index, 'number': number, 'id': raw_id, 'target': target,
                         'new': new_text, 'font': fontname, 'substituted': substituted,
                         'center_raw': [(target['rect_raw'][0] + target['rect_raw'][2]) / 2.0,
                                        (target['rect_raw'][1] + target['rect_raw'][3]) / 2.0],
                         'risk': []})
        if not_found or unsupported:
            result = _error('span_not_found' if not_found else 'font_unsupported',
                            detail={'not_found': not_found, 'unsupported': unsupported})
            result['requested'] = len(pairs)
            result['applied'] = 0
            return result
        if not plan:
            return _error('edits_empty')

        baseline = {index: _counter_of(_spans_of_collected(data))
                    for index, data in collected.items()}

        # ---------- 第二遍：真写 ----------
        touched = []
        by_page = {}
        for item in plan:
            by_page.setdefault(item['index'], []).append(item)

        for index, items in sorted(by_page.items()):
            page = doc.load_page(index)
            raw_width = float(page.cropbox.width) or float(page.rect.width)
            removed = 0
            for item in items:
                target = item['target']
                box = target['rect_raw']
                rect = pymupdf.Rect(box[0], box[1], box[2], box[3])
                if rect.width <= 0 or rect.height <= 0:
                    # 零面积的框（竖排细框之类）给 1pt 的最小面积，否则涂黑会当场拒绝
                    rect = pymupdf.Rect(box[0], box[1], box[0] + 1.0, box[1] + 1.0)
                under = target['under']
                dirty = bool(under['images'] or under['drawings'])
                item['dirty'] = dirty
                if under['images']:
                    item['risk'].append({'kind': 'under_image', 'blocks': under['images'][:20],
                                         'note': '底下有图片，未刷白漆；扫描件的话原稿墨迹还在页面上'})
                if under['drawings']:
                    item['risk'].append({'kind': 'under_drawing', 'blocks': under['drawings'][:20],
                                         'note': '底下有矢量框线/底纹，未刷白漆'})
                if under['spans']:
                    item['risk'].append({'kind': 'adjacent_text', 'blocks': under['spans'][:20],
                                         'note': '相邻文字与这一处重叠，删旧字时可能被削掉一角'})
                if item['substituted']:
                    item['risk'].append({'kind': 'font_substituted',
                                         'note': '原字体「%s」用不了，改用内置 %s'
                                                 % (target['font'], item['font'])})
                try:
                    # fill=False：底下有图/有矢量就不刷漆，只把字从内容流里拿掉
                    page.add_redact_annot(rect, fill=False if dirty else (1, 1, 1))
                    removed += 1
                except Exception:
                    logging.exception('page_edit: 涂黑框添加失败 id=%s', item['id'])
                    item['risk'].append({'kind': 'redact_failed', 'note': '旧字没能删掉'})

            method = 'redact'
            if removed:
                try:
                    # images=NONE / graphics=LINE_ART_NONE：只删字，图片和框线一根不动
                    if not page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                                 graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                                 text=pymupdf.PDF_REDACT_TEXT_REMOVE):
                        method = 'cover'
                except Exception:
                    logging.exception('page_edit: 应用涂黑失败，退回白块盖住 第 %s 页', index + 1)
                    method = 'cover'
            if method == 'cover':
                for item in items:
                    box = item['target']['rect_raw']
                    if not item['dirty']:
                        try:
                            page.draw_rect(pymupdf.Rect(box[0], box[1], box[2], box[3]),
                                           color=None, fill=(1, 1, 1))
                        except Exception:
                            logging.exception('page_edit: 白块盖住失败 第 %s 页', index + 1)
                    item['risk'].append({'kind': 'redact_unsupported',
                                         'note': '这一页没能执行涂黑，改用白块盖住；'
                                                 '旧字可能还留在文件里'})
            for item in items:
                item['method'] = method
                item['white_out'] = bool(method == 'redact' and not item['dirty'])

            # 写新字：位置、字号、颜色都沿用原 span（未转坐标那套）
            for item in items:
                target = item['target']
                rotate = target['rotate']
                if rotate is None:
                    # 斜排（dir 不是四个正方向之一）：insert_text 只认 0/90/180/270，
                    # 只能按横排写并如实标出来，绝不假装方向没变过
                    rotate = 0
                    item['risk'].append({'kind': 'rotated_text',
                                         'note': '原文是斜排 dir=%s，新文本按横排写'
                                                 % (target['dir_raw'],)})
                item['rotate'] = rotate
                origin = target['origin_raw']
                size = float(target['size'] or 11)
                color = _int_color_to_rgb(target['color'])
                written = 0
                if item['new']:
                    try:
                        written = page.insert_text(pymupdf.Point(origin[0], origin[1]),
                                                   text=item['new'], fontname=item['font'],
                                                   fontsize=size, color=color, rotate=rotate)
                    except Exception as error:
                        logging.exception('page_edit: 写入失败 id=%s', item['id'])
                        item['risk'].append({'kind': 'write_failed',
                                             'note': '%s: %s' % (type(error).__name__, error)})
                item['written'] = bool(written) or not item['new']
                try:
                    width = _font_of(item['font']).text_length(item['new'], fontsize=size)
                except Exception:
                    width = 0.0
                item['size_used'] = size
                item['width_before'] = round(_box_along_writing_direction(target), 2)
                item['width_after'] = round(width, 2)
                # 书写方向上还剩多少位置：横排看右边界，竖排按行进方向看
                if rotate in (0, 270):
                    room = raw_width - origin[0]
                else:
                    room = origin[1]
                if width > room + 0.5:
                    item['risk'].append({'kind': 'overflow',
                                         'note': '新文本比这一行剩下的位置长 %.1fpt，会写到页面外'
                                                 % (width - room)})
            touched.append(index)

        if not touched:
            return _error('nothing_to_apply', detail={'ids': [p[0] for p in pairs]})

        _save(doc, out, password, was_locked)
        saved = True
        final_path = os.path.abspath(str(out))

        # ---------- 第三遍：重新打开成品数一遍 ----------
        problems = []
        reports = []
        check = pymupdf.open(final_path)
        try:
            if was_locked and password:
                # 成品按同一口令重新加密了（_save 那条路），读回核对必须先带口令解开。
                # 不带口令直接开：page_count 给得出，但 load_page 当场抛
                # ValueError("document closed or encrypted")（本机实测 PyMuPDF 1.28.2）——
                # 整遍核对被外层兜底吃掉，明明写成了却只回一个裸的 verify_mismatch。
                if not check.authenticate(str(password)):
                    problems.append({'kind': 'output_not_unlockable', 'file': final_path})
            if check.page_count != total:
                problems.append({'kind': 'page_count', 'want': total, 'got': check.page_count})
            after = {}
            for item in plan:
                index = item['index']
                if index not in after:
                    data = collect_page(check, check.load_page(index))
                    after[index] = {'rows': _spans_of_collected(data),
                                    'counter': _counter_of(_spans_of_collected(data))}
            for item in plan:
                rows = after[item['index']]['rows']
                target = item['target']
                anchor = target['origin_raw']
                if item['new']:
                    got = _find(rows, item['new'], anchor)
                else:
                    got = {'bbox': None, 'drift': 0.0}      # 删除：本来就不该有文本读回来
                old_left = None
                if item['new'] != target['text']:
                    old_left = _find(rows, target['text'], anchor)
                verified_one = bool(got) and old_left is None
                reports.append({
                    'id': item['id'], 'page': item['number'],
                    'before': target['text'], 'after': item['new'],
                    'applied': bool(item['written']),
                    'method': item['method'], 'white_out': item['white_out'],
                    'font': item['font'], 'size': item['size_used'],
                    'color': _int_color_to_hex(target['color']),
                    'rotate': item['rotate'],
                    'width_before': item['width_before'], 'width_after': item['width_after'],
                    'read_back': got['bbox'] if got else None,
                    'drift_pt': got['drift'] if got else None,
                    'old_text_gone': old_left is None,
                    'risk': item['risk'],
                    'verified': verified_one,
                })
                if not got:
                    problems.append({'kind': 'text_missing', 'id': item['id'],
                                     'page': item['number'], 'want': item['new']})
                elif old_left is not None:
                    problems.append({'kind': 'old_text_left', 'id': item['id'],
                                     'page': item['number'], 'text': target['text']})
            # 「这一页其余 span 一个字没变」：把这一批动过的文本从两边都扣掉再比多重集。
            # 按页比一次（不是按条），否则同一页的第二条改动会把第一条的新文本当成多余的字。
            for index in sorted(after):
                batch = by_page[index]
                want = dict(baseline[index])
                for item in batch:
                    old = item['target']['text']
                    if want.get(old):
                        want[old] -= 1
                        if not want[old]:
                            del want[old]
                have = dict(after[index]['counter'])
                for item in batch:
                    new = item['new']
                    if have.get(new):
                        have[new] -= 1
                        if not have[new]:
                            del have[new]
                if want != have:
                    problems.append({'kind': 'other_spans_changed', 'page': index + 1,
                                     'less': sorted((Counter(want) - Counter(have)).items())[:5],
                                     'more': sorted((Counter(have) - Counter(want)).items())[:5]})
        finally:
            check.close()

        try:
            stat_after = os.stat(path)
            source_untouched = bool(stat_before
                                    and (stat_before.st_size, stat_before.st_mtime_ns)
                                    == (stat_after.st_size, stat_after.st_mtime_ns))
        except OSError:
            source_untouched = False

        verified = not problems
        result = {
            'success': bool(verified),
            'file': final_path,
            'verified': verified,
            'edits': reports,
            'pages': [i + 1 for i in touched],
            'page_count': total,
            'source': path,
            'source_untouched': source_untouched,
            'output_encrypted': bool(was_locked and password),
            'risk_count': sum(len(r['risk']) for r in reports),
            'elapsed_ms': int((time.monotonic() - started) * 1000),
        }
        if was_locked:
            result['source_encrypted'] = True
        if not verified:
            result['error'] = 'verify_mismatch'
            result['problems'] = problems
        logging.info('page_edit: %s -> %s（%d 处改动 / 核对=%s / %.2fs）',
                     path, final_path, len(reports), verified, time.monotonic() - started)
        return result
    except Exception as error:
        logging.exception('page_edit: 写回失败 %s', path)
        if saved:
            return _error('verify_mismatch', detail='%s: %s' % (type(error).__name__, error))
        return _error('write_failed', detail='%s: %s' % (type(error).__name__, error))
    finally:
        if not saved:
            try:
                doc.close()
            except Exception:
                pass


def editable_pages(pdf_path, password=''):
    """只读地逐页数一遍有没有文字层（给界面判断「哪些页能编辑」，成本比给整本模型低得多）。

    返回 `{'success':…, 'page_count':n, 'pages':[{'page','has_text_layer','text_chars','rotation'}]}`。
    """
    path = os.path.abspath(str(pdf_path or ''))
    if not path or not os.path.isfile(path):
        return _error('not_found', detail=path)
    try:
        doc = pymupdf.open(path)
    except Exception as error:
        logging.exception('page_edit: 打不开文件 %s', path)
        return _error('unreadable', detail='%s: %s' % (type(error).__name__, error))
    try:
        was_locked = bool(doc.needs_pass)
        if was_locked and not doc.authenticate(str(password or '')):
            return _error('needs_password' if not password else 'bad_password')
        rows = []
        for index in range(doc.page_count):
            page = doc.load_page(index)
            chars = len((page.get_text('text') or '').strip())
            rows.append({'page': index + 1, 'has_text_layer': chars > 0,
                         'text_chars': chars, 'rotation': int(page.rotation or 0)})
        return {'success': True, 'page_count': doc.page_count, 'pages': rows,
                'encrypted': was_locked}
    except Exception as error:
        logging.exception('page_edit: 逐页扫描失败 %s', path)
        return _error('parse_failed', detail='%s: %s' % (type(error).__name__, error))
    finally:
        try:
            doc.close()
        except Exception:
            pass
