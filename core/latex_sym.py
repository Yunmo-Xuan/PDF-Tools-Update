# -*- coding: utf-8 -*-
"""LaTeX 命令与符号表：软件里所有「吃 LaTeX / 吐 LaTeX」的地方共用这一份数据。

四条口径（改这张表前先读）：

1. **最终发出去的每个字符都必须内置字体真画得出来。** 界面字体走 PyMuPDF 自带的
   `china-s`（Droid Sans Fallback）与 Base-14 十四面。本模块把「LaTeX 原意」写在
   `IDEAL_*` 里，把 `china-s` 缺的那 34 枚字符经 `UNSUPPORTED_SUBS` 退成等价写法，
   合成对外的 `SYMBOLS`。`FORCED` 是这份退让的账（命令 → 原字符 / 实际字符），
   `tests/latex_check.py` 逐字符拿 `font.has_glyph()` 复核：**表里出现一个画不出来的
   字符就红**，空白方块到不了用户屏幕上。
2. **认不出来的一律进 `unknown`，不许静默丢。** 表里没有的命令由解析器原样收集
   （命令名 + 它带的参数 + 行号），排版时把参数里的文字照样排出来并在回包里列明，
   绝不出现「少了一段内容但界面说成功」。
3. **上下标、分式、根号、括号放大这些不靠字形，靠排版器画线段**（见
   `core/latex_render.py`），所以 `\\lfloor` 那类成对结构在表里不给字符，
   由 `DRAWN_STRUCTURES` 点名交给排版器画。
4. **反方向（PDF → .tex）的转义表在 `core/latex_out.py`**，两边必须能对上，
   往返一致性由测试钉住。
"""

# ---------------------------------------------------------------- 希腊字母
IDEAL_GREEK = {
    'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ', 'epsilon': 'ε',
    'varepsilon': 'ϵ', 'zeta': 'ζ', 'eta': 'η', 'theta': 'θ', 'vartheta': 'ϑ',
    'iota': 'ι', 'kappa': 'κ', 'lambda': 'λ', 'mu': 'μ', 'nu': 'ν',
    'xi': 'ξ', 'pi': 'π', 'rho': 'ρ', 'sigma': 'σ', 'tau': 'τ',
    'upsilon': 'υ', 'phi': 'φ', 'varphi': 'ϕ', 'chi': 'χ', 'psi': 'ψ', 'omega': 'ω',
    'Gamma': 'Γ', 'Delta': 'Δ', 'Theta': 'Θ', 'Lambda': 'Λ', 'Xi': 'Ξ',
    'Pi': 'Π', 'Sigma': 'Σ', 'Upsilon': 'Υ', 'Phi': 'Φ', 'Psi': 'Ψ', 'Omega': 'Ω',
}

# ------------------------------------------------------------ 运算符与关系
IDEAL_OPERATORS = {
    'pm': '±', 'mp': '∓', 'times': '×', 'div': '÷', 'ast': '∗', 'star': '⋆',
    'circ': '∘', 'bullet': '∙', 'cdot': '·', 'oplus': '⊕', 'otimes': '⊗', 'odot': '⊙',
    'leq': '≤', 'le': '≤', 'geq': '≥', 'ge': '≥', 'neq': '≠', 'ne': '≠',
    'approx': '≈', 'equiv': '≡', 'sim': '∼', 'simeq': '≃', 'cong': '≅',
    'propto': '∝', 'll': '≪', 'gg': '≫', 'doteq': '≐',
    'in': '∈', 'notin': '∉', 'ni': '∋', 'subset': '⊂', 'supset': '',
    'subseteq': '⊆', 'supseteq': '⊇', 'sqsubset': '⊏', 'sqsupset': '⊐',
    'cap': '∩', 'cup': '∪', 'setminus': '∖', 'emptyset': '∅', 'varnothing': '∅',
    'land': '∧', 'wedge': '∧', 'lor': '∨', 'vee': '∨', 'neg': '¬', 'lnot': '¬',
    'forall': '∀', 'exists': '∃', 'nexists': '∄', 'mid': '∣', 'parallel': '∥',
    'perp': '⊥', 'angle': '∠', 'triangle': '△', 'square': '□', 'diamond': '◇',
    'therefore': '∴', 'backslash': '\\', 'smallsetminus': '∖',
    'dagger': '†', 'ddagger': '‡', 'copyright': '©', 'textregistered': '®',
    'pounds': '£', 'textyen': '¥', 'euro': '€', 'degree': '°',
    'dotplus': '∔', 'ltimes': '⋉', 'rtimes': '⋊', 'leftthreetimes': '⋋',
    'rightthreetimes': '⋌', 'curlyvee': '', 'curlywedge': '⋏',
    'ominus': '⊖', 'oslash': '⊘', 'circledast': '⊛',
    'boxminus': '⊟', 'boxplus': '⊞', 'boxtimes': '⊠',
    'veebar': '⊻', 'barwedge': '⊼', 'doublebarwedge': '',
    'less': '<', 'gtr': '>', 'leqq': '≤', 'geqq': '≥',
}

