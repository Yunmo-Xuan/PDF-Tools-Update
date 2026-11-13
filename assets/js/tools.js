// ============================================================================
// 7.0.1 新增功能页：内容提取（F09 F10 F13）、排版与整理（F14–F17）、
// 安全与整理（F18–F21），外加书签管理（F22）和格式转换页新增的两个方向（F11 F12）。
//
// 单独成一个文件而不是继续往 app.js 里加，是因为老页面那 12 套逻辑（分批渲染、
// 页面管理的暂存/撤销/草稿）已经互相咬合得很紧，新页只共用 common.js 的
// callApi / showToast / reportFailure 和 app.js 的 selectedFiles / selectPdf。
// 两边唯一的接头是 app.js 里的 window.onPdfChosen 钩子（换文件后各自收尾）。
// ============================================================================

// ==================== 统一出口 ====================
// 所有新工具的成功/失败都从 finish() 走：状态行一律 '✓ …' / '✗ …'，失败一律
// reportFailure（顺带弹 toast）。老页面各自拼字符串的写法容易漏掉一条分支，
// 出现「点了没反应」——这里从源头堵掉。

/** 状态行写字母（'⏳'/'✓'/'✗'/'⚠' 的配色由 CSS 按前缀处理）。 */
function setStatus(statusId, text) {
    const el = document.getElementById(statusId);
    if (el) el.innerText = text;
}

/**
 * 取消时「已经做完多少」：ocr_pdf_text 把 pages 放成一页一页的数组，
 * 导出和增强放的是页数——同一个名字两种形状，直接 Number(数组) 会得到 NaN，
 * 于是「明明识别了两页」也被报成「什么都没留下」。两种都要认。
 */
function jobKept(res) {
    if (Array.isArray(res.pages)) return res.pages.length;
    return Number(res.pages || res.count || res.page_count || res.inserted_pages || 0);
}

/**
 * 收尾。okText 可以传字符串，也可以传一个用返回值算文案的函数——
 * 传字符串的话失败分支里 res 是空的，提前取 res.pages 就先炸在这里了。
 *
 * 「用户中途取消」单列一条分支：它既不是失败（前面那几页的成果是真的），
 * 也不是成功（结果并不完整，报「完成」会让人拿半本文件去用）。
 */
function finish(res, statusId, okText, toastText, failText) {
    if (res && res.cancelled) {
        const kept = jobKept(res);
        const text = kept ? uiTextf('job_cancelled_partial', '已取消，已完成 {0} 项', kept)
                          : uiText('job_cancelled', '已取消，没有写出结果');
        setStatus(statusId, '⚠ ' + text);
        showToast(text, 3200);
        // 有成果就回 true：调用方接着把已经识别出来的那几页展示出来，
        // 别让界面停在上一份文件的旧结果上，那比一句「已取消」更容易看错。
        return kept > 0;
    }
    if (res && res.success) {
        const text = typeof okText === 'function' ? okText(res) : okText;
        setStatus(statusId, '✓ ' + text);
        if (toastText) showToast(toastText, 2400);
        return true;
    }
    reportFailure(res, document.getElementById(statusId), failText);
    return false;
}

/** 取控件的值；控件不在就当空串，省得每个函数都要先判一次 null。 */
function val(id) {
    const el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
}

/** 取勾选状态。 */
function checked(id) {
    const el = document.getElementById(id);
    return !!(el && el.checked);
}

/** 界面文案：applyLanguage() 已经把字典存进 currentTextDict，动态生成的片段从这里取。 */
function t(key, fallback) {
    return currentTextDict[key] || fallback;
}

function baseName(path) {
    return String(path || '').split(/[\\/]/).pop();
}

/** 去掉扩展名的文件名，用来给成品起默认名。 */
function stem(path) {
    return baseName(path).replace(/\.[^.]+$/, '');
}

function fileSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(2) + ' MB';
}

/** 拼给 innerHTML 之前转义：文件名和书签标题都可能带 < > "。 */
function esc(text) {
    return String(text === undefined || text === null ? '' : text)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/**
 * 没选文件就别往下走：老页面 each 一句「请先选择 PDF」，这里集中一次。
 *
 * 合并页还顺带管一件事：页面管理的改动只暂存在内存里，磁盘上那份还是老样子。
 * 带着没保存的页序去加页码、裁剪、涂黑或加密，产出的成品是按磁盘那份算的，
 * 用户看不出差别，只会以为成品就该有那三页。所以只要这里的工具和页面管理用的是
 * 同一个文件、而那边还有未保存改动，就一律挡住，并把去哪儿保存说清楚。
 */
function requirePdf(type) {
    const path = selectedFiles[type];
    if (!path) {
        showToast(uiText('dyn_1117', '请先选择 PDF'));
        return '';
    }
    if (pmDirty && path === selectedFiles['pm']) {
        setStatus(type + '-status', uiText('dyn_1125', '✗ 页面管理有还没保存的改动，先处理完再用这里的工具'));
        showToast(uiText('dyn_1126', '页面管理里还有未保存的改动：先点那一格的「保存为新文件」或「放弃修改」。这里的工具读的是磁盘上那一份，页序对不上会出错文件'), 5600);
        return '';
    }
    return path;
}

async function pickSave(defaultName, filetypes) {
    // 第二个参数要么给完整的「描述 (*.ext)」要么干脆不给：不给才走后端默认的
    // PDF files (*.pdf)，传个空数组过去会被当成「所有文件」，存 PDF 时连后缀都不带。
    const out = (filetypes && filetypes.length)
        ? await callApi('save_file_dialog', defaultName, filetypes)
        : await callApi('save_file_dialog', defaultName);
    return typeof out === 'string' ? out : '';
}

// ==================== 面板内的小导航 ====================
// 每页一个 .card，顶端一排任务标签，下面只出这一件工具的表单。之所以不用纵向堆表单：
// 卡片要长到内容区底部（各页圆角框下沿在同一条线上是定死的验收项），
// 而堆到第五个表单就要往外滚，一滚下沿就跟着翻页跳。
// 标签条从左侧竖排改成顶端横排：页面管理并进排版与整理后一页有 6 件工具，
// 竖排既挤掉缩略图网格的宽度，这些标签本身也顶到下沿那条线。
const PANEL_TOOLS = {
    splitmerge: ['merge', 'split'],
    extract: ['text', 'images', 'html', 'ocr', 'latex'],
    layout: ['pm', 'number', 'header', 'crop', 'scale', 'enhance'],
    security: ['redact', 'clean', 'perms', 'encrypt'],
};

function panelSelect(page, tool, event) {
    if (event) event.stopPropagation();
    const pane = document.getElementById('pane-' + page + '-' + tool);
    if (!pane) return;
    (PANEL_TOOLS[page] || []).forEach(name => {
        const el = document.getElementById('pane-' + page + '-' + name);
        if (el) el.classList.toggle('hidden', name !== tool);
    });
    document.querySelectorAll('#page-' + page + ' .task-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.pane === page + '-' + tool);
    });
    // 裁剪面板一打开就把这页尺寸填进四个框：用户看不到自己的 PDF 是 595 还是 612，
    // 盲填一定会「裁剪框超出第 1 页」
    if (page === 'layout' && tool === 'crop') layFillPageSize();
    // 页面管理那一格是缩略图网格，隐藏期间观察器一条都不报，切回来要补装载/补画图
    if (page === 'layout' && tool === 'pm') pmTaskShown();
    // 那四格都有单页预览窗：切过来就按当前设置画一次（设置没变时 layRenderPreview 自己会跳过）
    if (page === 'layout' && LAY_PREVIEW_TOOLS.indexOf(tool) >= 0) layRenderPreview(tool);
    // OCR 页的引擎冷启动要十几秒：切到这一格就先在后台预热，用户填页面范围/精度时引擎已经准备好。
    if (page === 'extract' && tool === 'ocr') ocrWarmupInBackground();
    // 内容提取顶端那颗按钮跟着标签走：OCR 那格收图片，其余三格只该要 PDF。
    if (page === 'extract') extractActionLabel(tool);
    // 拍照变清晰这一格开出来就要看到对比，不然用户不知道三种效果差在哪，只能盲导。
    if (page === 'layout' && tool === 'enhance') enhancePreview();
    // 拆分与合并并成一页之后，右端那颗按钮跟着标签走：
    // 合并那一格要的是「往列表里加文件」，拆分要的是「选一份要拆的 PDF」，
    // 一句话顶两种意思会让人点错
    if (page === 'splitmerge') splitMergeActionLabel(tool);
}

/** 拆分与合并页右端那颗按钮：按当前标签改文案。 */
function splitMergeActionLabel(tool) {
    const btn = document.getElementById('splitmerge-action');
    if (!btn) return;
    const text = btn.querySelector('span:last-child');
    if (text) text.textContent = tool === 'merge' ? t('add_files', '添加文件')
                                                  : t('choose_pdf', '选择 PDF 文件');
}

/** 那颗按钮的实际动作。 */
function splitMergePick() {
    if (activeTask('splitmerge') === 'merge') { addMergeFiles(); return; }
    selectPdf('split');
}

/** 内容提取那一页顶端那颗按钮：OCR 那格收图片，其余三格仍然只该要 PDF。
    做成「同一颗按钮换语义」而不是在 OCR 格里再加一行按钮——那一格已经顶到
    卡片下沿那条齐平线了，多一行就把「所有功能页下沿只有一个值」撑破。 */
function extractPick() {
    if (activeTask('extract') === 'ocr') { selectOcrSource(); return; }
    selectPdf('extract');
}

function extractActionLabel(tool) {
    const btn = document.getElementById('extract-action');
    if (!btn) return;
    const text = btn.querySelector('span:last-child');
    if (text) text.textContent = tool === 'ocr' ? t('choose_pdf_or_image', '选择 PDF 或图片')
                                                : t('choose_pdf', '选择 PDF 文件');
}

// ==================== 内容提取（F09 文字 / F10 HTML / F13 图片）====================
let extractOutDir = '';

async function pickExtractDir() {
    const dir = await callApi('select_directory');
    if (!dir || typeof dir !== 'string') return;
    extractOutDir = dir;
    setStatus('ex-img-dir-echo', dir);
}

function exImgModeChanged() {
    // 「取内嵌原图」是把文件里那串字节原样写出去，清晰度档位对它没有任何意义，
    // 灰掉比「能选但没用」诚实
    const select = document.getElementById('ex-img-dpi');
    if (select) select.disabled = val('ex-img-mode') !== 'render';
}

async function doExportText() {
    const pdf = requirePdf('extract');
    if (!pdf) return;
    const out = await pickSave(stem(pdf) + '.txt', ['Text Document (*.txt)']);
    if (!out) return;
    setStatus('extract-status', uiText('dyn_1127', '⏳ 正在提取文字…'));
    const res = await callApi('export_text', pdf, val('ex-text-range'), out);
    if (res && res.success && !res.chars) {
        // 文件确实写出来了，但一个字符都没有 = 扫描件。报成功用户会以为有内容，
        // 报失败又不如实（接口没错），所以走 '⚠' 并把下一步指给他
        setStatus('extract-status', uiTextf('dyn_1128', '⚠ {0}（0 字符：这份 PDF 没有文字层）', out));
        showToast(uiText('dyn_1129', '这是扫描件，没有可提取的文字。请改用「提取图片 → 整页渲染成 PNG」'), 5200);
        return;
    }
    finish(res, 'extract-status',
           r => out + uiTextf('dyn_1157', '（{0} 页 / {1} 字符）', r.pages, r.chars),
           uiText('dyn_1131', '文字导出完成'), uiText('dyn_1132', '导出文字失败'));
}

