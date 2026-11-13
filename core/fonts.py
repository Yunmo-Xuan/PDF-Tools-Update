"""本机字体：清单、缓存、以及「按名字取一个能用的字体」的统一入口。

为什么要这个模块：可编辑文字（文字水印、页码、页眉页脚、TXT 转 PDF）以前只有
PyMuPDF 内置的 china-s 一种选择，用户说「太单一了」。本机 C:\\Windows\\Fonts
下有几百个字体文件，全量解析 name 表冷启动要好几秒，所以清单走
「内存 memo + 磁盘缓存（按 文件大小+mtime_ns 指纹失效）」：一次冷扫，之后
只重算变过的文件。

三条口径：

1. **族名从字体文件自己的 name 表读，不用文件名硬拼。** 文件名（msyh.ttc）和
   族名（Microsoft YaHei / 微软雅黑）经常对不上，注册表值名也有本地化的坑。
   `family` 给界面显示用的那个（有非英文本地化名就用它，如「微软雅黑」），
   `english_name` 恒为英文名，`full_name` 是带样式的完整名。
2. **坏文件不许炸清单。** 0 字节、扩展名对但内容不是字体、被占用、name 表畸形
   （本机实测 752 个文件里有不少 'name' table stringOffset 报错的）一律跳过该
   face 并记一条日志；坏指纹同样进缓存，下次启动不再重复解析。
3. **取不到用户选的字体就退回内置字体，并把「退回了」说出去。**
   load_fitz_font / load_pil_font 返回 (font, note_key)，note_key 是字典键名
   （不是成品中文），调用方过 core.i18n 翻成界面语言。

has_cjk 的判据（本机 761 个 face 对 cmap 全量真值实测校准）：OS/2
ulUnicodeRange 全局第 59 位「CJK Unified Ideographs」——135 个真能画常用汉字的
face 全部命中，未命中而真有汉字的为 0。没有 OS/2 的少数字体退回抽样 cmap。

embedded_ok 的判据：sfnt 表目录里带矢量轮廓（glyf / CFF / CFF2）——MuPDF 只嵌
矢量轮廓；纯点阵（EBLC/EBDT 一类）嵌不进去。真到嵌入那一步仍会 try/except
兜底（见 load_fitz_font），所以这个字段的作用是「清单上先给用户一个预期」，
不是唯一防线。

非 Windows 平台（darwin/linux）没有注册表，扫系统字体目录，口径相同。
"""

import io
import json
import logging
import os
import struct
import sys
import threading
import time

FONT_SUFFIXES = ('.ttf', '.ttc', '.otf')

# PyMuPDF 内置的简体中文名（和 api/_text.py 的 FONT_NAME 同一个东西）
BUILTIN_FONT_NAME = 'china-s'
# 用户选的字体读不出来、退回内置字体时给调用方的字典键（十语言文案由 core.i18n 翻）
FONT_FALLBACK_NOTE = 'api_font_fallback_default'

# 缓存文件的格式版本：face 解析口径一变就 +1，旧缓存整份作废重扫
_SCHEMA = 3
_CACHE_FILENAME = 'families.json'

# OS/2 ulUnicodeRange 里表示「有中日韩统一表意文字」的全局位号（word2 的第 27 位）
_CJK_IDEOGRAPH_BIT = 59
# 抽样判 cmap 时用的常用字：中、文、页、点、书
_CJK_SAMPLE = (0x4E2D, 0x6587, 0x9875, 0x70B9, 0x4E66)

_lock = threading.RLock()
# 进程内 memo：{"families": [...], "faces": {key: face_dict}}；冷扫成功后填充
_memo = {'families': None, 'faces': None}
# ttc 里抽出来的单 face 字节缓存（fontTools 重打包一个 face 实测 0.8s，
# 同一界面会话里反复用同一个字族不能次次重抽）：{key: bytes}，最多攒 4 份
_face_buffers = {}
_FACE_BUFFER_MAX = 4

log = logging.getLogger(__name__)


# ============================ 扫描来源 ============================

