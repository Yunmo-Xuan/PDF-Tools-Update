# -*- coding: utf-8 -*-
r"""LaTeX 排版器：`core/latex_doc.py` 的解析树 → PDF 页面。

分工：本模块只管「摆哪儿」，不管「认不认得」——所有不认识的东西在解析阶段
就已经变成 `unknown` 节点带着参数进来了，这里一律照排，绝不吞。

三条口径

1. **数学式不靠字形，靠算。** 分式 = 分子盒 + 横线 + 分母盒；根号 = `√` 字形按内容
   高度放大 + 一条上横线；上下标 = 0.7 倍字号再按基线偏移；`\left...\right` =
   括号字号跟着内容的总高走。这些位置全部要能被测试量到（`tests/latex_check.py`
   读回成品逐条核对），所以每个盒都记自己的 height/depth，而不是画完就忘。
2. **中文没有第二套面。** 内置 `china-s` 只有一档粗细，所以 `\textbf{中文}` 排出来
   和正文一样粗——这不是 bug，是字体限制；命中一次就记一条 `api_latex_cjk_style`
   说明，界面如实告诉用户，不假装加粗成功。
3. **不联网、不下载、不调外部程序。** 图片只吃 `image_resolver` 给回来的本地路径
   （由 api 层限定在用户选中的那份 .tex 同目录里）；找不到就画占位框 + 说明文字。
"""
import os

from core import latex_sym as S

PAPERS = {'a4': (595.28, 841.89), 'letter': (612.0, 792.0), 'b5': (496.06, 765.35)}
MARGIN = {'top': 72.0, 'bottom': 72.0, 'left': 80.0, 'right': 80.0}
# 拉丁走 Base-14（不嵌字体、文件小），中文一律 china-s
FACES = {'rm': 'tiro', 'bf': 'tibo', 'it': 'tiit', 'bi': 'tibi',
         'tt': 'cour', 'sf': 'helv', 'sfbf': 'hebo', 'sfit': 'heit'}
CJK_FACE = 'china-s'
SCRIPT_SCALE = 0.7          # 上下标字号比例
SUP_RAISE = 0.45            # 上标基线上抬（相对正文字号）
SUB_LOWER = 0.22            # 下标基线下移
FRAC_RULE = 0.05            # 分式横线粗细（相对正文字号）
AXIS = 0.26                 # 分式横线在基线上多高（相对正文字号）
PAR_INDENT = 15.0           # 段首缩进（TeX article 默认 15pt）
LINE_SPACING = 1.32         # 行高 = 字号 × 这个系数
LIST_INDENT = 18.0
VERBATIM_INDENT = 18.0


class Kit(object):
    """字体对象与字宽测量；同一档面只 new 一次。"""

    def __init__(self, fitz):
        self.fitz = fitz
        self._cache = {}
        self.notes = []

    def face(self, name):
        f = self._cache.get(name)
        if f is None:
            try:
                f = self.fitz.Font(name)
            except Exception:
                f = self.fitz.Font(CJK_FACE if name == CJK_FACE else 'helv')
            self._cache[name] = f
        return f

    @staticmethod
    def faces_for(text, style, math=False):
        """这段文字该用哪几档面：按中日韩/其余切段，中文一律内置字体。

        数学模式里的变量用斜体（`tiit`），这是 TeX 的默认观感；
        中文在数学模式里仍走内置字体，不然一个字都出不来。
        """
        base = style or ('rm' if not math else 'rm')
        if math and base == 'rm':
            base = 'it' if style is None else style
        out = []
        cur = None
        buf = ''
        for ch in text:
            cjk = S.is_cjk(ch)
            key = (cjk, base)
            if key != cur:
                if buf:
                    out.append((cur, buf))
                cur, buf = key, ch
            else:
                buf += ch
        if buf:
            out.append((cur, buf))
        return out

    def face_name(self, text, style, math=False):
        """整段用同一档面时的「主面」；混排时取第一段。"""
        segs = self.faces_for(text, style, math)
        if not segs:
            return FACES.get(style or 'rm', 'tiro')
        cjk, base = segs[0][0]
        if cjk:
            return CJK_FACE
        if base == 'rm':
            return 'tiro'
        return FACES.get(style or 'rm', 'tiro')

    def width(self, text, style, size, math=False):
        if not text:
            return 0.0
        total = 0.0
        for (cjk, base), seg in self.faces_for(text, style, math):
            name = CJK_FACE if cjk else FACES.get(base if base in FACES else 'rm', 'tiro')
            if not cjk and base == 'rm':
                name = 'tiro'
            total += self.face(name).text_length(seg, fontsize=size)
        return total


class Box(object):
    """一个排版盒：宽 width，基线以上 height，基线以下 depth，里面若干绘制指令。

    指令坐标都相对「盒左沿 + 盒基线」，装进行的时候整体平移。
    """

    __slots__ = ('width', 'height', 'depth', 'ops')

    def __init__(self, width=0.0, height=0.0, depth=0.0, ops=None):
        self.width = width
        self.height = height
        self.depth = depth
        self.ops = ops or []


