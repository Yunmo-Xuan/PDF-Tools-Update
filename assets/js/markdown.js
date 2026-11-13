// ============================================================================
// 极简 Markdown 渲染器（离线、无第三方库）。
// 正文来自更新源，属于外部输入：一律先转义再只添加我们自己生成的标签，
// 链接只放行 http/https/mailto，避免这个带 js_api 的窗口被注入执行任意脚本。
// ============================================================================

function mdEscape(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function mdSafeUrl(url) {
    const trimmed = String(url).trim().replace(/^<|>$/g, '');
    return /^(https?:|mailto:)[^\s]*$/i.test(trimmed) ? trimmed : '';
}

function mdInline(raw) {
    // 反引号内的内容不参与其它行内规则，先切出来
    const segments = String(raw).split(/(`[^`]+`)/);
    let out = '';
    for (const segment of segments) {
        if (!segment) continue;
        if (segment.length > 2 && segment[0] === '`' && segment[segment.length - 1] === '`') {
            out += '<code>' + mdEscape(segment.slice(1, -1)) + '</code>';
            continue;
        }
        let text = mdEscape(segment);
        // [文字](url) —— 转义后引号变成 &quot;，所以两种都认
        text = text.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;|\s+'[^']*')?\)/g,
            (all, label, target) => {
                const href = mdSafeUrl(target);
                return href ? `<a href="${href}" target="_blank" rel="noopener">${label}</a>` : all;
            });
        text = text.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
            .replace(/__([^_]+)__/g, '<strong>$1</strong>');
        out += text;
    }
    return out;
}

function mdIsBlockStart(line) {
    return /^\s{0,3}(#{1,6}\s|>|```|([-*+])\s|\d+[.)]\s)/.test(line)
        || /^\s{0,3}\|/.test(line)
        || /^(\s*[-*_]\s*){3,}$/.test(line)
        || !line.trim();
}

// 表格分隔行：| --- | :--: |，第二行出现才说明上一行是表头
function mdIsTableDivider(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
}

function mdSplitRow(line) {
    let text = line.trim();
    if (text.startsWith('|')) text = text.slice(1);
    if (text.endsWith('|')) text = text.slice(0, -1);
    return text.split('|').map(cell => cell.trim());
}

/** 把 Markdown 文本渲染成 HTML 字符串。 */
function renderMarkdown(source) {
    const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    let i = 0;

    while (i < lines.length) {
        const line = lines[i];

        if (!line.trim()) { i += 1; continue; }

        // 围栏代码块
        if (/^\s{0,3}```/.test(line)) {
            const body = [];
            i += 1;
            while (i < lines.length && !/^\s{0,3}```/.test(lines[i])) { body.push(lines[i]); i += 1; }
            i += 1;
            html.push('<pre><code>' + mdEscape(body.join('\n')) + '</code></pre>');
            continue;
        }

        // 标题
        const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
        if (heading) {
            const level = Math.min(heading[1].length, 4);
            html.push(`<h${level}>${mdInline(heading[2].replace(/\s+#+\s*$/, ''))}</h${level}>`);
            i += 1;
            continue;
        }

        // 分隔线
        if (/^(\s*[-*_]\s*){3,}$/.test(line)) { html.push('<hr>'); i += 1; continue; }

        // 表格：表头行 + | --- | 分隔行 + 数据行（README 的功能总览、下载表都用它）
        if (/^\s{0,3}\|/.test(line) && i + 1 < lines.length && mdIsTableDivider(lines[i + 1])) {
            const header = mdSplitRow(line);
            i += 2;
            const rows = [];
            while (i < lines.length && /^\s{0,3}\|/.test(lines[i]) && lines[i].includes('|')) {
                rows.push(mdSplitRow(lines[i]));
                i += 1;
            }
            const cells = (list, tag) => list
                .map(c => `<${tag}>${mdInline(c)}</${tag}>`)
                .join('');
            html.push(
                '<table><thead><tr>' + cells(header, 'th') + '</tr></thead>'
                + '<tbody>' + rows.map(r => '<tr>' + cells(r, 'td') + '</tr>').join('') + '</tbody></table>'
            );
            continue;
        }

        // 引用
        if (/^\s{0,3}>/.test(line)) {
            const quoted = [];
            while (i < lines.length && /^\s{0,3}>/.test(lines[i])) { quoted.push(lines[i].replace(/^\s{0,3}>\s?/, '')); i += 1; }
            html.push('<blockquote><p>' + quoted.map(mdInline).join('<br>') + '</p></blockquote>');
            continue;
        }

        // 无序列表
        if (/^\s*[-*+]\s+/.test(line)) {
            const items = [];
            while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
                items.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
                i += 1;
                // 缩进的续行并入上一项
                while (i < lines.length && lines[i].trim() && !mdIsBlockStart(lines[i])) {
                    items[items.length - 1] += ' ' + lines[i].trim();
                    i += 1;
                }
            }
            html.push('<ul>' + items.map(t => `<li>${mdInline(t)}</li>`).join('') + '</ul>');
            continue;
        }

        // 有序列表
        if (/^\s*\d+[.)]\s+/.test(line)) {
            const items = [];
            while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
                items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ''));
                i += 1;
                while (i < lines.length && lines[i].trim() && !mdIsBlockStart(lines[i])) {
                    items[items.length - 1] += ' ' + lines[i].trim();
                    i += 1;
                }
            }
            html.push('<ol>' + items.map(t => `<li>${mdInline(t)}</li>`).join('') + '</ol>');
            continue;
        }

        // 普通段落：连续的普通行合成一段，行内换行保留为 <br>
        const paragraph = [line];
        i += 1;
        while (i < lines.length && lines[i].trim() && !mdIsBlockStart(lines[i])) {
            paragraph.push(lines[i]);
            i += 1;
        }
        html.push('<p>' + paragraph.map(mdInline).join('<br>') + '</p>');
    }

    return html.join('\n');
}