# ---------------------------------------------------------------- 箭头
IDEAL_ARROWS = {
    'to': '→', 'rightarrow': '→', 'longrightarrow': '→', 'Rightarrow': '⇒',
    'leftarrow': '←', 'gets': '←', 'Leftarrow': '⇐',
    'leftrightarrow': '↔', 'Leftrightarrow': '⇔', 'mapsto': '↦',
    'longmapsto': '⟼', 'uparrow': '↑', 'downarrow': '↓',
    'Uparrow': '⇑', 'Downarrow': '⇓', 'nearrow': '↗', 'searrow': '↘',
    'swarrow': '↙', 'nwarrow': '↖', 'hookrightarrow': '↪', 'hookleftarrow': '↩',
    'implies': '⇒', 'iff': '⇔', 'rightharpoonup': '⇀', 'leftharpoonup': '↼',
    'rightarrowtail': '↣', 'twoheadrightarrow': '↠',
}

# ---------------------------------------------------------------- 大写运算符
# display 模式下上下限放在符号的正上/正下方，行内模式退化成上下标
IDEAL_BIG_OPS = {
    'sum': '∑', 'prod': '∏', 'coprod': '∐', 'bigcup': '∪', 'bigcap': '∩',
    'bigoplus': '⊕', 'bigotimes': '⊗', 'biguplus': '⊎', 'bigsqcup': '⊔',
    'int': '∫', 'iint': '∬', 'iiint': '∭', 'oint': '∮',
    'lim': 'lim', 'limsup': 'lim sup', 'liminf': 'lim inf',
    'max': 'max', 'min': 'min', 'sup': 'sup', 'inf': 'inf',
    'det': 'det', 'gcd': 'gcd', 'deg': 'deg', 'dim': 'dim', 'ker': 'ker',
    'Pr': 'Pr',
}
# 这些是「函数名」：排出来用正体（不是斜体变量）
FUNCTIONS = ('sin', 'cos', 'tan', 'cot', 'sec', 'csc', 'arcsin', 'arccos', 'arctan',
             'sinh', 'cosh', 'tanh', 'coth', 'log', 'ln', 'lg', 'exp', 'arg')

# ---------------------------------------------------------------- 杂项
IDEAL_MISC = {
    'infty': '∞', 'partial': '∂', 'nabla': '∇', 'surd': '√',
    'ldots': '…', 'dots': '…', 'cdots': '⋯', 'vdots': '⋮', 'ddots': '⋱',
    'prime': '′', 'backprime': '‵', 'hbar': 'ℏ', 'ell': 'ℓ',
    'Reals': 'ℝ', 'Nats': 'ℕ', 'Ints': 'ℤ', 'Rationals': 'ℚ', 'Complexes': 'ℂ',
    'aleph': 'ℵ', 'wp': '℘', 'Im': 'ℑ', 'Re': 'ℛ',
    'checkmark': '✓', 'bigstar': '★', 'male': '♂', 'female': '♀',
    'textbullet': '•', 'textbackslash': '\\', 'textasciitilde': '~',
    'textasciicircum': '^', 'textbar': '|', 'textbraceleft': '{',
    'textbraceright': '}', 'textless': '<', 'textgreater': '>',
    'langle': '⟨', 'rangle': '⟩', 'vert': '|', 'Vert': '‖',
    'backslashalt': '\\', 'S': '§', 'P': '¶', 'sect': '§', 'para': '¶',
    'middot': '·', 'textdegree': '°', 'texttrademark': '™',
    'quad': ' ', 'qquad': '  ', 'space': ' ',
}

