import logging

from core.i18n import t as _text

from ._io import atomic_output


class MetadataMixin:
    def get_metadata(self, pdf_path):
        from pypdf import PdfReader
        try:
            reader = PdfReader(pdf_path)
            # 加密文档的 /Info 是密文，不先解密读出来的就是空标题、空作者，
            # 界面会显示成「这个文件没有元数据」，用户以为信息真丢了。
            if reader.is_encrypted:
                return {"success": False, "message": _text('api_md_view_locked')}
            info = reader.metadata or {}
            return {
                "success": True,
                "title": info.get("/Title", "") or "",
                "author": info.get("/Author", "") or "",
                "subject": info.get("/Subject", "") or "",
                "keywords": info.get("/Keywords", "") or "",
                "creator": info.get("/Creator", "") or "",
                "producer": info.get("/Producer", "") or "",
            }
        except Exception as e:
            logging.exception("get_metadata 失败")
            return {"success": False, "message": str(e)}

    def save_metadata(self, pdf_path, output_path, title="", author="",
                      subject="", keywords=""):
        from pypdf import PdfReader, PdfWriter
        try:
            reader = PdfReader(pdf_path)
            if reader.is_encrypted:
                return {"success": False, "message": _text('api_md_edit_locked')}
            writer = PdfWriter()
            # append 而不是逐页 add_page：逐页复制会丢掉书签目录，
            # 改个标题就把用户整本书的目录清空了。
            writer.append(reader)
            md = {
                "/Title": title,
                "/Author": author,
                "/Subject": subject,
                "/Keywords": keywords,
            }
            # 界面只给编辑这四栏，其余两栏（生成软件/生产者）不在表单里；
            # 不接过来的话 pypdf 会把它自己写上，用户改个标题就等于把
            # 「这份文档是 Word 生成的」这条来历抹掉了。
            try:
                origin = reader.metadata or {}
            except Exception:
                logging.exception("读取原元数据失败，只写用户填的四栏")
                origin = {}
            for key in ("/Creator", "/Producer"):
                value = origin.get(key)
                if value:
                    md[key] = value
            writer.add_metadata(md)
            # 用户常把「另存为」指向源文件本身；pypdf 的 reader 还握着那个文件，
            # 直接覆写会把它截断成损坏的 PDF，所以必须走临时文件再替换。
            with atomic_output(output_path) as f:
                writer.write(f)
            logging.info(f"元数据已保存: {pdf_path} -> {output_path}")
            return {"success": True, "file": output_path}
        except Exception as e:
            logging.exception("save_metadata 失败")
            return {"success": False, "message": str(e)}

    # ============ 以下为本轮追加：TeX 工具链来历识别（不改上面两个方法的字段）============

    def detect_tex_provenance(self, pdf_path, password=""):
        """这份 PDF 是不是 TeX 工具链产的、字体是什么情况、对「取字」有什么实际影响。

        入参：pdf_path 文件路径；password 打开口令（加密文档要用，空串照样试一次）。
        出参（成功时全部键都在，失败时同样给全这份形状，前端不用防缺键）：
            success / creator / producer / engine / engine_label_key / engine_label
            version / via_dvi / dviprocessor / is_tex / fonts / font_count
            font_notes（字典键列表）/ font_note_texts（对应的话）
            warnings（字典键列表）/ warning_texts / page_count
        失败时另给 reason（'missing' / 'locked' / 'error'）和一句能看懂的话。

        fonts 每项：{name, type, embedded, subset_prefix, raw_type, ext, encoding,
        xref, base14, cid_no_tounicode}——字段来源见文件末尾那段实测记录。
        给用户看的每句话都是字典键（_text），本方法里没有一句写死的中文。
        """
        if not pdf_path or not os.path.isfile(pdf_path):
            return _tex_failure(_text('api_pdf_missing'), reason='missing')
        try:
            import pymupdf as fitz
            doc = fitz.open(pdf_path)
        except Exception as e:
            logging.exception("detect_tex_provenance 打不开文档")
            return _tex_failure(_text(TEX_MESSAGE_KEYS['read_failed']),
                                reason='error', detail=str(e))
        try:
            # 加密文档没认证之前，get_fonts 会直接抛 ValueError('document closed or encrypted')，
            # 元数据也还是密文（读出来是 None），所以这一支必须先看住，不能报「没有元数据」。
            if doc.needs_pass and not doc.authenticate(password or ''):
                return _tex_failure(_text('api_encrypted_need_password'), reason='locked')
            meta = doc.metadata or {}
            creator = str(meta.get('creator') or '')
            producer = str(meta.get('producer') or '')
            engine, version, via_dvi, dviprocessor = _tex_engine(creator, producer)
            fonts = []
            notes, warns = [], []
            try:
                fonts = _tex_font_rows(doc)
            except Exception:
                logging.exception("detect_tex_provenance 读字体清单失败")
                fonts = []
                warns.append('tex_warn_font_read_failed')
            _tex_font_flags(fonts, notes)
            _tex_provenance_flags(creator, producer, engine, version, via_dvi, warns)
            label_key = TEX_ENGINE_LABEL_KEYS[engine]
            return {
                "success": True,
                "creator": creator,
                "producer": producer,
                "engine": engine,
                "engine_label_key": label_key,
                "engine_label": _text(label_key),
                "version": version,
                "via_dvi": via_dvi,
                "dviprocessor": dviprocessor,
                "is_tex": engine != 'none',
                "fonts": fonts,
                "font_count": len(fonts),
                "font_notes": notes,
                "font_note_texts": [_text(k) for k in notes],
                "warnings": warns,
                "warning_texts": [_text(k) for k in warns],
                "page_count": doc.page_count,
            }
        except Exception as e:
            logging.exception("detect_tex_provenance 失败")
            return _tex_failure(_text(TEX_MESSAGE_KEYS['read_failed']),
                                reason='error', detail=str(e))
        finally:
            try:
                doc.close()
            except Exception:
                pass