async function doExtractImages() {
    const pdf = requirePdf('extract');
    if (!pdf) return;
    setStatus('extract-status', uiText('dyn_1133', '⏳ 正在提取图片…'));
    const mode = val('ex-img-mode') || 'original';
    const res = await callApi('extract_images', pdf, val('ex-img-range'), mode,
                              extractOutDir, parseInt(val('ex-img-dpi'), 10) || 150);
    const box = document.getElementById('ex-img-result');
    if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
    if (!finish(res, 'extract-status',
                r => r.dir + uiTextf('dyn_1134', '（{0} 张{1}）', r.count, (r.duplicates ? uiTextf('dyn_1298', '，重复引用略过 {0} 次', r.duplicates) : '')),
                uiText('dyn_1136', '图片提取完成'), uiText('dyn_1137', '提取图片失败'))) return;
    if (box) {
        const shown = (res.files || []).slice(0, 40);
        box.classList.remove('hidden');
        box.innerHTML = '<div class="result-title">' + esc(t('extract_result_hint', '导出的文件')) +
            (res.files.length > shown.length ? uiTextf('dyn_1138', '（下面只列前 {0} 个，共 {1} 个）', shown.length, res.files.length) : '') +
            '</div>' + shown.map(f => '<div class="result-row">' + esc(baseName(f)) + '</div>').join('');
    }
}

async function doExportHtml() {
    const pdf = requirePdf('extract');
    if (!pdf) return;
    const out = await pickSave(stem(pdf) + '.html', ['Web Page (*.html)']);
    if (!out) return;
    setStatus('extract-status', uiText('dyn_1139', '⏳ 正在导出 HTML…'));
    const res = await callApi('export_html', pdf, out, checked('ex-html-embed'));
    finish(res, 'extract-status',
           r => out + uiTextf('dyn_1140', '（{0} 页 / {1} 张图{2}）', r.pages, r.images, (checked('ex-html-embed') ? uiText('dyn_1427', '已内嵌') : uiText('dyn_1299', '未内嵌'))),
           uiText('dyn_1142', 'HTML 导出完成'), uiText('dyn_1143', '导出 HTML 失败'));
}

// ==================== 拍照变清晰（扫描增强）====================
// 这一格只另存新文件，原图原件不动；预览走的是同一套算法，看到的就是导出的。
let enhancePage = 1;
let enhanceImageList = [];

function enhanceIsImage(path) {
    return /\.(png|jpe?g|bmp|tiff?|webp)$/i.test(String(path || ''));
}

function enhanceOptions() {
    return { mode: val('en-mode') || 'magic',
             perspective: checked('en-perspective'),
             deskew: checked('en-deskew'), crop: checked('en-crop'),
             merge: checked('en-merge') };
}

function enhanceNote(text) {
    const note = document.getElementById('en-note');
    if (!note) return;
    note.classList.remove('hidden');
    note.innerText = text;
}

async function enhancePreview() {
    const src = selectedFiles['layout'];
    if (!src || enhanceIsImage(src)) {
        enhanceNote(uiText('dyn_1470', '先在上方选一份 PDF，这里就能看处理前后的对比。'));
        return;
    }
    const opt = enhanceOptions();
    enhanceNote(uiText('dyn_1471', '⏳ 正在算这一页的效果…'));
    const res = await callApi('enhance_preview', src, enhancePage - 1, opt.mode,
                              opt.deskew, opt.crop, opt.perspective);
    if (!res || !res.success) {
        enhanceNote((res && res.message) || uiText('dyn_1472', '预览失败'));
        return;
    }
    document.getElementById('en-before').src = res.before;
    document.getElementById('en-after').src = res.after;
    const info = document.getElementById('en-page-info');
    if (info) info.innerText = uiTextf('dyn_1473', '第 {0} 页 / 共 {1} 页',
                                       enhancePage, res.page_count || enhancePage);
    const note = document.getElementById('en-note');
    if (note) { note.classList.add('hidden'); note.innerText = ''; }
}

function enhanceStep(delta) {
    const src = selectedFiles['layout'];
    if (!src || enhanceIsImage(src)) return;
    enhancePage = Math.max(1, enhancePage + delta);
    enhancePreview();
}

async function doEnhancePdf() {
    const src = requirePdf('layout');
    if (!src) return;
    if (enhanceIsImage(src)) {
        showToast(uiText('dyn_1474', '这一项要的是 PDF；手机拍的照片请用旁边那行「选图片」。'), 4600);
        return;
    }
    const out = await pickSave(stem(src) + uiText('dyn_1475', '_变清晰.pdf'));
    if (!out) return;
    const opt = enhanceOptions();
    setStatus('layout-status', uiText('dyn_1476', '⏳ 正在逐页增强…'));
    // 参数顺序照 api/enhance.py 的签名来：dpi 后面才能接新的勾选项，
    // 插到中间会让老调用点把 dpi 当成开关值。
    const res = await callApi('enhance_pdf', src, out, val('en-range'), opt.mode,
                              opt.deskew, opt.crop, 150, opt.perspective);
    finish(res, 'layout-status',
           r => out + uiTextf('dyn_1477', '（{0} 页 / {1} 秒）', r.pages,
                              (r.elapsed_ms / 1000).toFixed(1)),
           uiText('dyn_1478', '增强完成'), uiText('dyn_1479', '增强失败'));
}

async function chooseEnhanceImages() {
    const paths = await callApi('open_files_dialog');
    if (!paths || !paths.length) return;
    enhanceImageList = paths.slice();
    const echo = document.getElementById('en-image-echo');
    if (echo) echo.innerText = uiTextf('dyn_1480', '已选 {0} 张图片', enhanceImageList.length);
}

async function doEnhanceImages() {
    if (!enhanceImageList.length) {
        showToast(uiText('dyn_1481', '先点「选图片」挑几张拍来的文件页'), 4200);
        return;
    }
    const opt = enhanceOptions();
    // 勾了「照片合成一份 PDF」走另一条出口：拍十几页的人要的是「一份能直接打印的东西」，
    // 不是一堆散图。没勾就照旧一张张存回原目录。
    const toPdf = opt.merge
        ? await pickSave(stem(enhanceImageList[0]) + uiText('enhance_merge_suffix', '_扫描件.pdf'))
        : '';
    if (opt.merge && !toPdf) return;
    setStatus('layout-status', toPdf ? uiText('enhance_merge_busy', '⏳ 正在增强并合成 PDF…')
                                      : uiText('dyn_1482', '⏳ 正在增强图片…'));
    const res = await callApi('enhance_images', enhanceImageList, '', opt.mode,
                              opt.deskew, opt.crop, toPdf, opt.perspective);
    if (toPdf) {
        finish(res, 'layout-status',
               r => r.file + uiTextf('dyn_1477', '（{0} 页 / {1} 秒）', r.pages,
                                     (r.elapsed_ms / 1000).toFixed(1)),
               uiText('enhance_merge_done', '照片已合成 PDF'),
               uiText('dyn_1485', '图片增强失败'));
        return;
    }
    finish(res, 'layout-status',
           r => uiTextf('dyn_1483', '完成 {0} 张，失败 {1} 张', r.count, r.failed),
           uiText('dyn_1484', '图片增强完成'), uiText('dyn_1485', '图片增强失败'));
    if (res && res.files && res.files.length) {
        ocrShowResult('<div class="result-title">' + uiText('dyn_1486', '成品位置') + '</div>'
                      + res.files.slice(0, 40).map(path => '<div class="result-row">'
                          + esc(path) + '</div>').join(''));
    }
}

// ==================== OCR 工具组（Beta）====================
// 预热按「档位|语言」记一次：这两样决定实际加载哪一份模型，
// 只记「热过了」会让换档/换语言永远不再预热。
let ocrWarmedKey = '';

function ocrQuality() {
    return val('ex-ocr-quality') || 'fast';
}

function ocrLang() {
    return val('ex-ocr-lang') || 'auto';
}

// 「正在准备引擎」那句到底是谁写的，用我们自己写进去的那一串来判断。
// 以前是拿 innerText.indexOf('正在准备 OCR') 嗅的：切到别的语言那一行变成
// "Preparing the OCR engine"，嗅不到 → 引擎好了也不把状态改过来，界面永远停在「正在准备」。
let ocrWarmText = '';

function ocrWarmShowing(el) {
    return !!el && !!ocrWarmText && el.innerText === ocrWarmText;
}

function ocrWarmupInBackground() {
    const key = ocrQuality() + '|' + ocrLang();
    if (ocrWarmedKey === key) return;
    ocrWarmedKey = key;
    const el = document.getElementById('extract-status');
    if (el && (!el.innerText || el.innerText.indexOf('OCR') >= 0)) {
        ocrWarmText = uiText('dyn_1144', '⏳ 正在准备 OCR 引擎（首次使用需要加载模型）…');
        setStatus('extract-status', ocrWarmText);
    }
    callApi('ocr_warmup', ocrQuality(), ocrLang()).then(res => {
        if (ocrWarmShowing(el)) {
            if (res && res.success) setStatus('extract-status', uiText('dyn_1145', '✓ OCR 引擎已就绪'));
            else setStatus('extract-status', uiText('dyn_1147', '⚠ OCR 引擎未准备好，点识别时会再试'));
        }
        ocrWarmText = '';
    }).catch(() => {
        ocrWarmedKey = '';
        if (ocrWarmShowing(el)) {
            setStatus('extract-status', uiText('dyn_1147', '⚠ OCR 引擎未准备好，点识别时会再试'));
        }
        ocrWarmText = '';
    });
}

async function selectOcrSource() {
    // OCR 这一栏收 PDF 也收图片；其余标签页（导出文字 / 提取图片 / HTML）仍然只该要 PDF，
    // 所以放开过滤器的只有这里。
    const path = await callApi('open_file_dialog',
        [uiText('ocr_source_filter', 'PDF 与图片')
         + ' (*.pdf;*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.webp)']);
    if (!path || typeof path !== 'string') return '';
    selectedFiles['extract'] = path;
    const twin = SHARED_SLOTS['extract'];
    if (twin) selectedFiles[twin] = path;
    const echo = document.getElementById('ex-ocr-source');
    if (echo) echo.innerText = path;
    setStatus('extract-status', '✓ ' + path);
    await callApi('add_recent_file', path);
    return path;
}

function requireOcrSource() {
    // 不走 requirePdf：那句提示写的是「请先选择 PDF」，而这一栏图片也算。
    const path = selectedFiles['extract'];
    if (!path) {
        showToast(uiText('dyn_1456', '请先选择 PDF 或图片'));
        return '';
    }
    return path;
}

function ocrIsImage(path) {
    return /\.(png|jpe?g|bmp|tiff?|webp)$/i.test(String(path || ''));
}

function ocrOptions() {
    return { quality: ocrQuality(), lang: ocrLang(), preprocess: checked('ex-ocr-preprocess'),
             layout: checked('ex-ocr-layout') };
}

function ocrResultBox() {
    const box = document.getElementById('ex-ocr-result');
    if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
    return box;
}

