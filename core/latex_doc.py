# -*- coding: utf-8 -*-
r"""LaTeX 子集解析器：`.tex` 源码 → 内部节点树（不出图，排版在 `core/latex_render.py`）。

三条硬口径

1. **不静默丢内容。** 认不出来的命令一律进 `unknown`（记命令名、行号、它带的参数），
   节点树里照样留一个 `unknown` 节点把参数里的文字排出来；宏包类命令进 `ignored`。
   回包给界面的是「排了多少页 + 哪些命令没照做」，绝不出现「成功」但用户发现少了一段。
2. **结构错了也不炸。** 少一个 `\end{itemize}`、多一个 `}`、`\begin{tabular}` 没给列规格
   这类，都记进 `errors` 并按最合理的解释继续排下去（未闭合的环境在文档末尾补闭合）。
3. **只做子集，且子集是明写的。** 支持范围见 `SUPPORTED_SUMMARY`，界面「能吃什么」那一栏
   直接读它，不许代码里悄悄扩而说明还停在旧范围。

节点形状（都是 dict，方便直接过 JS 桥）：
  块 `{'t': 'para'|'section'|'list'|'table'|'verbatim'|'quote'|'center'|'display_math'
            |'figure'|'newpage'|'toc'|'unknown', ...}`
  行内 `{'t': 'txt'|'run'|'math'|'br'|'space'|'nbsp'|'unknown'|'ref'|'script', ...}`
  数学 `{'t': 'mi'|'mn'|'mo'|'sym'|'bigop'|'frac'|'sqrt'|'accent'|'delim'|'mtext'|
            'sub'|'sup'|'subsup'|'overunder', ...}`
"""
import re
import time

from core import latex_sym as S

# 界面「能吃什么」那一栏读这份清单（键名进十本字典，值在这里只留命令名）
SUPPORTED_SUMMARY = {
    'structure': [
        '\\documentclass[10pt|11pt|12pt, a4paper|letterpaper|b5paper]',
        '\\title  \\author  \\date  \\maketitle',
        '\\tableofcontents',
        '\\section  \\subsection  \\subsubsection  \\paragraph  +  * ',
        '\\begin{itemize}  \\begin{enumerate}  \\begin{description}  (\\item, \\item[..])',
        '\\begin{center}  \\begin{flushleft}  \\begin{flushright}',
        '\\begin{quote}  \\begin{quotation}',
        '\\begin{verbatim}  \\begin{lstlisting}',
        '\\begin{tabular}{l c r |}  +  \\hline  \\cline',
        '\\begin{figure}  \\begin{table}  +  \\caption',
        '\\includegraphics[width=..cm]{file}',
        '\\newpage  \\pagebreak  \\clearpage',
        '\\label  \\ref  \\eqref  \\pageref  \\cite',
        '\\newcommand  \\renewcommand',
        '\\input  \\include',
    ],
    'text': [
        '\\textbf  \\textit  \\emph  \\texttt  \\textsf  \\textrm  \\textnormal',
        '\\underline  \\overline  \\textsuperscript  \\textsubscript',
        '\\bfseries  \\itshape  \\ttfamily  \\sffamily  \\rmfamily  \\normalfont',
        '\\footnote',
        '\\href  \\url',
        '\\quad  \\qquad  \\,  \\:  \\;  \\!',
        '\\%  \\&  \\$  \\#  \\_  \\{  \\}',
        '---   --   ``   \'\'   ~',
        '\\ldots  \\dots',
        '\\TeX  \\LaTeX  \\today',
    ],
    'math': [
        '$...$   \\( ... \\)   \\[ ... \\]',
        'equation / align / gather / multline',
        'x^2   x_1   x_i^2   x\'',
        '\\frac  \\dfrac  \\tfrac  \\binom',
        '\\sqrt[n]{...}',
        '\\sum  \\prod  \\int  \\oint  \\lim',
        '\\left( ... \\right)   \\left\\{ ... \\right\\}   \\lfloor \\rfloor   \\lceil \\rceil',
        '\\hat  \\widehat  \\tilde  \\bar  \\overline  \\vec  \\dot  \\ddot  \\acute',
        '\\text  \\mbox  \\mathrm  \\mathbf  \\mathit  \\mathtt  \\mathsf  \\boldsymbol',
        '\\alpha ... \\omega   \\Gamma ... \\Omega',
        '\\leq  \\geq  \\neq  \\approx  \\equiv  \\sim  \\propto  \\ll  \\gg',
        '\\in  \\notin  \\subset  \\supseteq  \\cap  \\cup  \\setminus  \\emptyset',
        '\\forall  \\exists  \\land  \\lor  \\neg  \\perp  \\parallel  \\angle',
        '\\to  \\rightarrow  \\Rightarrow  \\leftrightarrow  \\mapsto  \\nearrow',
        '\\infty  \\partial  \\nabla  \\hbar  \\ell  \\prime  \\cdots  \\vdots',
        '\\mathbb{R}  \\mathbb{N}  \\mathbb{Z}  \\mathbb{Q}  \\mathbb{C}',
        '\\sin \\cos \\tan \\log \\ln \\exp \\max \\min \\det \\gcd',
    ],
}

# 每段标题下面那句解释走字典键：命令名不翻（用户照着敲），说明要跟着界面语言走。
SUPPORTED_NOTES = {'structure': 'latex_caps_structure_note',
                   'text': 'latex_caps_text_note',
                   'math': 'latex_caps_math_note'}
# 整栏末尾那句「明确不支持什么」也走字典键
LIMITS_NOTE_KEY = 'latex_caps_limits_note'


class _Tok(object):
    """一个 token：值 + 行号。留着行号才能把「哪一行没照做」报给用户。"""

    __slots__ = ('v', 'line')

    def __init__(self, v, line):
        self.v = v
        self.line = line


MAX_NEST = 120     # 组/环境/数学最多往里钻这么多层；再深就原样成字