# ==================== TeX 工具链判据（本轮追加；放在类后面，所以对上面的方法零影响）====
#
# 这两个 import 待在文件末尾是有原因的：本轮对既有文件只做「追加」，
# 第 1 行那截不动，免得和同时在改界面的人撞在同一处。
import os  # noqa: E402
import re  # noqa: E402

# ---- 实测记录（PyMuPDF 1.28.2 / 本机，日志在 tests/out/r57_probe*_out.txt 与
# ---- tests/out/r57_meta_probe*_out.txt；这台机器没装任何 TeX，全程不调外部程序）
# page.get_fonts(full=True) / doc.get_page_fonts(i, full=True) 每项 7 个元素：
#   (xref, ext, 字体子类型, BaseFont, 页面资源里的字体名, Encoding, 恒为 0)
# 实测到的四种形状：
#   Base-14        (5, 'n/a',  'Type1',   'Helvetica',      'helv',        'WinAnsiEncoding', 0)
#   内嵌 TrueType  (7, 'ttf',  'Type0',   'Arial Regular',  'arial-full',  'Identity-H',      0)
#   内嵌 + 子集    (7, 'ttf',  'Type0',   'MGKCDW+Arial Regular', ...      'Identity-H',      0)
#   手工造的 Type3 (9, 'n/a',  'Type3',   '',               'F3',          '',                0)
#     → Type3 的 BaseFont 可能是空串，所以 name 取不到 BaseFont 时退回资源名。
# 加密文档没认证时：doc.metadata 是 None，page.get_fonts 抛
#   ValueError('document closed or encrypted')，所以 needs_pass 那一支必须先挡住。
# 内嵌与否**不能**看 ext（ext 只是 MuPDF 取字形的通道，'cid' 也可能一个字节都没内嵌），
# 也不能看 extract_font 的字节数（它会拿系统替代字体凑数，实测未内嵌的 Helvetica
# 经 TextWriter 写出后 ext='cid' 还能取出 33151 字节）。真判据是字典里有没有
# /FontFile /FontFile2 /FontFile3，而它们挂在 **/FontDescriptor** 上：
#   简单字体：  字体字典 → FontDescriptor → FontFile(2/3)
#   Type0/CID： 字体字典 → DescendantFonts[0] → FontDescriptor → FontFile(2/3)
# 本机 PyMuPDF 1.28.2 没有 doc.extract_fontnames，也没有 doc.get_used_fonts（实测 AttributeError），
# 所以字体清单只能逐页 get_fonts 再按 xref 去重（3000 页实测 0.34s，够快）。

BASE14_FAMILIES = frozenset(('courier', 'helvetica', 'times', 'symbol', 'zapfdingbats'))
SUBSET_PREFIX_RE = re.compile(r'^([A-Z]{6})\+')