class Renderer(object):
    """把解析树排成一页页指令，再写进 PDF。"""

    def __init__(self, doc, fitz, base_size=None, paper=None, image_resolver=None,
                 should_stop=None, on_progress=None):
        self.doc = doc or {}
        self.fitz = fitz
        self.kit = Kit(fitz)
        self.base = float(base_size or doc.get('fontsize') or 11)
        self.paper = paper or doc.get('paper') or 'a4'
        self.image_resolver = image_resolver
        self.should_stop = should_stop
        self.on_progress = on_progress
        self.parindent = doc.get('parindent')
        if self.parindent is None:
            self.parindent = PAR_INDENT
        self.parskip = doc.get('parskip') or 0.0
        self.notes = []
        self._note_keys = set()
        self._cjk_style_noted = False

    # -------------------------------------------------------------- 说明登记
    def note(self, key, **args):
        """同一条说明只记一次（一份文档里 `\foo` 出现 50 次不该刷 50 屏）。"""
        tag = (key, tuple(sorted(args.items())))
        if tag in self._note_keys:
            return
        self._note_keys.add(tag)
        self.notes.append({'key': key, 'args': args})

    # -------------------------------------------------------------- 入口
    def render(self, out_path):
        pages = self.layout()
        pw, ph = PAPERS.get(self.paper, PAPERS['a4'])
        doc = self.fitz.open()
        try:
            for items in pages:
                page = doc.new_page(width=pw, height=ph)
                self._draw_page(page, items)
            doc.subset_fonts()
            doc.save(out_path, garbage=3, deflate=True)
        finally:
            doc.close()
        return {'pages': len(pages), 'notes': self.notes}

    # -------------------------------------------------------------- 版面
    def layout(self):
        pw, ph = PAPERS.get(self.paper, PAPERS['a4'])
        self.text_width = pw - MARGIN['left'] - MARGIN['right']
        self.page_height = ph
        blocks = list(self.doc.get('blocks') or [])
        if self.doc.get('maketitle'):
            blocks = self._title_blocks() + blocks
        toc_at = self.doc.get('toc_at')
        if toc_at is not None:
            blocks.insert(min(toc_at, len(blocks)), {'t': 'toc', 'entries': []})
        pages = [[]]
        self.pages = pages
        self.cur = pages[0]
        self.y = MARGIN['top']
        self._emit_title = None
        for b in blocks:
            if self.should_stop and self.should_stop():
                self.note('api_latex_cancelled')
                break
            self._place_block(b, pages)
            if self.on_progress:
                self.on_progress(1)
        if self.doc.get('footnotes'):
            self._place_footnotes(pages)
        if not pages[0]:
            pages = [[]]
        # 目录需要真实页码：算完正文才有，回填后再排一趟目录那一页
        if toc_at is not None:
            self._fill_toc(pages)
        return pages

    def _title_blocks(self):
        out = []
        if self.doc.get('title'):
            out.append({'t': 'title_line', 'body': self.doc['title'], 'size': self.base * 1.55,
                        'style': 'bf'})
        if self.doc.get('author'):
            out.append({'t': 'title_line', 'body': self.doc['author'], 'size': self.base,
                        'style': 'rm'})
        if self.doc.get('date'):
            out.append({'t': 'title_line', 'body': self.doc['date'], 'size': self.base * 0.9,
                        'style': 'rm'})
        out.append({'t': 'title_rule'})
        return out

    def _fill_toc(self, pages):
        """把「第几页」回填进目录条目：条目位置来自正文里的 section 节点。"""
        entries = []
        for pi, items in enumerate(pages):
            for it in items:
                if it.get('kind') == 'section_mark':
                    entries.append((it['level'], it['number'], it['text'], pi + 1))
        for items in pages:
            for it in items:
                if it.get('kind') == 'toc':
                    it['entries'] = entries

    def _break_page(self):
        """开新的一页。

        必须把新列表挂回 `self.pages`：早先这里只 `self.cur = []`，
        游标指向一个没人引用的空表，翻页之后的所有正文就凭空蒸发——
        成品只剩画线、一个字都没有。
        """
        if not self.cur:
            return False
        self.cur = []
        self.pages.append(self.cur)
        self.y = MARGIN['top']
        return True

    def _need(self, height):
        """剩余空间放不下就翻页。"""
        limit = self.page_height - MARGIN['bottom']
        if self.y + height <= limit:
            return False
        return self._break_page()

    def _push(self, item):
        self.cur.append(item)

    def _place_block(self, b, pages):
        t = b.get('t')
        if t == 'para':
            self._place_para(b.get('inline') or [], pages)
        elif t == 'section':
            self._place_section(b, pages)
        elif t == 'list':
            self._place_list(b, pages)
        elif t == 'table':
            self._place_table(b, pages)
        elif t == 'figure':
            self._place_figure(b, pages)
        elif t == 'verbatim':
            self._place_verbatim(b, pages)
        elif t == 'display_math':
            self._place_display_math(b, pages)
        elif t == 'center':
            self._place_group(b.get('blocks') or [], 'center', pages)
        elif t == 'flush':
            self._place_group(b.get('blocks') or [], b.get('align') or 'left', pages)
        elif t == 'quote':
            self._place_group(b.get('blocks') or [], 'quote', pages)
        elif t == 'newpage':
            self._break_page()
        elif t == 'title_line':
            self._place_title_line(b, pages)
        elif t == 'title_rule':
            self._space(8)
            self._push({'kind': 'rule', 'x0': MARGIN['left'], 'x1': MARGIN['left'] + self.text_width,
                        'y': self.y + 4, 'w': 0.6})
            self.y += 14
        elif t == 'toc':
            self._place_toc(b, pages)
        elif t == 'included':
            for sub in b.get('blocks') or []:
                self._place_block(sub, pages)
        elif t in ('unknown_env',):
            self.note('api_latex_env_unsupported', name=b.get('name') or '')
            for sub in b.get('blocks') or []:
                self._place_block(sub, pages)
        else:
            # 兜底：任何没单独处理的块，只要有 inline 就照排，不丢内容
            inline = b.get('inline')
            if inline:
                self._place_para(inline, pages)

    def _space(self, pt):
        self.y += pt

    # -------------------------------------------------------------- 段落
    def _place_title_line(self, b, pages):
        box = self._inline_box(b.get('body') or [], b.get('size') or self.base,
                               b.get('style') or 'rm')
        self._need(box.height + box.depth + 4)
        x = MARGIN['left'] + max(0.0, (self.text_width - box.width) / 2.0)
        baseline = self.y + box.height
        self._push({'kind': 'box', 'x': x, 'baseline': baseline, 'box': box})
        self.y += box.height + box.depth + 4

    def _place_section(self, b, pages):
        level = int(b.get('level') or 1)
        size = self.base * {1: 1.32, 2: 1.16, 3: 1.06, 4: 1.0}.get(min(level, 4), 1.0)
        before = {1: 14.0, 2: 10.0, 3: 7.0}.get(min(level, 3), 6.0)
        prefix = (b.get('number') + '  ') if b.get('number') else ''
        text_box = self._inline_box(b.get('title') or [], size, 'bf')
        head_h = size * 1.2
        # 标题别落在页面最后一行（孤零零一个标题没正文很难看）
        if self.y + before + head_h + size * LINE_SPACING * 2 > self.page_height - MARGIN['bottom']:
            self._break_page()
        self.y += before
        label = self.kit_box_text(prefix, size, 'bf')
        x = MARGIN['left'] + label.width
        self._push({'kind': 'box', 'x': MARGIN['left'], 'baseline': self.y + size,
                    'box': label})
        self._push({'kind': 'box', 'x': x, 'baseline': self.y + size, 'box': text_box})
        self.y += head_h
        if self.should_stop and self.should_stop():
            return
        self._push({'kind': 'section_mark', 'level': level, 'number': b.get('number') or '',
                    'text': _plain(b.get('title') or [])})

    def _place_para(self, inline, pages, indent=None, align='left', extra_indent=0.0):
        words = self._words(inline, self.base, 'rm')
        if not words:
            self._space(self.base * 0.4)
            return
        first = indent if indent is not None else self.parindent
        lines = _break_lines(words, self.text_width - extra_indent, first)
        for i, line in enumerate(lines):
            height = max((w['box'].height for w in line), default=self.base * 0.7)
            depth = max((w['box'].depth for w in line), default=self.base * 0.25)
            line_h = max(height + depth, self.base * LINE_SPACING)
            self._need(line_h)
            baseline = self.y + height
            width = sum(w['box'].width for w in line) + sum(w.get('gap', 0.0) for w in line)
            x = MARGIN['left'] + extra_indent
            if align == 'center':
                x = MARGIN['left'] + max(0.0, (self.text_width - width) / 2.0)
            elif align == 'right':
                x = MARGIN['left'] + max(0.0, self.text_width - extra_indent - width)
            elif align == 'quote':
                x += 0.0
            for w in line:
                box = w['box']
                self._push({'kind': 'box', 'x': x, 'baseline': baseline, 'box': box})
                x += box.width + w.get('gap', 0.0)
            self.y += line_h
        self.y += self.parskip + 2.0

    def _place_group(self, blocks, style, pages):
        align = {'center': 'center', 'left': 'left', 'right': 'right',
                 'quote': 'left'}[style] if style in ('center', 'left', 'right', 'quote') else 'left'
        indent = LIST_INDENT * 1.6 if style == 'quote' else 0.0
        for b in blocks:
            if b.get('t') == 'para':
                self._place_para(b.get('inline') or [], pages, align=align,
                                 extra_indent=indent,
                                 indent=0.0 if style != 'quote' else self.parindent)
            else:
                self._place_block(b, pages)
        if style == 'quote':
            self._space(4)

    def _place_list(self, b, pages):
        env = b.get('env') or 'itemize'
        for idx, item in enumerate(b.get('items') or []):
            if env == 'enumerate':
                mark = '%d.' % (item.get('number') or idx + 1)
            elif env == 'description':
                mark = ''
            else:
                mark = '•'
            label_box = self._inline_box(item.get('label') or [], self.base, 'bf') \
                if (env == 'description' and item.get('label')) else \
                self.kit_box_text(mark, self.base, 'rm')
            body_words = self._words(item.get('body') or [], self.base, 'rm')
            if env == 'description' and item.get('label'):
                body_words = [{'box': label_box, 'gap': 6.0, 'brk': False}] + body_words
            lines = _break_lines(body_words, self.text_width - LIST_INDENT, 0.0)
            if not lines:
                lines = [[]]
            for li, line in enumerate(lines):
                height = max((w['box'].height for w in line), default=self.base * 0.7)
                depth = max((w['box'].depth for w in line), default=self.base * 0.25)
                line_h = max(height + depth, self.base * LINE_SPACING)
                self._need(line_h)
                baseline = self.y + height
                if li == 0 and env != 'description':
                    self._push({'kind': 'box', 'x': MARGIN['left'], 'baseline': baseline,
                                'box': label_box})
                x = MARGIN['left'] + LIST_INDENT
                for w in line:
                    self._push({'kind': 'box', 'x': x, 'baseline': baseline, 'box': w['box']})
                    x += w['box'].width + w.get('gap', 0.0)
                self.y += line_h
            self.y += 1.5

    def _place_verbatim(self, b, pages):
        size = self.base * 0.92
        for line in b.get('lines') or ['']:
            box = self.kit_box_text(line, size, 'tt', raw=True)
            # 等宽环境不折行也不吃命令：原样一行
            self._need(size * LINE_SPACING)
            self._push({'kind': 'box', 'x': MARGIN['left'] + VERBATIM_INDENT,
                        'baseline': self.y + size * 0.8, 'box': box})
            self.y += size * LINE_SPACING
        self._space(4)

    def _place_display_math(self, b, pages):
        rows = b.get('rows') or []
        number = ('(%s)' % b['number']) if b.get('number') else None
        num_box = self.kit_box_text(number or '', self.base, 'rm')
        for row in rows:
            boxes = [self._math_box(cells, self.base, display=True) for cells in row] \
                if isinstance(row, list) else []
            if not boxes:
                continue
            # align 用 & 分成「右 | 左」两栏，其余环境整行居中
            if len(boxes) == 2 and len(row) == 2:
                left_w = boxes[0].width
                right_w = boxes[1].width
                total = left_w + right_w + 8.0
                x = MARGIN['left'] + max(0.0, (self.text_width - total) / 2.0)
                height = max(bx.height for bx in boxes)
                depth = max(bx.depth for bx in boxes)
                line_h = max(height + depth, self.base * LINE_SPACING)
                self._need(line_h)
                baseline = self.y + height
                self._push({'kind': 'box', 'x': x, 'baseline': baseline, 'box': boxes[0]})
                self._push({'kind': 'box', 'x': x + left_w + 8.0, 'baseline': baseline,
                            'box': boxes[1]})
            else:
                box = boxes[0]
                if len(boxes) > 1:
                    merged = _hconcat(boxes, 0.0)
                    box = merged
                line_h = max(box.height + box.depth, self.base * LINE_SPACING)
                self._need(line_h)
                x = MARGIN['left'] + max(0.0, (self.text_width - box.width - num_box.width) / 2.0)
                self._push({'kind': 'box', 'x': x, 'baseline': self.y + box.height,
                            'box': box})
            self.y += line_h
            if number:
                self._push({'kind': 'box',
                            'x': MARGIN['left'] + self.text_width - num_box.width,
                            'baseline': self.y - 2, 'box': num_box})
        self._space(4)

    def _place_toc(self, b, pages):
        head = self.kit_box_text('目录', self.base * 1.2, 'bf')
        self._need(head.height + head.depth + 8)
        self._push({'kind': 'box', 'x': MARGIN['left'], 'baseline': self.y + self.base * 1.2,
                    'box': head})
        self.y += self.base * 1.5
        for level, number, text, page in (b.get('entries') or []):
            line = '%s%s' % ((number + '  ') if number else '', text)
            box = self.kit_box_text(line, self.base, 'rm')
            dots = self.kit_box_text(' %d' % page, self.base, 'rm')
            self._need(self.base * LINE_SPACING)
            x = MARGIN['left'] + (level - 1) * 12.0
            self._push({'kind': 'box', 'x': x, 'baseline': self.y + self.base * 0.8,
                        'box': box})
            self._push({'kind': 'box', 'x': MARGIN['left'] + self.text_width - dots.width,
                        'baseline': self.y + self.base * 0.8, 'box': dots})
            self.y += self.base * LINE_SPACING
        self._space(10)

    def _place_footnotes(self, pages):
        self._need(40)
        self._push({'kind': 'rule', 'x0': MARGIN['left'], 'x1': MARGIN['left'] + 90.0,
                    'y': self.y + 6, 'w': 0.6})
        self.y += 12
        for i, fn in enumerate(self.doc.get('footnotes') or [], 1):
            mark = self.kit_box_text('%d ' % i, self.base * 0.85, 'rm')
            words = self._words(fn.get('body') or [], self.base * 0.85, 'rm')
            lines = _break_lines(words, self.text_width - 22.0, 0.0)
            for li, line in enumerate(lines):
                height = max((w['box'].height for w in line), default=self.base * 0.7)
                depth = max((w['box'].depth for w in line), default=self.base * 0.25)
                self._need(max(height + depth, self.base * LINE_SPACING))
                baseline = self.y + height
                if li == 0:
                    self._push({'kind': 'box', 'x': MARGIN['left'], 'baseline': baseline,
                                'box': mark})
                x = MARGIN['left'] + 22.0
                for w in line:
                    self._push({'kind': 'box', 'x': x, 'baseline': baseline, 'box': w['box']})
                    x += w['box'].width + w.get('gap', 0.0)
                self.y += max(height + depth, self.base * LINE_SPACING)
        self.note('api_latex_footnotes_at_end')

    # -------------------------------------------------------------- 表格
    def _place_table(self, b, pages):
        rows = b.get('rows') or []
        if not rows:
            if b.get('caption'):
                self._place_caption(b, 'table')
            return
        spec = b.get('spec') or ''
        aligns, vbars = _parse_spec(spec, len(rows[0]))
        # 先量每列的自然宽
        cells = [[self._inline_box(c, self.base, 'rm') for c in row] for row in rows]
        ncols = max(len(r) for r in cells)
        natural = [0.0] * ncols
        for row in cells:
            for i, box in enumerate(row):
                natural[i] = max(natural[i], box.width)
        pad = 6.0
        natural = [w + pad * 2 for w in natural]
        total = sum(natural)
        # 列宽按内容来，整张表居中——这是 LaTeX tabular 的样子。
        # 原来把富余宽度全灌给 c 列，两列的表会被撑成整页宽，中间空一大片。
        if total > self.text_width and total > 0:
            scale = self.text_width / total
            natural = [w * scale for w in natural]
            total = self.text_width
        x0 = MARGIN['left'] + max(0.0, (self.text_width - total) / 2.0)
        row_h = self.base * LINE_SPACING
        self._need(row_h * min(len(rows) + 2, 6))
        # 表格说明在表上方（TeX 的 table 就是这个位置）
        if b.get('caption'):
            self._place_caption(b, 'table', x0=x0, width=total)
        top = self.y
        col_x = []
        acc = x0
        for w in natural:          # natural 里已经把左右 padding 算进去了
            col_x.append(acc)
            acc += w
        for ri, row in enumerate(cells):
            for mark_idx, marks in (b.get('hlines') or []):
                if mark_idx == ri:
                    for _m in marks:
                        self._push({'kind': 'rule', 'x0': x0, 'x1': x0 + total,
                                    'y': self.y + 1.0, 'w': 0.6})
                    self.y += 3.0
            height = max((box.height for box in row), default=self.base * 0.7)
            baseline = self.y + max(height, self.base * 0.7)
            for ci, box in enumerate(row):
                if ci >= ncols:
                    break
                a = aligns[ci] if ci < len(aligns) else 'l'
                cw = natural[ci]
                if a == 'c':
                    x = col_x[ci] + pad + max(0.0, (cw - box.width) / 2.0)
                elif a == 'r':
                    x = col_x[ci] + pad + max(0.0, cw - box.width)
                else:
                    x = col_x[ci] + pad
                self._push({'kind': 'box', 'x': x, 'baseline': baseline, 'box': box})
            self.y += row_h
        for mark_idx, marks in (b.get('hlines') or []):
            if mark_idx >= len(rows):
                for _m in marks:
                    self._push({'kind': 'rule', 'x0': x0, 'x1': x0 + total,
                                'y': self.y, 'w': 0.6})
        # 竖线（列规格里的 |）：vbars 比列数多一格，最后那格是表格右沿
        for i, has_bar in enumerate(vbars):
            if not has_bar:
                continue
            if i == 0:
                x = x0
            elif i >= len(col_x):
                x = x0 + total
            else:
                x = col_x[i] - pad
            self._push({'kind': 'vline', 'x': x, 'y0': top, 'y1': self.y})
        self._push({'kind': 'rule', 'x0': x0, 'x1': x0 + total, 'y': self.y, 'w': 0.6})
        self.y += 6
        if b.get('caption') and b.get('float'):
            pass   # 已在表头排过，不重复
        elif b.get('caption'):
            self._place_caption(b, 'table', x0=x0, width=total)

    def _place_caption(self, b, kind, x0=None, width=None):
        cap = b.get('caption') or {}
        body = cap.get('body') or []
        number = b.get('number')
        label = self.kit_box_text(('表 %d  ' % number) if (kind == 'table' and number)
                                  else ('图 %d  ' % number) if (kind == 'figure' and number)
                                  else '', self.base * 0.95, 'rm')
        box = self._inline_box(body, self.base * 0.95, 'rm')
        total = label.width + box.width
        avail = (width or self.text_width)
        x = (x0 if x0 is not None else MARGIN['left']) + max(0.0, (avail - total) / 2.0)
        self._need(label.height + label.depth + 4)
        baseline = self.y + label.height
        self._push({'kind': 'box', 'x': x, 'baseline': baseline, 'box': label})
        self._push({'kind': 'box', 'x': x + label.width, 'baseline': baseline, 'box': box})
        self.y += max(label.height + label.depth, self.base * LINE_SPACING) + 3

    def _place_figure(self, b, pages):
        graphics = b.get('graphics') or []
        width = self.text_width * 0.6
        for g in graphics:
            opt = g.get('opts') or ''
            m = _OPT_WIDTH.search(opt)
            if m:
                pt = _to_pt('%s%s' % (m.group(1), m.group(2)))
                if pt:
                    width = min(pt, self.text_width)
            path = self.image_resolver(g.get('path')) if self.image_resolver else None
            if path and os.path.exists(path):
                try:
                    img = self.fitz.open(path)
                    rect_h = width * (img[0].rect.height / max(img[0].rect.width, 1))
                    img.close()
                except Exception:
                    rect_h = width * 0.7
                    path = None
            if path:
                self._need(width * 0.7 + 8)
                rect = self.fitz.Rect(MARGIN['left'] + (self.text_width - width) / 2.0,
                                      self.y,
                                      MARGIN['left'] + (self.text_width + width) / 2.0,
                                      self.y + rect_h)
                self._push({'kind': 'image', 'path': path, 'rect': rect})
                self.y += rect_h + 6
            else:
                # 图找不到：画一个占位框并写明缺哪个文件，别留一片空白让用户猜
                h = 48.0
                self._need(h + 8)
                x0 = MARGIN['left'] + (self.text_width - width) / 2.0
                self._push({'kind': 'placeholder', 'rect': (x0, self.y, x0 + width, self.y + h),
                            'text': g.get('path') or ''})
                self.note('api_latex_image_missing', name=g.get('path') or '')
                self.y += h + 6
        if b.get('caption'):
            self._place_caption(b, 'figure')

    # -------------------------------------------------------------- 绘制
    def _draw_page(self, page, items):
        """一页指令 → 真页面。

        两个坑都在这儿钉死：
          * `TextWriter.append()` **不接 color**，颜色只能在 `write_text()` 上给；
            早先写在这里，整页字画得一个不剩，外面还套了 `except: continue` 把
            TypeError 吞了——表现是「接口成功、文件正常、页面全白」。
          * 字形缺失要在落笔前按 `has_glyph` 过一遍，缺的字符剔掉并记说明，
            而不是指望写的时候不报错。
        """
        pending = []
        for it in items:
            kind = it.get('kind')
            if kind == 'box':
                for op in it['box'].ops:
                    if op[0] == 'txt':
                        pending.append((op[1] + it['x'], op[2] + it['baseline'],
                                        op[3], op[4], op[5]))
                    elif op[0] == 'rule':
                        page.draw_line((op[1] + it['x'], op[2] + it['baseline']),
                                       (op[3] + it['x'], op[2] + it['baseline']),
                                       width=max(op[4], 0.4), color=(0, 0, 0))
            elif kind == 'rule':
                page.draw_line((it['x0'], it['y']), (it['x1'], it['y']),
                               width=max(it.get('w', 0.6), 0.4), color=(0, 0, 0))
            elif kind == 'vline':
                page.draw_line((it['x'], it['y0']), (it['x'], it['y1']),
                               width=0.6, color=(0, 0, 0))
            elif kind == 'image':
                page.insert_image(it['rect'], filename=it['path'])
            elif kind == 'placeholder':
                r = it['rect']
                page.draw_rect(r, color=(0.55, 0.55, 0.55), width=0.8)
                note = self.kit_box_text('缺图片：%s' % it['text'], self.base * 0.85, 'rm')
                for op in note.ops:
                    if op[0] == 'txt':
                        pending.append((r[0] + 6 + op[1], r[1] + 18 + op[2], op[3], op[4],
                                        op[5]))
        if not pending:
            return
        writer = self.fitz.TextWriter(page.rect)
        for x, y, face, size, text in pending:
            font = self.kit.face(face)
            kept = self._keep_glyphs(font, text)
            if not kept:
                continue
            writer.append((x, y), kept, font=font, fontsize=size)
        writer.write_text(page, color=(0, 0, 0))

    def _keep_glyphs(self, font, text):
        """挑出这档字体真能画的字符；有剔除就记一条说明。"""
        out = []
        dropped = 0
        for ch in text:
            try:
                ok = (not ch.strip()) or bool(font.has_glyph(ord(ch)))
            except Exception:
                ok = True
            if ok:
                out.append(ch)
            else:
                dropped += 1
        if dropped:
            self.note('api_latex_glyph_missing', n=dropped)
        return ''.join(out)

    # -------------------------------------------------------------- 盒构造
    def kit_box_text(self, text, size, style, raw=False):
        """一段纯文本 → 盒（按中日韩切段选字体，必要时补一条说明）。"""
        box = Box(0.0, size * 0.72, size * 0.22, [])
        if not text:
            return box
        x = 0.0
        for (cjk, base), seg in self.kit.faces_for(text, style):
            face = CJK_FACE if cjk else FACES.get(base if base in FACES else 'rm', 'tiro')
            if not cjk and base == 'rm':
                face = 'tiro'
            if cjk and style in ('bf', 'it', 'bi') and not raw:
                self._note_cjk_style()
            w = self.kit.face(face).text_length(seg, fontsize=size)
            box.ops.append(('txt', x, 0.0, face, size, seg, (0, 0, 0)))
            x += w
        box.width = x
        return box

    def _note_cjk_style(self):
        if self._cjk_style_noted:
            return
        self._cjk_style_noted = True
        self.note('api_latex_cjk_style')

    def _math_box(self, nodes, size, display=False, style=None):
        # 上游偶尔会把「单个节点」当列表传进来（少包一层 []）：归一一下，
        # 别让用户的一次转换因为这种疏忽整个失败
        if isinstance(nodes, dict):
            nodes = [nodes]
        return _MathBoxer(self, size, display, style).run(nodes or [])

    def _inline_box(self, inline, size, style='rm'):
        """行内节点 → 一个盒（段落里再按词切开）。"""
        boxer = _InlineBoxer(self, size, style)
        return boxer.run(inline or [])

    def _words(self, inline, size, style='rm'):
        """行内节点 → 「可断行的词」列表，每个词自带盒与后随空隙。

        词本身不拆：整句中文没有空格会变成一个超长的词，但那件事交给断行器
        `_break_lines` + `_chop` 在真正放不下时按可断点劈开。
        在这儿逐字拆会一段话造出一万多个盒子，绘制与测量都跟着变慢，
        还会把下划线那类横跨多个字的线段切成碎片。
        """
        return _InlineBoxer(self, size, style).words(inline or [])


