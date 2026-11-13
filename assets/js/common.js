// ============================================================================
// 公共前端库：主界面（app.js）与设置窗口（settings.js）共用。
// 必须在它们之前加载。
// ============================================================================

// 十个主题的色板。这一版把饱和度整体压到 60% 上下、暗部一律带灰：
// 原来的 #0066ff / #7b2ffc / #ff8800 是满饱和的霓虹档，几个主题并排看像同一套模板换了皮。
// 每个主题只保留原来那一点色相，认得出「这是绿色主题」就够了。
//
// 三处副本必须一起改：这里、core/splash.py 的 PANEL_START_*、assets/splash.html 的 PALETTE。
// 启动画面要在主界面的 JS 跑起来之前就出图，拿不到这份表，只能各自再抄一份。
const themeGradients = {
    blue: {
        light: { start: '#728db5', end: '#f4f5f7', primary: '#2f67bc' },
        dark: { start: '#141b24', end: '#090b0e', primary: '#84a5d6' }
    },
    green: {
        light: { start: '#72b593', end: '#f4f7f6', primary: '#218552' },
        dark: { start: '#14241c', end: '#090e0c', primary: '#6ece9d' }
    },
    cyan: {
        light: { start: '#72b0b5', end: '#f4f7f7', primary: '#2a9fa9' },
        dark: { start: '#142224', end: '#090e0e', primary: '#6ec7ce' }
    },
    teal: {
        light: { start: '#72b5b0', end: '#f4f7f7', primary: '#1e7b72' },
        dark: { start: '#142422', end: '#090e0e', primary: '#74c9c1' }
    },
    purple: {
        light: { start: '#7c72b5', end: '#f5f4f7', primary: '#422fbc' },
        dark: { start: '#171424', end: '#0a090e', primary: '#9085d6' }
    },
    pink: {
        light: { start: '#b57297', end: '#f7f4f6', primary: '#bc2f7c' },
        dark: { start: '#24141d', end: '#0e090c', primary: '#d98db6' }
    },
    red: {
        light: { start: '#b57380', end: '#f7f4f5', primary: '#bb304c' },
        dark: { start: '#241418', end: '#0e090a', primary: '#da909f' }
    },
    orange: {
        light: { start: '#b58f72', end: '#f7f6f4', primary: '#bc6c2f' },
        dark: { start: '#241b14', end: '#0e0b09', primary: '#ce986e' }
    },
    amber: {
        light: { start: '#b59772', end: '#f7f6f4', primary: '#a36b29' },
        dark: { start: '#241d14', end: '#0e0c09', primary: '#cea26e' }
    },
    slate: {
        light: { start: '#8090a8', end: '#f5f6f7', primary: '#3c5981' },
        dark: { start: '#181b20', end: '#0a0b0d', primary: '#94a3b8' }
    }
};

/**
 * 主题名：取文案要等到 get_all_text 回来，所以这几张表都写成函数、用的时候才取。
 *
 * 顶层 const 里直接调 uiText() 会在 common.js 还没跑完时就执行，
 * 那时 currentTextDict 还在暂时死区里，整份脚本直接 abort（后面全部未定义）。
 */
function themeLabels() {
    return {
        blue: uiText('theme_blue', '蓝色'), green: uiText('theme_green', '绿色'),
        cyan: uiText('dyn_1306', '青色'), teal: uiText('dyn_1307', '蓝绿'),
        purple: uiText('theme_purple', '紫色'),
        pink: uiText('dyn_1309', '粉红'), red: uiText('dyn_1310', '红色'),
        orange: uiText('theme_orange', '橙色'), amber: uiText('dyn_1312', '琥珀'),
        slate: uiText('dyn_1313', '灰蓝')
    };
}

// 色板顺序即设置页/向导里的显示顺序，蓝色固定第一（默认主题）
const THEME_ORDER = ['blue', 'green', 'cyan', 'teal', 'purple', 'pink', 'red', 'orange', 'amber', 'slate'];

function themeOptions() {
    const labels = themeLabels();
    return THEME_ORDER.map(value => ({
        value: value,
        label: labels[value],
        color: themeGradients[value].light.primary
    }));
}

/**
 * 按当前语言重画主题色板。
 *
 * 两处色板各归各的窗口建（设置窗口 renderThemeSwatches、向导 wizardRenderThemes），
 * 这里只负责「建出来过的才重画」：向导还没打开时 #wizard-themes 是空的，
 * 凭空填一份会把「用户还没选过」这个状态伪造成「选了蓝色」。
 * 重画时保住当前选中的那一档，不然换一次语言就把用户挑的主题顶回蓝色。
 */
