# -*- coding: utf-8 -*-
r"""行内 LaTeX 标记 → 一段段可绘制的 run。

水印、页码模板、页眉页脚、TXT 排 PDF 这四处的「用户输入的文字」共用这一份解析。
和整篇 `.tex` 那条路（`core/latex_doc.py` + `core/latex_render.py`）分开的理由：
这四行是**一行字**，没有段落、分页、断行这些概念，要的只是「这一串字按什么样式画」，
所以这里输出的是一维 run 序列 + 少量二维结构（分式、根号），由 `api/_text.py`
和 `api/watermark.py` 各自画（前者 fitz 矢量、后者 PIL 画 PNG）。

返回的 run 形状（都是 dict，方便直接过 JS 桥）：
  `{'k':'txt', 'v':文字, 'style':'rm|bf|it|tt|sf', 'shift':None|'sup'|'sub',
    'ul':下划线?, 'ol':上横线?, 'math':是否数学段}`
  `{'k':'frac', 'num':[run], 'den':[run]}`   `{'k':'sqrt', 'body':[run]}`
  `{'k':'space', 'em':倍数}`

三条口径

1. **没有标记就一个字都不动。** `has_markup()` 为假时调用方必须走原来的老路径，
   成品逐字节不变——这条由测试钉（同一行字，加标记前后写出来的页码完全一致）。
2. **认不出的命令退成参数文字**，并记一条说明点名是哪条命令；
   不许整条命令连同参数一起消失，也不许把 `\foo{bar}` 原样画上去让用户以为生效了。
3. **符号一律走 `core/latex_sym.py`**：那张表已经保证「内置字体画得出来」，
   这里不再自己列第二份表，免得两份各说各话。
"""
import re

from core import latex_sym as S

MAX_NEST = 10          # `\textbf{\textit{...}}` 这类嵌套的上限，超了就原样留字


def has_markup(text):
    r"""这串字里有没有 LaTeX 标记（`\` 或 `$`）。没有的话调用方走老路。"""
    if not text:
        return False
    s = str(text)
    return '\\' in s or '$' in s


def _txt(value, style='rm', shift=None, ul=False, ol=False, math=False):
    node = {'k': 'txt', 'v': value, 'style': style, 'shift': shift, 'ul': ul, 'ol': ol}
    if math:
        node['math'] = True
    return node


def plain(items):
    """run 序列摊成纯文本（回显、以及宽度算不出来时的兜底）。"""
    out = []
    for it in items or []:
        k = it.get('k')
        if k == 'txt':
            out.append(it.get('v', ''))
        elif k == 'frac':
            out.append(plain(it.get('num')) + '/' + plain(it.get('den')))
        elif k == 'sqrt':
            out.append('√' + plain(it.get('body')))
        elif k == 'space':
            out.append(' ')
    return ''.join(out)


def _apply_ligatures(text):
    for src, dst in S.LIGATURES:
        text = text.replace(src, dst)
    return text


class _Reader(object):
    """一行字的游标：`{...}` 组、单字符参数、`$...$` 数学段。"""

    def __init__(self, text):
        self.src = str(text or '')
        self.pos = 0
        self.notes = []

    def eof(self):
        return self.pos >= len(self.src)

    def skip_spaces(self):
        while self.pos < len(self.src) and self.src[self.pos] in ' \t':
            self.pos += 1

    def group_or_char(self):
        """`{...}` 里的内容，或紧跟的一个字符；都没有给空串。"""
        self.skip_spaces()
        if self.eof():
            return ''
        if self.src[self.pos] != '{':
            ch = self.src[self.pos]
            self.pos += 1
            return ch
        self.pos += 1
        depth = 1
        start = self.pos
        while self.pos < len(self.src) and depth:
            ch = self.src[self.pos]
            if ch == '\\':
                self.pos += 2
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    body = self.src[start:self.pos]
                    self.pos += 1
                    return body
            self.pos += 1
        self.notes.append({'key': 'api_latex_inline_brace_unclosed', 'args': {}})
        return self.src[start:]


