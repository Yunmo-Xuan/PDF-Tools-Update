# -*- coding: utf-8 -*-
r"""PDF page model → LaTeX (.tex) 源码生成器（纯函数，不碰界面）。

入口 `blocks_to_tex(pages_blocks, title_info=None, options=None) -> str`：

    pages_blocks : list[dict]
        每份是 core/page_model.py 的 build_model() 返回的页面模型字典（含 'blocks' 键）。
    title_info : dict | None
        {'title': str}，可选：在文件头 \title{} 里写一行。
    options : dict | None
        'tables'       : dict[int, list] — {page_index: [table_extract, ...]}
                         每份 table_extract = {'rows': list[list[str]], 'rect': [x0,y0,x1,y1]}
        'header_note'  : str — 文件头注释话术（从 i18n 键取）
        'no_table_note': str — "未识别表格"注释话术
        'image_note'   : str — 图片占位注释话术（带 {page}/{xref}）
        'math_note'    : str — "公式按纯文本"注释话术

标题阈值（由 r57_probe_sizes.py 量出）：
    正文 body_size = 全部 span 字号众数（取整到 0.5pt）;
    标题候选 = max(span.size) >= body_size + 0.5，整行全 bold，行文字 <=100 字。
    字号档位按降序映射 section > subsection > subsubsection；多余档并入 subsubsection。
    实测 r57_headings.pdf: body=11, heading sizes {12,14,18,24} → section(24)>subsection(18)>
    subsubsection({14,12}).
"""
import statistics
from collections import Counter
from core.latex_sym import is_cjk as _is_cjk

# ---------------------------------------------------------------- 转义表
# 每个键 → LaTeX 转义后的写法。转义后 parse 回 _plain_text 必须等于原文。
# `\~` 在解析器里 = space（TeX 语义），所以 '~' 用 \textasciitilde{} 保持往返。
# '\' 用 \textbackslash{}。
# '^' 用 \^{ }（反斜杠 + ^ + 空格 + 花括号防粘连），解析器 ESCAPES['^'] → '^'。
ESCAPE_MAP = {
    '\\': r'\textbackslash{}',
    '%': r'\%',
    '&': r'\&',
    '$': r'\$',
    '#': r'\#',
    '_': r'\_',
    '{': r'\{',
    '}': r'\}',
    '~': r'\textasciitilde{}',
    '^': r'\^{}',
}
_ESCAPE_CHARS = set(ESCAPE_MAP)


def _escape(text):
    """逐字符转义 LaTeX 特殊字符；连字序列（-- ---）不主动制造。"""
    out = []
    for ch in text:
        out.append(ESCAPE_MAP.get(ch, ch))
    return ''.join(out)


def _comment(text):
    """把一句话术收成 LaTeX 注释行：确保以 % 开头（解析器会整行丢掉，不进正文）。

    i18n 键取回来的原文不带 %（那是给用户看的句子，不是 .tex 指令），所以这里补上；
    已经带 % 的（内置 ASCII fallback）原样透传。换行会被拆成多行注释。
    """
    text = str(text or '')
    lines = text.splitlines() or ['']
    out = []
    for ln in lines:
        ln = ln.rstrip()
        if not ln:
            out.append('%')
        elif ln.startswith('%'):
            out.append(ln)
        else:
            out.append('% ' + ln)
    return '\n'.join(out)


# ---------------------------------------------------------------- 默认 ASCII fallback
_DEFAULT_HEADER = '%% Exported by PDF Toolkit (PDF -> LaTeX).'
_DEFAULT_NO_TABLE = '%% No table structure detected in this document.'
# 图片只出注释占位：整行以 % 开头 → 解析器丢弃、不产生真实文件依赖；
# 花括号里是「说明性占位」而非可用文件名（PDF 无法还原图片路径，不凭空造名）。
_DEFAULT_IMAGE = '% \\includegraphics{<image on page {page} (path not recoverable from PDF)>}'
_DEFAULT_MATH = '%% Math expressions rendered as plain text (cannot reverse-engineer from PDF).'


# ---------------------------------------------------------------- 样式
def _apply_style(text, bold=False, italic=False, monospaced=False):
    """把一段已转义的文字包上样式命令。"""
    if not text:
        return text
    # 从内到外: monospaced → italic → bold（读起来是 \textbf{\textit{\texttt{...}}}）
    if monospaced:
        text = r'\texttt{%s}' % text
    if italic:
        text = r'\textit{%s}' % text
    if bold:
        text = r'\textbf{%s}' % text
    return text


# ---------------------------------------------------------------- 字号聚类
def _body_size(all_sizes):
    """返回文档众数（round 到 0.5pt 后取 mode）；空文档回退 11.0。"""
    if not all_sizes:
        return 11.0
    rounded = [round(s * 2) / 2.0 for s in all_sizes]
    c = Counter(rounded)
    mode = c.most_common(1)[0][0]
    # 众数异常时（极端文档全是标题）退中位数兜底
    if c[mode] < 2:
        return statistics.median(rounded)
    return mode