function rerenderThemeSwatches() {
    if (typeof renderThemeSwatches === 'function') {
        const active = document.querySelector('#theme-swatches .swatch.active');
        renderThemeSwatches(active ? active.dataset.value : 'blue');
    }
    if (typeof wizardRenderThemes === 'function' &&
            document.querySelector('#wizard-themes .swatch')) {
        wizardRenderThemes(wizardTheme());
    }
}

// 缩略图清晰度与草稿策略的可选值：主界面出图/控件和设置窗口下拉框共用同一串，
// 两份列表各写各的就会出现「设置里存了个主界面不认的档位」。
// 值和默认值都必须和后端 core/drafts.py 的策略常量对得上。
const THUMB_DPI_OPTIONS = [64, 96, 128];
const THUMB_DPI_DEFAULT = 64;
const DRAFT_POLICY_OPTIONS = ['keep_recent', 'keep_current', 'off'];
const DRAFT_POLICY_DEFAULT = 'keep_recent';

let currentThemeColor = 'blue';
let currentTextDict = {};

/**
 * 取当前语言的文案并刷到本窗口。
 *
 * 放在 common.js 而不是 app.js：设置窗口和主窗口都要它，两边必须走同一份逻辑，
 * 否则会出现「主界面是英文、设置窗口还是中文」这种各改一半的样子。
 * 找不到键就留着 HTML 里的原文（简体中文），不会把界面刷成空白。
 */
/**
 * 把一句话写成 CSS 的 content 值。JSON.stringify 顺带把引号转义好，
 * 文案里出现「"」也不会把 CSS 语法捅穿。
 */
function setCssText(name, text) {
    document.documentElement.style.setProperty(name, JSON.stringify(String(text || '')));
}

/**
 * 动态写进页面的一句话：顺手记下它的键，切完语言才重画得动。
 *
 * 只在调用那一刻取字典是不够的——那一格已经画好在屏上时用户才去改语言，
 * DOM 上留的还是上一次语言的那句。登记进来以后，applyLanguage() 会按键重画一遍。
 * 节点从文档上摘掉就顺手清掉，这张表不会越攒越长。
 */
const DYNAMIC_TEXT = new Map();

function setDynamicText(el, key, fallback) {
    if (!el) return el;
    el.textContent = uiText(key, fallback);
    DYNAMIC_TEXT.set(el, [key, fallback]);
    return el;
}

function repaintDynamicText() {
    DYNAMIC_TEXT.forEach(function (entry, el) {
        if (!el.isConnected) { DYNAMIC_TEXT.delete(el); return; }
        el.textContent = uiText(entry[0], entry[1]);
    });
}

async function applyLanguage(preloaded) {
    // preloaded：广播那一趟已经带回来的文案包；有它就不必再回后端取一次
    const dict = preloaded || await callApi('get_all_text');
    if (!dict || typeof dict !== 'object') return currentTextDict;
    currentTextDict = dict;
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (dict[key] !== undefined) el.textContent = dict[key];
    });
    // 悬浮提示走另一个属性：按钮上的字（1:1、‹、▶|）本来就不该被文案换掉，
    // 但鼠标停上去那句话得跟着语言走
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        const key = el.getAttribute('data-i18n-title');
        if (dict[key] !== undefined) el.title = dict[key];
    });
    // 输入框里的灰字和读屏用的名字同理：这两处用户看得见（或听得见），
    // 留在中文就等于英文界面里夹一句中文，比标题没翻还刺眼
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        if (dict[key] !== undefined) el.placeholder = dict[key];
    });
    document.querySelectorAll('[data-i18n-aria]').forEach(el => {
        const key = el.getAttribute('data-i18n-aria');
        if (dict[key] !== undefined) el.setAttribute('aria-label', dict[key]);
    });
    // 输入框里**预填**的那句默认文字（水印的「机密」）：英文界面留着它就是夹一句中文。
    // 只跟着语言换「用户还没动过」的那些——已经自己填过内容的人切语言被覆盖掉，比不翻更糟。
    document.querySelectorAll('[data-i18n-value]').forEach(el => {
        const key = el.getAttribute('data-i18n-value');
        if (dict[key] === undefined) return;
        const last = el.dataset.i18nValueLast || el.defaultValue;
        if (el.value === '' || el.value === last) {
            el.value = dict[key];
            el.dataset.i18nValueLast = dict[key];
        }
    });
    // 已经拼进 DOM 的动态内容不在 data-i18n 覆盖范围内，得点名重画。
    // 首页「最近文件」那一栏就是：空态那句话是 loadRecentFiles() 写进去的，
    // 不重画的话切完语言那一行还留着中文。
    if (typeof loadRecentFiles === 'function' && document.getElementById('recent-files')) {
        loadRecentFiles();
    }
    // 字体下拉里那串选项是 JS 现建的（默认档那句和「含中文」标注都不挂 data-i18n，
    // option 由 fillFontSelect 重建），切语言不重画的话整屏换了字、下拉还留着上一次的语言。
    fillFontSelects();
    // 空态那两句话写在 CSS 的 content 里（`.merge-list:empty::before`、`.pm-grid:empty::before`），
    // 既不挂 data-i18n 也不经 JS，切语言一个字都不动——所以文案放 CSS 变量里，由这里写进去。
    setCssText('--empty-merge-hint', dict.merge_list_empty);
    setCssText('--empty-pm-hint', dict.pm_grid_empty);
    // 「本软件支持的 LaTeX 范围」那一栏是 JS 现拼的清单（内容取自后端，不在 data-i18n 覆盖范围里）：
    // 换完语言必须作废重取，否则整屏都换了、那一栏还留着上一次语言的几句。
    if (typeof latexCapsInvalidate === 'function') latexCapsInvalidate();
    // 各格预览区里那句「请先选择…」是 JS 现写的：那一格已经画好在屏上时切语言，
    // DOM 上留的还是上一次语言的句子（他 2026-10-31 圈的英文界面夹着中文就是这个）。
    repaintDynamicText();
    // 主题色板上那十个名字同样是 JS 现建的，切完语言不重画就是
    // 「整屏都换了、色板还留着上一次语言的ブルー」——设置窗口和向导两处都会撞上。
    rerenderThemeSwatches();
    // 浏览器/WebView2 的文档语言也跟着改：中文的「字体栈」和拉丁的换行规则不一样
    document.documentElement.lang = dict.app_locale || 'zh-CN';
    return currentTextDict;
}