# ------------------------------------------------- 内置字体画不出来的字符 → 退让
# 键是「LaTeX 原意那个字符」，值是「china-s 画得出的最接近写法」。
# 名单来自实测：`fitz.Font('china-s').has_glyph()` 逐字符过筛。
UNSUPPORTED_SUBS = {
    '∓': '±',   # \mp
    '⋆': '∗',   # \star
    '∘': '○',   # \circ
    '∙': '•',   # \bullet
    '≃': '≈',   # \simeq
    '≐': '=',   # \doteq
    '∄': '∃',   # \nexists（否定意味由上下文给，不留空白）
    '⊏': '⊂',   # \sqsubset
    '⊐': '⊃',   # \sqsupset
    '∖': '\\',  # \setminus
    '∔': '+',   # \dotplus
    '⋉': '×',   # \ltimes
    '⋊': '×',   # \rtimes
    '⋋': '×',   # \leftthreetimes
    '⋌': '×',   # \rightthreetimes
    '⋎': '∨',   # \curlyvee
    '⋏': '∧',   # \curlywedge（退成空串等于把符号吃掉，不许）
    '⊛': '⊗',   # \circledast
    '⊟': '-',   # \boxminus（⊖ 与数学减号都画不出来，直接退到 ASCII 减号）
    '⊖': '-',   # \ominus
    '⊘': '○',   # \oslash
    '⊞': '+',   # \boxplus
    '⊠': '×',   # \boxtimes
    '⊻': '∨',   # \veebar
    '⊼': '∧',   # \barwedge
    '⩘': '∧',   # \doublebarwedge
    '∐': '∏',   # \coprod
    '⊎': '∪',   # \biguplus
    '⊔': '∪',   # \bigsqcup
    '∭': '∫∫∫',  # \iiint
    '⋯': '…',   # \cdots
    '⋮': '…',   # \vdots
    '⋱': '…',   # \ddots
    '↦': '→',   # \mapsto
    '⟼': '→',   # \longmapsto
    '↩': '←',   # \hookleftarrow
    '↪': '→',   # \hookrightarrow
    '↼': '←',   # \leftharpoonup
    '↣': '→',   # \rightarrowtail
    '↠': '→',   # \twoheadrightarrow
    'ℏ': 'h',   # \hbar
    '℘': 'P',   # \wp
    'ℑ': 'I',   # \Im
    'ℛ': 'R',   # \Re
    'ℝ': 'R',   # \Reals / \mathbb{R}
    'ℕ': 'N',   # \Nats
    'ℤ': 'Z',   # \Ints
    'ℚ': 'Q',   # \Rationals
    'ℂ': 'C',   # \Complexes
    'ϵ': 'ε',   # \varepsilon
    'ϕ': 'φ',   # \varphi
    '⌈': '[',   # \lceil（排版器会画，这里是漏网兜底）
    '⌉': ']',
    '⌊': '[',
    '⌋': ']',
    '⟨': '〈',   # \langle
    '⟩': '〉',   # \rangle
    '‑': '-',   # 不换行连字符
    '−': '-',   # 数学减号
}


def _coerce(ch):
    return UNSUPPORTED_SUBS.get(ch, ch)


def _build(ideal):
    out = {}
    for cmd, ch in ideal.items():
        out[cmd] = ''.join(_coerce(c) for c in ch)
    return out


GREEK = _build(IDEAL_GREEK)
OPERATORS = _build(IDEAL_OPERATORS)
ARROWS = _build(IDEAL_ARROWS)
BIG_OPS = _build(IDEAL_BIG_OPS)
MISC = _build(IDEAL_MISC)

# 合并成一张「命令 → 字符」的总表；后写的同名命令覆盖前面的（杂项最后，符号类优先）
SYMBOLS = {}
SYMBOLS.update(GREEK)
SYMBOLS.update(OPERATORS)
SYMBOLS.update(ARROWS)
SYMBOLS.update(MISC)