def _windows_fonts_dir():
    root = os.environ.get('SystemRoot') or os.environ.get('WINDIR') or r'C:\Windows'
    return os.path.join(root, 'Fonts')


def _user_fonts_dir():
    """每用户字体目录（Win10+ 右键「为所有用户安装以外」的安装落点）。"""
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        return None
    return os.path.join(local, 'Microsoft', 'Windows', 'Fonts')


def _posix_font_dirs():
    if sys.platform == 'darwin':
        return ['/System/Library/Fonts', '/Library/Fonts',
                os.path.expanduser('~/Library/Fonts')]
    return ['/usr/share/fonts', '/usr/local/share/fonts',
            os.path.expanduser('~/.fonts'), os.path.expanduser('~/.local/share/fonts')]


def _registry_font_paths():
    """HKLM 字体注册表里的文件路径（只读）。值名可能是相对文件名也可能是绝对路径。

    注册表这路的意义是「装在系统字体目录之外」的字体：文件本体照样要能打开才收。
    """
    if sys.platform != 'win32':
        return []
    import winreg
    fonts_dir = _windows_fonts_dir()
    paths = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts',
                             0, winreg.KEY_QUERY_VALUE)
    except OSError:
        log.warning('字体注册表打不开（不作为错误上抛，只丢这路来源）')
        return []
    try:
        for index in range(winreg.QueryInfoKey(key)[1]):
            try:
                value = winreg.EnumValue(key, index)[1]
            except OSError:
                continue
            name = str(value).strip().strip('"')
            if not name.lower().endswith(FONT_SUFFIXES):
                continue
            path = name if os.path.isabs(name) else os.path.join(fonts_dir, name)
            if os.path.isfile(path):
                paths.append(path)
    finally:
        winreg.CloseKey(key)
    return paths


def _scan_paths(extra_dirs=None):
    """三处来源合成去重的文件清单（保序：系统目录 → 每用户目录 → 注册表 → 附加目录）。"""
    collected = []
    seen = set()

    def add(path):
        if not path:
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        collected.append(key)

    def listdir_fonts(directory, recursive=False):
        if not directory or not os.path.isdir(directory):
            return
        if recursive:
            for root, _dirs, names in os.walk(directory):
                for n in names:
                    if n.lower().endswith(FONT_SUFFIXES):
                        add(os.path.join(root, n))
            return
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            log.warning('字体目录读不了，跳过: %s', directory)
            return
        for n in names:
            if n.lower().endswith(FONT_SUFFIXES):
                add(os.path.join(directory, n))

    if sys.platform == 'win32':
        listdir_fonts(_windows_fonts_dir())
        listdir_fonts(_user_fonts_dir())
        for p in _registry_font_paths():
            add(p)
    else:
        for d in _posix_font_dirs():
            listdir_fonts(d, recursive=True)
    for d in (extra_dirs or []):
        listdir_fonts(d)
    return collected


# ============================ face 解析 ============================

def _face_count(path):
    """ttc 集合里有多少个 face；其余为 1。读文件头 12 字节，不碰解析器。

    这一步同时把「扩展名对但内容不是字体」的文件挡在解析器之前（返回 0）。
    """
    try:
        with open(path, 'rb') as handle:
            head = handle.read(12)
    except OSError:
        return 0
    if head[:4] == b'ttcf':
        try:
            return max(0, struct.unpack('>I', head[8:12])[0])
        except struct.error:
            return 0
    return 1 if head[:4] in (b'\x00\x01\x00\x00', b'OTTO', b'true') else 0