class _InlineBoxer(object):
    """把行内节点摊成盒；`words()` 版本会按空格/中日韩切出可断行的单位。"""

    def __init__(self, owner, size, style):
        self.owner = owner
        self.size = size
        self.style = style

    def run(self, inline):
        box = Box(0.0, self.size * 0.72, self.size * 0.22, [])
        for w in self.words(inline):
            b = w['box']
            for op in b.ops:
                if op[0] == 'txt':
                    box.ops.append(('txt', op[1] + box.width, op[2], op[3], op[4], op[5], op[6]))
                else:
                    box.ops.append((op[0], op[1] + box.width, op[2], op[3] + box.width, op[4]))
            box.width += b.width + w.get('gap', 0.0)
            box.height = max(box.height, b.height)
            box.depth = max(box.depth, b.depth)
        return box

    def words(self, inline):
        out = []
        for node in inline:
            t = node.get('t')
            piece = None
            if t == 'txt':
                piece = self._text_box(node.get('v') or '', self.style)
            elif t == 'run':
                piece = self._text_box(_plain(node.get('body') or []), node.get('style') or 'rm')
                if node.get('style') == 'ul':
                    piece = self._underlined(piece)
            elif t == 'math':
                piece = self.owner._math_box(node.get('body') or [], self.size,
                                             display=False, style=self.style)
            elif t == 'nbsp':
                piece = self._text_box(' ', self.style)
            elif t == 'space':
                w = (node.get('em') or 0) * self.size if node.get('em') is not None \
                    else (node.get('pt') or 0.0)
                piece = Box(max(w, 0.0), self.size * 0.7, 0.0, [])
            elif t in ('br', 'parbreak'):
                piece = None
            elif t == 'ref':
                piece = self._text_box(node.get('v') or '??', self.style)
            elif t == 'href':
                piece = self._text_box(_plain(node.get('body') or []), self.style)
            elif t == 'footnote_mark':
                piece = self._sup_mark(node.get('no'))
            elif t == 'caption':
                piece = self._text_box(_plain(node.get('body') or []), self.style)
            elif t == 'includegraphics':
                piece = Box(0.0, 0.0, 0.0, [])
                self.owner.note('api_latex_image_inline', name=node.get('path') or '')
            elif t == 'unknown':
                body = node.get('body') or []
                text = _plain(body) if body else ''
                piece = self._text_box(text, self.style)
            elif t == 'style_switch':
                self.style = node.get('style') or 'rm'
                continue
            elif t == 'amp':
                piece = self._text_box('&', self.style)
            elif t == 'item':
                continue
            elif t in ('newpage_inline', 'delim'):
                continue
            else:
                text = _plain([node])
                piece = self._text_box(text, self.style) if text else None
            if piece is None:
                continue
            if piece.width <= 0 and not piece.ops and t in ('space',):
                if out:
                    out[-1]['gap'] = out[-1].get('gap', 0.0) + piece.width
                continue
            out.append({'box': piece, 'gap': 0.0, 'brk': t in ('txt', 'run'),
                        'kind': t})
        return out

    def _text_box(self, text, style):
        if not text:
            return Box(0.0, self.size * 0.72, self.size * 0.22, [])
        box = self.owner.kit_box_text(text, self.size, style)
        return box

    def _underlined(self, box):
        if box.width:
            box.ops.append(('rule', 0.0, box.depth + 0.6, box.width, 0.5))
            box.depth += 1.6
        return box

    def _sup_mark(self, no):
        size = self.size * SCRIPT_SCALE
        box = self.owner.kit_box_text(str(no or ''), size, 'rm')
        ops = [('txt', op[1], op[2] - self.size * 0.45, op[3], op[4], op[5], op[6])
               for op in box.ops]
        return Box(box.width, self.size * 0.9, box.depth, ops)