function ocrShowResult(html) {
    const box = ocrResultBox();
    if (box) { box.classList.remove('hidden'); box.innerHTML = html; }
}

/**
 * 这一页的字是哪来的：内嵌文字层 / 这次识别的 / 上一次识别的（缓存）。
 * 标出来是有原因的——同一份文件里两种页混着放时，用户看到错字得知道该怪谁，
 * 而我们也不该把内嵌文字的质量算到 OCR 头上。
 */
function ocrSourceLabel(page) {
    if (page.source === 'text') return uiText('ocr_src_text', '内嵌文字');
    if (page.cached) return uiText('ocr_src_cached', '沿用上次');
    return uiText('ocr_src_ocr', 'OCR');
}

function pageRangeText(pages) {
    // [1,2,3,7] → '1-3,7'。中途取消后要把「页面范围」收回已经识别的那几页：
    // 范围空着等于整本 80 页，下一次导出就会从没识别的那页开始重跑，
    // 进度条走 1/80、2/80，成品里也多出用户根本没要的页。
    const nums = [...new Set(pages.map(p => Number(p.page)).filter(n => n > 0))]
        .sort((a, b) => a - b);
    if (!nums.length) return '';
    const parts = [];
    let start = nums[0], last = nums[0];
    nums.slice(1).forEach(n => {
        if (n === last + 1) { last = n; return; }
        parts.push(start === last ? String(start) : start + '-' + last);
        start = last = n;
    });
    parts.push(start === last ? String(start) : start + '-' + last);
    return parts.join(',');
}

async function doOcrText() {
    const pdf = requireOcrSource();
    if (!pdf) return;
    const opt = ocrOptions();
    setStatus('extract-status', uiText('dyn_1457', '⏳ 正在识别文字…'));
    const res = await callApi('ocr_pdf_text', pdf, ocrIsImage(pdf) ? '' : val('ex-ocr-range'),
                              opt.quality, opt.preprocess, opt.layout, opt.lang);
    if (!finish(res, 'extract-status',
                r => r.page_count + uiTextf('dyn_1149', ' 页 / {0} 字符 / {1} 秒', r.chars, (r.elapsed_ms / 1000).toFixed(1)),
                uiText('dyn_1150', 'OCR 识别完成'), uiText('dyn_1151', 'OCR 识别失败'))) return;
    const kept = res.cancelled ? pageRangeText(res.pages || []) : '';
    if (kept) {
        const field = document.getElementById('ex-ocr-range');
        if (field) field.value = kept;
    }
    const pages = (res.pages || []).slice(0, 12);
    let html = '<div class="result-title">' + uiText('dyn_1152', '识别结果（Beta，最多展示前 12 页）') + '</div>';
    if (kept) {
        html += '<div class="form-note">'
              + uiTextf('ocr_cancel_range', '已把页面范围收回到 {0} 页：接着导出只认这几页，不会重跑整本。', kept)
              + '</div>';
    }
    if (res.tables) {
        html += '<div class="result-row">' + uiTextf('dyn_1459', '认到 {0} 张表格', res.tables) + '</div>';
    }
    if (res.text_layer_pages) {
        html += '<div class="form-note">' + uiTextf('dyn_1458', '这份文件里 {0} 页本来就带文字层，已直接抽出（比识别更准更快）；其余才是 OCR 出来的。', res.text_layer_pages) + '</div>';
    }
    if (res.cached_pages) {
        html += '<div class="form-note">' + uiTextf('ocr_cached_note', '这次有 {0} 页直接用了上一次的识别结果，没有重算。', res.cached_pages) + '</div>';
    }
    pages.forEach(p => {
        const text = String(p.layout_text || p.text || '').slice(0, 12000);
        html += '<div class="result-row">' + '<b>' + uiTextf('dyn_1402', '第 {0} 页', p.page)
              + ' · ' + esc(ocrSourceLabel(p)) + '</b>' + '</div>' + '<pre class="ocr-text">' + esc(text || uiText('dyn_1153', '（本页没有识别到文字）')) + '</pre>';
    });
    if ((res.pages || []).length > pages.length) {
        html += '<div class="form-note">' + uiTextf('dyn_1154', '还有 {0} 页未展示，可导出 TXT 查看全部。', ((res.pages || []).length - pages.length)) + '</div>';
    }
    ocrShowResult(html);
    if (!res.chars) showToast(uiText('dyn_1155', '没有识别到文字，可以尝试「扫描优化」或换一页'), 4200);
}

async function ocrExportAs(kind) {
    const src = requireOcrSource();
    if (!src) return;
    const suffix = {txt: '.txt', docx: '.docx', xlsx: '.xlsx'}[kind];
    const filter = {txt: 'Text Document (*.txt)',
                    docx: 'Word Document (*.docx)',
                    xlsx: 'Excel Workbook (*.xlsx)'}[kind];
    const out = await pickSave(stem(src) + '_OCR' + suffix, [filter]);
    if (!out) return;
    const opt = ocrOptions();
    const busy = {txt: uiText('dyn_1156', '⏳ 正在识别并导出文字…'),
                  docx: uiText('dyn_1460', '⏳ 正在识别并导出 Word…'),
                  xlsx: uiText('dyn_1463', '⏳ 正在识别并导出 Excel…')}[kind];
    const done = {txt: uiText('dyn_1158', 'OCR 文本已导出'),
                  docx: uiText('dyn_1461', 'Word 文件已导出'),
                  xlsx: uiText('dyn_1464', 'Excel 文件已导出')}[kind];
    const failed = {txt: uiText('dyn_1159', 'OCR 文本导出失败'),
                    docx: uiText('dyn_1462', 'Word 导出失败'),
                    xlsx: uiText('dyn_1465', 'Excel 导出失败')}[kind];
    setStatus('extract-status', busy);
    const args = kind === 'txt'
        ? [src, val('ex-ocr-range'), opt.quality, opt.preprocess, out, opt.layout, opt.lang]
        : [src, out, val('ex-ocr-range'), opt.quality, opt.preprocess, opt.lang];
    const res = await callApi('ocr_export_' + kind, ...args);
    finish(res, 'extract-status',
           r => out + uiTextf('dyn_1157', '（{0} 页 / {1} 字符）', r.pages, r.chars),
           done, failed);
}

function doOcrExportText() { return ocrExportAs('txt'); }
function doOcrExportDocx() { return ocrExportAs('docx'); }
function doOcrExportXlsx() { return ocrExportAs('xlsx'); }

function requireOcrPdf() {
    // 可搜索 PDF / 书签 / 质检这几项改的是 PDF 本身，图片没有「页」可改。
    const path = requirePdf('extract');
    if (!path) return '';
    if (ocrIsImage(path)) {
        showToast(uiText('dyn_1466', '这一项只处理 PDF；图片请用「识别文字」或导出 TXT / Word / Excel'), 4600);
        return '';
    }
    return path;
}

async function doOcrSearchable() {
    const pdf = requireOcrPdf();
    if (!pdf) return;
    const out = await pickSave(stem(pdf) + uiText('dyn_1160', '_可搜索.pdf'));
    if (!out) return;
    setStatus('extract-status', uiText('dyn_1161', '⏳ 正在生成可搜索 PDF（首次识别需加载 OCR 引擎）…'));
    const res = await callApi('ocr_searchable_pdf', pdf, out, val('ex-ocr-range'),
                              ocrQuality(), ocrLang());
    finish(res, 'extract-status',
           r => out + uiTextf('dyn_1162', '（写入 {0} 页 / {1} 字符，已有文字层跳过 {2} 页，旋转页跳过 {3} 页）', r.inserted_pages, r.chars, r.skipped_text_pages, r.skipped_rotated_pages),
           uiText('dyn_1163', '可搜索 PDF 已生成'), uiText('dyn_1164', '生成可搜索 PDF 失败'));
}

async function doOcrSensitive() {
    const pdf = requireOcrPdf();
    if (!pdf) return;
    setStatus('extract-status', uiText('dyn_1165', '⏳ 正在识别并检查敏感信息…'));
    const res = await callApi('ocr_sensitive_info', pdf, val('ex-ocr-range'), ocrQuality(),
                              ocrLang());
    if (!finish(res, 'extract-status',
                r => uiTextf('dyn_1166', '检查 {0} 页，命中 {1} 处', r.pages, r.count),
                uiText('dyn_1167', '敏感信息检查完成'), uiText('dyn_1168', '敏感信息检查失败'))) return;
    if (!res.hits || !res.hits.length) {
        ocrShowResult('<div class="result-title">' + uiText('dyn_1169', '没有发现手机号、身份证、银行卡或邮箱') + '</div>');
        return;
    }
    const kindName = {phone: uiText('kind_phone', '手机号'), id_card: uiText('dyn_1171', '身份证'), id_card_15: uiText('dyn_1172', '15 位身份证'),
                      bank_card: uiText('dyn_1173', '银行卡'), email: uiText('dyn_1174', '邮箱')};
    let html = '<div class="result-title">' + uiTextf('dyn_1175', '命中 {0} 处（Beta，仅提示，不会自动修改文件）', res.count) + '</div>';
    res.hits.slice(0, 80).forEach(h => {
        html += '<div class="result-row">' + uiTextf('dyn_1410', '第 {0} 页 · {1} · {2}', h.page, esc(kindName[h.kind] || h.kind), esc(h.text)) + '</div>';
    });
    if (res.hits.length > 80) html += '<div class="form-note">' + uiText('dyn_1176', '只展示前 80 条。') + '</div>';
    ocrShowResult(html);
}

async function doOcrScanReport() {
    const pdf = requireOcrPdf();
    if (!pdf) return;
    setStatus('extract-status', uiText('dyn_1177', '⏳ 正在做扫描质检…'));
    const res = await callApi('ocr_scan_report', pdf, val('ex-ocr-range'), 100);
    if (!finish(res, 'extract-status',
                r => r.page_count + uiTextf('dyn_1178', ' 页，{0} 页有提示', r.flagged),
                uiText('dyn_1179', '扫描质检完成'), uiText('dyn_1180', '扫描质检失败'))) return;
    const flagName = {blank: uiText('dyn_1181', '空白/几乎无内容'), blur: uiText('dyn_1182', '模糊'), dark: uiText('dyn_1183', '偏暗'),
                      low_contrast: uiText('dyn_1184', '对比度低'), skew: uiText('dyn_1185', '倾斜')};
    let html = '<div class="result-title">' + uiText('dyn_1186', '扫描质检（Beta）') + '</div>';
    (res.pages || []).slice(0, 60).forEach(p => {
        const flags = (p.flags || []).map(f => flagName[f] || f).join('、') || uiText('dyn_1409', '正常');
        html += '<div class="result-row">' + uiTextf('dyn_1411', '第 {0} 页 · {1}×{2} · 内容占比 {3}% · 倾斜 {4}° · {5}', p.page, p.width, p.height, (p.ink_ratio * 100).toFixed(2), p.skew, esc(flags)) + '</div>';
    });
    if ((res.pages || []).length > 60) html += '<div class="form-note">' + uiText('dyn_1187', '只展示前 60 页。') + '</div>';
    ocrShowResult(html);
}