class Parser(object):
    r"""把 .tex 文本解析成节点树。

    read_file: 可选的回调（相对路径 → 文本），由调用方限定在用户选中的目录里；
               不给就只记一条 ignored（\input 不越权读盘）。
    """

    MAX_DEPTH = 8
    MAX_MACRO_EXPAND = 50     # 宏展开层数上限：自引用/互引用到这就停，正常文档远碰不到

    def __init__(self, text, read_file=None):
        self.src = text or ''
        self.nest = 0
        self.guard = None      # 宏展开深度：共享一个列表，子解析器用的是同一份计数
        self.pos = 0
        self.line = 1
        self.read_file = read_file
        self.macros = {}
        self.unknown = []
        self.ignored = []
        self.errors = []
        self.footnotes = []
        self.preamble_flags = {}

    # -------------------------------------------------------------- 词法小工具
    def eof(self):
        return self.pos >= len(self.src)

    def peek(self, n=1):
        return self.src[self.pos:self.pos + n]

    def advance(self, n=1):
        chunk = self.src[self.pos:self.pos + n]
        self.pos += n
        self.line += chunk.count('\n')
        return chunk

    def at(self, s):
        return self.src.startswith(s, self.pos)

    def skip_spaces(self):
        out = ''
        while not self.eof() and self.src[self.pos] in ' \t\r\n':
            out += self.advance()
        return out

    def skip_comment(self):
        """% 到行尾（含行尾换行本身留给上层，段落判断要用它）。"""
        while not self.eof() and self.src[self.pos] != '\n':
            self.advance()

    def read_command_name(self):
        r"""反斜杠后面那串：字母取到非字母为止；非字母就是单字符转义（\\% \\& 之类）。"""
        if self.eof():
            return ''
        if self.src[self.pos].isalpha():
            m = re.match(r'[A-Za-z@*]+', self.src[self.pos:])
            return self.advance(len(m.group())) if m else self.advance()
        return self.advance()

    def read_group(self, braces=True):
        """读一个 {...} 组（含嵌套、含注释），返回原文；没有组就返回 None。

        原样返回字符串交给对应处理器再解析——命令的参数本身常常又是一段 LaTeX。
        """
        saved_pos, saved_line = self.pos, self.line
        self.skip_spaces()
        if braces and not self.at('{'):
            self.pos, self.line = saved_pos, saved_line
            return None
        if not braces:
            # 无花括号的单字符参数（\bfseries x 里那种）
            if self.eof():
                return ''
            return self.advance()
        depth = 0
        start = self.pos
        while not self.eof():
            ch = self.src[self.pos]
            if ch == '\\':
                self.advance(2)
                continue
            if ch == '%':
                self.skip_comment()
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    body = self.src[start + 1:self.pos]
                    self.advance()
                    return body
            self.advance()
        self.errors.append({'line': self.line, 'key': 'api_latex_group_unclosed'})
        # 花括号没闭合：**不能把游标倒回原位**。倒回去的话，`{{{{{{` 这种输入
        # 会让上层在同一步上原地重扫——实测一发六字符的垃圾耗掉 270 秒。
        # 正确做法是把剩下的都当这一组的内容吃掉（内容不丢），游标必须往前走。
        body = self.src[start + 1:]
        self.pos = len(self.src)
        self.line = self.src[:self.pos].count('\n') + 1
        return body

    def read_optional(self):
        """读 [ ... ]（可选参数），没有就返回 None。"""
        saved = self.pos
        self.skip_spaces()
        if self.at('['):
            self.advance()
            depth = 1
            start = self.pos
            while not self.eof() and depth:
                ch = self.advance()
                if ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
            return self.src[start: self.pos - 1] if depth == 0 else self.src[start:self.pos]
        self.pos = saved
        return None

    # -------------------------------------------------------------- 文档骨架
    def parse_document(self):
        blocks = []
        title = author = date = None
        maketitle = False
        toc_at = None
        in_document = False
        docclass = {'name': 'article', 'options': []}
        while not self.eof():
            before = self.pos
            node = self._parse_block()
            if self.pos == before:
                # 游标一步没动就再走一次会原地打转：吃掉一个字符继续，最坏是少排一个字，
                # 好过把整个转换卡死（解析器任何输入都必须终止）
                self.advance()
            if isinstance(node, tuple):
                kind, payload = node
                if kind == 'preamble':
                    continue
                if kind == 'title':
                    title = payload
                    continue
                if kind == 'author':
                    author = payload
                    continue
                if kind == 'date':
                    date = payload
                    continue
                if kind == 'maketitle':
                    maketitle = True
                    continue
                if kind == 'toc':
                    toc_at = len(blocks)
                    continue
                if kind == 'docclass':
                    docclass = payload
                    continue
                if kind == 'begin_document':
                    in_document = True
                    continue
                if kind == 'end_document':
                    break
                node = None
            if node is not None:
                blocks.append(node)
        self._number(blocks)
        return {
            'blocks': blocks, 'title': title, 'author': author, 'date': date,
            'maketitle': maketitle, 'toc_at': toc_at, 'in_document': in_document,
            'documentclass': docclass,
            'fontsize': self.preamble_flags.get('fontsize', 11),
            'paper': self.preamble_flags.get('paper', 'a4'),
            'twocolumn': self.preamble_flags.get('twocolumn', False),
            'parindent': self.preamble_flags.get('parindent'),
            'parskip': self.preamble_flags.get('parskip'),
            'unknown': self.unknown, 'ignored': self.ignored, 'errors': self.errors,
            'footnotes': self.footnotes,
        }

    # -------------------------------------------------------------- 编号
    def _number(self, blocks):
        """把章节号、图表号、公式号补上（TeX 的编号是二次编译算的，这里一趟就够）。"""
        counters = {'sec': [0, 0, 0, 0, 0, 0], 'fig': [0], 'tab': [0], 'eq': [0]}
        self._number_walk(blocks, counters)

    def _number_walk(self, blocks, counters):
        for b in blocks:
            t = b.get('t')
            if t == 'section':
                level = int(b.get('level') or 1)
                if b.get('numbered'):
                    idx = min(max(level, 1), 6) - 1
                    counters['sec'][idx] += 1
                    for j in range(idx + 1, 6):
                        counters['sec'][j] = 0
                    b['number'] = '.'.join(str(counters['sec'][k])
                                           for k in range(idx + 1))
                else:
                    b['number'] = None
            elif t == 'figure':
                counters['fig'][0] += 1
                b['number'] = counters['fig'][0]
            elif t == 'table':
                counters['tab'][0] += 1
                b['number'] = counters['tab'][0]
            elif t == 'display_math':
                if b.get('numbered'):
                    counters['eq'][0] += 1
                    b['number'] = counters['eq'][0]
                else:
                    b['number'] = None
            elif t == 'list':
                counters['enum'] = 0
                for item in b.get('items') or []:
                    if b.get('env') == 'enumerate':
                        counters['enum'] = counters.get('enum', 0) + 1
                        item['number'] = counters['enum']
            for key in ('blocks',):
                if isinstance(b.get(key), list):
                    self._number_walk(b[key], counters)
            if t == 'included' and isinstance(b.get('blocks'), list):
                self._number_walk(b['blocks'], counters)

    # -------------------------------------------------------------- 块级
    def _parse_block(self):
        """读一个块级节点；空白返回 None，前缀命令返回 ('preamble', ...) 之类的标记。"""
        self._skip_blank()
        if self.eof():
            return None
        if self.at('\\['):
            return self._read_display_math()
        if self.at('\\begin{'):
            return self._parse_environment()
        if self.at('\\'):
            m = re.match(r'\\([A-Za-z@*]+)', self.src[self.pos:])
            cmd = m.group(1) if m else ''
            if cmd == 'begin':
                return self._parse_environment()        # 游标留在反斜杠上
            if cmd == 'end' or cmd in self._BLOCK_CMDS:
                self.advance(1 + len(cmd))              # 吃掉「\ + 命令名」
                return self._parse_block_command(cmd)
            # 其余反斜杠命令一律交给段落：那里才知道 \caption、\includegraphics
            # 这些「块位置上其实是行内」的东西该怎么挂节点
            return self._parse_paragraph()
        # 普通段落：读到空行 / \begin / \end / 结构命令为止
        return self._parse_paragraph()

    def _skip_blank(self):
        """跳过空白与注释行。"""
        while not self.eof():
            ch = self.src[self.pos]
            if ch in ' \t\r\n':
                self.advance()
            elif ch == '%':
                self.skip_comment()
            else:
                return

    def _at_blank_line(self):
        """当前位置往后是不是「空行」（段落结束）。"""
        i = self.pos
        seen_newline = False
        while i < len(self.src):
            ch = self.src[i]
            if ch in ' \t\r':
                i += 1
            elif ch == '\n':
                if seen_newline:
                    return True
                seen_newline = True
                i += 1
            elif ch == '%':
                j = self.src.find('\n', i)
                if j < 0:
                    return False
                i = j
            else:
                return False
        return False

    def _parse_block_command(self, cmd):
        """块位置上的命令。返回节点、段落，或 ('title', inline) 这类标记元组。"""
        if cmd == 'end':
            name = (self.read_group() or '').strip()
            if name == 'document':
                return ('end_document', True)
            self.errors.append({'line': self.line, 'key': 'api_latex_env_stray_end',
                                'args': {'name': name}})
            return ('preamble', None)
        if cmd in ('begin',):
            return self._parse_environment()
        if cmd in ('section', 'subsection', 'subsubsection', 'paragraph',
                   'subparagraph', 'part', 'chapter'):
            numbered = True
            if self.at('*'):
                self.advance()
                numbered = False
            arg = self.read_group()
            node = {'t': 'section', 'level': S.SECTIONING.get(cmd, 1),
                    'numbered': numbered, 'title': self.parse_inline(arg or ''),
                    'line': self.line}
            return node
        if cmd in ('title', 'author', 'date'):
            arg = self.read_group()
            return (cmd, self.parse_inline(arg or ''))
        if cmd == 'maketitle':
            return ('maketitle', True)
        if cmd == 'tableofcontents':
            return ('toc', True)
        if cmd in ('newpage', 'pagebreak', 'clearpage', 'cleardoublepage'):
            return {'t': 'newpage'}
        if cmd in ('documentclass', 'usepackage', 'RequirePackage'):
            opt = self.read_optional()
            name = self.read_group()
            if cmd == 'documentclass':
                opts = [o.strip() for o in (opt or '').split(',') if o.strip()]
                for o in opts:
                    if o in ('10pt', '11pt', '12pt', '14pt'):
                        self.preamble_flags['fontsize'] = float(o[:-2])
                    if o in ('a4paper', 'letterpaper', 'legalpaper', 'b5paper'):
                        self.preamble_flags['paper'] = o.replace('paper', '')
                    if o == 'twocolumn':
                        self.preamble_flags['twocolumn'] = True
                return ('docclass', {'name': (name or 'article').strip(), 'options': opts})
            self.ignored.append({'cmd': cmd, 'arg': name, 'line': self.line})
            return ('preamble', None)
        if cmd in ('input', 'include'):
            # LaTeX 两种写法都有：\input{file} 与 \input file；先按花括号读，
            # 读不到再吃一个裸词——只吃一个字符会把 '{' 本身当成文件名
            arg = self.read_group()
            if arg is None:
                m = re.match(r'[^\s{}]+', self.src[self.pos:])
                arg = self.advance(len(m.group())) if m else ''
            return self._include(cmd, (arg or '').strip())
        if cmd == 'setlength':
            name = self.read_group()
            value = self.read_group()
            if name and value:
                key = name.strip().lstrip('\\')
                if key in ('parindent', 'parskip'):
                    self.preamble_flags[key] = self._dim_pt(value)
                elif key in ('columnsep', 'textwidth', 'textheight', 'oddsidemargin',
                             'topmargin', 'itemindent', 'labelsep', 'labelwidth'):
                    self.ignored.append({'cmd': 'setlength', 'arg': name, 'line': self.line})
            return ('preamble', None)
        if cmd in ('renewcommand', 'providecommand', 'newcommand', 'newenvironment'):
            return self._define_macro(cmd)
        if cmd in ('bibliographystyle', 'bibliography', 'addbibresource',
                   'graphicspath', 'DeclareMathOperator', 'newtheorem',
                   'thispagestyle', 'pagestyle', 'appendix', 'sloppy',
                   'raggedbottom', 'flushbottom', 'hbadness', 'vbadness',
                   'emergencystretch', 'nocite'):
            self.read_optional()
            self.read_group()
            self.ignored.append({'cmd': cmd, 'line': self.line})
            return ('preamble', None)
        if cmd == 'centering':
            return ('preamble', None)
        if cmd == 'noindent':
            return ('preamble', None)
        # 落到行内处理：段落里继续读，块位置上的行内命令也当段落排
        inline = self.parse_inline_until_block()
        if inline:
            return {'t': 'para', 'inline': inline}
        return ('preamble', None)

    def _include(self, cmd, path):
        r"""\input/\include：能读到就递归，读不到如实记进 ignored。"""
        if not path:
            return ('preamble', None)
        if not self.read_file:
            self.ignored.append({'cmd': cmd, 'arg': path, 'line': self.line,
                                 'key': 'api_latex_input_skipped'})
            return ('preamble', None)
        if self._depth >= self.MAX_DEPTH:
            self.errors.append({'line': self.line, 'key': 'api_latex_input_too_deep',
                                'args': {'max': self.MAX_DEPTH}})
            return ('preamble', None)
        try:
            text = self.read_file(path if path.endswith('.tex') else path + '.tex')
        except Exception:
            self.ignored.append({'cmd': cmd, 'arg': path, 'line': self.line,
                                 'key': 'api_latex_input_missing'})
            return ('preamble', None)
        if not text:
            self.ignored.append({'cmd': cmd, 'arg': path, 'line': self.line,
                                 'key': 'api_latex_input_missing'})
            return ('preamble', None)
        sub = Parser(text, read_file=self.read_file)
        sub._depth = self._depth + 1
        blocks = []
        while not sub.eof():
            node = sub._parse_block()
            if node is None or isinstance(node, tuple):
                continue
            blocks.append(node)
        self.unknown.extend({'cmd': u['cmd'], 'line': u['line'], 'from': path}
                            for u in sub.unknown)
        self.ignored.extend(sub.ignored)
        self.errors.extend(sub.errors)
        self.footnotes.extend(sub.footnotes)
        return {'t': 'included', 'from': path, 'blocks': blocks}

    _depth = 0

    def _define_macro(self, cmd):
        r"""\newcommand{\foo}[n]{定义}：只支持无可选参数的简单宏；其余如实记 ignored。"""
        name = self.read_group() or self.read_group(braces=False)
        if cmd == 'renewcommand' and name and name.lstrip('\\') in self.macros:
            pass
        opt = self.read_optional()
        body = self.read_group()
        key = (name or '').strip().lstrip('\\')
        if not key or body is None:
            self.ignored.append({'cmd': cmd, 'arg': key, 'line': self.line})
            return ('preamble', None)
        if opt is not None and '[' in (opt or ''):
            # 带可选参数的宏（\newcommand{\x}[2][def]{...}）不支持，说明而非静默
            self.ignored.append({'cmd': cmd, 'arg': key, 'line': self.line,
                                 'key': 'api_latex_macro_optional_skipped'})
        params = 0
        if opt:
            try:
                params = int(''.join(ch for ch in opt if ch.isdigit()) or '0')
            except ValueError:
                params = 0
        self.macros[key] = (params, body)
        return ('preamble', None)

    def _dim_pt(self, value):
        r"""把 `1.5cm`/`12pt`/`2mm`/`1in`/`\\baselineskip` 这类长度换成 pt；量不出来给 None。"""
        text = (value or '').strip()
        if text.startswith('\\'):
            return {'baselineskip': 13.6, 'smallskipamount': 4.0,
                    'medskipamount': 8.0, 'bigskipamount': 16.0}.get(text[1:].strip())
        m = re.match(r'^(-?\d+(?:\.\d+)?)\s*(pt|mm|cm|in|em|ex|bp)$', text)
        if not m:
            return None
        num, unit = float(m.group(1)), m.group(2)
        factor = {'pt': 1.0, 'bp': 1.0, 'mm': 2.8346, 'cm': 28.346,
                  'in': 72.0, 'em': num and 1.0, 'ex': 0.5}[unit]
        if unit == 'em':
            return num * self.preamble_flags.get('fontsize', 11)
        return num * factor

    # -------------------------------------------------------------- 环境
    def _parse_environment(self):
        r"""`\begin{name}` 起头（游标还在反斜杠上）：吃掉 \begin 与名字，再交给下半段。"""
        self.advance(len('\\begin'))
        self.skip_spaces()
        name = (self.read_group() or '').strip()
        opt = self.read_optional()
        return self._parse_environment_after_begin(name, opt)

    def _parse_environment_after_begin(self, name, opt=None):
        r"""环境名已经读到，只剩 `... \end{name}` 那一段。"""
        if not name:
            self.errors.append({'line': self.line, 'key': 'api_latex_env_unnamed'})
            return None
        if name == 'document':
            return ('begin_document', True)
        handler = getattr(self, '_env_' + re.sub(r'[^a-zA-Z]', '', name), None)
        if handler:
            return handler(name, opt)
        if name in S.VERBATIM_ENVS:
            return self._env_verbatim(name, opt)
        if name in S.MATH_ENVS:
            return self._env_math(name, opt)
        if name in ('table', 'tabular', 'array'):
            return self._env_tabular(name, opt)
        if name in ('figure',):
            return self._env_figure(name, opt)
        # 未知环境：内容照常排，环境名记进 unknown（不丢内容）
        self.unknown.append({'cmd': 'begin{' + name + '}', 'line': self.line})
        blocks = self._parse_blocks_until_end(name)
        return {'t': 'unknown_env', 'name': name, 'blocks': blocks}

    def _read_until_end(self, name):
        r"""把正文读到匹配的 \\end{name} 为止，返回原文（未闭合就读到结尾并记错误）。"""
        start = self.pos
        start_line = self.line
        stack = [name]
        pat = re.compile(r'\\(?:begin|end)\{([^{}]*)\}')
        while True:
            m = pat.search(self.src, self.pos)
            if not m:
                break
            tag, inner = m.group(0), m.group(1).strip()
            if tag.startswith('\\begin'):
                stack.append(inner)
            else:
                if stack and stack[-1] == inner:
                    stack.pop()
                    if not stack:
                        body = self.src[start:m.start()]
                        line_breaks = body.count('\n')
                        self.pos = m.start() + len(tag)
                        self.line = start_line + line_breaks
                        return body
                else:
                    # \end 找不到对应的 \begin：记一条错误，继续当正文
                    self.errors.append({'line': self.line,
                                        'key': 'api_latex_env_mismatch',
                                        'args': {'name': inner}})
            self.pos = m.end()
            self.line = self.src[:self.pos].count('\n') + 1
        body = self.src[start:]
        self.pos = len(self.src)
        self.line = self.src[:self.pos].count('\n') + 1
        self.errors.append({'line': start_line, 'key': 'api_latex_env_unclosed',
                            'args': {'name': name}})
        return body

    def _parse_blocks_until_end(self, name):
        body = self._read_until_end(name)
        sub = Parser(body, read_file=self.read_file)
        sub._depth = self._depth
        sub.nest = self.nest
        sub.macros = self.macros
        blocks = []
        while not sub.eof():
            node = sub._parse_block()
            if node is None or isinstance(node, tuple):
                continue
            blocks.append(node)
        self.unknown.extend(sub.unknown)
        self.ignored.extend(sub.ignored)
        self.errors.extend(sub.errors)
        self.footnotes.extend(sub.footnotes)
        return blocks

    def _env_itemize(self, name, opt):
        return self._list_env(name, opt, 'itemize')

    def _env_enumerate(self, name, opt):
        return self._list_env(name, opt, 'enumerate')

    def _env_description(self, name, opt):
        return self._list_env(name, opt, 'description')

    def _list_env(self, name, opt, kind):
        body = self._read_until_end(name)
        items = []
        # \item 切条目；\item[标记] 给自定义标记（description 里是 \item[标题]）
        parts = re.split(r'\\item\s*(\[[^\]]*\])?', body)
        head = parts[0]
        if head.strip() and not head.strip().startswith('%'):
            # 条目之前的杂字（常是注释或空白）：非空就照排，不丢
            inline = self.parse_inline(head)
            if any(n.get('t') != 'space' for n in inline):
                items.append({'label': None, 'body': inline})
        for i in range(1, len(parts), 2):
            mark = parts[i]
            text = parts[i + 1] if i + 1 < len(parts) else ''
            label = None
            if mark:
                label = self.parse_inline(mark.strip()[1:-1])
            items.append({'label': label, 'body': self.parse_inline(text),
                          'numbered': kind == 'enumerate'})
        return {'t': 'list', 'env': kind, 'items': items, 'line': self.line}

    def _env_center(self, name, opt):
        return {'t': 'center', 'blocks': self._parse_blocks_until_end(name)}

    def _env_flushleft(self, name, opt):
        return {'t': 'flush', 'align': 'left', 'blocks': self._parse_blocks_until_end(name)}

    def _env_flushright(self, name, opt):
        return {'t': 'flush', 'align': 'right', 'blocks': self._parse_blocks_until_end(name)}

    def _env_quote(self, name, opt):
        return {'t': 'quote', 'blocks': self._parse_blocks_until_end(name)}

    def _env_quotation(self, name, opt):
        return {'t': 'quote', 'indent': True, 'blocks': self._parse_blocks_until_end(name)}

    def _env_verbatim(self, name, opt):
        body = self._read_until_end(name)
        lines = body.replace('\r\n', '\n').split('\n')
        if lines and not lines[0].strip():
            lines.pop(0)
        if lines and not lines[-1].strip():
            lines.pop()
        return {'t': 'verbatim', 'lines': lines, 'lang': (opt or '').strip()}

    def _env_math(self, name, opt):
        body = self._read_until_end(name)
        numbered = not name.endswith('*') and name not in ('displaymath',)
        rows = []
        for row in re.split(r'\\\\|\n\n', body):
            cells = [self._parse_math(c) for c in row.split('&')]
            if any(cells):
                rows.append(cells)
        if not rows:
            rows = [[self._parse_math(body)]]
        return {'t': 'display_math', 'env': name, 'rows': rows,
                'numbered': numbered, 'line': self.line}

    def _env_tabular(self, name, opt):
        if name in ('tabular', 'array'):
            spec = (self.read_group() or '').strip()
            body = self._read_until_end(name)
        else:
            # table 浮动体：里面通常套一个 tabular + caption
            body = self._read_until_end(name)
            sub = Parser(body, read_file=self.read_file)
            sub._depth = self._depth
            inner = None
            caption = None
            while not sub.eof():
                node = sub._parse_block()
                if isinstance(node, dict) and node.get('t') == 'table':
                    inner = node
                elif isinstance(node, dict) and node.get('t') == 'para':
                    for n in node['inline']:
                        if n.get('t') == 'caption':
                            caption = n
                elif isinstance(node, tuple) and node[0] == 'preamble':
                    continue
            if inner is None:
                inner = {'t': 'table', 'spec': '', 'rows': []}
            inner['caption'] = caption
            inner['float'] = True
            return inner
        return self._build_table(spec, body)

    def _build_table(self, spec, body):
        rows = []
        hlines = []
        for raw_row in re.split(r'\\\\', body):
            line = raw_row.strip()
            if not line:
                continue
            marks = []
            # \hline / \cline{2-3} 记在「这一行之前」
            for m in re.finditer(r'\\(hline|cline)(\{[^}]*\})?', line):
                marks.append((m.group(1), m.group(2) or ''))
            line = re.sub(r'\\(?:hline|cline)(?:\{[^}]*\})?', '', line).strip()
            if marks:
                hlines.append((len(rows), marks))
            if not line:
                continue
            cells = [self.parse_inline(c) for c in _split_cells(line)]
            rows.append(cells)
        return {'t': 'table', 'spec': spec, 'rows': rows, 'hlines': hlines,
                'line': self.line}

    def _env_figure(self, name, opt):
        body = self._read_until_end(name)
        sub = Parser(body, read_file=self.read_file)
        sub._depth = self._depth
        graphics = []
        caption = None
        while not sub.eof():
            node = sub._parse_block()
            if isinstance(node, dict) and node.get('t') == 'para':
                for n in node['inline']:
                    if n.get('t') == 'includegraphics':
                        graphics.append(n)
                    elif n.get('t') == 'caption':
                        caption = n
            elif isinstance(node, dict) and node.get('t') == 'figure':
                graphics.extend(node.get('graphics') or [])
                caption = node.get('caption') or caption
        return {'t': 'figure', 'graphics': graphics, 'caption': caption,
                'line': self.line}

    # -------------------------------------------------------------- 段落与行内
    def _parse_paragraph(self):
        inline = self.parse_inline_until_block()
        if not inline:
            # 只剩空白/注释：推进一个字符避免死循环
            if not self.eof():
                self.advance()
            return None
        return {'t': 'para', 'inline': inline}

    def parse_inline_until_block(self):
        r"""读到「下一个块级结构」为止（\\begin、\\end、空行、章节命令）。"""
        return self._inline(loop_breakers=True)

    # 这些命令只能在块的位置出现：段落里读到就要回头，交回块级循环处理
    _BLOCK_CMDS = frozenset(S.SECTIONING) | frozenset(
        ('title', 'author', 'date', 'maketitle', 'tableofcontents', 'newpage',
         'pagebreak', 'clearpage', 'cleardoublepage', 'documentclass', 'usepackage',
         'RequirePackage', 'setlength', 'newcommand', 'renewcommand', 'providecommand',
         'newenvironment', 'input', 'include', 'bibliographystyle', 'bibliography',
         'appendix', 'thispagestyle', 'pagestyle'))

    def _at_block_command(self):
        r"""游标在 `\` 上：往后看是不是一个「只该出现在块位置」的命令。"""
        m = re.match(r'\\([A-Za-z@*]+)', self.src[self.pos:])
        return bool(m) and m.group(1) in self._BLOCK_CMDS

    def parse_inline(self, text):
        """把一段文本解析成行内节点（独立小解析器，不动主游标）。

        深度闸：`{{{{{{{{…` 这种一路钻下去会把 Python 的递归栈撑爆
        （实测一千层花括号直接 RecursionError）。超过上限就把剩下的**原样当文字**，
        内容一个字不丢，只是不再解释样式。
        """
        if self.nest >= MAX_NEST:
            return [{'t': 'txt', 'v': ''.join(_apply_ligatures(text or '')),
                     'line': self.line}]
        sub = Parser(text)
        sub.macros = self.macros
        sub.nest = self.nest + 1
        out = sub._inline(loop_breakers=False)
        self.unknown.extend(sub.unknown)
        self.ignored.extend(sub.ignored)
        self.errors.extend(sub.errors)
        self.footnotes.extend(sub.footnotes)
        return out

    def _inline(self, loop_breakers):
        out = []
        buf = []
        line = self.line

        def flush():
            if not buf:
                return
            text = ''.join(buf)
            buf.clear()
            for piece in _apply_ligatures(text):
                out.append({'t': 'txt', 'v': piece, 'line': line})

        while not self.eof():
            ch = self.src[self.pos]
            if ch == '%':
                self.skip_comment()
                continue
            if ch == '\n':
                # 换行 = 空格；连续空行 = 段落结束
                self.advance()
                if loop_breakers and self._at_blank_line():
                    flush()
                    return out
                buf.append(' ')
                continue
            if loop_breakers and self.at('\\begin{'):
                flush()
                return out
            if loop_breakers and self.at('\\end{'):
                flush()
                return out
            if loop_breakers and self.at('\\['):
                # 行间公式单独成块，别把它吃进段落（否则紧跟其后的 \section 会被当行内命令）
                flush()
                return out
            if ch == '\\':
                if loop_breakers and self._at_block_command():
                    # 章节/标题这类块命令紧贴上一段落下（中间没有空行）时会被当行内命令吃掉，
                    # 于是「\section」进了 unknown、那一节再也不是标题。先回头再判。
                    flush()
                    return out
                self.advance()
                cmd = self.read_command_name()
                node = self._inline_command(cmd, out, flush, loop_breakers)
                if node is _STOP:
                    flush()
                    return out
                continue
            if ch == '$':
                flush()
                out.append(self._parse_dollar_math())
                continue
            if ch == '{':
                flush()
                body = self.read_group()
                inner = self.parse_inline(body if body is not None else '')
                out.extend(inner)
                continue
            if ch == '}':
                # 多出来的右括号：记一条错误并当字面量排出来，不吞
                self.errors.append({'line': self.line, 'key': 'api_latex_stray_brace'})
                self.advance()
                buf.append('}')
                continue
            if ch == '~':
                flush()
                out.append({'t': 'nbsp'})
                self.advance()
                continue
            if ch == '&':
                if loop_breakers:
                    flush()
                    out.append({'t': 'amp'})
                    self.advance()
                    continue
                buf.append('&')
                self.advance()
                continue
            if ch == "'":
                buf.append("'")
                self.advance()
                continue
            if ch == '^' or ch == '_':
                # 文本模式里的上下标：LaTeX 直接报错，我们照字面排并记一条说明
                self.errors.append({'line': self.line, 'key': 'api_latex_text_script'})
                buf.append(ch)
                self.advance()
                continue
            line = self.line
            buf.append(self.advance())
        flush()
        return out

    def _inline_command(self, cmd, out, flush, loop_breakers):
        r"""行内位置的反斜杠命令。返回 _STOP 表示要收段落（\end{document} 之类）。"""
        if not cmd:
            return None
        # `\\` 是换行，不是「转义出来的反斜杠」——必须排在 ESCAPES 之前判
        if cmd == '\\':
            opt = self.read_optional()
            flush()
            out.append({'t': 'br', 'gap': self._dim_pt(opt) if opt else None})
            return None
        # 转义字符
        if cmd in S.ESCAPES:
            flush()
            out.append({'t': 'txt', 'v': S.ESCAPES[cmd], 'line': self.line})
            return None
        if cmd in ('begin', 'end'):
            # 段落中间出现环境：把它当块排进来，内容绝不因为「这里只串行内」而丢掉
            return self._inline_env_fallback(cmd, out)
        if cmd == '[':
            flush()
            out.append(self._read_display_math())
            return None
        if cmd == ']':
            return None          # 多余的 \]（配对的 \[ 已经吃掉它了）
        if cmd == 'par':
            flush()
            out.append({'t': 'parbreak'})
            return None
        if cmd == 'item':
            mark = self.read_optional()
            flush()
            out.append({'t': 'item', 'label': self.parse_inline(mark[1:-1]) if mark else None})
            return None
        if cmd in S.STYLE_CMDS:
            style = S.STYLE_CMDS[cmd]
            if cmd == 'verb':
                delim = self.advance()
                start = self.pos
                idx = self.src.find(delim, start) if delim else -1
                raw = self.src[start:idx] if idx > 0 else ''
                self.pos = (idx + 1) if idx > 0 else len(self.src)
                flush()
                out.append({'t': 'run', 'style': 'tt',
                            'body': [{'t': 'txt', 'v': raw}]})
                return None
            arg = self.read_group()
            flush()
            out.append({'t': 'run', 'style': style,
                        'body': self.parse_inline(arg if arg is not None else '')})
            if cmd in ('textbf', 'textit', 'texttt', 'textsf', 'textrm', 'emph',
                       'underline', 'textnormal') and arg is None:
                self.errors.append({'line': self.line, 'key': 'api_latex_arg_missing',
                                    'args': {'cmd': cmd}})
            return None
        if cmd in S.STYLE_SWITCHES:
            flush()
            out.append({'t': 'style_switch', 'style': S.STYLE_SWITCHES[cmd]})
            return None
        if cmd in ('textcolor', 'color'):
            self.read_group()          # 颜色名丢掉，文字留下
            arg = self.read_group()
            flush()
            out.append({'t': 'txt', 'v': ''.join(_plain_text(self.parse_inline(arg or ''))),
                        'line': self.line})
            self.ignored.append({'cmd': cmd, 'arg': 'color', 'line': self.line})
            return None
        if cmd in ('footnote',):
            arg = self.read_group()
            self.footnotes.append({'line': self.line,
                                   'body': self.parse_inline(arg or '')})
            flush()
            out.append({'t': 'footnote_mark', 'no': len(self.footnotes)})
            return None
        if cmd in ('ref', 'eqref', 'pageref', 'cite'):
            key = self.read_group() or ''
            flush()
            out.append({'t': 'ref', 'kind': cmd, 'target': key.strip(),
                        'v': '??' if cmd != 'cite' else '[?]'})
            return None
        if cmd in ('href',):
            url = self.read_group() or ''
            label = self.read_group() or ''
            flush()
            out.append({'t': 'href', 'url': url.strip(),
                        'body': self.parse_inline(label or url)})
            return None
        if cmd == 'url':
            arg = self.read_group() or ''
            flush()
            out.append({'t': 'run', 'style': 'tt',
                        'body': [{'t': 'txt', 'v': arg.strip()}]})
            return None
        if cmd == 'label':
            self.read_group()
            return None
        if cmd == 'caption':
            arg = self.read_group() or ''
            flush()
            out.append({'t': 'caption', 'body': self.parse_inline(arg)})
            return None
        if cmd == 'includegraphics':
            opt = self.read_optional()
            path = (self.read_group() or '').strip()
            flush()
            out.append({'t': 'includegraphics', 'path': path, 'opts': opt})
            return None
        if cmd in ('today',):
            flush()
            out.append({'t': 'txt', 'v': time.strftime('%Y-%m-%d'), 'line': self.line})
            return None
        if cmd in ('TeX', 'LaTeX', 'LaTeXe'):
            flush()
            out.append({'t': 'txt', 'v': {'TeX': 'TeX', 'LaTeX': 'LaTeX',
                                          'LaTeXe': 'LaTeX2e'}[cmd], 'line': self.line})
            return None
        if cmd in S.SPACING:
            flush()
            out.append({'t': 'space', 'em': S.SPACING[cmd]})
            return None
        if cmd in ('vspace', 'hspace', 'smallskip', 'medskip', 'bigskip'):
            arg = self.read_group()
            flush()
            em = {'smallskip': 0.3, 'medskip': 0.7, 'bigskip': 1.4}.get(cmd)
            pt = self._dim_pt(arg) if arg else None
            out.append({'t': 'space', 'em': None, 'pt': pt if pt is not None else em})
            return None
        if cmd in ('frac', 'dfrac', 'tfrac', 'cfrac', 'binom'):
            flush()
            # body 一律是「节点列表」；_math_frac 返回的是单个节点，忘了包一层
            # 就会让排版器遍历字典的键，报 'str' object has no attribute 'get'
            out.append({'t': 'math', 'body': [self._math_frac(cmd, force=True)]})
            return None
        if cmd == 'sqrt':
            flush()
            out.append({'t': 'math', 'body': [self._math_sqrt()]})
            return None
        if cmd in S.SYMBOLS:
            flush()
            out.append({'t': 'txt', 'v': S.SYMBOLS[cmd], 'line': self.line})
            return None
        if cmd in S.BIG_OPS or cmd in S.FUNCTIONS:
            flush()
            out.append({'t': 'txt', 'v': S.BIG_OPS.get(cmd) or cmd, 'line': self.line})
            return None
        if cmd in S.IGNORED_CMDS:
            self.read_optional()
            self.read_group()
            self.ignored.append({'cmd': cmd, 'line': self.line})
            return None
        if cmd in S.PAGE_BREAKS:
            flush()
            out.append({'t': 'newpage_inline'})
            return None
        if cmd in ('noindent', 'centering', 'raggedright', 'raggedleft', 'protect',
                   'normalsize', 'small', 'footnotesize', 'scriptsize', 'tiny',
                   'large', 'Large', 'LARGE', 'huge', 'Huge', 'sloppy', 'newline'):
            if cmd == 'newline':
                flush()
                out.append({'t': 'br'})
            return None
        if cmd == 'and':
            # \author{甲 \and 乙}：作者之间的分隔，退成「、」比留个未知命令有用
            flush()
            out.append({'t': 'txt', 'v': '、', 'line': self.line})
            return None
        # 自定义宏展开
        if cmd in self.macros:
            return self._expand_macro(cmd, out, flush)
        # 认不出来的：带花括号参数的，参数照排、命令名丢掉；**没带参数的一律原样留字**。
        # 少了后半句，`C:\temp\file.txt` 会被排成 `C:.txt`——那是把用户的字吃掉。
        arg = self.read_group()
        flush()
        node = {'t': 'unknown', 'cmd': cmd, 'line': self.line}
        if arg is not None:
            node['body'] = self.parse_inline(arg)
        else:
            out.append({'t': 'txt', 'v': '\\' + cmd, 'line': self.line})
        out.append(node)
        self.unknown.append({'cmd': cmd, 'line': self.line})
        return None

    def _expand_macro(self, cmd, out, flush):
        params, body = self.macros[cmd]
        args = [self.read_group() or '' for _ in range(params)]
        guard = self.guard if self.guard is not None else [0]
        self.guard = guard
        if guard[0] >= self.MAX_MACRO_EXPAND:
            # 自引用（宏的体里又写自己）与两个宏互相引用，会一路展开到底：
            # 到上限就停下并记一条说明，剩下的原样写字，绝不悄悄丢
            self.errors.append({'line': self.line, 'key': 'api_latex_macro_too_deep'})
            out.append({'t': 'txt', 'v': '\\' + cmd, 'line': self.line})
            return None
        guard[0] += 1
        try:
            text = body
            for i, val in enumerate(args):
                text = text.replace('#%d' % (i + 1), val)
            if re.search(r'#\d', text):
                # 参数没给全（宏声明 2 个参数却只用了 1 个）：剩下的原样留着，别造空白
                text = re.sub(r'#\d', '', text)
            sub = Parser(text)
            sub.macros = self.macros
            sub.guard = guard
            sub.nest = self.nest
            inner = sub._inline(loop_breakers=False)
        finally:
            guard[0] -= 1
        # 子解析器记下的错误/未知命令要并回主树，否则「哪儿没照做」会漏报
        self.unknown.extend(sub.unknown)
        self.ignored.extend(sub.ignored)
        self.errors.extend(sub.errors)
        self.footnotes.extend(sub.footnotes)
        out.extend(inner)
        return None

    def _inline_env_fallback(self, cmd, out):
        r"""段落里遇到 \begin{env} / \end{env}：环境整块解析后挂进这一行。

        不这么做的话，「这里只串行内」会把环境内容直接跳过——那是丢内容，不允许。
        """
        self.skip_spaces()
        name = (self.read_group() or '').strip()
        if cmd == 'end':
            # 多余的 \end：记一条错误，后面的正文继续排
            self.errors.append({'line': self.line, 'key': 'api_latex_env_stray_end',
                                'args': {'name': name}})
            return None
        saved_pos, saved_line = self.pos, self.line
        node = self._parse_environment_after_begin(name)
        if node is None:
            self.pos, self.line = saved_pos, saved_line
            return None
        out.append(node if not isinstance(node, tuple) else {'t': 'para', 'inline': []})
        return None

    def _is_document_end_hint(self):
        return False

    # -------------------------------------------------------------- 数学
    def _read_display_math(self):
        r"""`\[ ... \]` 行间公式：单独成块、不编号（equation 环境才编号）。"""
        self.advance(2)
        start = self.pos
        idx = self.src.find('\\]', start)
        if idx < 0:
            self.errors.append({'line': self.line, 'key': 'api_latex_math_unclosed'})
            body = self.src[start:]
            self.pos = len(self.src)
        else:
            body = self.src[start:idx]
            self.line = self.src[:self.pos].count('\n') + 1
            self.pos = idx + 2
        return {'t': 'display_math', 'env': 'display', 'rows': [[self._parse_math(body)]],
                'numbered': False, 'line': self.line}

    def _parse_dollar_math(self):
        self.advance()                      # 吃 $
        start = self.pos
        depth = 0
        while not self.eof():
            ch = self.src[self.pos]
            if ch == '\\':
                nxt = self.src[self.pos + 1:self.pos + 2]
                if nxt in ('(', ')', '[', ']'):
                    self.advance(2)
                    continue
                self.advance(2)
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == '$' and depth <= 0:
                body = self.src[start:self.pos]
                self.advance()
                return {'t': 'math', 'display': False, 'body': self._parse_math(body),
                        'line': self.line}
            self.advance()
        self.errors.append({'line': self.line, 'key': 'api_latex_math_unclosed'})
        return {'t': 'math', 'display': False, 'body': self._parse_math(self.src[start:]),
                'line': self.line}

    def _parse_math(self, text):
        if self.nest >= MAX_NEST:
            # 同 parse_inline 的深度闸：数学里也不许无限往里钻，剩下的原样成字
            return [{'t': 'sym', 'v': text or ''}]
        self.nest += 1
        try:
            sub = _MathParser(text, self)
            nodes = sub.parse()
        finally:
            self.nest -= 1
        self.unknown.extend(sub.unknown)
        self.errors.extend(sub.errors)
        return nodes

    def _math_frac(self, cmd, force=False):
        num = self.read_group() or ''
        den = self.read_group() or ''
        return {'t': 'frac', 'num': self._parse_math(num), 'den': self._parse_math(den),
                'small': cmd == 'tfrac', 'binom': cmd == 'binom'}

    def _math_sqrt(self):
        index = self.read_optional()
        body = self.read_group() or ''
        return {'t': 'sqrt', 'body': self._parse_math(body),
                'index': self._parse_math(index) if index else None}