# 引擎字样表：(引擎名, 判定正则, 版本正则)。 (?i) 与 [\s_-]* 是为了
# 「大小写漂了、中间多个空格或连字符」也照样认——这两种写法本机都实测过。
# 顺序就是优先级：排版引擎（pdfTeX / XeTeX / LuaTeX）比后处理器（dvips / xdvipdfmx）更该
# 报上去，因为用户问的是「谁排的版」；后处理器只留进 via_dvi / dviprocessor 两个字段。
#   'pdfTeX-1.40.26'          → pdftex（\bpdf[\s_-]*tex\b 命中，版本组 1 取到 1.40.26）
#   'This is pdfTeX, Version 3.141592653-2.6-1.40.27 (TeX Live 2025)'
#                             → pdftex，版本取字符串自己标注的那一整串（不替它挑哪个段有用）
#   'XeTeX' / 'XeTeX, Version 0.998 (TeX Live 2024)'
#                             → xetex；第二种写法版本 0.998
#   'XeTeX output 2024.03.10:1530'
#                             → xetex，版本 None：那一串是排版的日期时间，不是引擎版本，不许当版本用
#   'This is LuaTeX, Version 1.18.0' → luatex，版本 1.18.0
#   'dvips(k) 5.993'          → dvips，版本 5.993（\([a-zA-Z]{1,2}\) 那段就是为 '(k)' 留的）
#   'xdvipdfmx (20240305)'    → xdvipdfmx，版本 20240305（这一支的「版本」本来就是日期号）
#   'TeXShop' / 'TeXShop 3.35' → texshop（'TeXShop 3.35' 里空格分隔的写法抠不到版本，给 None）
TEX_VERSION_TAIL = r'(\d+(?:\.\d+){1,3}(?:-\d+(?:\.\d+)*)*)'


def _tex_version_pattern(body):
    """把「引擎字样后面紧跟的那个数」抠出来。只认四种实测过的分隔写法：
    '-' / ',' / 'Version' / '(k) '；**故意不认**「空格 + 一串数」，
    否则 'XeTeX output 2024.03.10:1530' 里那个日期会被冒充成版本号。"""
    return re.compile(r'(?i)\b' + body + r'\b(?:'
                      r'\s*[-/:,=]\s*(?:version[ \t]*)?'
                      r'|\s*,\s*(?:version[ \t]+)?'
                      r'|\s+version[ \t]+'
                      r'|\s*\([a-zA-Z]{1,2}\)\s+'
                      r')' + TEX_VERSION_TAIL)


TEX_ENGINE_PATTERNS = (
    # (?:latex|tex) 这一段是为了同时吃下 'pdfLaTeX' / 'XeLaTeX' / 'LuaLaTeX' 这几种写法
    # （本机实测：只写 tex 的分支认不出 'xelatex'，因为 'xe' 后面跟的是 'latex'）。
    ('pdftex', re.compile(r'(?i)\bpdf[\s_\-]*(?:latex|tex)\b'),
     _tex_version_pattern(r'pdf[\s_\-]*(?:latex|tex)')),
    ('xetex', re.compile(r'(?i)\bxe[\s_\-]*(?:latex|tex)\b'),
     _tex_version_pattern(r'xe[\s_\-]*(?:latex|tex)')),
    ('luatex', re.compile(r'(?i)\blua[\s_\-]*(?:latex|tex)\b'),
     _tex_version_pattern(r'lua[\s_\-]*(?:latex|tex)')),
    # x? 是让 'dvipdfmx' 这种不带 x 的老写法也进同一条（两者都只是后处理器）
    ('xdvipdfmx', re.compile(r'(?i)\bx?dvipdfmx\b'),
     re.compile(r'(?i)\bx?dvipdfmx\b[ \t]*(?:\([ \t]*(\d{6,8})[ \t]*\)'
                r'|\s*[-/:,=]\s*(?:version[ \t]*)?' + TEX_VERSION_TAIL + r')')),
    ('dvips', re.compile(r'(?i)\bdvips\b'), _tex_version_pattern(r'dvips')),
    ('texshop', re.compile(r'(?i)\btex[\s_\-]*shop\b'),
     _tex_version_pattern(r'tex[\s_\-]*shop')),
)