async function doOcrBookmarks() {
    const pdf = requireOcrPdf();
    if (!pdf) return;
    const out = await pickSave(stem(pdf) + uiText('dyn_1188', '_带书签.pdf'));
    if (!out) return;
    setStatus('extract-status', uiText('dyn_1189', '⏳ 正在识别标题并写入书签…'));
    const res = await callApi('ocr_auto_bookmarks', pdf, out, val('ex-ocr-range'), ocrQuality(),
                              ocrLang());
    if (!finish(res, 'extract-status',
                r => r.count ? out + uiTextf('dyn_1190', '（生成 {0} 条书签）', r.count)
                             : uiText('dyn_1191', '没有找到可用的标题（Beta 只认大字号和章节式标题）'),
                uiText('dyn_1192', 'OCR 书签已生成'), uiText('dyn_1193', 'OCR 自动书签失败'))) return;
    const rows = (res.bookmarks || []).slice(0, 40).map(b =>
        '<div class="result-row">' + uiTextf('dyn_1009', '第 {0} 页 · {1}', b.page, esc(b.title)) + '</div>').join('');
    ocrShowResult('<div class="result-title">' + uiText('dyn_1194', '识别到的书签') + '</div>' + rows);
}

// ==================== 排版与整理：页码（F14）====================
function posGridPick(cell) {
    const grid = cell.parentNode;
    grid.querySelectorAll('.pos-cell').forEach(c => c.classList.toggle('active', c === cell));
    // 九宫格是按钮，不会触发 input/change，事件委托收不到，只能在这儿点一下
    layPreviewChanged('number');
}

function layPosition() {
    const cell = document.querySelector('#lay-pn-grid .pos-cell.active');
    return cell ? cell.dataset.pos : 'bottom-center';
}

/** 加页码的表单 → 后端 opt。「开始加页码」和右侧预览共用这一份，字段只有一处定义。 */
function layNumberOpt() {
    return {
        position: layPosition(),
        template: val('lay-pn-template') || '{page}',
        start: val('lay-pn-start'),
        number_style: val('lay-pn-style') || 'arabic',
        font_size: val('lay-pn-size'),
        margin: val('lay-pn-margin'),
        color: val('lay-pn-color'),
        page_range: val('lay-pn-range'),
        // 本机字体（族名，见 common.js 的字体下拉那一族）：空 = 后端内置口径；
        // 预览（preview_page_effect）和成品（add_page_numbers）吃的是同一个 opt
        font: val('lay-pn-font'),
        // 按不按 LaTeX 标记解析模板：默认关（见 index.html 的 latex_switch_desc 那句说明）。
        // 预览和成品同样共用这一份 opt，勾上以后两边不会是两副样子
        latex_markup: checked('pn-latex'),
    };
}

/** 成品回包里若带着「字体退回内置」那类 message（res.message），成功也要说完、不许静默。 */
function echoBackendNote(res, statusId) {
    if (!res || !res.message) return false;
    const el = document.getElementById(statusId);
    if (el) el.innerText = el.innerText + ' ⚠ ' + res.message;
    showToast(res.message, 5200);
    return true;
}

async function doPageNumbers() {
    const pdf = requirePdf('layout');
    if (!pdf) return;
    const out = await pickSave(stem(pdf) + uiText('dyn_1195', '_页码.pdf'));
    if (!out) return;
    setStatus('layout-status', uiText('dyn_1196', '⏳ 正在加页码…'));
    const res = await callApi('add_page_numbers', pdf, out, layNumberOpt());
    if (finish(res, 'layout-status', r => out + uiTextf('dyn_1295', '（{0} 页）', r.pages), uiText('dyn_1198', '页码已添加'), uiText('dyn_1199', '加页码失败'))) {
        echoBackendNote(res, 'layout-status');
    }
}

// ==================== 排版与整理：页眉页脚（F15）====================
function layEvenChanged() {
    const box = document.getElementById('lay-hf-even-box');
    if (box) box.classList.toggle('hidden', !checked('lay-hf-even'));
}

/** 页眉页脚的表单 → 后端 opt。 */
function layHeaderOpt() {
    const opt = {
        header: val('lay-hf-header'),
        footer: val('lay-hf-footer'),
        align: val('lay-hf-align') || 'center',
        font_size: val('lay-hf-size'),
        margin: val('lay-hf-margin'),
        color: val('lay-hf-color'),
        page_range: val('lay-hf-range'),
        font: val('lay-hf-font'),
        // 按不按 LaTeX 标记解析页眉页脚文字：默认关，和加页码那一格同一个口径
        latex_markup: checked('hf-latex'),
    };
    // 勾了「奇偶不同」才把偶数页那两项交下去：后端认的是「有没有这个键」，
    // 传一个空串过去等于把偶数页的页眉清空，那是双面印刷想要的另一种效果，不能默认给
    if (checked('lay-hf-even')) {
        opt.even_header = val('lay-hf-even-header');
        opt.even_footer = val('lay-hf-even-footer');
    }
    return opt;
}

async function doHeaderFooter() {
    const pdf = requirePdf('layout');
    if (!pdf) return;
    if (!val('lay-hf-header') && !val('lay-hf-footer') &&
        !(checked('lay-hf-even') && (val('lay-hf-even-header') || val('lay-hf-even-footer')))) {
        showToast(uiText('dyn_1200', '页眉和页脚都空着，至少写一项'));
        return;
    }
    const out = await pickSave(stem(pdf) + uiText('dyn_1201', '_页眉页脚.pdf'));
    if (!out) return;
    setStatus('layout-status', uiText('dyn_1202', '⏳ 正在写页眉页脚…'));
    const res = await callApi('add_header_footer', pdf, out, layHeaderOpt());
    if (finish(res, 'layout-status', r => out + uiTextf('dyn_1295', '（{0} 页）', r.pages), uiText('dyn_1204', '页眉页脚已应用'), uiText('dyn_1205', '页眉页脚失败'))) {
        echoBackendNote(res, 'layout-status');
    }
}

// ==================== 排版与整理：裁剪（F16）====================
function layCropSizeChanged() {
    const manual = !val('lay-crop-size');
    const box = document.getElementById('lay-crop-manual');
    if (box) box.classList.toggle('hidden', !manual);
}

/** 读第 1 页的可视尺寸填进裁剪框（默认「整页不裁」，用户只往里改数就行）。 */
async function layFillPageSize() {
    const pdf = selectedFiles['layout'];
    const echo = document.getElementById('lay-crop-info');
    if (!pdf) {
        if (echo) echo.innerText = '';
        return;
    }
    const res = await callApi('get_page_size', pdf, 0);
    if (!res || !res.success) {
        if (echo) echo.innerText = (res && res.message) || uiText('dyn_1206', '读不到页面尺寸');
        return;
    }
    const assign = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.value = value;
    };
    assign('lay-crop-x0', 0);
    assign('lay-crop-y0', 0);
    assign('lay-crop-x1', res.width);
    assign('lay-crop-y1', res.height);
    if (echo) {
        echo.innerText = uiTextf('dyn_1207', '第 {0} 页可视 {1}×{2} pt{3}，全文共 {4} 页', res.page, res.width, res.height, (res.rotation ? uiTextf('dyn_1300', '（页面转了 {0}°，坐标按你看到的算）', res.rotation) : ''), res.page_count);
    }
    // 直接写 value 不会触发 input 事件，预览要在这儿自己点一次，
    // 否则刚选完文件切到裁剪格，预览还是按上一个文件的 595×842 画的
    layPreviewChanged('crop');
}

function layLoadPageSize() {
    const pdf = requirePdf('layout');
    if (!pdf) return;
    layFillPageSize();
}

/** 裁剪的表单 → 后端 opt；四个数字没填全时返回 null（预览就当用户还没填完，不问后端）。 */
function layCropOpt() {
    const preset = val('lay-crop-size');
    let rect = preset;
    if (!preset) {
        const parts = ['lay-crop-x0', 'lay-crop-y0', 'lay-crop-x1', 'lay-crop-y1'].map(val);
        if (parts.some(p => p === '' || isNaN(Number(p)))) return null;
        rect = parts.map(Number);
    }
    return {rect_pt: rect, page_range: val('lay-crop-range')};
}

async function doCrop() {
    const pdf = requirePdf('layout');
    if (!pdf) return;
    const opt = layCropOpt();
    if (!opt) {
        showToast(uiText('dyn_1209', '裁剪框的四个数字都要填（左上 x、y 和右下 x、y）'));
        return;
    }
    const out = await pickSave(stem(pdf) + uiText('dyn_1210', '_裁剪.pdf'));
    if (!out) return;
    setStatus('layout-status', uiText('dyn_1211', '⏳ 正在裁剪…'));
    const res = await callApi('crop_pages', pdf, out, opt.rect_pt, opt.page_range);
    finish(res, 'layout-status', r => out + uiTextf('dyn_1295', '（{0} 页）', r.pages), uiText('dyn_1213', '裁剪完成'), uiText('dyn_1214', '裁剪失败'));
}

// ==================== 排版与整理：缩放（F17）====================
function layScaleModeChanged() {
    const bySize = val('lay-scale-mode') !== 'percent';
    const a = document.getElementById('lay-scale-size-box');
    const b = document.getElementById('lay-scale-percent-box');
    if (a) a.classList.toggle('hidden', !bySize);
    if (b) b.classList.toggle('hidden', bySize);
}

/** 缩放的表单 → 后端 opt；百分比空着时返回 null。 */
function layScaleOpt() {
    const byPercent = val('lay-scale-mode') === 'percent';
    const target = byPercent ? val('lay-scale-percent') : val('lay-scale-size');
    if (byPercent && (!target || isNaN(Number(target)))) return null;
    return {target: target, page_range: val('lay-scale-range'), by_percent: byPercent};
}

async function doScale() {
    const pdf = requirePdf('layout');
    if (!pdf) return;
    const opt = layScaleOpt();
    if (!opt) {
        showToast(uiText('dyn_1215', '请填一个 10–400 之间的百分比'));
        return;
    }
    const out = await pickSave(stem(pdf) + (opt.by_percent ? uiTextf('dyn_1216', '_缩放{0}%.pdf', opt.target)
                                                            : '_' + opt.target + '.pdf'));
    if (!out) return;
    setStatus('layout-status', uiText('dyn_1217', '⏳ 正在重排页面…'));
    const res = await callApi('scale_pages', pdf, out, opt.target, opt.page_range);
    finish(res, 'layout-status',
           r => out + uiTextf('dyn_1218', '（重排 {0} 页 / 成品 {1} 页）', r.scaled, r.pages),
           uiText('dyn_1219', '缩放完成'), uiText('dyn_1220', '缩放失败'));
}