class _MathBoxer(object):
    """数学盒：逐个 atom 排，`_`/`^` 挂到前一个盒上，分式/根号/括号按算出来的高度放大。"""

    def __init__(self, owner, size, display, style=None):
        self.owner = owner
        self.size = size
        self.display = display
        self.style = style

    def run(self, nodes):
        box = Box(0.0, self.size * 0.7, self.size * 0.2, [])
        for n in nodes:
            piece = self.atom(n)
            if piece is None:
                continue
            t = n.get('t')
            if t == 'mo' and piece.width and n.get('v') not in (',', ';', '.', '!'):
                pad = self.size * 0.18
                box.ops = _shift(box.ops, pad)
                box.width += pad
            for op in piece.ops:
                if op[0] == 'txt':
                    box.ops.append(('txt', op[1] + box.width, op[2], op[3], op[4], op[5], op[6]))
                else:
                    box.ops.append((op[0], op[1] + box.width, op[2], op[3] + box.width, op[4]))
            box.width += piece.width
            if t == 'mo' and piece.width and n.get('v') not in (',', ';', '.', '!'):
                pad = self.size * 0.18
                box.ops = _shift(box.ops, pad)
                box.width += pad
            box.height = max(box.height, piece.height)
            box.depth = max(box.depth, piece.depth)
        return box

    def atom(self, n):
        t = n.get('t')
        size = self.size
        if t in ('mi',):
            return self.owner.kit_box_text(n.get('v') or '', size, 'it')
        if t in ('mn', 'mo'):
            return self.owner.kit_box_text(n.get('v') or '', size, 'rm')
        if t == 'sym':
            v = n.get('v') or ''
            # 小写希腊字母按 TeX 习惯用斜体，其余正体
            style = 'it' if (v.isalpha() and v.islower() and not v.isascii()) else 'rm'
            return self.owner.kit_box_text(v, size, style)
        if t == 'mtext_run':
            return self.owner.kit_box_text(n.get('v') or '', size, 'rm')
        if t == 'mtext':
            return _InlineBoxer(self.owner, size, 'rm').run(n.get('body') or [])
        if t == 'mstyle':
            return _MathBoxer(self.owner, size, self.display,
                              n.get('style')).run(n.get('body') or [])
        if t == 'group':
            return _MathBoxer(self.owner, size, self.display).run(n.get('body') or [])
        if t == 'bigop':
            return self._bigop(n)
        if t == 'frac':
            return self._frac(n)
        if t == 'sqrt':
            return self._sqrt(n)
        if t == 'accent':
            return self._accent(n)
        if t == 'delim':
            return self._delim(n)
        if t == 'sub':
            return self._script(n['base'], None, n.get('nuc'))
        if t == 'sup':
            return self._script(n['base'], n.get('nuc'), None)
        if t == 'subsup':
            return self._script(n['base'], n.get('sup'), n.get('sub'))
        if t == 'space':
            w = (n.get('em') * size) if n.get('em') is not None else (n.get('pt') or 0.0)
            return Box(max(w, 0.0), size * 0.7, 0.0, [])
        if t == 'nbsp':
            return self.owner.kit_box_text(' ', size, 'rm')
        if t == 'alignmark':
            return Box(0.0, size * 0.7, 0.0, [])
        if t == 'over':
            return Box(0.0, size * 0.7, 0.0, [])
        if t == 'unknown':
            body = n.get('body')
            text = _plain_math(body) if body else (n.get('cmd') or '')
            return self.owner.kit_box_text(text, size, 'rm')
        return Box(0.0, size * 0.7, 0.0, [])

    def _script(self, base_node, sup_nodes, sub_nodes):
        base = self.atom(base_node) if base_node else Box(0.0, 0, 0, [])
        small = self.size * SCRIPT_SCALE
        sb = _MathBoxer(self.owner, small, False, self.style)
        sup = sb.run(sup_nodes) if sup_nodes else None
        sub = sb.run(sub_nodes) if sub_nodes else None
        ops = list(base.ops)
        width = base.width
        height = base.height
        depth = base.depth
        gap = self.size * 0.08
        if sup:
            dy = -self.size * SUP_RAISE
            ops += _shift(sup.ops, width + gap, dy)
            width += gap + sup.width
            height = max(height, base.height + sup.height + dy * -1 - sup.height)
            height = max(height, -dy + sup.height)
        if sub:
            dy = self.size * SUB_LOWER
            ops += _shift(sub.ops, width + gap if sup is None else base.width + gap, dy)
            if sup is None:
                width += gap + sub.width
            else:
                width = max(width, base.width + gap + sub.width)
            depth = max(depth, dy + sub.height + sub.depth)
        return Box(width, height, depth, ops)

    def _bigop(self, n):
        v = n.get('v') or ''
        size = self.size * (1.5 if self.display and v in ('∑', '', '∫', '∮', '', '∭') else 1.0)
        base = self.owner.kit_box_text(v, size, 'rm')
        sub = n.get('sub_limit')
        sup = n.get('sup_limit')
        if not (sub or sup):
            return base
        small = self.size * 0.75
        if self.display and v in ('∑', '', '⋃', '⋂', 'lim'):
            sb = _MathBoxer(self.owner, small, False, self.style)
            ops = list(base.ops)
            width = base.width
            height = base.height
            depth = base.depth
            if sup:
                b = sb.run(sup)
                dy = -(base.height + b.depth + self.size * 0.12)
                ops += _shift(b.ops, (width - b.width) / 2.0, dy)
                height = max(height, -dy + b.height)
            if sub:
                b = sb.run(sub)
                dy = base.depth + self.size * 0.16
                ops += _shift(b.ops, (width - b.width) / 2.0, dy)
                depth = max(depth, dy + b.depth)
            return Box(width, height, depth, ops)
        # 行内（或积分）：上下限退化成右侧上下标
        node = {'t': 'subsup', 'base': {'t': 'bigop', 'v': v}, 'sup': sup, 'sub': sub}
        return self._script(node['base'], sup, sub)

    def _frac(self, n):
        scale = 0.85 if (n.get('small') or not self.display) else 0.95
        num_size = self.size * scale
        sb = _MathBoxer(self.owner, num_size, False, self.style)
        num = sb.run(n.get('num') or [])
        den = sb.run(n.get('den') or [])
        rule_w = self.size * FRAC_RULE
        axis = self.size * AXIS
        gap = num_size * 0.18
        width = max(num.width, den.width) + self.size * 0.24
        height = axis + gap + num.height + num.depth
        depth = axis - gap - den.depth
        depth = -depth if depth > 0 else depth
        depth = abs(axis - gap - den.depth)
        x0 = (width - num.width) / 2.0
        ops = list(_shift(num.ops, x0, -(axis + gap + num.depth)))
        x1 = (width - den.width) / 2.0
        ops += list(_shift(den.ops, x1, axis - gap + den.height))
        ops.append(('rule', self.size * 0.08, -axis, width - self.size * 0.08, rule_w))
        if n.get('binom'):
            left = self.owner.kit_box_text('(', self.size * 1.6, 'rm')
            right = self.owner.kit_box_text(')', self.size * 1.6, 'rm')
            body = Box(width, height, depth, ops)
            return _hconcat([left, Box(1.5, height, depth, []), body,
                             Box(1.5, height, depth, []), right], 0.0)
        return Box(width, height, depth, ops)

    def _sqrt(self, n):
        body = _MathBoxer(self.owner, self.size, False, self.style).run(n.get('body') or [])
        rad_size = self.size * max(1.0, (body.height + body.depth) / self.size * 0.95)
        rad = self.owner.kit_box_text('√', rad_size, 'rm')
        thick = self.size * 0.045
        gap = self.size * 0.06
        height = max(body.height, rad.height) + gap
        ops = list(rad.ops)
        x = rad.width
        ops += _shift(body.ops, x, 0.0)
        ops.append(('rule', x - gap, -(body.height + gap), x + body.width, thick))
        width = x + body.width
        depth = max(body.depth, rad.depth)
        if n.get('index'):
            ib = _MathBoxer(self.owner, self.size * 0.6, False, self.style).run(n['index'])
            ops += _shift(ib.ops, rad.width * 0.25 - ib.width * 0.6,
                          -(height + ib.height * 0.2))
            height = max(height, ib.height + ib.depth)
        return Box(width, height, depth, ops)

    def _accent(self, n):
        body = _MathBoxer(self.owner, self.size, False, self.style).run(n.get('body') or [])
        glyph = n.get('glyph')
        kind = n.get('kind')
        ops = list(body.ops)
        height = body.height
        if kind == 'rule':
            thick = self.size * 0.05
            dy = -(body.height + self.size * 0.12)
            ops.append(('rule', 0.0, dy, body.width, thick))
            height = body.height + self.size * 0.2
            if n.get('cmd') == 'underline':
                ops = list(body.ops)
                dy = body.depth + self.size * 0.12
                ops.append(('rule', 0.0, dy, body.width, thick))
                height = body.height
        else:
            ab = self.owner.kit_box_text(glyph or '', self.size * 0.7, 'rm')
            dy = -(body.height + ab.depth + self.size * 0.05)
            ops += _shift(ab.ops, (body.width - ab.width) / 2.0, dy)
            height = max(height, -dy + ab.height)
        return Box(body.width, height, body.depth, ops)

    def _delim(self, n):
        left = n.get('left') or ''
        right = n.get('right') or ''
        body = None
        if n.get('body') is not None:
            body = _MathBoxer(self.owner, self.size, self.display,
                              self.style).run(n.get('body') or [])
        height = (body.height if body else self.size * 0.7)
        depth = (body.depth if body else self.size * 0.2)
        scale = 1.0
        if n.get('scaled'):
            scale = min(3.0, max(1.0, (height + depth) / self.size))
        ops = []
        width = 0.0
        for ch in (left, right):
            if not ch or ch == '.':
                continue
            if ch in ('floor-left', 'ceil-left', 'floor-right', 'ceil-right'):
                w = self.size * 0.35 * scale
                h = (height + depth)
                top = -height
                x = width
                if ch.endswith('left'):
                    ops.append(('rule', x, top, x + w, self.size * 0.05 * scale))
                    ops.append(('rule', x, top, x, top + h, self.size * 0.05 * scale))
                else:
                    ops.append(('rule', x + w, top, x + w, top + h, self.size * 0.05 * scale))
                    ops.append(('rule', x, top + h, x + w, top + h, self.size * 0.05 * scale))
                width += w + self.size * 0.08
                continue
            face_size = self.size * scale
            b = self.owner.kit_box_text(ch, face_size, 'rm')
            # 括号要压住内容：按内容中心对齐
            dy = -((height + depth) / 2.0) + (b.height - b.depth) / 2.0
            ops += _shift(b.ops, width, dy)
            width += b.width + self.size * 0.04
        if body is not None:
            ops += _shift(body.ops, width, 0.0)
            width += body.width
        return Box(width, max(height, (height + depth) / 2.0 + self.size * 0.3 * scale),
                   max(depth, (height + depth) / 2.0 - self.size * 0.3 * scale), ops)