def _heading_level(size, body_size, heading_sizes_sorted):
    """根据字号在标题档中的排名 → section/subsection/subsubsection；
    返回 LaTeX 命令名，或 None（低于阈值不是标题）。"""
    if size < body_size + 0.5:
        return None
    # heading_sizes_sorted 是降序的列表（最大在最前面）
    for rank, threshold in enumerate(heading_sizes_sorted):
        if size >= threshold - 0.25:  # 容差 0.25pt
            if rank == 0:
                return r'\section'
            elif rank == 1:
                return r'\subsection'
            else:
                return r'\subsubsection'
    return r'\subsubsection'


# ---------------------------------------------------------------- 行/段处理
def _line_text_with_style(line):
    """把一行（dict with 'spans'）拼成带 LaTeX 样式标记的字符串。"""
    parts = []
    for span in line.get('spans') or []:
        raw = span.get('text') or ''
        if not raw:
            continue
        escaped = _escape(raw)
        styled = _apply_style(
            escaped,
            bold=span.get('bold', False),
            italic=span.get('italic', False),
            monospaced=span.get('monospaced', False),
        )
        parts.append(styled)
    return ''.join(parts)


def _block_is_heading(block, body_size, heading_sizes_sorted):
    """判断一个文本块是否是标题行。返回 (LaTeX命令, 纯文本) 或 (None, 纯文本)。"""
    lines = block.get('lines') or []
    if not lines:
        return None, ''
    # 取整块所有文字
    all_text = ''.join(
        s.get('text', '') for l in lines for s in (l.get('spans') or [])
    ).strip()
    if not all_text or len(all_text) > 100:
        return None, all_text
    # 取块内最大字号
    spans = [s for l in lines for s in (l.get('spans') or [])]
    if not spans:
        return None, all_text
    max_size = max(s.get('size', 0) for s in spans)
    if max_size < body_size + 0.5:
        return None, all_text
    # 所有非空 span 必须 bold
    nz = [s for s in spans if (s.get('text') or '').strip()]
    if not all(s.get('bold', False) for s in nz):
        return None, all_text
    # 映射标题级别
    cmd = _heading_level(max_size, body_size, heading_sizes_sorted)
    return cmd, all_text


def _block_plain_text(block):
    """把块内所有 span 文字拼出来（给 heading 判断用）。"""
    return ''.join(
        s.get('text', '') for l in (block.get('lines') or [])
        for s in (l.get('spans') or [])
    )


# ---------------------------------------------------------------- 段落内行合并
def _block_to_para(block):
    """把块的各行合成 LaTeX 段落文本（同一行内 span 拼接，行间加空格）。"""
    lines = block.get('lines') or []
    parts = []
    for line in lines:
        t = _line_text_with_style(line)
        if t:
            parts.append(t)
    # 行间用空格连接（不是换行——LaTeX 段落里的换行只是排版性空白）
    return ' '.join(parts)