// ==================== 本机字体下拉（三处可编辑文字共用）====================
// 后端 api/fonts.py 的 list_fonts 给族名清单（本机实测 344 族 / 610 样式）；
// 冷扫描要 3 秒上下，绝不能压在开窗口那一下——app.js 的 init() 里延迟排一次 fontsWarmup()，
// 清单回来前四个下拉都只有「软件内置（默认）」一项，回来后再填。选字体写进 PDF 的口径
// （族名 / 族名|样式 / 路径）见 core/fonts.py；读不出来后端会退回内置字体并把
// api_font_fallback_default 那句话回传（res.message），各界面必须显示、不许静默。
let fontFamilies = null;

function fontSelectLabels() {
    // 用之前先把可能迟到的字典取上：uiText 在字典没回来前会用中文兜底，不至于空串
    return {
        def: uiText('tool_font_default', '软件内置（默认）'),
        cjk: uiText('tool_font_cjk_mark', '含中文'),
    };
}

/** 填一个字体下拉：默认档在最上，其余按族名列出，含中文的族在后面标注一句。 */
function fillFontSelect(sel) {
    if (!sel) return;
    const keep = sel.value;
    const labels = fontSelectLabels();
    sel.innerHTML = '';
    const def = document.createElement('option');
    def.value = '';
    def.textContent = labels.def;
    sel.appendChild(def);
    (fontFamilies || []).forEach(fam => {
        const opt = document.createElement('option');
        // 传给后端的是英文族名（大小写不敏感、不受显示语言影响），显示用后端给的首选名
        opt.value = fam.english_name || fam.family;
        let text = fam.family || opt.value;
        if (fam.english_name && fam.english_name !== fam.family) text += ' (' + fam.english_name + ')';
        if (fam.has_cjk) text += ' · ' + labels.cjk;
        opt.textContent = text;
        sel.appendChild(opt);
    });
    // 重建（第一次填表 / 切语言）都不许把用户挑的那一档顶回默认
    sel.value = keep;
    if (sel.value !== keep) sel.value = '';
}

/** 填所有挂在 select.font-select 上的下拉（水印 / 页码 / 页眉页脚 / TXT→PDF 四处）。 */
function fillFontSelects() {
    document.querySelectorAll('select.font-select').forEach(fillFontSelect);
}

/** 启动后异步预热一次字体清单；已有缓存就直接复用、不再问后端。 */
function fontsWarmup() {
    if (fontFamilies) { fillFontSelects(); return Promise.resolve(fontFamilies); }
    return callApi('list_fonts').then(res => {
        if (res && res.success) {
            fontFamilies = res.families || [];
            fillFontSelects();
        }
        return fontFamilies;
    }).catch(() => null);
}
window.fontsWarmup = fontsWarmup;
window.fillFontSelects = fillFontSelects;

/**
 * 取当前语言的一句话，取不到就用传进去的简体中文原文兜底。
 *
 * 放在 common.js：主窗口、设置窗口和向导都要用，而 tools.js 里那个同类的 t() 是
 * 在 app.js 之后才加载的，启动阶段调它会 undefined。
 */