# ------------------------------------------------------------------ 工具函数
_OPT_WIDTH = None


def _setup_re():
    global _OPT_WIDTH
    import re
    _OPT_WIDTH = re.compile(r'width\s*=\s*([\d.]+)\s*(cm|mm|pt|in)')


_setup_re()


def _measure(text, face, size):
    """量宽（排版器自己用，不建 Kit 以免每次重新造字体对象）。"""
    import pymupdf as _fitz

    key = (face, size, text)
    hit = _MEASURE_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        font = _FACE_CACHE.get(face)
        if font is None:
            font = _fitz.Font(face)
            _FACE_CACHE[face] = font
        width = font.text_length(text, fontsize=size)
    except Exception:
        width = size * len(text)      # 量不了就按最宽的中文估，宁可早换行
    _MEASURE_CACHE[key] = width
    return width


_MEASURE_CACHE = {}
_FACE_CACHE = {}




def _shift(ops, dx=0.0, dy=0.0):
    """平移盒内指令。

    rule 的统一形状是 `('rule', x0, y, x1, 粗细)`——早先这里按
    `(x0, x1, y)` 搬，等于把「横线在哪一度」和「画到哪儿」互换，
    分式那条线就画到天上去了（成品里表现为分子分母挤在一起、看不见分数线）。
    """
    out = []
    for op in ops:
        if op[0] == 'txt':
            out.append(('txt', op[1] + dx, op[2] + dy, op[3], op[4], op[5], op[6]))
        elif op[0] == 'rule':
            out.append(('rule', op[1] + dx, op[2] + dy, op[3] + dx, op[4]))
    return out