# ---------------------------------------------------------------- 公共入口
def blocks_to_tex(pages_blocks, title_info=None, options=None):
    r"""把多页 page_model 转成 .tex 字符串。

    pages_blocks: 每页是 {'blocks': [...], 'page_index': N}，可选键 'page'（1 基）。
    options: 见文件头 docstring。
    """
    opts = options or {}
    header_note = opts.get('header_note') or _DEFAULT_HEADER
    no_table_note = opts.get('no_table_note') or _DEFAULT_NO_TABLE
    image_note_tpl = opts.get('image_note') or _DEFAULT_IMAGE
    math_note = opts.get('math_note') or _DEFAULT_MATH
    tables_info = opts.get('tables') or {}  # {page_index: [extracted tables]}

    # ---- 收集全文 span 字号（定 body_size 和标题档） ----
    all_sizes = []
    for pm in pages_blocks:
        for blk in (pm.get('blocks') or []):
            if blk.get('type') != 'text':
                continue
            for ln in (blk.get('lines') or []):
                for sp in (ln.get('spans') or []):
                    sz = sp.get('size')
                    if sz:
                        all_sizes.append(sz)

    bsz = _body_size(all_sizes)
    # 标题候选字号：大于 bsz+0.5，取降序去重
    larger = sorted(set(s for s in all_sizes if s >= bsz + 0.5), reverse=True)
    heading_sizes_sorted = larger  # 全部 > body + 0.5 的都当标题候选

    # ---- 检测是否包含 CJK（用 latex_sym.is_cjk 统一口径） ----
    has_cjk = False
    for pm in pages_blocks:
        for blk in (pm.get('blocks') or []):
            if blk.get('type') != 'text':
                continue
            if any(_is_cjk(c) for c in _block_plain_text(blk)):
                has_cjk = True
                break
        if has_cjk:
            break

    # ---- 是否有表格（从 options 来） ----
    has_tables = bool(tables_info) and any(v for v in tables_info.values())

    # ---- 组装 .tex ----
    out_lines = []
    # 文件头注释（全部走 _comment：i18n 取回的句子不带 %，不补就会漏进正文）
    out_lines.append(_comment(header_note))
    out_lines.append(_comment(math_note))
    if not has_tables:
        out_lines.append(_comment(no_table_note))
    out_lines.append('')

    # preamble
    out_lines.append(r'\documentclass[12pt]{article}')
    if has_cjk:
        out_lines.append(r'\usepackage[UTF8]{ctex}')
    if title_info and title_info.get('title'):
        out_lines.append(r'\title{%s}' % _escape(title_info['title']))
    out_lines.append(r'\begin{document}')
    if title_info and title_info.get('title'):
        out_lines.append(r'\maketitle')
    out_lines.append('')

    # ---- 逐页处理 ----
    for pi, pm in enumerate(pages_blocks):
        page_blocks = pm.get('blocks') or []
        # 识别本页表格区域
        page_tables = tables_info.get(pi, [])
        table_rects = []
        for tbl in page_tables:
            rect = tbl.get('rect')
            if rect:
                table_rects.append(rect)  # [x0, y0, x1, y1]

        # 已输出的表格索引（避免重复输出同一表格）
        tables_emitted = set()

        for blk in page_blocks:
            btype = blk.get('type', '')
            if btype == 'image':
                # 图片 → 注释占位（不造文件名，无法从 PDF 还原路径）
                page_num = pm.get('page', pi + 1)
                out_lines.append(_comment(image_note_tpl.format(
                    page=page_num, xref=blk.get('xref', ''))))
                out_lines.append('')
                continue
            if btype == 'drawing':
                # 矢量（含表格框线）→ 不单独输出（表格通过 tables_info 处理）
                continue
            if btype != 'text':
                continue
            # 文本块
            blk_bbox = blk.get('bbox') or [0, 0, 0, 0]
            # 检查是否在某个表格区域内
            in_table = False
            tbl_idx = None
            for ti, tr in enumerate(table_rects):
                if _bbox_inside(blk_bbox, tr):
                    in_table = True
                    tbl_idx = ti
                    break
            if in_table:
                # 输出表格（只第一次碰到时输出）
                if tbl_idx not in tables_emitted:
                    tables_emitted.add(tbl_idx)
                    out_lines.append(_tabular_from_extract(page_tables[tbl_idx]))
                    out_lines.append('')
                continue

            # 标题 vs 段落
            cmd, plain = _block_is_heading(blk, bsz, heading_sizes_sorted)
            if cmd:
                styled = _block_para_heading_text(blk)
                out_lines.append('%s{%s}' % (cmd, styled))
                out_lines.append('')
            else:
                para = _block_to_para(blk)
                if para.strip():
                    out_lines.append(para)
                    out_lines.append('')

        # 页面末尾补 % page N 注释
        page_num = pm.get('page', pi + 1)
        out_lines.append('%% --- page %d ---' % page_num)
        out_lines.append('')

    out_lines.append(r'\end{document}')
    return '\n'.join(out_lines) + '\n'


def _block_para_heading_text(block):
    """标题行内也带样式（但通常全是 bold，这里简化用 \textbf 避免双重）。"""
    lines = block.get('lines') or []
    parts = []
    for line in lines:
        for span in (line.get('spans') or []):
            raw = span.get('text') or ''
            if raw:
                parts.append(_escape(raw))
    return ' '.join(parts)


def _bbox_inside(inner, outer):
    """inner = [x0,y0,x1,y1], outer = [X0,Y0,X1,Y1]; inner 的中心在 outer 内？"""
    cx = (inner[0] + inner[2]) / 2.0
    cy = (inner[1] + inner[3]) / 2.0
    return outer[0] - 1 <= cx <= outer[2] + 1 and outer[1] - 1 <= cy <= outer[3] + 1


def _tabular_from_extract(tbl):
    r"""从 {rows: [[cell_text, ...], ...]} 生成 \begin{tabular}...\end{tabular}。"""
    rows = tbl.get('rows') or []
    if not rows:
        return ''
    ncols = max(len(r) for r in rows)
    spec = '|' + 'l|' * ncols
    lines = [r'\begin{tabular}{%s}' % spec]
    lines.append(r'\hline')
    for ri, row in enumerate(rows):
        cells = [_escape(str(c) if c else '') for c in row]
        # 补齐空列
        while len(cells) < ncols:
            cells.append('')
        lines.append(' & '.join(cells) + r' \\')
        lines.append(r'\hline')
    lines.append(r'\end{tabular}')
    return '\n'.join(lines)