class _Parser(object):
    """文本模式解析器；数学模式在同类的 `parse_math` 里。"""

    def __init__(self, reader, style='rm', shift=None, ul=False, ol=False, depth=0):
        self.r = reader
        self.style = style
        self.shift = shift
        self.ul = ul
        self.ol = ol
        self.depth = depth

    def child(self, text, style=None, shift=None, ul=None, ol=None):
        sub = _Reader(text)
        p = _Parser(sub, style or self.style, self.shift if shift is None else shift,
                    self.ul if ul is None else ul, self.ol if ol is None else ol,
                    self.depth + 1)
        items = p.parse()
        self.r.notes.extend(p.r.notes)
        return items

    def parse(self):
        out = []
        buf = []
        r = self.r

        def flush():
            if not buf:
                return
            text = _apply_ligatures(''.join(buf))
            buf.clear()
            if text:
                out.append(_txt(text, self.style, self.shift, self.ul, self.ol))

        while not r.eof():
            ch = r.src[r.pos]
            if ch == '%':
                # 行内没有「注释」概念，% 当字面量留著（水印文字里 % 是常见字符）
                buf.append(ch)
                r.pos += 1
                continue
            if ch == '\\':
                r.pos += 1
                if not self._command(out, flush):
                    continue
                continue
            if ch == '$':
                flush()
                r.pos += 1
                end = r.src.find('$', r.pos)
                if end < 0:
                    body = r.src[r.pos:]
                    r.pos = len(r.src)
                    r.notes.append({'key': 'api_latex_inline_math_unclosed', 'args': {}})
                else:
                    body = r.src[r.pos:end]
                    r.pos = end + 1
                out.extend(self.math_items(body))
                continue
            if ch == '{':
                flush()
                r.pos += 1
                depth = 1
                start = r.pos
                while r.pos < len(r.src) and depth:
                    c = r.src[r.pos]
                    if c == '\\':
                        r.pos += 2
                        continue
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            break
                    r.pos += 1
                body = r.src[start:r.pos]
                if r.pos < len(r.src):
                    r.pos += 1
                if self.depth >= MAX_NEST:
                    # 深度闸：`{{{{{…` 一路钻会把递归栈撑爆，剩下的原样成字
                    out.append(_txt(body, self.style, self.shift, self.ul, self.ol))
                else:
                    out.extend(self.child(body))
                continue
            if ch == '}':
                flush()
                r.notes.append({'key': 'api_latex_inline_stray_brace', 'args': {}})
                r.pos += 1
                buf.append('}')
                continue
            if ch == '~':
                buf.append('\u00a0')
                r.pos += 1
                continue
            if ch in '^_':
                flush()
                r.pos += 1
                arg = r.group_or_char()
                out.extend(self.child(arg, shift='sup' if ch == '^' else 'sub'))
                continue
            buf.append(ch)
            r.pos += 1
        flush()
        return out

    # ------------------------------------------------------------ 命令
    def _command(self, out, flush):
        r = self.r
        if r.eof():
            flush()
            out.append(_txt('\\', self.style, self.shift, self.ul, self.ol))
            return True
        m = re.match(r'[A-Za-z]+|.', r.src[r.pos:])
        name = m.group()
        r.pos += len(name)

        if name in S.ESCAPES:
            flush()
            out.append(_txt(S.ESCAPES[name], self.style, self.shift, self.ul, self.ol))
            return True
        styled = {'textbf': 'bf', 'bfseries': 'bf', 'textit': 'it', 'emph': 'it',
                  'itshape': 'it', 'texttt': 'tt', 'ttfamily': 'tt',
                  'textsf': 'sf', 'sffamily': 'sf', 'textrm': 'rm',
                  'textnormal': 'rm', 'normalfont': 'rm', 'mathrm': 'rm'}
        if name in styled:
            arg = r.group_or_char()
            flush()
            if not arg:
                r.notes.append({'key': 'api_latex_inline_noarg',
                                'args': {'cmd': '\\' + name}})
                return True
            out.extend(self.child(arg, style=styled[name]))
            return True
        if name == 'underline':
            arg = r.group_or_char()
            flush()
            out.extend(self.child(arg, ul=True))
            return True
        if name == 'overline':
            arg = r.group_or_char()
            flush()
            out.extend(self.child(arg, ol=True))
            return True
        if name == 'textsuperscript':
            out.extend(self.child(r.group_or_char(), shift='sup'))
            return True
        if name == 'textsubscript':
            out.extend(self.child(r.group_or_char(), shift='sub'))
            return True
        if name in ('frac', 'dfrac', 'tfrac'):
            num = r.group_or_char()
            den = r.group_or_char()
            flush()
            out.append({'k': 'frac', 'num': self.child(num), 'den': self.child(den)})
            return True
        if name == 'sqrt':
            body = r.group_or_char()
            flush()
            out.append({'k': 'sqrt', 'body': self.child(body)})
            return True
        if name in S.SPACING:
            flush()
            out.append({'k': 'space', 'em': S.SPACING[name]})
            return True
        if name in S.SYMBOLS:
            flush()
            out.append(_txt(S.SYMBOLS[name], self.style, self.shift, self.ul, self.ol))
            return True
        if name in ('TeX', 'LaTeX', 'LaTeXe'):
            flush()
            out.append(_txt({'TeX': 'TeX', 'LaTeX': 'LaTeX', 'LaTeXe': 'LaTeX2e'}[name],
                            self.style, self.shift, self.ul, self.ol))
            return True
        if name in ('quad', 'qquad'):
            flush()
            out.append({'k': 'space', 'em': S.SPACING.get(name, 1.0)})
            return True
        # 认不出来的命令：带花括号参数的，参数照排、命令名丢掉；
        # 没有参数的（`\temp`、`\file.txt` 这种把反斜杠当路径分隔符写的），
        # **原样留下 `\name`**——这里丢掉就是吃用户的字，比画得不好看严重得多。
        arg = ''
        if not r.eof() and r.src[r.pos] == '{':
            arg = r.group_or_char()
        flush()
        if arg:
            out.extend(self.child(arg))
        else:
            out.append(_txt('\\' + name, self.style, self.shift, self.ul, self.ol))
        r.notes.append({'key': 'api_latex_inline_ignored', 'args': {'cmd': '\\' + name}})
        return True

    # ------------------------------------------------------------ 数学
    def math_items(self, text):
        return self.math_parse(_Reader(text))

    def math_parse(self, sub):
        """数学模式：变量斜体、数字与运算符正体、符号换字符、`^`/`_` 变上下标。"""
        out = []
        p = _MathParser(sub, self)
        out.extend(p.parse())
        self.r.notes.extend(sub.notes)
        return out