def _fingerprint(path):
    """(大小, mtime_ns) 指纹；文件没了/读不动返回 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _record_text(name_table, name_id, prefer_langs):
    """按语言优先级从 name 表取一个字符串；全取不到返回 ''。"""
    best = ''
    for rec in getattr(name_table, 'names', ()):
        if rec.nameID != name_id:
            continue
        try:
            text = str(rec.toUnicode()).strip()
        except Exception:
            continue
        if not text:
            continue
        key = (rec.platformID, rec.langID)
        for rank, want in enumerate(prefer_langs):
            if key == want:
                return text
        if not best and rec.platformID == 3:
            best = text
    return best


def _names_of(name_table):
    """取 (显示族名, 英文族名, 样式名, 完整名)；任何一层取不到都退回下一层，不抛。

    族名优先用 nameID 16（typographic family，一家字重全归它），老字体没有就退回
    nameID 1。中文名（如「微软雅黑」）在 Windows 平台 langID 0x804/0x404 的记录里，
    英文名在 langID 0x409（或 Mac 平台 1/0）——两边都取，显示名优先非 ASCII 的那份。
    """
    en_langs = ((3, 0x409), (1, 0))
    zh_langs = (0x804, 0x404, 0x411, 0x412)
    english = (_record_text(name_table, 16, en_langs)
               or _record_text(name_table, 1, en_langs))
    localized = ''
    for name_id in (16, 1):
        for lang in zh_langs:
            found = _record_text_by_lang(name_table, name_id, lang)
            if found and found != english and not _is_ascii(found):
                localized = found
                break
        if localized:
            break
    style = (_record_text(name_table, 17, en_langs)
             or _record_text(name_table, 2, en_langs) or 'Regular')
    for name_id in (17, 2):
        for lang in (0x804, 0x404):
            found = _record_text_by_lang(name_table, name_id, lang)
            if found and not _is_ascii(found):
                style = found
                break
        else:
            continue
        break
    full = _record_text(name_table, 4, en_langs)
    for lang in (0x804, 0x404):
        found = _record_text_by_lang(name_table, 4, lang)
        if found and not _is_ascii(found):
            full = found
            break
    display = localized or english
    full_name = full or ' '.join(x for x in (display, style) if x)
    return display, english, style, full_name


def _record_text_by_lang(name_table, name_id, lang_id):
    for rec in getattr(name_table, 'names', ()):
        if rec.nameID != name_id or rec.langID != lang_id:
            continue
        try:
            return str(rec.toUnicode()).strip()
        except Exception:
            return ''
    return ''


def _is_ascii(text):
    try:
        text.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def _parse_face(path, face_index):
    """解析一个 face 的展示信息；返回 face 字典，坏文件返回 {'bad': 1, ...指纹}。

    任何异常都必须收敛在这里：清单是只读浏览，一个坏字体不许掀翻整张表。
    """
    created = time.perf_counter()
    tt = None
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, fontNumber=face_index, lazy=True)
        try:
            family, english, style, full_name = _names_of(tt['name'])
        except Exception:
            log.info('name 表读不动，按文件名兜底: %s face %d', path, face_index)
            family = english = full_name = os.path.splitext(os.path.basename(path))[0]
            style = 'Regular'
        if not family:
            # name 表在但族名记录全空（个别盗版/魔改字库）：按文件名兜底，
            # 宁可难看点也别让这个字族从清单里凭空消失
            base = os.path.splitext(os.path.basename(path))[0]
            family = english = full_name or base
            style = style or 'Regular'
        # OS/2：字重、斜体位、Unicode 范围位（has_cjk 的主判据）
        weight, italic, has_cjk, cjk_known = 400, False, False, True
        try:
            os2 = tt.get('OS/2')
        except Exception:
            os2 = None
        if os2 is not None:
            weight = int(getattr(os2, 'usWeightClass', 400) or 400)
            italic = bool(getattr(os2, 'fsSelection', 0) & 1)
            bits = set()
            for word in range(1, 5):
                value = int(getattr(os2, 'ulUnicodeRange%d' % word, 0) or 0)
                bits |= {(word - 1) * 32 + b for b in range(32) if value >> b & 1}
            has_cjk = _CJK_IDEOGRAPH_BIT in bits
        else:
            # 没有 OS/2（极老的字库）：抽样 cmap 真值判
            try:
                cmap = tt.getBestCmap()
                has_cjk = sum(1 for cp in _CJK_SAMPLE if cp in cmap) >= 3
            except Exception:
                cjk_known = False
        try:
            tables = set(tt.keys())
        except Exception:
            tables = set()
        embedded_ok = bool(tables & {'glyf', 'CFF ', 'CFF2'})
        face = {
            'family': family,
            'english_name': english,
            'style': style,
            'full_name': full_name,
            'weight': weight,
            'italic': italic,
            'has_cjk': bool(has_cjk),
            'embedded_ok': bool(embedded_ok),
        }
        if not cjk_known:
            face['has_cjk'] = False
        return face
    except Exception as exc:
        log.info('字体解析失败（跳过该 face）: %s face %d（%s，%.0f ms）',
                 path, face_index, type(exc).__name__, (time.perf_counter() - created) * 1000)
        return {'bad': 1}
    finally:
        try:
            tt.close()
        except Exception:
            pass


# ============================ 缓存 ============================

def _cache_file():
    from core.paths import FONT_CACHE_DIR
    return os.path.join(FONT_CACHE_DIR, _CACHE_FILENAME)


def _load_disk_cache():
    try:
        with open(_cache_file(), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get('schema') == _SCHEMA:
            faces = data.get('faces')
            if isinstance(faces, dict):
                return faces
    except FileNotFoundError:
        pass
    except Exception:
        log.exception('字体缓存读不出来，按冷启动重扫')
    return None


def _store_disk_cache(faces):
    path = _cache_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump({'schema': _SCHEMA, 'faces': faces}, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        # 缓存只是加速器：写不进去（只读目录、磁盘满）不影响本次清单正确性
        log.warning('字体缓存写不进去: %s', path)


def _face_key(path, face_index):
    return '%s|%d' % (os.path.normcase(os.path.abspath(path)), face_index)


# ============================ 对外清单 ============================

def list_families(refresh=False, extra_dirs=None):
    """本机可用字体清单，按族分组。每项：

    {
      'family':       界面显示名（有非英文名就用之，如「微软雅黑」）,
      'english_name': 英文族名（Microsoft YaHei）,
      'full_name':    默认样式的完整名,
      'path':         默认样式的字体文件绝对路径,
      'face_index':   默认样式在 ttc 里的序号,
      'has_cjk':      默认样式能不能出中文,
      'embedded_ok':  默认样式能不能被 PyMuPDF 嵌进 PDF,
      'styles':       [{'style','weight','italic','path','face_index',
                        'has_cjk','embedded_ok'}, ...] 该族全部字重/样式,
    }

    refresh=True：重扫目录并核对指纹（变了的重算），但内存里没缓存也要走磁盘缓存；
    首调用总是全量建。三处来源（系统字体目录 / 每用户目录 / HKLM 注册表）去重。
    """
    with _lock:
        if _memo['families'] is not None and not refresh and not extra_dirs:
            return _memo['families']
        built, faces = _build(extra_dirs)
        if not extra_dirs:
            _memo['families'] = built
            _memo['faces'] = faces
        return built


def _build(extra_dirs=None):
    """一次冷/热扫描：磁盘缓存 + 指纹增量 → face 解析 → 分族组装。返回 (族列表, faces)。"""
    started = time.perf_counter()
    paths = _scan_paths(extra_dirs)
    cached = _load_disk_cache()
    if cached is None and _memo['faces'] is not None:
        cached = _memo['faces']
    cached = cached or {}
    faces = {}
    parsed = 0
    for path in paths:
        count = _face_count(path)
        for index in range(count):
            key = _face_key(path, index)
            old = cached.get(key)
            fp = _fingerprint(path)
            if (isinstance(old, dict) and fp is not None
                    and old.get('fp') == fp):
                faces[key] = old
                continue
            if fp is None:
                continue  # 文件消失了（注册表里残链），下轮自然不再出现
            if count == 0:
                faces[key] = {'fp': fp, 'face': {'bad': 1}}
                continue
            face = _parse_face(path, index)
            face['path'] = path
            face['face_index'] = index
            faces[key] = {'fp': fp, 'face': face}
            parsed += 1
    # 有文件被删/被判定坏完之后再回来：只要 faces 数量变了就重写缓存。
    # 带 extra_dirs 的构建（验收沙盒用）不动磁盘缓存，免得把临时目录写进去。
    if not extra_dirs and faces != cached:
        _store_disk_cache(faces)
    families = _group_families(faces)
    log.info('字体清单构建完成：%d 个文件 / %d 个 face（本次解析 %d），%.2fs',
             len(paths), len(faces), parsed, time.perf_counter() - started)
    return families, faces


def _group_families(faces):
    """face 字典 → 族列表。同族多文件（arial.ttf / arial_0.ttf、Bold/Italic 各自成文件）
    合并进一个 family 的 styles；dedup 按 (族键, 样式名小写)。"""
    groups = {}
    for entry in faces.values():
        face = entry.get('face') or {}
        if face.get('bad') or not face.get('family'):
            continue
        key = (face.get('english_name') or face.get('family')).casefold()
        style = face.get('style') or 'Regular'
        bucket = groups.setdefault(key, {'english': face.get('english_name')
                                         or face.get('family'), 'items': []})
        if not any((item['style'] or '').casefold() == style.casefold()
                   for item in bucket['items']):
            bucket['items'].append(face)
    families = []
    for bucket in groups.values():
        items = sorted(bucket['items'],
                       key=lambda f: (int(f.get('weight', 400)), bool(f.get('italic')),
                                      str(f.get('style'))))
        # 默认样式：先找「常规/Regular」且非斜体，找不到用第一个
        default = next((f for f in items if (f.get('style') or '').casefold()
                        in ('regular', 'normal', '常规', '标准')
                        and not f.get('italic')), items[0])
        families.append({
            'family': default['family'],
            'english_name': bucket['english'],
            'full_name': default.get('full_name') or default['family'],
            'path': default['path'],
            'face_index': default['face_index'],
            'has_cjk': bool(default.get('has_cjk')),
            'embedded_ok': bool(default.get('embedded_ok')),
            'styles': [{'style': f.get('style') or 'Regular',
                        'weight': int(f.get('weight', 400)),
                        'italic': bool(f.get('italic')),
                        'path': f['path'],
                        'face_index': f['face_index'],
                        'has_cjk': bool(f.get('has_cjk')),
                        'embedded_ok': bool(f.get('embedded_ok'))}
                       for f in items],
        })
    families.sort(key=lambda x: (x['english_name'] or x['family']).casefold())
    return families


def reset_memo():
    """清进程内 memo（验收/强制冷启动用；磁盘缓存不动）。"""
    with _lock:
        _memo['families'] = None
        _memo['faces'] = None
        _face_buffers.clear()


# ============================ 按引用取字体 ============================

def resolve_font_file(ref):
    """把界面传来的字体引用解析成 (绝对路径, face 序号)；解析不出返回 None。

    认三种写法：
      1. 族名/英文族名/完整名（不分大小写）→ 该族的默认样式；
      2. 「族名|样式名」→ 该样式对应的文件与 face 序号；
      3. 字体文件绝对路径（可带「|face 序号」）——注册表残链或用户手动指定时用。
    """
    text = str(ref or '').strip()
    if not text:
        return None
    name, _, tail = text.partition('|')
    name = name.strip()
    face_index = 0
    style_target = ''
    tail = tail.strip()
    if tail.isdigit():
        face_index = max(0, int(tail))
    elif tail:
        style_target = tail.casefold()
    if os.path.isfile(name):
        return os.path.abspath(name), face_index
    target = name.casefold()
    for family in list_families():
        if target not in (family['family'].casefold(),
                          (family['english_name'] or '').casefold(),
                          (family['full_name'] or '').casefold()):
            continue
        if style_target:
            for style in family['styles']:
                if style['style'].casefold() == style_target:
                    return style['path'], style['face_index']
            return None
        return family['path'], family['face_index']
    return None


def _extract_face_bytes(path, face_index):
    """从 ttc 里抽第 face_index 个 face，重打包成独立字库字节（fitz 走 fontbuffer）。"""
    key = _face_key(path, face_index)
    hit = _face_buffers.get(key)
    if hit is not None:
        return hit
    from fontTools.ttLib import TTFont
    tt = TTFont(path, fontNumber=face_index, lazy=True)
    buf = io.BytesIO()
    tt.save(buf)
    tt.close()
    data = buf.getvalue()
    if len(_face_buffers) >= _FACE_BUFFER_MAX:
        _face_buffers.clear()
    _face_buffers[key] = data
    return data


def load_fitz_font(fitz_module, ref=None):
    """给矢量文字那条路用：返回 (fitz.Font, note_key)。

    ref 为空 → 内置 china-s（老行为，一个字节都不变）；
    ref 给了但取不到（没装、被删、MuPDF 读不动）→ 退回内置字体，
    note_key = FONT_FALLBACK_NOTE，调用方翻成界面语言告诉用户「换字体没换成」。
    """
    if not ref:
        return fitz_module.Font(BUILTIN_FONT_NAME), None
    located = resolve_font_file(ref)
    if located is None:
        log.info('字体引用解析不到，退回内置字体: %s', ref)
        return fitz_module.Font(BUILTIN_FONT_NAME), FONT_FALLBACK_NOTE
    path, face_index = located
    try:
        if face_index == 0:
            font = fitz_module.Font(fontfile=path)
        else:
            font = fitz_module.Font(fontbuffer=_extract_face_bytes(path, face_index))
        return font, None
    except Exception:
        log.exception('PyMuPDF 读字体失败，退回内置字体: %s face %d', path, face_index)
        return fitz_module.Font(BUILTIN_FONT_NAME), FONT_FALLBACK_NOTE


def load_pil_font(size, ref=None):
    """给水印 PNG 那条路用（PIL 画字）：返回 (ImageFont, note_key)。

    ref 取不到时不是直接报错而是退回原来的三级链（系统中文 → PyMuPDF 内置
    中文 → 默认位图），同时给 note_key，语义和 load_fitz_font 一致。
    """
    from PIL import ImageFont
    note = None
    if ref:
        located = resolve_font_file(ref)
        if located is None:
            log.info('字体引用解析不到（PIL 路），退回默认字体: %s', ref)
            note = FONT_FALLBACK_NOTE
        else:
            path, face_index = located
            try:
                return ImageFont.truetype(path, size, index=face_index), None
            except Exception:
                log.exception('PIL 读字体失败，退回默认字体: %s face %d', path, face_index)
                note = FONT_FALLBACK_NOTE
    return _load_builtin_pil(ImageFont, size), note


def _load_builtin_pil(ImageFont, size):
    """原 load_font 的三级兜底，一字不动地搬过来。"""
    path = resolve_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            logging.exception(f"加载系统字体失败: {path}")
    try:
        import pymupdf as fitz
        return ImageFont.truetype(io.BytesIO(fitz.Font(BUILTIN_FONT_NAME).buffer), size)
    except Exception:
        logging.exception("加载 PyMuPDF 内置中文字体失败")
    return ImageFont.load_default()


# ============================ 兼容旧接口 ============================

def resolve_font_path():
    """按平台查找可用的中文字体文件路径；找不到返回 None。"""
    candidates = []
    if sys.platform == 'win32':
        candidates = [
            'C:/Windows/Fonts/msyh.ttc',
            'C:/Windows/Fonts/msyh.ttf',
            'C:/Windows/Fonts/simhei.ttf',
            'C:/Windows/Fonts/simsun.ttc',
            'msyh.ttf',
            'simhei.ttf',
        ]
    elif sys.platform == 'darwin':
        candidates = [
            '/System/Library/Fonts/PingFang.ttc',
            '/System/Library/Fonts/STHeiti Light.ttc',
            '/Library/Fonts/Arial Unicode.ttf',
        ]
    else:
        candidates = [
            '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def load_font(size):
    """旧接口：返回一个能画中文的 PIL 字体对象（不带选字体）。

    保留是因为老调用点（api/watermark.py 曾直接用它）语义不变；
    要用户自选字体走 load_pil_font / load_fitz_font。
    """
    font, _note = load_pil_font(size)
    return font