# 退让的账：命令 → (LaTeX 原字符, 实际用的字符)，界面「能力清单」和测试都用它
FORCED = {}
for _ideal, _real in ((IDEAL_GREEK, GREEK), (IDEAL_OPERATORS, OPERATORS),
                      (IDEAL_ARROWS, ARROWS), (IDEAL_BIG_OPS, BIG_OPS),
                      (IDEAL_MISC, MISC)):
    for _cmd, _want in _ideal.items():
        _got = _real[_cmd]
        if _got != _want:
            FORCED[_cmd] = (_want, _got)

# 数学模式里直接写的 ASCII 组合（`<=` 这类）
MATH_ASCII = {'<=': '≤', '>=': '≥', '!=': '≠', '...': '…'}

# ---------------------------------------------------------------- 间距命令
# 值是「相对当前字号的 em 倍数」；负数是收紧
SPACING = {
    ',': 0.167, ':': 0.222, ';': 0.278, '!': -0.167,
    'quad': 1.0, 'qquad': 2.0, 'thinspace': 0.167, 'negthinspace': -0.167,
    'enspace': 0.5, 'emspace': 1.0, 'hspace': 0.5,
}

# ---------------------------------------------------------------- 重音命令
# kind='rule' 由排版器画线（横线/下划线），kind='glyph' 取字形放在符号上方
ACCENTS = {
    'hat': ('^', 'glyph'), 'widehat': ('^', 'glyph'),
    'tilde': ('~', 'glyph'), 'widetilde': ('~', 'glyph'),
    'bar': (None, 'rule'), 'overline': (None, 'rule'),
    'vec': ('→', 'glyph'), 'dot': ('·', 'glyph'), 'ddot': ('··', 'glyph'),
    'check': ('ˇ', 'glyph'), 'breve': ('˘', 'glyph'), 'acute': ('´', 'glyph'),
    'grave': ('`', 'glyph'), 'mathring': ('˚', 'glyph'),
    'overrightarrow': ('→', 'glyph'), 'overleftarrow': ('←', 'glyph'),
    'underline': (None, 'rule'), 'underbrace': (None, 'rule'),
    'overbrace': (None, 'rule'),
}

# ---------------------------------------------------------------- 字体与强调
STYLE_CMDS = {
    'textbf': 'bf', 'bfseries': 'bf', 'mathbf': 'bf', 'boldsymbol': 'bf',
    'textit': 'it', 'emph': 'it', 'itshape': 'it', 'mathit': 'it',
    'texttt': 'tt', 'ttfamily': 'tt', 'mathtt': 'tt', 'verb': 'tt',
    'textsf': 'sf', 'sffamily': 'sf', 'mathsf': 'sf',
    'textrm': 'rm', 'rmfamily': 'rm', 'mathrm': 'rm', 'operatorname': 'rm',
    'textnormal': 'rm', 'normalfont': 'rm',
    'underline': 'ul', 'em': 'it', 'textsc': 'rm',   # 小型大写没有第二套面，退成正体
}
# 无参数、只影响其后文字的开关命令
STYLE_SWITCHES = {
    'bfseries': 'bf', 'itshape': 'it', 'ttfamily': 'tt', 'sffamily': 'sf',
    'rmfamily': 'rm', 'normalfont': 'rm', 'em': 'it',
}

# ---------------------------------------------------------------- 结构
SECTIONING = {'part': 0, 'chapter': 0, 'section': 1, 'subsection': 2,
              'subsubsection': 3, 'paragraph': 4, 'subparagraph': 5}
BLOCK_ARG_CMDS = {'caption': 'caption', 'title': 'title', 'author': 'author',
                  'date': 'date', 'footnote': 'footnote', 'thanks': 'footnote'}

LIST_ENVS = ('itemize', 'enumerate', 'description')
VERBATIM_ENVS = ('verbatim', 'verbatim*', 'lstlisting', 'minted', 'alltt')
MATH_ENVS = ('equation', 'equation*', 'align', 'align*', 'gather', 'gather*',
             'multline', 'multline*', 'eqnarray', 'eqnarray*', 'displaymath')
PAGE_BREAKS = ('newpage', 'pagebreak', 'clearpage', 'cleardoublepage')