// ==================== 排版与整理：单页效果预览（F14–F17 共用）====================
// 这四件工具改的都是「看数字想不出来」的东西：页码会不会压到正文、页眉够不够宽、
// 裁剪框裁掉了哪几个字、换成 A4 之后四周留白有多少。所以每一格右边给一个单页预览窗，
// 能翻着看第几页都行（预览哪页就渲染哪页）。
//
// 出图走后端 preview_page_effect：它在内存里把那一页按当前设置改一遍再出图，
// 不写临时文件、不动原文件，而且落笔的那几段代码和「开始加页码」用的是同一段
// （api/pagelayout.py 的 _apply_*），所以预览和成品不会是两副样子。
const LAY_PREVIEW_TOOLS = ['number', 'header', 'crop', 'scale'];
// 这四格的引导话术要等文案字典回来才取得到，所以写成函数、显示的那一刻才取
function layPreviewEmptyTexts() {
    return {
        number: uiText('dyn_1221', '选择 PDF 并设置页码位置与字号后，此处按当前设置显示添加页码后的单页效果。'),
        header: uiText('dyn_1222', '选择 PDF 并填写页眉或页脚文字后，此处显示添加页眉页脚后的单页效果。'),
        crop: uiText('dyn_1223', '选择 PDF 并填写裁剪框或选择幅面后，此处显示裁剪后的单页效果。'),
        scale: uiText('dyn_1224', '选择 PDF 并确定目标幅面或缩放比例后，此处显示该页调整幅面后的单页效果。'),
    };
}
// 上面那四句的键：预览区已经画好在屏上时用户才改语言，得靠这个表把话重画一遍
// （showPreviewHint 登记的是键，不是当时取到的那句）。
const LAY_PREVIEW_EMPTY_KEYS = { number: 'dyn_1221', header: 'dyn_1222',
                                 crop: 'dyn_1223', scale: 'dyn_1224' };
const layPreviewTimer = {};
const layPreviewSeq = {};
const layPreviewStamp = {};

function layPreviewNote(key, text) {
    const el = paneEl(key, 'note');
    if (!el) return;
    el.innerText = text || '';
    el.classList.toggle('hidden', !text);
}

function layPreviewOpt(key) {
    if (key === 'number') return layNumberOpt();
    if (key === 'header') return layHeaderOpt();
    if (key === 'crop') return layCropOpt();
    return layScaleOpt();
}

/** 表单一动就排一次：一次键入问一次后端太浪费，350ms 内的改动合成一次。 */
function layPreviewChanged(key) {
    if (LAY_PREVIEW_TOOLS.indexOf(key) < 0) return;
    if (layPreviewTimer[key]) clearTimeout(layPreviewTimer[key]);
    layPreviewTimer[key] = setTimeout(() => {
        layPreviewTimer[key] = null;
        layRenderPreview(key);
    }, 350);
}

function layPaneVisible(key) {
    const pane = document.getElementById('pane-layout-' + key);
    return !!pane && !pane.classList.contains('hidden');
}

/** 左边那列设置一动就排一次预览。
    几十个输入框逐个写 oninput 一定会漏，所以在 document 上收 input/change，
    再看它落在哪一格、只排那一格。右边翻页条和缩放档不在 .tool-controls 里，
    它们有自己的 onchange，不该在这儿多排一次。 */
function layPreviewFormEvent(event) {
    const el = event.target;
    if (!el || !el.closest) return;
    const controls = el.closest('.tool-controls');
    if (!controls) return;
    const pane = controls.closest('.panel-pane');
    const hit = pane && /^pane-layout-(number|header|crop|scale)$/.exec(pane.id);
    if (hit) layPreviewChanged(hit[1]);
}

document.addEventListener('input', layPreviewFormEvent);
document.addEventListener('change', layPreviewFormEvent);

/** 切回排版页时按当前窗宽补算一次「适应宽度」（只有正在显示的格子量得到宽度）。 */
function layRefitPreviews() {
    LAY_PREVIEW_TOOLS.forEach(key => {
        if (layPaneVisible(key)) applyPreviewZoom(key);
    });
}

/** 换了文件之后收尾：上一份文档的图、页数、设置指纹都不能接着用。 */
function layResetPreviews() {
    LAY_PREVIEW_TOOLS.forEach(key => {
        if (layPreviewTimer[key]) {
            clearTimeout(layPreviewTimer[key]);
            layPreviewTimer[key] = null;
        }
        layPreviewStamp[key] = '';
        resetPreviewPage(key);
        layPreviewNote(key, '');
        // 裁剪格这里不排防抖：那四个数字要等 layFillPageSize 从后端把这一页的真尺寸读回来
        // 才会重写，而那一趟往返比 350 ms 防抖晚到（实测 0.5 s 时才写进框）。先排一次就是
        // 拿出厂默认的 595×842 画一张立刻作废的图，而 A4 的真高是 841.9 pt，于是窗下还要
        // 闪一句自相矛盾的「裁剪框超出第 1 页（该页可视范围 595.3×841.9 pt）」。
        // 交给 layFillPageSize 末尾那一下：第一次画就是按真尺寸画的，中间只留一个空窗。
        if (key === 'crop' && selectedFiles['layout']) {
            const box = paneEl('crop', 'viewport');
            if (box) box.innerHTML = '';
            return;
        }
        // 换文件时裁剪框的数字还要被 layFillPageSize 重写一遍，所以这里也只排防抖：
        // 两边合在同一趟里，不必先按旧数字画一张立刻又作废的图
        if (layPaneVisible(key)) layPreviewChanged(key);
        else showPreviewHint(key, layPreviewEmptyTexts()[key], LAY_PREVIEW_EMPTY_KEYS[key]);
    });
}

async function layRenderPreview(key) {
    if (LAY_PREVIEW_TOOLS.indexOf(key) < 0) return;
    const vp = paneEl(key, 'viewport');
    if (!vp) return;
    if (layPreviewTimer[key]) {
        clearTimeout(layPreviewTimer[key]);
        layPreviewTimer[key] = null;
    }
    const pdf = selectedFiles['layout'];
    if (!pdf) {
        layPreviewStamp[key] = '';
        layPreviewNote(key, '');
        showPreviewHint(key, layPreviewEmptyTexts()[key], LAY_PREVIEW_EMPTY_KEYS[key]);
        return;
    }
    const opt = layPreviewOpt(key);
    // 数字还没填全（手填裁剪框空了一位、百分比空着）：不猜也不问后端，
    // 等用户填完那一下自然会再来一趟，比在预览窗里刷一句报错好
    if (!opt) return;
    const st = previewState[key];
    const stamp = pdf + '|' + st.page + '|' + JSON.stringify(opt);
    if (layPreviewStamp[key] === stamp) return;   // 设置没变，别白跑一趟后端
    const seq = (layPreviewSeq[key] || 0) + 1;
    layPreviewSeq[key] = seq;
    const res = await callApi('preview_page_effect', key, pdf, st.page, opt, PREVIEW_DPI);
    // 用户在这趟路上又改了设置：这次的结果已经过时，让最新那一次去画图
    if (layPreviewSeq[key] !== seq || !layPaneVisible(key)) return;
    if (!res || !res.success || !res.image) {
        layPreviewStamp[key] = '';
        showPreviewHint(key, (res && res.message) || uiText('dyn_1225', '这一页预览不出来'),
                        res && res.message ? '' : 'dyn_1225');
        layPreviewNote(key, '');
        return;
    }
    layPreviewStamp[key] = stamp;
    vp.innerHTML = '';
    const img = document.createElement('img');
    img.className = 'page-img';
    img.alt = '';
    img.src = res.image;
    // 宽度要等图解码完才知道 naturalWidth，所以缩放档在 onload 里再套一次
    img.onload = () => applyPreviewZoom(key);
    vp.appendChild(img);
    setPreviewCount(key, res.page_count);
    layPreviewNote(key, layPreviewNoteText(key, res));
    // 页数第一次拿到时当前页可能被夹回去（比如上一份文档只剩 1 页），重画一次
    if (st.page !== res.page - 1) layRenderPreview(key);
}

/** 预览窗下面那行小字：有话说就说，没说就整条收起。 */
function layPreviewNoteText(key, res) {
    if (res.note) return res.note;
    if (!res.applied) return '';
    if (key === 'crop' || key === 'scale') {
        const what = key === 'crop' ? uiText('dyn_1428', '裁完剩下的可视范围') : uiText('dyn_1226', '换好的纸');
        return what + '：' + res.width + '×' + res.height + ' pt';
    }
    return '';
}

// ==================== 安全与整理：敏感信息检测 + 涂黑（F19 F18）====================
// 检测分片跑：后端每片最多 3 秒就交回结果，这里循环调下去，
// 「停止」只把标志清掉、不再要下一片——后端不留状态，中断也就断了。
const KIND_LABELS = {
    id_card: 'kind_id_card', id_card_15: 'kind_id_card_15',
    phone: 'kind_phone', bank_card: 'kind_bank_card',
};
function confidenceLabels() {
    return {
        high: uiText('dyn_1227', '命中项能过校验位，把握大'),
        notice: uiText('dyn_1228', '只能按位数提示，未必是'),
        suspect: uiText('dyn_1229', '位数与校验都过，但仍可能是别的内容'),
    };
}

let secHits = [];
let detectRunning = false;

function renderHits() {
    const box = document.getElementById('sec-hits');
    const bar = document.getElementById('sec-hits-bar');
    if (!box) return;
    if (!secHits.length) {
        box.innerHTML = '';
        box.classList.add('hidden');
        if (bar) bar.classList.add('hidden');
        return;
    }
    box.classList.remove('hidden');
    if (bar) bar.classList.remove('hidden');
    box.innerHTML = secHits.map((hit, i) => {
        const kind = hit.kind === 'manual'
            ? t('kind_manual', '手动添加')
            : (hit.kind in KIND_LABELS ? t(KIND_LABELS[hit.kind], hit.kind) : hit.kind);
        return '<label class="hit-row">' + '<input type="checkbox" ' + (hit.on ? 'checked ' : '') + 'onchange="hitToggle(' + i + ', this.checked)">' + '<span class="hit-page">' + uiTextf('dyn_1402', '第 {0} 页', hit.page) + '</span>' + '<span class="hit-kind">' + esc(kind) + '</span>' + '<span class="hit-text">' + esc(hit.text) + '</span>' + '<span class="hit-conf" title="' + esc(confidenceLabels()[hit.confidence] || '') + '">' + esc(t('conf_' + hit.confidence, hit.confidence)) + '</span>' + '</label>';
    }).join('');
}

function hitToggle(index, on) {
    if (secHits[index]) secHits[index].on = !!on;
}

/**
 * 扫描途中刷新清单：攒一下再画，别每收到一片就整表重绘一次。
 *
 * detectSensitive 是一片一片要的，每片都要把清单整块 innerHTML 重排一遍；命中越多
 * 每一片越贵，扫到后面是「越扫越卡」。这里 250ms 之内来的都并进同一趟，
 * 扫描结束时照例还有一次直接 renderHits() 收尾，清单不会停在中间态。
 */
let hitsSoonTimer = null;
function renderHitsSoon() {
    if (hitsSoonTimer) return;
    hitsSoonTimer = setTimeout(() => {
        hitsSoonTimer = null;
        renderHits();
    }, 250);
}

function hitSelectAll(on) {
    secHits.forEach(hit => { hit.on = !!on; });
    renderHits();
}

function addManualHit() {
    const pdf = requirePdf('security');
    if (!pdf) return;
    const parts = ['sec-manual-x0', 'sec-manual-y0', 'sec-manual-x1', 'sec-manual-y1'].map(val);
    const page = parseInt(val('sec-manual-page'), 10);
    if (!page || page < 1 || parts.some(p => p === '' || isNaN(Number(p)))) {
        showToast(uiText('dyn_1230', '手动加位置要把页码和四个坐标都填全'));
        return;
    }
    const rect = parts.map(Number);
    if (rect[2] - rect[0] < 1 || rect[3] - rect[1] < 1) {
        showToast(uiText('dyn_1231', '右下角要在左上角之外（涂黑范围至少 1×1 pt）'));
        return;
    }
    secHits.push({ page: page, rect: rect, text: '', kind: 'manual', confidence: 'high', on: true });
    renderHits();
}

