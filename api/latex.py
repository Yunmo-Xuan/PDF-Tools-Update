# -*- coding: utf-8 -*-
"""LaTeX 相关的后端方法：`.tex` 排成 PDF、以及「这软件到底能吃什么 LaTeX」的清单。

分工：解析在 `core/latex_doc.py`、排版在 `core/latex_render.py`、符号表在
`core/latex_sym.py`。这一层只做四件事——读文件、限路径、报进度、把结果整理成
界面能直接用的形状。**所有给用户看的话都走字典键**（`api_latex_*`），
一句写死的中文都没有，`env` 的 `I19` 基线是 0。

三条口径

1. **只吃用户选中的那份 .tex 所在目录里的东西。** `\\input`、`\\include`、
   `\\includegraphics` 都限定在同一目录内（按 realpath 判），
   不把目录外的文件读进成品里。
2. **不覆盖源文件。** 成品一律另存；输出路径没给就直接失败说明，不猜一个位置写下去。
3. **没照做的部分必须点名。** 未知命令、被忽略的宏包命令、结构错误、
   内置字体画不出来的字符，全部原样带回界面（键名 + 行号），
   界面那句「排好了」后面跟着的就是这份清单——不许出现「成功但内容少了」。
"""
import logging
import os

from core import jobctl
from core.i18n import t as _text

_JOB_NAME = 'latex_to_pdf'
# \includegraphics 认这些后缀（fitz 能直接贴的位图 + PDF 单页）
_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.pdf')
# .tex 里常见的编码，按顺序试；全试不到才按 latin-1 硬解（至少不崩）。
# utf-8-sig 不在名单里：带 BOM 的文件在 `_read_tex` 开头就单独认出来了，
# 放这儿会把「没有 BOM 的普通 utf-8」也报成 utf-8-sig，用户看着像编码判错了。
_ENCODINGS = ('utf-8', 'gb18030', 'big5', 'shift_jis', 'latin-1')


def _fail(key, extra=None, **args):
    out = {'success': False, 'message': _text(key, **args)}
    if extra:
        out.update(extra)
    return out


def _inside(directory, name):
    """把「.tex 里写的相对路径」限制在那份 .tex 自己的目录里。

    不这么限的话，一份写着 `\\input{../../secret}` 的文件就能把目录外的东西读进
    用户拿到的成品里——这是转换工具，不是文件浏览器。
    """
    if not name:
        return None
    base = os.path.realpath(directory)
    # 只取相对路径的最后一段：`\input{sub/file}` 这种子目录写法先不支持，
    # 但也不能让它顺着 `..` 跑出去
    leaf = os.path.basename(str(name).replace('\\', '/').strip())
    if not leaf or leaf in ('.', '..', ''):
        return None
    target = os.path.realpath(os.path.join(base, leaf))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