function uiText(key, fallback) {
    return (currentTextDict && currentTextDict[key]) || fallback;
}

/**
 * 带占位符的一句：模板里的 {0}、{1} 按顺序换成后面的实参。
 *
 * 拼接句一律走这里而不是在 JS 里做加法——中文的「第 3 页（共 10 页）」，
 * 英语是 "Page 3 of 10"，语序不一样，把句子劈成片段拼出来的一定是病句。
 * 占位符用 {n} 不用 %s，免得文案里本来就有百分号（「占比 45%」）被吃掉。
 */
function uiTextf(key, fallback) {
    const args = Array.prototype.slice.call(arguments, 2);
    const tpl = (currentTextDict && currentTextDict[key]) || fallback;
    return String(tpl).replace(/\{(\d+)\}/g, function (m, i) {
        const v = args[Number(i)];
        return v === undefined || v === null ? m : String(v);
    });
}

/** 语言代码 → 界面上的名字（含「跟随系统」这一档）。 */
function languageLabel(info, code) {
    const hit = (info.available || []).find(a => a.code === code);
    return hit ? hit.name : code;
}
let loadingTimer = null;

// ==================== 顶部加载条 ====================
function showLoading() {
    const bar = document.getElementById('loading-bar');
    if (bar) bar.classList.add('show');
}

function hideLoading() {
    const bar = document.getElementById('loading-bar');
    if (bar) bar.classList.remove('show');
    if (loadingTimer) { clearTimeout(loadingTimer); loadingTimer = null; }
}

// ==================== 长任务进度与取消 ====================
// 整本 OCR、可搜索 PDF、扫描质检、拍照增强这些在后端都是「一页一页的循环」，
// 每做完一页把 done/total 写进 core/jobctl.py；这里每 600ms 读一次画到右下角那条上。
//
// 为什么是前端轮询而不是后端推进来：pywebview 的 js_api 只有「前端问、后端答」这一条路，
// 那个循环跑在工作线程里，手里没有窗口引用，推不出来。600ms 读一个小 dict 的代价
// 比另开一条通道（还要处理线程与窗口生命周期）低得多。
let jobTimer = null;
let jobAsking = false;
let jobIdle = 0;
// 后端那一笔请求结束前，进度条不能因为「这一跳没读到任务」就消失：
// 点下按钮到 jobctl.start() 生效之间有几十毫秒的空档，起点上误判一次就把条收走了。
const JOB_IDLE_LIMIT = 4;

function jobShow(on) {
    const bar = document.getElementById('job-bar');
    if (bar) bar.classList.toggle('hidden', !on);
}

function jobPaint(st) {
    if (!st || !st.active) {
        jobIdle++;
        if (jobIdle >= JOB_IDLE_LIMIT) jobStop();
        return;
    }
    jobIdle = 0;
    jobShow(true);
    const total = Math.max(1, Number(st.total) || 1);
    const done = Math.min(total, Math.max(0, Number(st.done) || 0));
    const byFile = st.kind === 'file';
    const text = document.getElementById('job-text');
    if (text) {
        // done 是「已经做完几页」，此刻正在处理的是下一页；全部做完后别再指到第 11 页。
        // 两种单位各写一句完整的字典话：把「页/张」拆成片段拼，换成长短不一的语言就是病句。
        const current = Math.min(done + 1, total);
        text.innerText = byFile
            ? uiTextf('job_progress_file', '第 {0} / {1} 张', current, total)
            : uiTextf('job_progress_page', '第 {0} / {1} 页', current, total);
    }
    const fill = document.getElementById('job-fill');
    if (fill) fill.style.width = (Number(st.percent) || 0) + '%';
    const btn = document.getElementById('job-cancel');
    if (btn) {
        btn.disabled = !!st.cancelling;
        btn.innerText = st.cancelling
            ? uiText('job_cancelling', '取消中…')
            : uiText('job_cancel_btn', '取消');
    }
}

/** 开始盯着某个长任务；已经在盯了就是重置空闲计数，不会叠出第二个定时器。 */
function jobWatch() {
    jobIdle = 0;
    if (jobTimer) return;
    jobTimer = setInterval(async () => {
        if (jobAsking) return;      // 上一笔还没回来就跳过这一跳，别把请求堆起来
        jobAsking = true;
        try {
            const st = await callApi('job_status');
            // 回包迟到了就别画：这一问正在路上时任务刚好结束，请求落地的 finally 已经把条
            // 收掉、定时器也清了；再拿这一帧 active=true 画回去，条就一直挂着没人负责收
            //（下一次长任务才会把它带走）。判据就一个：定时器还在才说明还有人盯着。
            if (jobTimer) jobPaint(st);
        } finally {
            jobAsking = false;
        }
    }, 600);
}