_STOP = object()


class _MathParser(object):
    r"""数学式解析：一串 atom，`_`/`^` 挂到前一个 atom 上，`\\left` 配对到 `\\right`。"""

    def __init__(self, text, owner):
        self.src = text or ''
        self.pos = 0
        self.owner = owner
        self.unknown = []
        self.errors = []

    def eof(self):
        return self.pos >= len(self.src)

    def peek(self):
        return self.src[self.pos:self.pos + 1]

    def eat(self, n=1):
        """吃掉 n 个字符并**把它们返回**：`_arg()` 靠这个取单字符参数。

        早先这里只挪游标不返回，`x_i` 的参数就成了空串——上下标全部消失，
        而盒子的宽度还按有内容算，表现是「公式看着没坏但少字」。
        """
        chunk = self.src[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def parse(self):
        r"""解析成 atom 列表。

        `\left ... \right` 用栈当场配对（不是解析完再合并）：后置合并会让
        `\left(\frac{a}{b}\right)^2` 的 `^2` 挂到右括号本身而不是整对括号上。
        """
        stack = [[]]
        self._stack = stack
        self._open = []        # 与「多出来的那一层」并列：这层是哪个左括号开起来的

        def emit(node):
            stack[-1].append(node)

        while not self.eof():
            ch = self.src[self.pos]
            if ch in ' \t\r\n':
                self.eat()
                continue
            if ch == '%':
                idx = self.src.find('\n', self.pos)
                self.pos = len(self.src) if idx < 0 else idx
                continue
            if ch == '\\':
                self._command(emit)
                continue
            if ch == '{':
                grp = self._group()
                emit({'t': 'group', 'body': self.owner._parse_math(grp)})
                continue
            if ch == '}':
                self.errors.append({'line': 1, 'key': 'api_latex_stray_brace'})
                self.eat()
                continue
            if ch == '$':
                self.eat()
                continue
            if ch == '&':
                emit({'t': 'alignmark'})
                self.eat()
                continue
            if ch == '^' or ch == '_':
                self._script(emit, ch)
                continue
            if ch == "'":
                self._script(emit, '^')
                self.eat()
                continue
            if ch.isalpha():
                emit({'t': 'mi', 'v': ch})
                self.eat()
                continue
            if ch.isdigit() or (ch == '.' and self.src[self.pos + 1:self.pos + 2].isdigit()):
                m = re.match(r'\d*\.?\d+', self.src[self.pos:])
                if m:
                    emit({'t': 'mn', 'v': m.group()})
                    self.eat(len(m.group()))
                    continue
                # 上标数字（²³）、带圈数字这类 `isdigit()` 为真但正则不认的字符，
                # 走下面的通用分支原样成字，别拿 None.group() 崩掉整份转换
            if ch in '+-=<>/,;:!?|':
                emit({'t': 'mo', 'v': ch})
                self.eat()
                continue
            if ch in '()[]{}' and ch not in '{}':
                emit({'t': 'delim', 'left': ch, 'right': '', 'body': None,
                      'scaled': False})
                self.eat()
                continue
            if ch in '~@':
                emit({'t': 'nbsp'})
                self.eat()
                continue
            # 其余字符（含中文）原样进正文
            emit({'t': 'mtext_run', 'v': ch})
            self.eat()
        while len(stack) > 1:               # \left 没配上 \right：内容照样排，记一条
            parts = stack.pop()
            emit({'t': 'delim', 'left': self._open.pop(), 'right': '',
                  'body': parts, 'scaled': True})
            self.errors.append({'line': 1, 'key': 'api_latex_delim_unclosed'})
        return stack[0]

    def _group(self):
        depth = 0
        start = self.pos
        while not self.eof():
            ch = self.src[self.pos]
            if ch == '\\':
                self.pos += 2
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    body = self.src[start + 1:self.pos]
                    self.eat()
                    return body
            self.pos += 1
        self.errors.append({'line': 1, 'key': 'api_latex_group_unclosed'})
        body = self.src[start:]
        self.pos = len(self.src)
        return body

    def _arg(self):
        """命令的参数：一个组、一个控制序列（`^\\infty`）、或单字符（`x^2`）。

        必须认得「上标里直接跟命令」这种写法：`\\int_0^\\infty` 的 `^\\infty`
        只吃一个字符的话，`\\` 被吞掉、`infty` 变成六个斜体字母，公式就错了。
        """
        self._skip_ws()
        if self.at_char('{'):
            return self._group()
        if self.eof():
            return ''
        if self.src[self.pos] == '\\':
            self.eat()
            m = re.match(r'[A-Za-z@]+|.', self.src[self.pos:])
            name = m.group() if m else ''
            self.pos += len(name)
            return '\\' + name
        return self.eat() or ''

    def at_char(self, ch):
        return self.pos < len(self.src) and self.src[self.pos] == ch

    def _skip_ws(self):
        while not self.eof() and self.src[self.pos] in ' \t\r\n':
            self.eat()

    def _script(self, emit, kind):
        self.eat()
        body = self._arg()
        nuc = self.owner._parse_math(body)
        nodes = self._stack[-1]
        base = nodes.pop() if nodes else {'t': 'txt', 'v': ''}
        key = 'sup' if kind == '^' else 'sub'
        if base.get('t') in ('sub', 'sup', 'subsup'):
            # x^2_3：合并成 subsup，别让上标套下标。sub/sup 节点的内容都存在 'nuc' 里，
            # 这里取错键就会 KeyError（第一版就是这么炸的）
            if base.get('t') == 'sub':
                merged = {'t': 'subsup', 'base': base['base'], 'sub': base['nuc'],
                          'sup': nuc}
            elif base.get('t') == 'sup':
                merged = {'t': 'subsup', 'base': base['base'], 'sub': nuc,
                          'sup': base['nuc']}
            else:
                merged = dict(base)
                merged[key] = nuc
            nodes.append(merged)
            return
        if base.get('t') == 'bigop':
            base[key + '_limit'] = nuc
            nodes.append(base)
            return
        nodes.append({'t': key, 'base': base, 'nuc': nuc})

    def _command(self, emit):
        self.eat()
        m = re.match(r'[A-Za-z@]+|.', self.src[self.pos:])
        if not m:
            # 反斜杠正好在末尾（`$x\` 这种被截断的写法）：当字面量留下，
            # 拿 m.group() 会 AttributeError
            emit({'t': 'sym', 'v': '\\'})
            return
        cmd = m.group()
        self.pos += len(cmd)
        if cmd in ('frac', 'dfrac', 'tfrac', 'cfrac', 'binom'):
            num = self._arg()
            den = self._arg()
            emit({'t': 'frac', 'num': self.owner._parse_math(num),
                  'den': self.owner._parse_math(den),
                  'small': cmd == 'tfrac', 'binom': cmd == 'binom'})
            return
        if cmd == 'sqrt':
            self._skip_ws()
            index = None
            if self.at_char('['):
                self.eat()
                start = self.pos
                idx = self.src.find(']', start)
                index = self.src[start:idx if idx > 0 else len(self.src)]
                self.pos = (idx + 1) if idx > 0 else len(self.src)
            body = self._arg()
            emit({'t': 'sqrt', 'body': self.owner._parse_math(body),
                  'index': self.owner._parse_math(index) if index else None})
            return
        if cmd in ('text', 'mbox', 'textrm', 'textnormal'):
            body = self._arg()
            emit({'t': 'mtext', 'body': self.owner.parse_inline(body)})
            return
        if cmd in ('mathrm', 'mathbf', 'mathit', 'mathtt', 'mathsf', 'boldsymbol',
                   'operatorname'):
            body = self._arg()
            style = {'mathrm': 'rm', 'mathbf': 'bf', 'mathit': 'it', 'mathtt': 'tt',
                     'mathsf': 'sf', 'boldsymbol': 'bf', 'operatorname': 'rm'}[cmd]
            emit({'t': 'mstyle', 'style': style,
                  'body': self.owner._parse_math(body)})
            return
        if cmd in ('left', 'middle', 'right'):
            self._skip_ws()
            delim = self._delim_text()
            if cmd == 'left':
                # 开一层：之后的 atom 先进这一层，配上 \right 再收成一个大括号节点
                self._stack.append([])
                self._open.append(delim)
            elif cmd == 'right':
                if len(self._stack) > 1:
                    parts = self._stack.pop()
                    emit({'t': 'delim', 'left': self._open.pop(), 'right': delim,
                          'body': parts, 'scaled': True})
                else:
                    self.errors.append({'line': 1, 'key': 'api_latex_delim_stray_right'})
                    emit({'t': 'delim', 'left': delim, 'right': '', 'body': None,
                          'scaled': True})
            else:
                emit({'t': 'delim', 'left': delim, 'right': '', 'body': None,
                      'scaled': True})
            return
        if cmd in S.DRAWN_DELIMS:
            emit({'t': 'delim', 'left': S.DRAWN_DELIMS[cmd], 'right': '',
                  'body': None, 'scaled': False})
            return
        if cmd in S.ACCENTS:
            glyph, kind = S.ACCENTS[cmd]
            body = self._arg()
            emit({'t': 'accent', 'cmd': cmd, 'glyph': glyph, 'kind': kind,
                  'body': self.owner._parse_math(body)})
            return
        if cmd == 'over':                       # 老式写法 a \over b
            emit({'t': 'over'})
            return
        if cmd in S.SPACING:
            emit({'t': 'space', 'em': S.SPACING[cmd]})
            return
        if cmd in ('kern', 'mkern', 'mspace'):
            arg = self._arg()
            emit({'t': 'space', 'em': None,
                  'pt': self.owner._dim_pt(arg) or 0.0})
            return
        if cmd in S.BIG_OPS:
            emit({'t': 'bigop', 'cmd': cmd, 'v': S.BIG_OPS[cmd]})
            return
        if cmd in S.FUNCTIONS:
            emit({'t': 'mo', 'v': cmd, 'func': True})
            return
        if cmd in S.SYMBOLS:
            emit({'t': 'sym', 'v': S.SYMBOLS[cmd], 'cmd': cmd})
            return
        if cmd in S.ESCAPES:
            emit({'t': 'mo' if cmd in '#&' else 'sym', 'v': S.ESCAPES[cmd]})
            return
        if cmd in ('displaystyle', 'textstyle', 'scriptstyle', 'scriptscriptstyle',
                   'limits', 'nolimits', 'centering', 'label'):
            return
        if cmd in S.IGNORED_CMDS:
            self.owner.ignored.append({'cmd': cmd, 'line': 1})
            return
        if cmd in self.owner.macros:
            guard = self.owner.guard or [0]
            self.owner.guard = guard
            if guard[0] >= self.owner.MAX_MACRO_EXPAND:
                emit({'t': 'sym', 'v': '\\' + cmd})
                self.owner.errors.append({'line': 1,
                                          'key': 'api_latex_macro_too_deep'})
                return
            params, body = self.owner.macros[cmd]
            args = [self._arg() for _ in range(params)]
            for i, val in enumerate(args):
                body = body.replace('#%d' % (i + 1), val)
            guard[0] += 1
            try:
                sub = _MathParser(re.sub(r'#\d', '', body), self.owner)
                for node in sub.parse():
                    emit(node)
            finally:
                guard[0] -= 1
            self.unknown.extend(sub.unknown)
            return
        # 认不出来的数学命令：参数照排
        body = None
        if self.at_char('{') or not self.eof():
            body = self._arg()
        node = {'t': 'unknown', 'cmd': cmd}
        if body:
            node['body'] = self.owner._parse_math(body)
        emit(node)
        self.unknown.append({'cmd': cmd, 'line': 1})

    def _delim_text(self):
        r"""\left( / \left\{ / \left| / \left. 里的定界符。"""
        if self.eof():
            return '.'
        ch = self.src[self.pos]
        if ch == '\\':
            self.eat()
            m = re.match(r'[A-Za-z]+|.', self.src[self.pos:])
            name = m.group()
            self.pos += len(name)
            return {'{}': '{', 'lbrace': '{', 'rbrace': '}', 'langle': '〈',
                    'rangle': '〉', 'vert': '|', 'Vert': '‖', '|': '|',
                    'lfloor': 'floor-left', 'rfloor': 'floor-right',
                    'lceil': 'ceil-left', 'rceil': 'ceil-right'}.get(name, name[:1])
        self.eat()
        return ch


def _split_cells(line):
    """按不在括号里的 & 切表格单元格。"""
    cells = []
    buf = []
    depth = 0
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '\\' and i + 1 < len(line):
            buf.append(ch)
            buf.append(line[i + 1])
            i += 2
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        if ch == '&' and depth == 0:
            cells.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append(''.join(buf))
    return cells


def _apply_ligatures(text):
    """`---`/`--`/``/'' 连字，切回一段段文本（保持顺序）。"""
    out = [text]
    for src, dst in S.LIGATURES:
        expanded = []
        for piece in out:
            if src in piece:
                expanded.extend(_replace_keep_order(piece, src, dst))
            else:
                expanded.append(piece)
        out = expanded
    return [p for p in out if p != '']


def _replace_keep_order(text, src, dst):
    parts = text.split(src)
    res = []
    for i, p in enumerate(parts):
        if i:
            res.append(dst)
        res.append(p)
    return res


def _plain_text(inline):
    """行内节点摊平成纯文本（丢颜色那类命令要用一次，别再造第二棵树）。"""
    out = []
    for n in inline:
        t = n.get('t')
        if t == 'txt':
            out.append(n.get('v', ''))
        elif t in ('run', 'mtext', 'unknown', 'href', 'caption'):
            out.append(_plain_text(n.get('body') or []))
        elif t == 'nbsp':
            out.append(' ')
        elif t == 'math':
            out.append(_plain_math(n.get('body') or []))
    return ''.join(out)


def _plain_math(nodes):
    """数学树摊平成纯文本。

    大写运算符的上下限、括号本身的字符都要一起出来：漏了它们，
    「tex → pdf → 读回文字」的往返就会少内容，而少内容在这里等于判错。
    """
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            out.append(str(n or ''))
            continue
        t = n.get('t')
        if t in ('mi', 'mn', 'mo', 'sym', 'bigop'):
            out.append(str(n.get('v', '')))
        elif t == 'delim':
            for key in ('left', 'right'):
                ch = n.get(key) or ''
                if not ch or ch == '.':
                    continue
                out.append({'floor-left': '[', 'floor-right': ']',
                            'ceil-left': '[', 'ceil-right': ']'}.get(ch, ch))
        elif t == 'mtext':
            out.append(_plain_text(n.get('body') or []))
        elif t == 'space':
            out.append(' ')
        for key in ('body', 'num', 'den', 'base', 'nuc', 'sub', 'sup', 'index',
                    'sub_limit', 'sup_limit'):
            v = n.get(key)
            if isinstance(v, list):
                out.append(_plain_math(v))
            elif isinstance(v, dict):
                out.append(_plain_math([v]))
    return ''.join(out)


def parse(text, read_file=None):
    """模块入口：.tex 文本 → 文档树。"""
    return Parser(text, read_file=read_file).parse_document()