class LatexMixin:
    # ---------------------------------------------------------- .tex → PDF
    def latex_to_pdf(self, tex_path, output_path=''):
        """把一份 `.tex` 排成 PDF。

        返回 `{success, file, pages, encoding, notes, unknown, ignored, errors, message}`；
        `notes/unknown/ignored/errors` 都是「哪儿没照做」的清单，界面逐条列给用户看。
        """
        import pymupdf as fitz
        from core import latex_doc, latex_render

        try:
            if not output_path:
                return _fail('api_need_save_path')
            if not tex_path or not os.path.isfile(tex_path):
                # 这条老键是 `%s` 写法（t() 只认 {命名}），沿用老代码的取法
                return {'success': False, 'message': _text('api_file_not_found') % str(tex_path or '')}
            try:
                size = os.path.getsize(tex_path)
            except OSError:
                size = 0
            if size > 8 * 1024 * 1024:
                # 8 MB 的 .tex 已经是几十万字，排版要跑很久，多半是选错了文件
                return _fail('api_latex_too_big', size='%d' % (size // 1024))

            text, encoding = self._read_tex(tex_path)
            if text is None:
                return _fail('api_latex_read_failed', name=os.path.basename(tex_path))

            directory = os.path.dirname(os.path.realpath(tex_path))

            def read_file(name):
                path = _inside(directory, name)
                if not path or not os.path.isfile(path):
                    return None
                body, _enc = self._read_tex(path)
                return body

            def resolve_image(name):
                path = _inside(directory, name)
                if not path:
                    return None
                if os.path.isfile(path):
                    return path
                for ext in _IMAGE_EXTS:
                    cand = path + ext
                    if os.path.isfile(cand):
                        return cand
                return None

            doc = latex_doc.parse(text, read_file=read_file)
            blocks = doc.get('blocks') or []
            jobctl.start(_JOB_NAME, max(1, len(blocks)), kind='pages')
            try:
                out_dir = os.path.dirname(str(output_path)) or '.'
                if not os.path.isdir(out_dir):
                    os.makedirs(out_dir, exist_ok=True)
                result = latex_render.render(
                    doc, output_path, fitz, image_resolver=resolve_image,
                    should_stop=jobctl.should_stop, on_progress=jobctl.advance)
            finally:
                # jobctl 只有 `cancelling` 这个字段（「按了取消、还在收尾」），没有 cancelled
                cancelled = bool(jobctl.status().get('cancelling'))
                jobctl.finish()
            if cancelled and not os.path.exists(output_path):
                # 取消不是失败，也不许把 'cancelled' 这种内部码当话术甩上屏：
                # 给明确的 cancelled 标记 + 该语言那句「已取消」
                return {'success': False, 'cancelled': True, 'pages': 0,
                        'message': _text('api_latex_cancelled')}

            notes = [{'key': n['key'], 'args': n.get('args') or {},
                      'text': _text(n['key'], **(n.get('args') or {}))}
                     for n in result.get('notes') or []]
            pages = result.get('pages') or 0
            out = {'success': True, 'file': output_path, 'pages': pages,
                   'encoding': encoding,
                   'unknown': doc.get('unknown') or [],
                   'ignored': doc.get('ignored') or [],
                   'errors': doc.get('errors') or [],
                   'notes': notes}
            # 一句话汇总：哪儿没照做，界面直接念这句
            summary = self._latex_summary(out, cancelled)
            if summary:
                out['message'] = summary
            logging.info('LaTeX 排版完成: %s → %s（%d 页，编码 %s，未知命令 %d，忽略 %d）',
                         os.path.basename(tex_path), output_path, pages, encoding,
                         len(out['unknown']), len(out['ignored']))
            return out
        except Exception as error:
            logging.exception('latex_to_pdf 失败')
            return {'success': False, 'message': _text('api_latex_convert_failed',
                                                        error=str(error)[:200])}

    @staticmethod
    def _read_tex(path):
        """读 .tex：按常见编码逐个试，全试不到按 latin-1 硬解（至少不崩）。

        返回 (文本, 用到的编码名)；读不出来返回 (None, None)。
        """
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except OSError:
            logging.exception('读 .tex 失败: %s', path)
            return None, None
        if raw.startswith(b'\xef\xbb\xbf'):
            return raw.decode('utf-8-sig', errors='replace'), 'utf-8-sig'
        for enc in _ENCODINGS:
            try:
                return raw.decode(enc), enc
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode('latin-1', errors='replace'), 'latin-1'

    @staticmethod
    def _latex_summary(out, cancelled=False):
        """把「哪儿没照做」收成一句人话；全都没问题就不返回话（别没事报「成功」）。"""
        bits = []
        if cancelled:
            bits.append(_text('api_latex_cancelled'))
        if out.get('errors'):
            bits.append(_text('api_latex_struct_errors', n=len(out['errors'])))
        if out.get('unknown'):
            names = sorted({str(u.get('cmd') or '') for u in out['unknown']})[:6]
            bits.append(_text('api_latex_unknown_cmds',
                               n=len(out['unknown']),
                               names=', '.join('\\' + n for n in names if n)))
        if out.get('ignored'):
            names = sorted({str(i.get('cmd') or '') for i in out['ignored']})[:6]
            bits.append(_text('api_latex_ignored_cmds',
                              n=len(out['ignored']),
                              names=', '.join('\\' + n for n in names if n)))
        for note in out.get('notes') or []:
            if note.get('text'):
                bits.append(note['text'])
        return '\n'.join(b for b in bits if b) or None

    # ---------------------------------------------------------- 能力清单
    def get_latex_capabilities(self):
        """界面「本软件支持的 LaTeX 范围」那一栏的数据：支持范围 + 退让账。

        这份清单和解析器用的是同一批常量（`SUPPORTED_SUMMARY` / `FORCED`），
        所以代码里扩了命令、说明会跟着变，不会出现「界面还写着旧的」。
        命令名本身不翻译（用户要照着敲），但每段下面那句说明和末尾「明确不支持」
        都走字典键，换语言时那一栏跟着换。
        """
        from core import latex_doc, latex_sym

        try:
            commands = sorted(latex_sym.all_commands())
        except Exception:
            logging.exception('取 LaTeX 命令清单失败')
            return _fail('api_latex_caps_failed')
        forced = [{'cmd': cmd, 'want': want, 'got': got}
                  for cmd, (want, got) in sorted(latex_sym.FORCED.items())]
        notes = {}
        for group, key in (getattr(latex_doc, 'SUPPORTED_NOTES', {}) or {}).items():
            notes[group] = _text(key)
        return {'success': True,
                'summary': latex_doc.SUPPORTED_SUMMARY,
                'notes': notes,
                'limits_note': _text(latex_doc.LIMITS_NOTE_KEY),
                'command_count': len(commands),
                'symbol_count': len(latex_sym.SYMBOLS),
                'forced': forced,
                'forced_count': len(forced)}

    def check_tex_file(self, tex_path):
        """只解析不排版：给界面当「这份文件吃得下吗」的预检。

        返回命令数、页数估计、未知命令清单。用户点「转换」之前先知道有没有坑，
        比排完 200 页再告诉他「有三条命令没生效」友好。
        """
        from core import latex_doc

        try:
            if not tex_path or not os.path.isfile(tex_path):
                # 这条老键是 `%s` 写法（t() 只认 {命名}），沿用老代码的取法
                return {'success': False, 'message': _text('api_file_not_found') % str(tex_path or '')}
            text, encoding = self._read_tex(tex_path)
            if text is None:
                return _fail('api_latex_read_failed', name=os.path.basename(tex_path))
            directory = os.path.dirname(os.path.realpath(tex_path))

            def read_file(name):
                path = _inside(directory, name)
                if not path or not os.path.isfile(path):
                    return None
                body, _enc = self._read_tex(path)
                return body

            doc = latex_doc.parse(text, read_file=read_file)
            return {'success': True, 'encoding': encoding,
                    'blocks': len(doc.get('blocks') or []),
                    'unknown': doc.get('unknown') or [],
                    'ignored': doc.get('ignored') or [],
                    'errors': doc.get('errors') or [],
                    'title': ''.join(
                        n.get('v', '') for n in (doc.get('title') or [])
                        if n.get('t') == 'txt') or None,
                    'message': self._latex_summary(
                        {'errors': doc.get('errors') or [],
                         'unknown': doc.get('unknown') or [],
                         'ignored': doc.get('ignored') or [], 'notes': []})}
        except Exception as error:
            logging.exception('check_tex_file 失败')
            return {'success': False,
                    'message': _text('api_latex_convert_failed', error=str(error)[:200])}