# 忽略但要如实说明的命令（要外部宏包/二次编译才有效果）
IGNORED_CMDS = (
    'usepackage', 'RequirePackage', 'documentclass', 'input', 'include',
    'bibliographystyle', 'bibliography', 'addbibresource',
    'listoffigures', 'listoftables',
    'eqref', 'pageref', 'nocite', 'bibitem', 'hyperref', 'graphicspath',
    'setlength', 'setcounter', 'value', 'protect', 'noindent',
    'raggedright', 'raggedleft', 'centering', 'newline', 'linebreak',
    'nobreak', 'sloppy', 'raggedbottom', 'flushbottom', 'appendix',
    'thispagestyle', 'pagestyle', 'emergencystretch', 'hbadness', 'vbadness',
    'renewcommand', 'providecommand', 'DeclareMathOperator', 'newtheorem',
)
# 这些命令解析器特殊处理（不算「忽略」，也不在 SYMBOLS 里）
SPECIAL_CMDS = ('frac', 'dfrac', 'tfrac', 'cfrac', 'binom', 'sqrt', 'text', 'mbox',
                'item', 'begin', 'end', 'newcommand', 'label', 'ref', 'cite', 'href',
                'url', 'caption', 'title', 'author', 'date', 'maketitle',
                'tableofcontents', 'includegraphics', 'vspace', 'hspace',
                'smallskip', 'medskip', 'bigskip', 'input', 'include')

# 转义：反斜杠 + 它们 = 那个字符本身
ESCAPES = {'\\': '\\', '&': '&', '%': '%', '$': '$', '#': '#', '_': '_',
           '{': '{', '}': '}', '~': ' ', '^': '^'}
# 照 TeX 习惯的连字
LIGATURES = (('---', '—'), ('--', '–'), ('``', '“'), ("''", '”'))

# 内置字体没有、必须由排版器画出来的成对结构
DRAWN_STRUCTURES = ('lfloor', 'rceil', 'lceil', 'rfloor')
# \lfloor 一类命令 → 画哪一种
DRAWN_DELIMS = {'lfloor': 'floor-left', 'rfloor': 'floor-right',
                'lceil': 'ceil-left', 'rceil': 'ceil-right'}

# 中日韩统一表意文字 + 常用扩展标点：排版器按这个决定用哪套字体
_CJK_RANGES = ((0x3000, 0x303F), (0x3040, 0x30FF), (0x3400, 0x4DBF),
               (0x4E00, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF),
               (0xFF00, 0xFFEF), (0x20000, 0x2FA1F))


def is_cjk(ch):
    """这个字符要不要走中文字体。"""
    if not ch:
        return False
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def has_cjk(text):
    return any(is_cjk(ch) for ch in text or '')


def sanitize(text):
    """把内置字体画不出来的字符换成等价写法（表里不该再有漏网的）。

    这张表由 `UNSUPPORTED_SUBS` 单向下发，两处不会各写一份名单而对不上。
    """
    if not text:
        return text
    for src, dst in UNSUPPORTED_SUBS.items():
        if src in text:
            text = text.replace(src, dst)
    return text


def symbol_of(name):
    """命令名（不含反斜杠）→ 字符；不在表里返回 None。"""
    return SYMBOLS.get(name)


def all_commands():
    """这张表认得的全部命令名（测试与「能力清单」用）。"""
    names = set(SYMBOLS) | set(SPACING) | set(ACCENTS) | set(STYLE_CMDS)
    names |= set(IGNORED_CMDS) | set(SPECIAL_CMDS) | set(BIG_OPS)
    names |= set(FUNCTIONS) | set(SECTIONING) | set(DRAWN_DELIMS)
    return names


def supported_chars():
    """对外表里出现过的全部非 ASCII 字符（测试逐条验 has_glyph 用）。"""
    out = set()
    for tbl in (SYMBOLS, BIG_OPS, ACCENTS_VALUES_TABLE()):
        for v in tbl.values():
            if isinstance(v, str):
                out |= set(v)
    for v in ACCENTS.values():
        if v[0]:
            out |= set(v[0])
    out |= set(MATH_ASCII.values())
    out |= {c for _, c in LIGATURES}
    return {c for c in out if ord(c) > 0x7F and c.strip()}


def ACCENTS_VALUES_TABLE():    # noqa: N802  给 supported_chars 用的占位，保持函数签名统一
    return {}