async function detectSensitive() {
    const pdf = requirePdf('security');
    if (!pdf) return;
    if (detectRunning) return;
    const kinds = Array.from(document.querySelectorAll('.sec-kind:checked')).map(cb => cb.value);
    if (!kinds.length) {
        showToast(uiText('dyn_1232', '至少勾一项要找的信息'));
        return;
    }
    detectRunning = true;
    secHits = [];
    renderHits();
    const range = val('sec-detect-range');
    let index = 0;
    let scanned = 0;
    let sweptTo = false;
    setStatus('security-status', uiText('dyn_1233', '⏳ 正在扫描…'));
    while (detectRunning) {
        const res = await callApi('detect_sensitive_info', pdf, kinds, range, index);
        if (!res || !res.success) {
            detectRunning = false;
            renderHits();
            reportFailure(res, document.getElementById('security-status'), uiText('dyn_1234', '检测失败'));
            return;
        }
        (res.hits || []).forEach(hit => secHits.push(Object.assign({ on: true }, hit)));
        scanned += res.scanned_pages || 0;
        if (res.done) { sweptTo = true; break; }
        if (res.next_index <= index) break;   // 后端没往前走就立刻收摊，别在这儿空转
        index = res.next_index;
        renderHitsSoon();
        setStatus('security-status', uiTextf('dyn_1235', '⏳ 已扫 {0} 页，命中 {1} 处…', scanned, secHits.length));
    }
    detectRunning = false;
    // 攒着的那一趟必须当场补画掉：扫描已经结束，清单要停在最终态
    if (hitsSoonTimer) { clearTimeout(hitsSoonTimer); hitsSoonTimer = null; }
    renderHits();
    const tail = secHits.length
        ? uiTextf('dyn_1236', '，命中 {0} 处。勾掉不想涂的，再点「涂黑选中项」', secHits.length)
        : uiText('dyn_1237', '，没发现所选的信息');
    setStatus('security-status', uiTextf('dyn_1238', '✓ 已扫 {0} 页{1}', scanned, tail));
    if (!sweptTo) showToast(uiTextf('dyn_1239', '扫描已停止（{0} 页）', scanned), 3200);
    else showToast(secHits.length ? uiTextf('dyn_1240', '找到 {0} 处', secHits.length) : uiText('dyn_1241', '没发现所选信息'), 3200);
}

function stopDetect() {
    // 后端不留状态：不再要下一片就是停止，已经找到的清单原样留着
    detectRunning = false;
}

async function doRedact() {
    const pdf = requirePdf('security');
    if (!pdf) return;
    const picked = secHits.filter(hit => hit.on);
    if (!picked.length) {
        showToast(uiText('dyn_1242', '清单里还没有勾选要涂掉的位置'));
        return;
    }
    const out = await pickSave(stem(pdf) + uiText('dyn_1243', '_已涂黑.pdf'));
    if (!out) return;
    const targets = picked.map(hit => ({ page: hit.page, rect: hit.rect }));
    setStatus('security-status', uiTextf('dyn_1244', '⏳ 正在涂黑 {0} 处…', targets.length));
    const res = await callApi('apply_redaction', pdf, out, targets,
                              parseInt(val('sec-image-mode'), 10));
    if (!finish(res, 'security-status',
                r => out + uiTextf('dyn_1245', '（涂掉 {0} 处{1}）', r.removed, (r.empty ? uiTextf('dyn_1429', '，{0} 处那块本来就是空的', r.empty) : '')),
                uiText('dyn_1246', '涂黑完成'), uiText('dyn_1247', '涂黑失败'))) return;
    // 成品接回当前文件：下一步自然是「再检测一遍确认干净」，
    // 而不是拿着原来那份（字还在里面）重复操作
    selectedFiles['security'] = out;
    setStatus('security-status', uiTextf('dyn_1248', '✓ {0}（涂掉 {1} 处） —— 现在处理的是这份成品，可以再点一次「开始检测」核对', out, res.removed));
    secHits = [];
    renderHits();
}

// ==================== 安全与整理：清理隐藏信息（F20）====================
async function doCleanHidden() {
    const pdf = requirePdf('security');
    if (!pdf) return;
    const opt = {
        clear_metadata: checked('sec-clean-meta'),
        clear_xmp: checked('sec-clean-xmp'),
        clear_attachments: checked('sec-clean-att'),
        clear_scripts: checked('sec-clean-js'),
    };
    if (!Object.keys(opt).some(k => opt[k])) {
        showToast(uiText('dyn_1249', '一项都没勾，那就没有要清理的'));
        return;
    }
    const out = await pickSave(stem(pdf) + uiText('dyn_1250', '_已清理.pdf'));
    if (!out) return;
    setStatus('security-status', uiText('dyn_1251', '⏳ 正在清理并重存…'));
    const res = await callApi('clean_hidden_info', pdf, out, opt);
    const box = document.getElementById('sec-clean-result');
    if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
    if (!finish(res, 'security-status',
                r => out + '（' + fileSize(r.before) + ' → ' + fileSize(r.after) + '）',
                uiText('dyn_1252', '清理完成'), uiText('dyn_1253', '清理隐藏信息失败'))) return;
    // note 是「本来就没多少可清的、体积没再小」这类如实说明，不当失败但要说出来
    if (res.note) showToast(res.note, 5200);
    if (box) {
        const lines = [];
        lines.push(uiText('dyn_1404', '清掉的元数据项：') + ((res.dropped_metadata || []).join('、') || uiText('dyn_1256', '无')));
        lines.push(uiText('dyn_1012', 'XMP 描述块：') + (res.xmp_bytes ? res.xmp_bytes + uiText('dyn_1430', ' 字节') : uiText('dyn_1256', '无')));
        lines.push(uiText('dyn_1406', '内嵌附件：') + ((res.attachments || []).join('、') || uiText('dyn_1256', '无')));
        lines.push(uiTextf('dyn_1407', 'JavaScript 引用：{0} 处', (res.script_refs || 0)));
        lines.push(uiTextf('dyn_1408', '页数 {0} 页，书签（目录）{1} 条，体积 {2} → ', res.pages, res.toc, fileSize(res.before)) + fileSize(res.after));
        box.classList.remove('hidden');
        box.innerHTML = '<div class="result-title">' + esc(t('clean_result_hint', '清理结果（已重新打开核对）')) +
            '</div>' + lines.map(s => '<div class="result-row">' + esc(s) + '</div>').join('');
    }
    selectedFiles['security'] = out;
}

// ==================== 安全与整理：权限限制（F21）====================
const PERM_KEYS = {
    print: 'perm_print', copy: 'perm_copy', modify: 'perm_modify', annotate: 'perm_annotate',
    forms: 'perm_forms', assemble: 'perm_assemble', print_hq: 'perm_print_hq',
    accessibility: 'perm_accessibility',
};

async function doPermissions() {
    const pdf = requirePdf('security');
    if (!pdf) return;
    const deny = Array.from(document.querySelectorAll('.sec-perm:checked')).map(cb => cb.value);
    if (!deny.length) {
        showToast(uiText('dyn_1257', '至少勾一项要禁止的操作'));
        return;
    }
    const out = await pickSave(stem(pdf) + uiText('dyn_1258', '_受限.pdf'));
    if (!out) return;
    setStatus('security-status', uiText('dyn_1259', '⏳ 正在加权限限制…'));
    const res = await callApi('set_pdf_permissions', pdf, out, val('sec-perm-user'),
                              val('sec-perm-owner'), deny, val('sec-perm-engine') || 'fitz');
    finish(res, 'security-status', r => {
        const names = (r.denied || []).map(k => t(PERM_KEYS[k] || '', k)).join('、');
        return out + uiTextf('dyn_1260', '（已禁止：{0}；读回权限位 {1}）', names, r.permissions_readback);
    }, uiText('dyn_1261', '权限限制已应用'), uiText('dyn_1262', '设置权限失败'));
}

// ==================== 书签（F22，挂在页面管理页）====================
let bmList = [];
let bmOpen = false;

function pmToggleBookmarks() {
    const panel = document.getElementById('pm-bookmarks');
    if (!panel) return;
    bmOpen = !bmOpen;
    panel.classList.toggle('hidden', !bmOpen);
    if (bmOpen) bmReload();
}

function bmReset() {
    bmList = [];
    const tree = document.getElementById('bm-tree');
    if (tree) tree.innerHTML = '';
    setStatus('bm-msg', '');
}

function bmMessage(text) {
    setStatus('bm-msg', text);
}

async function bmReload() {
    const pdf = selectedFiles['pm'];
    const tree = document.getElementById('bm-tree');
    if (!pdf || !tree) return;
    const res = await callApi('get_bookmarks', pdf);
    if (!res || !res.success) {
        bmList = [];
        tree.innerHTML = '<div class="bm-empty">' + esc((res && res.message) || uiText('dyn_1263', '读取书签失败')) + '</div>';
        return;
    }
    bmList = res.bookmarks || [];
    if (!bmList.length) {
        tree.innerHTML = '<div class="bm-empty">' + esc(res.message || uiText('dyn_1264', '这份 PDF 没有书签')) + '</div>';
        return;
    }
    tree.innerHTML = bmList.map((row, i) =>
        '<div class="bm-row" style="padding-left:' + ((row.level - 1) * 14) + 'px" ' +
        'onclick="bmJump(' + row.page + ')" title="' + uiTextf('dyn_1265', '在页面管理里选中第 {0} 页', row.page) + '">' +
        '<span class="bm-title">' + esc(row.title) + '</span>' +
        '<span class="bm-page">' + uiTextf('dyn_1402', '第 {0} 页', row.page) + '</span>' +
        '<span class="bm-index">' + (i + 1) + '</span>' +
        '</div>').join('');
    bmMessage(uiTextf('dyn_1266', '{0} 条书签 · 全文 {1} 页 · 点一行可在缩略图里选中那一页', bmList.length, res.page_count));
}

/** 从书签跳到页面管理里的对应格：按原始页码找格子，删过页就如实说找不到。 */
function bmJump(pageNo) {
    const pos = pmSlots.findIndex(slot => slot.orig === pageNo - 1);
    if (pos < 0) {
        showToast(uiTextf('dyn_1267', '第 {0} 页不在当前的页面管理列表里（可能已经删掉了）', pageNo));
        return;
    }
    const slot = pmSlots[pos];
    pmSetSelection([slot.id]);
    const cell = pmCell(slot.id);
    pmScrollCellIntoView(cell);
}

async function bmExport() {
    const pdf = requirePdfSlot('pm');
    if (!pdf) return;
    const out = await pickSave(stem(pdf) + uiText('dyn_1268', '_书签.json'), ['JSON (*.json)']);
    if (!out) return;
    const res = await callApi('export_bookmarks', pdf, out);
    finish(res, 'pm-status', r => out + uiTextf('dyn_1269', '（{0} 条书签）', r.count), uiText('dyn_1270', '书签已导出'), uiText('dyn_1271', '导出书签失败'));
}