/** 请求结束（成功、失败、取消都算）：收掉定时器并把条藏起来。 */
function jobStop() {
    if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
    jobAsking = false;
    jobIdle = 0;
    jobShow(false);
}

async function cancelJob() {
    const btn = document.getElementById('job-cancel');
    if (btn) btn.disabled = true;
    await callApi('job_cancel');
    jobPaint(await callApi('job_status'));
    showToast(uiText('job_cancel_hint', '已请求取消，做完当前这一项就停'), 3000);
}

// ==================== API 调用 ====================
// 同时有几个请求在飞的时候（缩略图一批 + 配置一次 + 日志一次是常事），这根条不能
// 按「谁最后结束」算：原来每一笔各自往同一个 loadingTimer 上挂、又在 finally 里
// 一起清掉，结果先回来的那笔把别人还没到期的定时器也清了、顺手把条收起——
// 表现就是「明明还在算，顶部那条已经没了」，或者反过来「早该没了还挂着」。
// 现在只数在飞的笔数：第一笔起飞才起计时器，最后一笔落地才收条。
let apiInFlight = 0;

// 后端会往 core/jobctl.py 里登记进度的那几笔。写在这一处而不是每个调用点各包一层，
// 是为了「以后再加一个慢功能，只要把名字登记进来」，不用回头改十几个函数。
const LONG_JOBS = new Set([
    'ocr_pdf_text', 'ocr_export_txt', 'ocr_export_docx', 'ocr_export_xlsx',
    'ocr_searchable_pdf', 'ocr_sensitive_info', 'ocr_scan_report', 'ocr_auto_bookmarks',
    'enhance_pdf', 'enhance_images',
    // 翻译整本是慢活：逐页取原文 → 送 provider → 按 span 写回，进度与取消都靠这条登记
    'translate_pdf_to_pdf', 'export_translation_pair',
    // 骑缝印是逐页写 + 每页读回核对，整本下来是慢活，要进度条与取消
    'apply_cross_page_seal',
    // LaTeX 两头都是逐页的活：.tex 排版成 PDF 一段一段落笔，PDF 导 .tex 一页一页解析。
    // 后端按页往 jobctl 里写进度（api/latex.py、api/text_export.py），这里登记才有进度条和取消。
    'latex_to_pdf', 'export_latex',
]);

async function callApi(name, ...args) {
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api[name]) {
        showToast(uiTextf('dyn_1314', '接口未就绪: {0}', name), 3000);
        return { success: false, message: 'API not ready' };
    }
    apiInFlight++;
    if (apiInFlight === 1) loadingTimer = setTimeout(showLoading, 150);
    const isJob = LONG_JOBS.has(name);
    if (isJob) jobWatch();
    try {
        return await window.pywebview.api[name](...args);
    } catch (e) {
        console.error(`API 调用失败 [${name}]:`, e);
        const msg = (e && (e.message || e.toString())) || uiText('dyn_1096', '未知错误');
        showToast(uiTextf('dyn_1316', '操作失败: {0}', msg), 4000);
        return { success: false, message: msg };
    } finally {
        // 请求一落地就收条：进度停在哪儿不要紧，「还在转」才是问题
        if (isJob) jobStop();
        apiInFlight--;
        if (apiInFlight <= 0) {
            apiInFlight = 0;
            if (loadingTimer) { clearTimeout(loadingTimer); loadingTimer = null; }
            hideLoading();
        }
    }
}

// ==================== 轻提示 ====================
let toastTimer = null;

function showToast(message, duration = 2400) {
    const toast = document.getElementById('toast');
    if (!toast) {
        console.log('[toast]', message);
        return;
    }
    toast.innerText = message;
    toast.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), duration);
}

// 后端失败时返回 {success:false, message:...} 而不是抛异常，callApi 不会弹提示。
// 所有调用点统一走这里，避免「点了没反应」。
function reportFailure(res, statusEl, fallback) {
    const msg = (res && res.message) ? res.message : (fallback || uiText('dyn_1317', '操作失败'));
    if (statusEl) statusEl.innerText = '✗ ' + msg;
    showToast(msg, 4000);
    return msg;
}

// ==================== 主题 ====================

/**
 * #rrggbb 拆成 HSL 三档，供 --hue/--sat/--light 用。
 *
 * 描边、浅底、hover 色这些派生态在 style.css 里是 calc(var(--light) - 7%) 这样算出来的，
 * 而自定义属性的 var() 在声明它的那一层就替换完了：派生令牌声明在 body 上，
 * 所以主题必须把 hue/sat/light 也写到 body，只写 --primary 的话边框会永远停在默认蓝。
 */