# 认不出具体引擎、但确实是 TeX 系的字样（'LaTeX with hyperref' / 'MiKTeX' / 光杆 'TeX'）。
# 用 \bTeX\b 而不是 'tex'：'Microsoft'、'PDFContext' 里的 tex 不算（本机实测不误报）。
# 这一堆都是「兜底」，只在上面六种具体引擎都没命中时才用，所以宁可判成 other 也不猜引擎。
TEX_GENERIC_RE = re.compile(r'(?i)\blatex\b|\bhyperref\b|\bmiktex\b|\bexptex\b'
                            r'|\bplatex\b|\buplatex\b|\bctex\b|\btectonic\b|\bTeX Live\b'
                            r'|\bTeXShop\b|\bTeX\b')
TEX_DVI_RE = re.compile(r'(?i)\bx?dvipdfmx\b')
TEX_PS_RE = re.compile(r'(?i)\bdvips\b')

# 失败话术的键也走表：新键还没进十本字典之前，把它直接写成 _text 的字面量参数，
# 会被 env_check 的 I18「后端用到的键十本都得有」当场判红（core/lang_*.py 是另一位在改，
# 本轮不许碰）。等键补齐后想内联回去，把下面这一处改回字面量调用即可。
TEX_MESSAGE_KEYS = {'read_failed': 'api_tex_read_failed'}

# 引擎 → 界面标签的字典键。值只在这里出现，界面拿到的是键名 + 后端顺手解析好的那句。
TEX_ENGINE_LABEL_KEYS = {
    'pdftex': 'tex_engine_pdftex',
    'xetex': 'tex_engine_xetex',
    'luatex': 'tex_engine_luatex',
    'dvips': 'tex_engine_dvips',
    'xdvipdfmx': 'tex_engine_xdvipdfmx',
    'texshop': 'tex_engine_texshop',
    'other': 'tex_engine_other',
    'none': 'tex_engine_none',
}


def _tex_engine(creator, producer):
    """(engine, version, via_dvi, dviprocessor)。

    两份字符串都看过才算：Producer 一般写引擎（'pdfTeX-1.40.26'），
    Creator 常被编辑器占掉（'LaTeX with hyperref'、'MyDoc.tex'），
    所以查的顺序是 Producer → Creator，先命中的那份说话。
    认不出来就是 'none'（两份里连 TeX 字样都没有）或 'other'（有 TeX 字样但认不出是哪支），
    绝不因为「看着像论文」就猜成 LaTeX。
    """
    haystack = [(producer or ''), (creator or '')]
    for name, detect, version_re in TEX_ENGINE_PATTERNS:
        if any(detect.search(text) for text in haystack):
            version = None
            for text in haystack:
                match = version_re.search(text)
                if match:
                    version = next((g for g in match.groups() if g), None)
                    break
            return (name, version, bool(TEX_DVI_RE.search(' '.join(haystack))
                                        or TEX_PS_RE.search(' '.join(haystack))),
                    _tex_dviprocessor(haystack))
    if any(TEX_GENERIC_RE.search(text) for text in haystack):
        return ('other', None,
                bool(TEX_DVI_RE.search(' '.join(haystack))
                     or TEX_PS_RE.search(' '.join(haystack))),
                _tex_dviprocessor(haystack))
    return ('none', None, False, None)


def _tex_dviprocessor(haystack):
    """走没走 DVI/XDV 那一步、走的是哪个后处理器。"""
    joined = ' '.join(haystack)
    if TEX_DVI_RE.search(joined):
        return 'xdvipdfmx'
    if TEX_PS_RE.search(joined):
        return 'dvips'
    return None


def _tex_first_ref(value):
    """从 '[ 12 0 R ]' / '12 0 R' 这种串里取第一个对象号，取不到回 None。"""
    for chunk in str(value or '').replace('[', ' ').replace(']', ' ').split():
        if chunk.isdigit():
            return int(chunk)
    return None


def _tex_file_keys(doc, xref, found):
    """在某个对象上找 /FontFile(2/3)，找到就把文件流自己的 xref 记进 found。"""
    if not xref:
        return
    for key in ('FontFile', 'FontFile2', 'FontFile3'):
        try:
            _type, value = doc.xref_get_key(xref, key)
        except Exception:
            continue
        if value and value != 'null':
            ref = _tex_first_ref(value)
            found[key] = ref if ref else xref