def _hconcat(boxes, gap=0.0):
    box = Box(0.0, 0.0, 0.0, [])
    for b in boxes:
        box.ops += _shift(b.ops, box.width, 0.0)
        box.width += b.width + gap
        box.height = max(box.height, b.height)
        box.depth = max(box.depth, b.depth)
    return box


def _parse_spec(spec, ncols):
    """`l|c|r` → (对齐表, 竖线表)。"""
    aligns = []
    vbars = [False]
    for ch in spec:
        if ch in 'lcrXp':
            aligns.append('c' if ch in 'cpX' and ch != 'p' else ('l' if ch in 'lX' else ch))
            vbars.append(False)
        elif ch == '|':
            if aligns:
                vbars[-1] = True
            vbars.append(True)
        elif ch == '@':
            continue
    while len(aligns) < ncols:
        aligns.append('l')
        vbars.append(False)
    return aligns, vbars


def _to_pt(value):
    import re
    m = re.match(r'^([\d.]+)\s*(cm|mm|pt|in)$', (value or '').strip())
    if not m:
        return None
    num = float(m.group(1))
    return num * {'pt': 1.0, 'mm': 2.8346, 'cm': 28.346, 'in': 72.0}[m.group(2)]


def _chop(box, limit):
    """把一个放不下的词从**可断点**劈成两段：左段正好放得下，右段留给下一行。

    可断点只有两种：空格之后、中日韩字符之后。西文单词中间不劈——
    劈出来的 `extra`→`extr`/`a` 比超宽一行难看得多，宁可让它顶出去一行。
    带横线指令（下划线、分式线）的盒子不劈：劈了那条线就不知道盖哪几个字。
    劈不动时返回 None，调用方按整词处理。
    """
    if box.width <= limit or any(op[0] != 'txt' for op in box.ops):
        return None
    chars = []           # (字, 基线偏移, 面, 字号)
    for op in box.ops:
        _, _x, dy, face, size, text, _color = op
        for ch in text:
            chars.append((ch, dy, face, size))
    used = 0.0
    cut = 0              # 左段取到第几个字（不含）
    for i, (ch, _dy, face, size) in enumerate(chars):
        w = _measure(ch, face, size)
        if used + w > limit:
            break
        used += w
        if S.is_cjk(ch) or ch == ' ':
            cut = i + 1
    if cut == 0:
        return None
    left = _rebuild(chars[:cut], box)
    right = _rebuild(chars[cut:], box)
    return left, right