async function bmImport() {
    const pdf = requirePdfSlot('pm');
    if (!pdf) return;
    const json = await callApi('open_file_dialog', ['Bookmark JSON (*.json)']);
    if (!json || typeof json !== 'string') return;
    const out = await pickSave(stem(pdf) + uiText('dyn_1272', '_导入书签.pdf'));
    if (!out) return;
    const opt = { mode: val('bm-import-mode') || 'replace', skip_invalid: checked('bm-skip-invalid') };
    bmMessage(uiText('dyn_1273', '⏳ 正在导入书签…'));
    const res = await callApi('import_bookmarks', pdf, out, json, opt);
    if (!finish(res, 'pm-status',
                r => out + uiTextf('dyn_1274', '（写入 {0} 条，{1}{2}{3}）', r.count, (r.mode === 'append' ? uiText('dyn_1431', '接在原有后面') : uiText('bm_mode_replace', '整树覆盖')), (r.levels_normalized ? uiTextf('dyn_1432', '，{0} 条层级压平', r.levels_normalized) : ''), (r.skipped && r.skipped.length ? uiTextf('dyn_1303', '，跳过 {0} 条越界', r.skipped.length) : '')),
                uiText('dyn_1277', '书签已导入'), uiText('dyn_1278', '导入书签失败'))) {
        bmMessage('');
        return;
    }
    if (res.message) showToast(res.message, 5200);
    bmReload();
}

async function bmSplit() {
    const pdf = requirePdfSlot('pm');
    if (!pdf) return;
    const dir = await callApi('select_directory');
    if (!dir || typeof dir !== 'string') return;
    const level = parseInt(val('bm-level'), 10) || 1;
    bmMessage(uiTextf('dyn_1279', '⏳ 正在按第 {0} 层书签拆分…', level));
    const res = await callApi('split_by_bookmarks', pdf, dir, level,
                              { keep_outline: checked('bm-keep-outline') });
    if (!finish(res, 'pm-status',
                r => dir + uiTextf('dyn_1280', '（{0} 个分册{1}）', (r.files || []).length, (r.skipped_pages ? uiTextf('dyn_1433', '，{0} 页不在任何书签里', r.skipped_pages) : '')),
                uiText('dyn_1060', '拆分完成'), uiText('dyn_1282', '按书签拆分失败'))) {
        bmMessage('');
        return;
    }
    bmMessage(res.message || '');
    if (res.reordered) showToast(uiText('dyn_1283', '书签页码本来是乱序，已按页码排好再拆'), 4600);
}

/** 书签三项操作的是磁盘上那份 PDF，页面管理里没保存的暂存改动不在其中，先说清楚。 */
function requirePdfSlot(type) {
    const path = selectedFiles[type];
    if (!path) {
        showToast(uiText('dyn_1284', '请先在页面管理里选择 PDF'));
        return '';
    }
    if (type === 'pm' && pmDirty) {
        showToast(uiText('dyn_1285', '页面管理里还有没保存的改动：书签操作的是磁盘上那一份，暂存的排序/删除不算在内'), 5200);
    }
    return path;
}

// ==================== 格式转换页的另外三个方向（F11 F12 + LaTeX → PDF）====================
let imageFilesList = [];
let imageDragSrc = -1;
let txtFilePath = '';

function toggleConvertDirection() {
    const dir = val('convert-direction') || 'pdf';
    ['pdf', 'images', 'text', 'latex'].forEach(name => {
        const el = document.getElementById('convert-' + name + '-block');
        if (el) el.classList.toggle('hidden', name !== dir);
    });
}

async function pickImages() {
    // 单选框点十几张照片没人受得了，这里走多选通道（open_files_dialog）
    const files = await callApi('open_files_dialog');
    if (!Array.isArray(files) || !files.length) return;
    let added = 0;
    files.forEach(path => {
        if (imageFilesList.includes(path)) return;
        imageFilesList.push(path);
        added += 1;
    });
    renderImageList();
    setStatus('convert-status', uiTextf('dyn_1286', '✓ 已添加 {0} 张，列表共 {1} 张', added, imageFilesList.length));
}

function clearImages() {
    if (!imageFilesList.length) return;
    imageFilesList = [];
    renderImageList();
}

function removeImageFile(index) {
    imageFilesList.splice(index, 1);
    renderImageList();
}

function moveImageFile(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= imageFilesList.length) return;
    const tmp = imageFilesList[index];
    imageFilesList[index] = imageFilesList[target];
    imageFilesList[target] = tmp;
    renderImageList();
}

function renderImageList() {
    const list = document.getElementById('image-list');
    if (!list) return;
    if (!imageFilesList.length) { list.innerHTML = ''; return; }
    // 结构和合并列表一样（拖行首 ⋮⋮ 排序 + ↑↓ + ✕），但回调各走各的数组，
    // 复用合并那几个函数会把 PDF 列表搅乱
    list.innerHTML = imageFilesList.map((path, i) => {
        const isFirst = i === 0;
        const isLast = i === imageFilesList.length - 1;
        return '<div class="merge-item" draggable="true"' +
            ' ondragstart="onImgDragStart(event, ' + i + ')"' +
            ' ondragover="onImgDragOver(event, ' + i + ')"' +
            ' ondrop="onImgDrop(event, ' + i + ')"' +
            ' ondragend="onImgDragEnd(event)">' +
            '<span class="drag-handle" title="' + uiText('dyn_1403', '拖拽排序') + '">⋮⋮</span>' +
            '<span class="merge-index">' + (i + 1) + '.</span>' +
            '<span class="merge-name" title="' + esc(path) + '">' + esc(baseName(path)) + '</span>' +
            '<button class="action-btn"' + (isFirst ? ' disabled' : '') +
                ' onclick="moveImageFile(' + i + ', -1)">↑</button>' +
            '<button class="action-btn"' + (isLast ? ' disabled' : '') +
                ' onclick="moveImageFile(' + i + ', 1)">↓</button>' +
            '<button class="action-btn remove-btn" onclick="removeImageFile(' + i + ')">✕</button>' +
            '</div>';
    }).join('');
}

function onImgDragStart(e, index) {
    imageDragSrc = index;
    e.dataTransfer.effectAllowed = 'move';
    e.currentTarget.classList.add('dragging');
}

function onImgDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    e.currentTarget.classList.add('drag-over');
}

function onImgDrop(e, index) {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    if (imageDragSrc < 0 || imageDragSrc === index) return;
    const moved = imageFilesList.splice(imageDragSrc, 1)[0];
    imageFilesList.splice(index, 0, moved);
    imageDragSrc = -1;
    renderImageList();
}

function onImgDragEnd() {
    document.querySelectorAll('#image-list .merge-item').forEach(el => el.classList.remove('drag-over'));
}

async function doImagesToPdf() {
    if (!imageFilesList.length) {
        showToast(uiText('dyn_1287', '请先添加图片'));
        return;
    }
    const out = await pickSave(uiText('dyn_1288', '图片合集.pdf'));
    if (!out) return;
    setStatus('convert-status', uiTextf('dyn_1289', '⏳ 正在合成 PDF（{0} 张）…', imageFilesList.length));
    const res = await callApi('images_to_pdf', imageFilesList, val('images-size') || 'a4',
                              checked('images-aspect'), out);
    if (!finish(res, 'convert-status',
                r => out + uiTextf('dyn_1290', '（{0} 页{1}）', r.pages, (r.failed && r.failed.length ? uiTextf('dyn_1401', '，跳过 {0} 张读不出的', r.failed.length) : '')),
                '', uiText('dyn_1292', '图片转 PDF 失败'))) return;
    if (res.message) showToast(res.message, 5200);
}

async function selectTxtFile() {
    const path = await callApi('open_file_dialog', ['Text (*.txt)', 'All files (*.*)']);
    if (!path || typeof path !== 'string') return;
    txtFilePath = path;
    setStatus('convert-status', '✓ ' + path);
}

async function doTextToPdf() {
    if (!txtFilePath) {
        showToast(uiText('dyn_1293', '请先选择文本文件'));
        return;
    }
    const out = await pickSave(stem(txtFilePath) + '.pdf');
    if (!out) return;
    setStatus('convert-status', uiText('dyn_1294', '⏳ 正在排版成 PDF…'));
    // 最后一个参数是按不按 LaTeX 标记解析（默认关，见 index.html 那句 latex_switch_desc）：
    // 文本文件里反斜杠太常见（路径、代码），不勾就一个字符都不动
    const res = await callApi('text_to_pdf', txtFilePath, out,
                              val('text-size') || 11, val('text-margin') || 56.7,
                              val('text-font'), checked('text-latex'));
    if (finish(res, 'convert-status', r => out + uiTextf('dyn_1295', '（{0} 页）', r.pages), uiText('dyn_1296', '已生成 PDF'), uiText('dyn_1297', '文本转 PDF 失败'))) {
        echoBackendNote(res, 'convert-status');
    }
}

// ==================== 格式转换：LaTeX → PDF ====================
// 三个后端方法各管一件事：check_tex_file 只解析不出图（点「转换」之前先看有没有坑）、
// get_latex_capabilities 给「本软件支持的 LaTeX 范围」的清单、latex_to_pdf 出成品
// （由 app.js 的 doConvert 按方向派发过来）。
// 回包里的 notes / unknown / ignored / errors 就是「哪儿没照做」的账，一律逐条列出来，
// 不许出现只报一句「成功」而内容少了的情况（后端 api/latex.py 同口径）。
let latexTexPath = '';
let latexCapsLoaded = false;
// 同一个命令在一份文件里可能出现几十次，全列没人看；超过这个数只列前 N 条并说明共几条
const LATEX_REPORT_LIMIT = 40;

/** 一行结果框里的条目（沿用 .result-box / .result-row 那套，不新造容器） */
function latexRow(html) {
    return '<div class="caps-item">' + html + '</div>';
}

/**
 * 后端回包的说明条目（{key, args:{name:..}}）→ 当前语言的那句话。
 *
 * uiTextf 只按 {0} 位置替换，而这些条目带的是**具名**参数，和后端 t(key, **args)
 * 同一口径，所以这里按名字代入。字典里查不到这个键就返回空串——
 * 宁可少一句明细，也不能把 'api_latex_env_mismatch' 这种键名甩到屏幕上。
 */
function latexIssueText(item) {
    const dict = currentTextDict || {};
    const tpl = dict[item && item.key];
    if (!tpl) return '';
    const args = (item && item.args) || {};
    return String(tpl).replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, function (m, name) {
        return args[name] === undefined ? m : String(args[name]);
    });
}

/** 「第 12 行」那一小段；行号缺失（后端没记到）就整段不要，别留一句半截话。 */
function latexLineTag(item) {
    if (!item || !(Number(item.line) > 0)) return '';
    return '<span class="caps-line">' + esc(uiTextf('latex_at_line', '第 {0} 行', item.line)) + '</span>';
}

/** 「\cmd 参数（第 N 行）」：unknown 与 ignored 两种条目形状一样，共用一行。 */
function latexCmdRow(item) {
    const arg = item && item.arg ? esc(String(item.arg)) : '';
    const line = latexLineTag(item);
    return latexRow('<span class="caps-cmd">\\' + esc(String((item && item.cmd) || '')) + '</span>'
        + (arg ? ' ' + arg : '') + (line ? ' ' + line : ''));
}