function hexToHslParts(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return null;
    const n = parseInt(m[1], 16);
    const r = ((n >> 16) & 255) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const l = (max + min) / 2;
    let h = 0, s = 0;
    if (max !== min) {
        const d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
        else if (max === g) h = (b - r) / d + 2;
        else h = (r - g) / d + 4;
    }
    return { h: Math.round(h * 60), s: Math.round(s * 100), l: Math.round(l * 100) };
}

function applyThemeColor(color) {
    currentThemeColor = themeGradients[color] ? color : 'blue';
    const theme = themeGradients[currentThemeColor];
    const isDark = document.body.classList.contains('dark');
    const palette = isDark ? theme.dark : theme.light;
    const body = document.body.style;
    body.setProperty('--bg-start', palette.start);
    body.setProperty('--bg-end', palette.end);
    body.setProperty('--primary', palette.primary);
    const parts = hexToHslParts(palette.primary);
    if (parts) {
        body.setProperty('--hue', String(parts.h));
        body.setProperty('--sat', parts.s + '%');
        body.setProperty('--light', parts.l + '%');
    }
}

function toggleDarkModeClass(isDark) {
    document.body.classList.toggle('dark', !!isDark);
}

/**
 * 后端在窗口露出来之前推来的一次性状态：±5 度的色相偏移、按时段定的冷暖，和一句问候。
 * 两个数只喂装饰层那两段渐变（--decor-a / --decor-b），主色、卡片描边、按钮一律不动，
 * 所以同一台机器开两次不会完全一样，又不会让人觉得「颜色怎么变了」。
 * 窗口刚 loaded 时页面已经就绪，这里不必再排队；推不到就当没有，界面保持默认。
 */
function applyLaunchMood(mood) {
    if (!mood || typeof mood !== 'object' || !document.body) return;
    const clamp = (v, lo, hi) => {
        const n = Number(v);
        return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : 0;
    };
    const body = document.body.style;
    // setProperty 只收字符串，数字在这里会静默不生效
    body.setProperty('--hue-offset', String(clamp(mood.hueOffset, -12, 12)));
    body.setProperty('--decor-warmth', String(clamp(mood.warmth, -12, 12)));
    const greet = document.getElementById('welcome-greet');
    if (!greet) return;
    // 后端给的是键：这样切语言时这一句能跟着重画，深夜没有问候就清空。
    if (mood.greetingKey) setDynamicText(greet, mood.greetingKey, '');
    else { DYNAMIC_TEXT.delete(greet); greet.textContent = ''; }
}
window.applyLaunchMood = applyLaunchMood;

/** 功能说明开关：默认开，只有明确存成 false 才收起那些小字说明。 */
function toggleFeatureNotesClass(show) {
    document.body.classList.toggle('hide-feature-notes', show === false);
}

/**
 * 导航栏收起状态：默认展开，配置里明确存 true 才收成图标条。
 *
 * 把手上的箭头和提示语在这里一起改，收起键（app.js 的 toggleSidebar）和开机还原
 * （applyStoredSettings）走同一个入口，两边就不会各写一半、跑出「图标是 › 但提示写着展开」。
 * 设置窗口没有导航栏，那里调用只会改 body 的类名，不伤别的窗口。
 */
function applySidebarCollapsed(collapsed) {
    const on = collapsed === true;
    document.body.classList.toggle('sidebar-collapsed', on);
    // 栏被点开展开了，悬停那层临时态就没有意义了，留着会一直压着内容
    if (!on) document.body.classList.remove('sidebar-hover');
    const btn = document.getElementById('nav-collapse');
    if (!btn) return;
    const tip = on ? uiText('dyn_1434', '展开导航栏') : uiText('dyn_1318', '收起导航栏');
    btn.textContent = on ? '›' : '‹';
    btn.title = tip;
    btn.setAttribute('aria-label', tip);
    // 键盘焦点落在把手上时，读屏软件念的是 aria-expanded，光改提示语不算说完
    btn.setAttribute('aria-expanded', on ? 'false' : 'true');
    // 只剩图标时得让人认得出哪个是哪个，把这一项的文字名挂成原生悬浮提示（WPS 那条图标栏也是这么给的）；
    // 展开态文字就印在旁边，提示反而挡事，所以回去的时候一并清掉。
    document.querySelectorAll('.nav-btn').forEach(nav => {
        const label = nav.querySelector('.nav-label');
        if (!label) return;
        if (on) nav.title = label.textContent.trim();
        else nav.removeAttribute('title');
    });
}

/**
 * 「悬停临时展开」这一项的开关：只有 body 上有 .sidebar-hoverable，
 * app.js 绑的那对 mouseenter/mouseleave 才允许给 body 加 .sidebar-hover。
 * 默认开（配置里明确存 false 才关），关掉之后行为就退回「只有把手」。
 */