def _rebuild(chars, box):
    """把一串带样式的字重新拼成盒（宽度按实测累加，x 依次前移）。"""
    ops = []
    x = 0.0
    for ch, dy, face, size in chars:
        ops.append(('txt', x, dy, face, size, ch, (0, 0, 0)))
        x += _measure(ch, face, size)
    return Box(x, box.height, box.depth, ops)


def _break_lines(words, limit, first_indent):
    """贪心断行：空格处可断，中日韩每个字后面都可断（放不下就劈开那个词）。"""
    lines = []
    cur = []
    used = 0.0
    avail = limit - first_indent

    def close():
        if cur:
            lines.append(list(cur))
            cur.clear()

    for w in words:
        box = w['box']
        piece_w = box.width + w.get('gap', 0.0)
        if cur and used + piece_w > avail:
            close()
            used = 0.0
            avail = limit
        # 一个词就比一整行还宽：反复劈，直到剩下的能放下
        while piece_w > avail:
            chopped = _chop(box, avail)
            if not chopped:
                break
            left, box = chopped
            cur.append({'box': left, 'gap': 0.0, 'brk': True, 'kind': 'txt'})
            close()
            used = 0.0
            avail = limit
            piece_w = box.width + w.get('gap', 0.0)
        cur.append(dict(w, box=box))
        used += piece_w
    close()
    return lines


def _plain(inline):
    """行内节点摊成纯文本（目录条目、章节标记用）。"""
    from core.latex_doc import _plain_text
    return _plain_text(inline)


def _plain_math(nodes):
    from core.latex_doc import _plain_math as f
    return f(nodes)


def render(doc, out_path, fitz, image_resolver=None, should_stop=None, on_progress=None):
    """模块入口：解析树 → PDF 文件。"""
    r = Renderer(doc, fitz, image_resolver=image_resolver,
                 should_stop=should_stop, on_progress=on_progress)
    res = r.render(out_path)
    res['notes'] = r.notes
    return res