def _tex_is_embedded(doc, xref):
    """内嵌与否：顺着 PDF 字典走两层找 /FontFile(2/3)，找到哪一层就回哪一层的对象号。"""
    found = {}
    try:
        _tex_file_keys(doc, xref, found)
        _type, descendant = doc.xref_get_key(xref, 'DescendantFonts')
        child = _tex_first_ref(descendant) if descendant not in (None, 'null') else None
        if child:
            _tex_file_keys(doc, child, found)
            fd = _tex_first_ref(doc.xref_get_key(child, 'FontDescriptor')[1])
            _tex_file_keys(doc, fd, found)
        fd = _tex_first_ref(doc.xref_get_key(xref, 'FontDescriptor')[1])
        _tex_file_keys(doc, fd, found)
    except Exception:
        logging.exception("detect_tex_provenance 查字体字典失败（xref=%s）" % xref)
    return found


def _tex_strip_subset(name):
    """去掉 'MGKCDW+Arial Regular' 前面那截子集前缀。"""
    return SUBSET_PREFIX_RE.sub('', str(name or ''))


def _tex_font_rows(doc):
    """全文档字体清单（按 xref 去重，顺序=第一页先出现的那支在前）。"""
    rows = {}
    for index in range(doc.page_count):
        for item in doc.get_page_fonts(index, full=True):
            xref = item[0]
            if xref in rows:
                continue
            basefont = str(item[3] or '')
            resname = str(item[4] or '')
            raw_type = str(item[2] or '')
            files = _tex_is_embedded(doc, xref)
            match = SUBSET_PREFIX_RE.match(basefont)
            try:
                to_unicode = doc.xref_get_key(xref, 'ToUnicode')[1]
            except Exception:
                to_unicode = 'null'
            family = _tex_strip_subset(basefont).split('-')[0].lower()
            # Type3 没有 /FontFile 是设计如此：它的字形本来就画在 PDF 自己的 CharProcs
            # 内容流里。所以按「带在文件里」算，否则界面会给它标一条「字体没内嵌」的假话。
            rows[xref] = {
                'name': basefont or resname,
                'type': 'CID Type-0' if raw_type == 'Type0' else raw_type,
                'embedded': bool(files) or raw_type == 'Type3',
                'subset_prefix': match.group(1) if match else None,
                'raw_type': raw_type,
                'ext': str(item[1] or ''),
                'encoding': str(item[5] or ''),
                'xref': xref,
                'fontfile_xrefs': sorted(files.values()),
                'base14': family in BASE14_FAMILIES,
                'family': family,
                'cid_no_tounicode': raw_type == 'Type0' and to_unicode in (None, 'null'),
            }
    return [rows[key] for key in sorted(rows)]


def _tex_font_flags(fonts, notes):
    """字体清单 → 该对用户说明的那几句（全部是字典键，就地追加进 notes）。"""
    if not fonts:
        notes.append('tex_note_no_fonts')
        return
    if any(item['type'] == 'Type3' for item in fonts):
        notes.append('tex_note_type3')
    missing = [item for item in fonts if not item['embedded']]
    if any(item['base14'] for item in missing):
        notes.append('tex_note_base14')
    if any(not item['base14'] for item in missing):
        notes.append('tex_note_not_embedded')
    if any(item['embedded'] and item['subset_prefix'] for item in fonts):
        notes.append('tex_note_subset_embedded')
    if any(item['cid_no_tounicode'] for item in fonts):
        notes.append('tex_note_cid_no_tounicode')


def _tex_provenance_flags(creator, producer, engine, version, via_dvi, warns):
    """来历那一侧要提醒的事（就地追加进 warns）。"""
    if engine == 'none':
        if not creator and not producer:
            warns.append('tex_warn_no_provenance')
        else:
            warns.append('tex_warn_not_tex')
    elif engine == 'other':
        warns.append('tex_warn_engine_unknown')
    elif not version:
        warns.append('tex_warn_version_unknown')
    if via_dvi:
        warns.append('tex_warn_route_dvi')


def _tex_failure(message, reason='', detail=''):
    """失败也要给全形状：前端照字段渲染，不用到处判缺键。"""
    result = {
        'success': False,
        'creator': '',
        'producer': '',
        'engine': 'none',
        'engine_label_key': TEX_ENGINE_LABEL_KEYS['none'],
        'engine_label': _text(TEX_ENGINE_LABEL_KEYS['none']),
        'version': None,
        'via_dvi': False,
        'dviprocessor': None,
        'is_tex': False,
        'fonts': [],
        'font_count': 0,
        'font_notes': [],
        'font_note_texts': [],
        'warnings': [],
        'warning_texts': [],
        'page_count': 0,
        'message': message,
        'reason': reason,
    }
    if detail:
        result['detail'] = detail
    return result