function applySidebarHoverExpand(on) {
    const allow = on !== false;
    document.body.classList.toggle('sidebar-hoverable', allow);
    if (!allow) document.body.classList.remove('sidebar-hover');
}

/** 读取配置并应用主题；返回配置对象，失败返回 null。 */
async function applyStoredSettings(bundle) {
    // bundle：设置窗口保存后一起广播过来的那一包（{config, texts, ...}）；
    // 没有就照旧自己去取，别的调用方（启动时那一次）不受影响
    const config = (bundle && bundle.config) || await callApi('load_config');
    if (!config || typeof config !== 'object') return null;
    toggleDarkModeClass(config.dark_mode);
    applyThemeColor(config.theme_color || 'blue');
    toggleFeatureNotesClass(config.show_feature_notes);
    const collapsedBefore = document.body.classList.contains('sidebar-collapsed');
    applySidebarCollapsed(config.sidebar_collapsed);
    applySidebarHoverExpand(config.sidebar_hover_expand);
    // 栏宽是 .24s 的动画过去的，而这一条路径（设置里恢复默认、或别的窗口改了配置）
    // 不像点把手那样自己补算：不补的话预览图与查看页版面停在旧操作区宽度上。
    if (collapsedBefore !== (config.sidebar_collapsed === true) &&
            typeof sidebarResizeSettled === 'function') {
        setTimeout(sidebarResizeSettled, 280);
    }
    // 首页置顶顺序也存在配置里，得跟着这次广播重排一次：
    // 只在 init() 里读一遍的话，「恢复默认」把配置清空了，卡片却还停在旧顺序上，
    // 用户看到的就是「这一项没给我恢复」。设置窗口没有首页格子，函数不在就跳过。
    if (typeof homeLoadPinned === 'function') homeLoadPinned(config);
    // 语言也走这条广播：设置窗口改完点「应用」，主窗口和设置窗口一起换字
    // 广播里带着文案包就直接用，不再为这一句回后端取整包文案
    await applyLanguage(bundle && bundle.texts);
    return config;
}
// 设置窗口保存后会广播，各窗口重新应用
window.applyStoredSettings = applyStoredSettings;

// ==================== 版本显示 ====================
let appInfoCache = null;

/** 程序元信息（名称、版本、作者、更新源），只取一次。 */
async function getAppInfo() {
    if (!appInfoCache) {
        const info = await callApi('get_app_info');
        // 失败时 callApi 给的是兜底对象，不能把它当成「版本号为空」缓存起来，
        // 否则向导这一轮再也拿不到版本号。
        if (info && typeof info === 'object' && info.version) appInfoCache = info;
    }
    return appInfoCache || {};
}

async function applyVersion() {
    let version = '';
    try {
        version = await callApi('get_version');
    } catch (e) {
        console.error('获取版本号失败:', e);
    }
    if (typeof version !== 'string') version = '';
    if (!version) {
        // pywebview 的 api 是页面跑起来之后才注入的，DOMContentLoaded 那一枪有可能取到
        // 兜底对象而不是版本字符串；标题里那串版本号就这么丢了（窗口标题退成「PDF Toolkit」）。
        // 等一小段再要一次，取不到也不报错——标题停在静态文案上不算坏，只是少个版本号。
        await new Promise(r => setTimeout(r, 300));
        try {
            const again = await callApi('get_version');
            if (typeof again === 'string') version = again;
        } catch (e) {
            console.error('再取版本号失败:', e);
        }
    }
    if (version) document.title = uiTextf('dyn_1319', 'PDF Toolkit {0}', version);
    return version;
}

// ==================== 设置窗口 ====================
async function openSettings() {
    const res = await callApi('open_settings_window');
    if (!res || !res.success) {
        reportFailure(res, null, uiText('dyn_1320', '无法打开设置窗口'));
    }
}

// ==================== 通用弹窗 ====================
// 两个窗口共用一套弹窗：HTML 里放 #message-modal 即可。
function showMessageDialog(title, text, actions) {
    const mask = document.getElementById('message-modal');
    if (!mask) {
        showToast(text || title, 5000);
        return;
    }
    document.getElementById('message-modal-title').innerText = title || '';
    document.getElementById('message-modal-text').innerText = text || '';

    const bar = document.getElementById('message-modal-actions');
    bar.innerHTML = '';
    (actions && actions.length ? actions : [{ label: uiText('dyn_1321', '知道了'), primary: true }]).forEach(action => {
        const btn = document.createElement('button');
        btn.className = 'btn' + (action.primary ? '' : ' ghost');
        btn.innerText = action.label;
        btn.onclick = () => {
            closeMessageDialog();
            if (typeof action.onClick === 'function') action.onClick();
        };
        bar.appendChild(btn);
    });

    mask.classList.remove('hidden');
}