/** 「哪儿没照做」的清单 → 结果框里的那几行（结构问题 / 未识别 / 已忽略 三组）。 */
function latexIssueRows(res) {
    const out = [];
    const groups = [
        { items: (res && res.errors) || [],
          title: uiTextf('latex_group_errors', '结构问题 {0} 处'),
          row: function (it) {
              const text = latexIssueText(it);
              const line = latexLineTag(it);
              if (text) return latexRow(esc(text) + (line ? ' ' + line : ''));
              return line ? latexRow(line) : '';
          } },
        { items: (res && res.unknown) || [],
          title: uiTextf('latex_group_unknown', '未识别的命令 {0} 条'), row: latexCmdRow },
        { items: (res && res.ignored) || [],
          title: uiTextf('latex_group_ignored', '按宏包命令忽略 {0} 条'), row: latexCmdRow },
    ];
    groups.forEach(function (g) {
        if (!g.items.length) return;
        out.push('<div class="caps-group-title">' + esc(g.title) + '</div>');
        g.items.slice(0, LATEX_REPORT_LIMIT).forEach(function (it) {
            const row = g.row(it);
            if (row) out.push(row);
        });
        if (g.items.length > LATEX_REPORT_LIMIT) {
            out.push(latexRow(esc(uiTextf('latex_rows_cut',
                '（下面只列前 {0} 条，共 {1} 条）', LATEX_REPORT_LIMIT, g.items.length))));
        }
    });
    return out;
}

/**
 * 把预检 / 转换的结论写进这一格的结果区，返回「这份文件有没有要说的事」。
 *
 * 上面那几行是后端整句取好的汇总（res.message，一行一条），下面是逐条明细
 * （哪个命令在第几行没照做）。两条都要：汇总让人一眼看清严重程度，
 * 明细让人能回去改那份 .tex。一条都没有时也写一句「没有需要说明的项」，
 * 不能让结果框空着让人以为这一步没跑。
 */
function showLatexReport(intro, res) {
    const box = document.getElementById('latex-report');
    if (!box) return false;
    const rows = [];
    String((res && res.message) || '').split('\n').forEach(function (line) {
        if (line.trim()) rows.push(latexRow(esc(line.trim())));
    });
    const flagged = rows.length > 0;
    Array.prototype.push.apply(rows, latexIssueRows(res));
    if (!rows.length) rows.push(latexRow(esc(uiText('latex_no_issue', '没有需要说明的项'))));
    box.innerHTML = '<div class="result-title">' + intro + '</div>' + rows.join('');
    box.classList.remove('hidden');
    return flagged;
}

function clearLatexReport() {
    const box = document.getElementById('latex-report');
    if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
}

async function selectTexFile() {
    // 过滤器是给后端原生的二元组（见 api/base.py 的 open_file_dialog），和 selectTxtFile 同一写法
    const path = await callApi('open_file_dialog', ['TeX (*.tex)', 'All files (*.*)']);
    if (!path || typeof path !== 'string') return;
    latexTexPath = path;
    const echo = document.getElementById('latex-tex-echo');
    if (echo) echo.innerText = path;
    // 换文件等于上一份的结论作废：那一屏「几个命令没照做」说的是上一份文件
    clearLatexReport();
    setStatus('convert-status', uiText('latex_picked', '✓ 已选择文件，可先检查再转换'));
}

async function doCheckTex() {
    if (!latexTexPath) {
        showToast(uiText('latex_need_tex', '请先选择 .tex 文件'));
        return;
    }
    setStatus('convert-status', uiText('latex_checking', '⏳ 正在检查这份文件…'));
    const res = await callApi('check_tex_file', latexTexPath);
    clearLatexReport();
    if (!res || !res.success) {
        reportFailure(res, document.getElementById('convert-status'),
                      uiText('latex_check_fail', '检查这份文件时出错'));
        return;
    }
    // 预检不写文件，所以不走 finish()（那句是「写出了什么」用的）：结论一句 + 明细列表
    setStatus('convert-status', '✓ ' + uiTextf('latex_check_ok',
        '{0}（编码 {1}，内容段 {2} 个）', baseName(latexTexPath), res.encoding, res.blocks));
    const title = res.title ? uiTextf('latex_check_title', '文档标题：{0}', res.title)
                            : uiText('latex_check_intro', '这份文件的预检结果');
    if (showLatexReport(esc(title), res)) {
        // 状态行只补一句指路的话，长汇总留在结果区里逐条看（状态行不保留换行）
        echoBackendNote({ message: uiText('latex_report_hint', '结果区里列出了需要留意的地方') },
                        'convert-status');
    }
}

/** 展开区第一次打开才去后端取清单：不开这一栏就不该多跑一趟。 */
function latexCapsToggled(details) {
    if (!details || !details.open || latexCapsLoaded) return;
    loadLatexCaps();
}

/**
 * 换语言后让这份清单作废（applyLanguage() 里点名调）。
 *
 * 这一栏是 JS 现拼的 HTML，不在 data-i18n 的覆盖范围里：语言换了、栏里的字还留着上一次那句。
 * 清掉重取；栏还没开就只记「不新鲜」，等下一次开的时候再取（不必为这句去多跑一趟后端）。
 */
function latexCapsInvalidate() {
    latexCapsLoaded = false;
    const details = document.getElementById('latex-caps-box');
    if (details && details.open) loadLatexCaps();
}

async function loadLatexCaps() {
    const box = document.getElementById('latex-caps-body');
    if (!box) return;
    box.innerHTML = latexRow(esc(uiText('latex_caps_loading', '正在读取支持范围…')));
    const res = await callApi('get_latex_capabilities');
    if (!res || !res.success) {
        reportFailure(res, document.getElementById('convert-status'),
                      uiText('latex_caps_fail', '取不到支持范围'));
        box.innerHTML = '';
        return;
    }
    latexCapsLoaded = true;
    const summary = res.summary || {};
    const parts = [];
    parts.push('<div class="caps-count">' + esc(uiTextf('latex_caps_counts',
        '命令 {0} 条 / 符号 {1} 个 / 内置字体画不出、已换成等价写法的 {2} 条',
        res.command_count, res.symbol_count, res.forced_count)) + '</div>');
    [['structure', 'latex_caps_structure'], ['text', 'latex_caps_text'],
     ['math', 'latex_caps_math']].forEach(function (g) {
        const items = summary[g[0]] || [];
        if (!items.length) return;
        parts.push('<div class="caps-group-title">' + esc(uiText(g[1], g[0])) + '</div>');
        // 每段下面那句说明走后端给的本地化文案（命令名不翻，说明要翻）
        const note = (res.notes || {})[g[0]];
        if (note) {
            parts.push('<div class="caps-group-note">' + esc(note) + '</div>');
        }
        parts.push('<ul class="caps-list">'
            + items.map(function (it) { return '<li>' + esc(it) + '</li>'; }).join('')
            + '</ul>');
    });
    if (res.limits_note) {
        parts.push('<div class="caps-limits">' + esc(res.limits_note) + '</div>');
    }
    box.innerHTML = parts.join('');
}

/** 转换方向选到 LaTeX 时 doConvert 走这一条（见 app.js）。 */
async function convertLatexToPdf() {
    if (!latexTexPath) {
        showToast(uiText('latex_need_tex', '请先选择 .tex 文件'));
        return;
    }
    const out = await pickSave(stem(latexTexPath) + '.pdf');
    if (!out) return;
    setStatus('convert-status', uiText('latex_converting', '⏳ 正在排版成 PDF…'));
    const res = await callApi('latex_to_pdf', latexTexPath, out);
    clearLatexReport();
    if (!finish(res, 'convert-status',
                r => out + uiTextf('dyn_1295', '（{0} 页）', r.pages),
                uiText('latex_done_toast', '已生成 PDF'),
                uiText('latex_fail', 'LaTeX 转 PDF 失败'))) return;
    // 「排好了」后面必须跟着「哪儿没照做」：res.message 是后端拼好的汇总，
    // 明细（哪个命令在第几行没生效）逐条列进结果区，不许只留一个 ✓
    if (showLatexReport(esc(uiTextf('latex_convert_intro',
            '{0} 已写出（编码 {1}）', out, res.encoding)), res)) {
        echoBackendNote({ message: uiText('latex_report_hint', '结果区里列出了需要留意的地方') },
                        'convert-status');
    }
}

// ==================== 内容提取：导出 LaTeX ====================
async function doExportLatex() {
    const pdf = requirePdf('extract');
    if (!pdf) return;
    const out = await pickSave(stem(pdf) + '.tex', ['LaTeX Document (*.tex)']);
    if (!out) return;
    setStatus('extract-status', uiText('latex_exporting', '⏳ 正在导出 LaTeX…'));
    // 第四个参数是打开口令：这一页其余几格都不问口令（加密文件后端会点名要），
    // 这里保持同一个口径，留空交给后端按 needs_password 那条说明回话
    const res = await callApi('export_latex', pdf, val('ex-latex-range'), out, '');
    const box = document.getElementById('ex-latex-result');
    if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
    if (!finish(res, 'extract-status',
                r => out + uiTextf('latex_export_done', '（{0} 页 / {1} 字符）', r.pages, r.chars),
                uiText('latex_export_toast', 'LaTeX 导出完成'),
                uiText('latex_export_fail', '导出 LaTeX 失败'))) return;
    // 反推不出来的那部分（公式、图片）后端放在 notes 里，必须显示：
    // 只报页数会让人以为 .tex 和原 PDF 内容等价
    const notes = (res.notes || []).filter(function (s) { return !!String(s || '').trim(); });
    if (box && notes.length) {
        box.innerHTML = '<div class="result-title">' + esc(uiText('latex_export_notes', '这份 .tex 的说明'))
            + '</div>' + notes.map(function (s) { return latexRow(esc(s)); }).join('');
        box.classList.remove('hidden');
    } else if (notes.length) {
        echoBackendNote({ message: notes.join(' ') }, 'extract-status');
    }
}

// ==================== 与 app.js 的接头 ====================
/**
 * 换了文件之后各新面板要各自收尾：上一份文档的命中清单、尺寸、书签树都不能接着用。
 * app.js 的 selectPdf 末尾调这一个钩子，它不认识具体有哪些新页。
 */
window.onPdfChosen = function (type) {
    if (type === 'security') {
        detectRunning = false;
        secHits = [];
        renderHits();
    }
    if (type === 'extract') {
        // 上一份文档的「这份 .tex 有什么反推不出来」说明对这一份没有意义，先收掉
        const latexBox = document.getElementById('ex-latex-result');
        if (latexBox) { latexBox.classList.add('hidden'); latexBox.innerHTML = ''; }
    }
    // 页面管理并进了排版与整理：这一页的标签共用同一份文件，所以换文件时
    // 两个 key 都要收尾（尺寸填进裁剪框、上一份的书签树清掉）。
    if (type === 'layout' || type === 'pm') {
        // 先收预览（上一份文档的图、页数、设置指纹全作废），再填裁剪框的新尺寸：
        // 两边都只排防抖，最后落到后端的是「新尺寸 + 新文件」那一趟
        layResetPreviews();
        layFillPageSize();
        bmReset();
    }
};

/** init() 里调一次：把各面板的控件初值和当前选项对上。 */
function toolsInit() {
    toggleConvertDirection();
    exImgModeChanged();
    layCropSizeChanged();
    layScaleModeChanged();
    layEvenChanged();
    // 骑缝印那块：出厂 radio 是「按磅」，另一档输入框就该是灰的（别看着两个都能填）
    crossSealWidthModeChanged();
    renderHits();
    renderImageList();
}