class _MathParser(object):
    def __init__(self, reader, owner):
        self.r = reader
        self.owner = owner

    def parse(self):
        out = []
        r = self.r
        while not r.eof() and r.src[r.pos] != '$':
            ch = r.src[r.pos]
            if ch == '\\':
                r.pos += 1
                self._command(out)
                continue
            if ch in '^_':
                r.pos += 1
                arg = r.group_or_char()
                sub = _Reader(arg)
                inner = _MathParser(sub, self.owner).parse()
                for it in inner:
                    if it.get('k') == 'txt':
                        it['shift'] = 'sup' if ch == '^' else 'sub'
                    out.append(it)
                r.notes.extend(sub.notes)
                continue
            if ch in ' \t\n':
                r.pos += 1
                continue
            if ch in '{}':
                r.pos += 1
                continue
            if ch.isalpha():
                out.append(_txt(ch, 'it', math=True))
                r.pos += 1
                continue
            if ch.isdigit() or ch == '.':
                m = re.match(r'[\d.]+', r.src[r.pos:])
                out.append(_txt(m.group(), 'rm', math=True))
                r.pos += len(m.group())
                continue
            if ch in '+-=<>':
                out.append(_txt(' %s ' % ch, 'rm', math=True))
                r.pos += 1
                continue
            out.append(_txt(ch, 'rm', math=True))
            r.pos += 1
        return out

    def _command(self, out):
        r = self.r
        m = re.match(r'[A-Za-z]+|.', r.src[r.pos:])
        name = m.group() if m else ''
        r.pos += len(name)
        if name in ('frac', 'dfrac', 'tfrac'):
            num = r.group_or_char()
            den = r.group_or_char()
            out.append({'k': 'frac',
                        'num': _MathParser(_Reader(num), self.owner).parse(),
                        'den': _MathParser(_Reader(den), self.owner).parse()})
            return
        if name == 'sqrt':
            body = r.group_or_char()
            out.append({'k': 'sqrt', 'body': _MathParser(_Reader(body),
                                                          self.owner).parse()})
            return
        if name in ('text', 'mbox', 'textrm', 'mathrm'):
            body = r.group_or_char()
            out.extend(self.owner.child(body, style='rm'))
            return
        if name in S.SPACING:
            out.append({'k': 'space', 'em': S.SPACING[name]})
            return
        if name in S.SYMBOLS:
            v = S.SYMBOLS[name]
            style = 'it' if (v.isalpha() and v.islower() and not v.isascii()) else 'rm'
            out.append(_txt(v, style, math=True))
            return
        if name in S.BIG_OPS:
            out.append(_txt(S.BIG_OPS[name], 'rm', math=True))
            return
        if name in S.FUNCTIONS:
            out.append(_txt(name + ' ', 'rm', math=True))
            return
        if name in S.ESCAPES:
            out.append(_txt(S.ESCAPES[name], 'rm', math=True))
            return
        arg = ''
        if not r.eof() and r.src[r.pos] == '{':
            arg = r.group_or_char()
        if arg:
            out.extend(_MathParser(_Reader(arg), self.owner).parse())
        else:
            # 同文本模式那条：没带参数的认不出来的命令，原样留字，别把用户的字符吃掉
            out.append(_txt('\\' + name, 'rm', math=True))
        r.notes.append({'key': 'api_latex_inline_ignored', 'args': {'cmd': '\\' + name}})


def parse(text):
    """入口：一行用户输入 → (run 序列, 说明列表)。"""
    reader = _Reader(text)
    items = _Parser(reader).parse()
    return items, reader.notes