function closeMessageDialog() {
    const mask = document.getElementById('message-modal');
    if (mask) mask.classList.add('hidden');
}

/** 新版本弹窗里的下载按钮目标地址（version.json 的 download_url）。 */
let updateDownloadUrl = '';

function closeUpdateModal() {
    const mask = document.getElementById('update-modal');
    if (mask) mask.classList.add('hidden');
}

async function downloadUpdate() {
    closeUpdateModal();
    if (updateDownloadUrl) await callApi('open_url', updateDownloadUrl);
}

async function openUpdateReleasePage() {
    const info = await getAppInfo();
    const url = info.readme_page || 'https://github.com/Yunmo-Xuan/PDF-Tools-Update/blob/main/README.md';
    await callApi('open_url', url);
}

/** 取本次版本的条目；版本号对不上就用最新一条（README 的更新日志是新→旧排的）。 */
function pickReleaseEntry(res, version) {
    const list = (res && res.entries) || [];
    const want = String(version || '').replace(/^v/i, '').trim();
    return list.find(item => String(item.version).replace(/^v/i, '') === want) || list[0] || null;
}

function updateNotesMeta(res) {
    if (!res || !res.success) return '';
    const source = res.stale
        ? (res.bundled ? uiText('dyn_1435', '当前离线，显示随程序内置的更新日志') : uiText('dyn_1322', '当前离线，显示上次缓存的更新日志'))
        : (res.bundled ? uiText('dyn_1323', '随程序内置的更新日志')
            : (res.cached ? uiText('dyn_1436', '本地缓存的更新日志') : uiText('dyn_1324', '已从更新源获取最新日志')));
    const current = res.current_version ? uiTextf('dyn_1437', ' · 当前版本 v{0}', res.current_version) : '';
    return source + current;
}

/**
 * 「发现新版本」弹窗：正文是 README 里的更新日志，联网读取、渲染方式和许可协议一致。
 * 取不到正文也照样能弹（只有下载按钮可用），不能因为网络失败就不让用户知道有新版本。
 */
async function showUpdateDialog(version, url) {
    const mask = document.getElementById('update-modal');
    if (!mask) {
        showMessageDialog(uiText('update_available', '发现新版本'), uiTextf('dyn_1326', '新版本 v{0} 已发布，是否前往下载？', version), [
            { label: uiText('update_later', '稍后'), primary: false },
            { label: uiText('update_now', '立即下载'), primary: true, onClick: () => { if (url) callApi('open_url', url); } }
        ]);
        return;
    }

    updateDownloadUrl = url || '';
    const download = document.getElementById('update-download');
    if (download) download.disabled = !updateDownloadUrl;
    const notes = document.getElementById('update-modal-notes');
    const meta = document.getElementById('update-modal-meta');
    const title = document.getElementById('update-modal-title');
    const sub = document.getElementById('update-modal-sub');

    if (title) title.innerText = uiTextf('dyn_1329', 'PDF Toolkit {0} 已发布', version ? 'v' + version : uiText('dyn_1333', '新版本'));
    if (sub) sub.innerText = uiText('dyn_1330', '本次更新的内容如下，建议升级后继续使用');
    if (notes) notes.innerHTML = '<p>' + uiText('dyn_1331', '正在加载更新内容…') + '</p>';
    if (meta) meta.innerText = '';
    mask.classList.remove('hidden');

    // 第二个 false：弹窗要的是「即将发布的那一版」，它可能已经跨到大版本（6.x -> 7.0.0），
    // 按本机大版本过滤就会拿不到正文，所以这里不过滤。
    // 第一个必须 true：本地缓存是按本机当前版本同步的，里面还没有新版本那一条，
    // 走缓存就等于给用户看一份「还没写出来」的旧日志。
    const res = await callApi('get_release_notes', true, false);
    if (!res || !res.success) {
        if (notes) notes.innerHTML = '<p>' + uiText('dyn_1332', '暂时无法获取更新内容，可点击下方按钮在浏览器中查看。') + '</p>';
        if (meta) meta.innerText = (res && res.message) || '';
        return;
    }

    const entry = pickReleaseEntry(res, version);
    if (entry && entry.body) {
        if (notes) notes.innerHTML = `<h4>v${entry.version}</h4>` + renderMarkdown(entry.body);
    } else if (notes) {
        notes.innerHTML = renderMarkdown(res.text || '');
    }
    if (meta) meta.innerText = updateNotesMeta(res);
}
window.showUpdateDialog = showUpdateDialog;
window.closeMessageDialog = closeMessageDialog;
