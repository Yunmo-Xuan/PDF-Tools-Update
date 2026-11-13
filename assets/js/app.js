// ==================== 全局状态 ====================
// 主题、callApi、showToast、reportFailure、弹窗等公共部分在 js/common.js
let selectedFiles = {};
let mergeFilesList = [];
let stampImagePath = '';
let batchFilesList = [];
let stampImageElement = null;
let dragSrcIndex = -1;

// 页面管理状态
// pmSlots 是「待保存的这份文件长什么样」的唯一真相：数组顺序就是输出顺序，
// 缺席即删除，rot 是叠加在页面原有 /Rotate 上的增量角度。
let pmSlots = [];
let pmSlotSeq = 0;
let pmSelectedIds = new Set();
let pmHistory = [];
let pmDirty = false;
let pmDragSrcPos = -1;
// 键盘选两头的坐标：锚点是按下第一个方向键前那一格，Shift 推着走的是活动端（F23）
let pmAnchorPos = -1;
let pmActivePos = -1;
let pmRotCache = new Map();
let pmRotPending = new Set();
let pmGridBound = false;
let pmImagesCache = [];
let pmTotalPages = 0;
// 网格里现在摆的是哪份文件的页。合并到「排版与整理」之后，换文件时页面管理常常不在
// 前台、图就先不画；切回那一格要靠这个和 selectedFiles['pm'] 比一比，决定是补加载还是
// 只把已经在屏里的格子补画一次。
let pmLoadedPath = '';
let pmEverDrawn = new Set();
let pmRequestedBatches = new Set();
let pmPendingBatches = new Set();
// 屏幕（含预读边距）里还有哪几批格子，取图时只在这几批里挑；离中心多远不在这儿算
// （观察器只在相交状态变化那一帧回报，路过的那几批会留下一个永远刷不新的旧值，
//  真要排队得按当下格子的位置现算，见 pmNearestWanted）
let pmVisibleBatches = new Set();
// 一批 24 格里当前在屏里的那几格（含预读边距）；只有一格都不剩了才认为「这批离开了屏幕」
let pmVisibleCells = new Map();
// 真正落在视口里（不含预读边距）的那几格，按批分开数：清晰度档位只看这个——
// 预读用的批按标清出图，滚进屏幕才补成用户选的高档，否则高档会把内存跟着页数
// 一起抬上去（F25 / §4.12）。数法与 pmVisibleCells 一致：一整批全出去才算离开。
let pmViewportCells = new Map();
// 每一批当前「已经取到手的档位」：同批同档不重复请求，换了档才补一次
let pmBatchDpi = new Map();
let pmPumping = false;
// 流水线正忙时又收到的叫唤，先记一笔，忙完补跑一次（见 pmPump 的收尾）。
// 不记就会漏：滚动停下的那一帧观察器叫醒泵，泵还在等上一笔后端返回，直接 return；
// 之后观察器不再回报（相交状态没变化），谁也不会再叫醒它，落地那一屏就永远空着。
let pmPumpWake = false;
// 视野停住多久才去取图、以及一直滚时的最长等待（见 pmPumpSoon）
const PM_PUMP_QUIET_MS = 140;
const PM_PUMP_MAX_WAIT_MS = 700;
let pmPumpTimer = null;
let pmPumpSince = 0;
let pmThumbObserver = null;
let pmStatusNotice = '';
// ---------- 可取消渲染（F07）----------
// 代际令牌：每次装载或跳页都让它前进一次。取图请求发出前记住当时的值，
// 回来时对不上号就把整批作废——不写缓存、不重排 DOM，也不再占桥的带宽。
// 后端已经发出去的渲染掐不掉，所以「取消」取消的是接收与展示，不是 PyMuPDF。
let pmRequestGen = 0;
// 正在等后端返回的那一批（-1 表示没有在途）
let pmInflightBatch = -1;
// 下面三个是 F07 的观测量：验收口径「多余渲染次数为 0」核的是这批写进缓存的批数，
// 不是后端调用次数，所以两个都要留着才能证明取消真的生效了。
let pmRenderCalls = 0;
let pmAppliedBatches = 0;
let pmCancelledBatches = 0;
// ---------- 草稿（F08）----------
// 停手 0.8 秒才落一次盘：用户手不停就一笔都不写，避免边拖边写盘把 js 桥占满。
// 强杀进程时任何关窗钩子都不会跑，所以「没保存就崩了能不能接上」全押在这个防抖上。
let pmDraftTimer = null;
let pmDraftKey = '';
// 打开文件时查到的那份草稿（{restorable, draft, ...}），横幅上的「恢复/丢弃」都读它
let pmDraftCache = null;


// ==================== 页面切换 ====================
function showPage(name, event) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const target = document.getElementById('page-' + name);
    if (target) target.classList.add('active');
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    const btn = event && event.target ? event.target.closest('.nav-btn') : null;
    if (btn) btn.classList.add('active');
    // 从首页卡片进的那一页也要在侧边栏里亮起来：合并之后「页面管理」那张卡片进的是
    // 排版与整理，谁点进去都不该看到侧边栏一片空白（以前从卡片进就没人高亮）。
    else {
        const match = [...document.querySelectorAll('.nav-btn')]
            .find(b => (b.getAttribute('onclick') || '').indexOf("'" + name + "'") >= 0);
        if (match) match.classList.add('active');
    }
    if (name === 'home') loadRecentFiles();
    // 切走页面管理时浮层还停在旧坐标上，指着的已经不是刚才那一页
    pmCloseMenu();

    // 隐藏着的窗格量不到宽度，切回来要按真实宽度补算一次，否则页面图停在旧尺寸上
    if (previewState[name]) applyPreviewZoom(name);
    // 拆分与合并并成一页之后，页级那颗按钮的文案由当前那格决定（合并=添加文件、拆分=选择 PDF），
    // 而 HTML 上出厂写的是「选择 PDF 文件」、默认露出来的却是合并那格。进这一页先刷一次，
    // 不然用户看到「选择 PDF 文件」按下去，实际是往合并列表里加文件。
    if (name === 'splitmerge' && typeof splitMergeActionLabel === 'function') {
        splitMergeActionLabel(activeTask('splitmerge') || 'merge');
    }
    // 排版页那四格的预览窗和页面不同名（叫 number/header/crop/scale），上面那行管不到它们：
    // 在别的页上把导航栏收了再回来，「适应宽度」的图还停在旧窗宽上，所以补算一次。
    if (name === 'layout' && typeof layRefitPreviews === 'function') layRefitPreviews();
    // 查看页同理：这一页藏起来时量不到宽度，切回来先按真实宽度重排一遍版面再刷窗口
    if (name === 'viewer' && viewer) { viewerRelayout(); viewerUpdate(); }
}

// ==================== 首页常用功能置顶（F26）====================
// 顺序存进 config.json 的 pinned_pages，走既有的 save_config 通道，不新增桥方法。
// 只存页面 id：文案改了、以后加新页都不用迁移配置；读回来对不上的 id 直接忽略。
let pinnedPages = [];
// 出厂顺序：第一次渲染时抄下来。有了它，「置顶 → 取消」才回得去原来的样子，
// 而不是停在上一次重排的结果上——用户按第二下就是指望它复原的。
let homeCardOrder = [];

/** 按 pinned_pages 的顺序重排卡片：置顶的排到最前，没置顶的按出厂次序。 */
function renderPinnedCards() {
    const grid = document.getElementById('home-grid');
    if (!grid) return;
    const cards = Array.from(grid.children);
    const byId = new Map(cards.map(card => [card.dataset.card, card]));
    if (!homeCardOrder.length) homeCardOrder = cards.map(card => card.dataset.card);
    // 基准队列按出厂顺序取，DOM 里新冒出来的卡片（以后加的功能页）排在它后面
    const base = homeCardOrder.map(id => byId.get(id)).filter(Boolean);
    cards.forEach(card => { if (!base.includes(card)) base.push(card); });
    const order = [];
    pinnedPages.forEach(id => {
        const card = byId.get(id);
        if (card && !order.includes(card)) order.push(card);
    });
    base.forEach(card => { if (!order.includes(card)) order.push(card); });
    // 整批搬一次：这些卡片是静态 DOM，没有输入状态，重排不会丢东西
    const frag = document.createDocumentFragment();
    order.forEach(card => frag.appendChild(card));
    grid.appendChild(frag);
    cards.forEach(card => {
        const pinned = pinnedPages.includes(card.dataset.card);
        card.classList.toggle('pinned', pinned);
        const pin = card.querySelector('.card-pin');
        if (pin) {
            pin.title = pinned ? uiText('dyn_1418', '取消置顶') : uiText('home_pin_title', '置顶这个功能');
            pin.setAttribute('aria-label', pin.title);
        }
    });
}

/** 启动时从配置里读置顶顺序（配置已由 init 取到，不再多要一次桥）。 */
function homeLoadPinned(config) {
    const list = Array.isArray((config || {}).pinned_pages) ? config.pinned_pages : [];
    const grid = document.getElementById('home-grid');
    const known = new Set(grid ? Array.from(grid.children).map(card => card.dataset.card) : []);
    pinnedPages = list.map(String).filter(id => known.has(id));
    renderPinnedCards();
}

async function homeStorePinned() {
    const config = await callApi('load_config');
    if (!config || typeof config !== 'object') return false;
    config.pinned_pages = pinnedPages.slice();
    return await callApi('save_config', config);
}

/** 卡片右上角的图钉：点它只改置顶，不顺带跳进那一页。 */
async function homePinCard(event, id) {
    event.stopPropagation();
    const pos = pinnedPages.indexOf(id);
    if (pos >= 0) pinnedPages.splice(pos, 1);
    else pinnedPages.push(id);
    renderPinnedCards();
    const saved = await homeStorePinned();
    showToast(pos >= 0 ? uiText('dyn_1414', '已取消置顶') : uiText('dyn_1001', '已置顶到首页最前面'), 2200);
    if (!saved) showToast(uiText('dyn_1002', '置顶顺序写入配置失败，请查看日志'), 4000);
}

// ==================== 导航栏收起 ====================
/**
 * 点右沿那个把手：整条导航收成图标，操作区宽出来；状态存进配置，下次开还是这个样子。
 * 跟置顶顺序一样走 load_config → save_config，不新增桥方法。
 */
/** 栏宽落定之后按新宽度补算一次：「适应宽度」的预览图与查看页版面都绑在操作区宽度上。 */
function sidebarResizeSettled() {
    const active = document.querySelector('.page.active');
    const name = active ? active.id.replace('page-', '') : '';
    if (previewState[name]) applyPreviewZoom(name);
    // 栏窄了操作区就窄 152px，那几千格的占位全是按宽度算的
    if (name === 'viewer' && viewer) { viewerRelayout(); viewerUpdate(); }
    // 页面管理那张网的占位数同样跟着列宽走。动画途中那个观察者会反复重算，
    // 最后这一下才是「宽度真的停定了」之后的准数——收栏/展栏都走这一条。
    pmSyncCellRowHeight();
}

async function toggleSidebar() {
    const on = !document.body.classList.contains('sidebar-collapsed');
    applySidebarCollapsed(on);
    // 宽度是 .24s 的动画过去的，等它落定再按新宽度补算一次预览：
    // 「适应宽度」的印章/水印图停在旧尺寸上，收完栏看着就像图没跟着变宽。
    setTimeout(sidebarResizeSettled, 280);
    const config = await callApi('load_config');
    if (!config || typeof config !== 'object') return;
    config.sidebar_collapsed = on;
    if (!await callApi('save_config', config)) {
        showToast(uiText('dyn_1003', '导航栏状态写入配置失败，请查看日志'), 4000);
    }
}

/**
 * 图标条状态下的「悬停临时展开」：鼠标碰到侧边（含把手）就把栏展开，移开 160ms 后收回去。
 *
 * 三点要说清：① 只在「已收成图标条」且设置里开着这一项时生效，展开态点哪儿都不该有额外动作；
 * ② 展开这一下**和操作区一起让位**（2026-10-27 改的口径：浮在内容上会盖住卡片左沿），
 * 所以进/出都要像 toggleSidebar 那样等宽度落定补算一次预览与查看页版面，不然图停在旧窗宽上；
 * ③ 移开给一点宽限，因为把手压在栏的右沿上，鼠标从栏爬到把手的那一两像素会被判成「离开」，
 * 收回去就没法点固定了（和查看页那条浮条同一个坑）。
 */
let sidebarHoverTimer = 0;
let sidebarSettleTimer = 0;

function sidebarHoverAllowed() {
    const b = document.body.classList;
    return b.contains('sidebar-hoverable') && b.contains('sidebar-collapsed');
}

function bindSidebarHover() {
    const rail = document.querySelector('.sidebar');
    const handle = document.getElementById('nav-collapse');
    if (!rail || rail.__hoverBound) return;
    rail.__hoverBound = true;
    // 鼠标在栏与把手之间来回爬时，宽度会连着切好几次：每次都补算一遍太吵，
    // 只认最后那一下，且等 .24s 的过渡走完再算。
    const settle = () => {
        clearTimeout(sidebarSettleTimer);
        sidebarSettleTimer = setTimeout(sidebarResizeSettled, 280);
    };
    const enter = () => {
        clearTimeout(sidebarHoverTimer);
        if (sidebarHoverAllowed() && !document.body.classList.contains('sidebar-hover')) {
            document.body.classList.add('sidebar-hover');
            settle();
        }
    };
    const leave = () => {
        clearTimeout(sidebarHoverTimer);
        sidebarHoverTimer = setTimeout(() => {
            if (!document.body.classList.contains('sidebar-hover')) return;
            document.body.classList.remove('sidebar-hover');
            settle();
        }, 160);
    };
    [rail, handle].forEach(el => {
        if (!el) return;
        el.addEventListener('mouseenter', enter);
        el.addEventListener('mouseleave', leave);
    });
}

// ==================== 文件选择 ====================
// 状态栏 id 与 type 并非简单拼接（水印页是 wm-status，不是 watermark-status）
const STATUS_IDS = {
    viewer: 'viewer-status',
    pm: 'pm-status',
    stamp: 'stamp-status',
    watermark: 'wm-status',
    split: 'split-status',
    compress: 'compress-status',
    encrypt: 'encrypt-status',
    convert: 'convert-status',
    metadata: 'metadata-status'
};

// 合并页里「标签之间共用的文件槽位」：左边那条 key 是页级「选择 PDF」按钮写进去的，
// 右边那条是老功能代码一直认的那条。页面管理那一整套（暂存/撤销/草稿）和加密解密都还
// 在读自己原来那条 key，验收脚本也按这些老名字赋值，所以这里两个 key 一起写，
// 不去动那套逻辑。
const SHARED_SLOTS = {layout: 'pm', pm: 'layout', security: 'encrypt', encrypt: 'security'};

/** 这一页现在显示的是哪件工具（顶端任务标签）。 */
function activeTask(page) {
    const bar = document.querySelector('#page-' + page + ' .taskbar');
    const tab = bar && bar.querySelector('.task-tab.active');
    return tab ? String(tab.dataset.pane || '').replace(page + '-', '') : '';
}

async function selectPdf(type) {
    // 页面管理并进「排版与整理」之后，页级那个「选择 PDF」按钮选的也是页面管理正在用的
    // 那份文件：暂存的改动还在内存里，换文件就等于丢掉它们，所以照旧先问一句。
    if ((type === 'pm' || type === 'layout') && pmDirty && !await pmConfirmDiscard()) return;
    const path = await callApi('open_file_dialog');
    if (!path || typeof path !== 'string') return;
    selectedFiles[type] = path;
    const twin = SHARED_SLOTS[type];
    if (twin) selectedFiles[twin] = path;
    const statusEl = document.getElementById(STATUS_IDS[type] || (type + '-status'));
    if (statusEl) statusEl.innerText = '✓ ' + path;
    await callApi('add_recent_file', path);
    if (type === 'viewer') await loadViewerPreview(path);
    // 页面管理不在前台时先不画图：三千页的册子一进来就整表重建，而用户点的是裁剪那一格，
    // 白等一趟。切到页面管理标签时再补加载，见 pmTaskShown。
    if ((type === 'pm' || type === 'layout') && activeTask('layout') === 'pm') await loadPMPreview(path);
    if (type === 'stamp') await loadStampPreview(path);
    if (type === 'watermark') {
        resetPreviewPage('watermark');
        loadWatermarkPreview();
    }
    if (type === 'metadata') await loadMetadata();
    // 7.0.1 的新面板（排版/安全/内容提取）要在换文件后重算页面尺寸、清掉上一份的命中清单，
    // 但它们不属于这里的 12 个老页面，所以留一个钩子交给 tools.js 挂，app.js 不认识它们。
    if (typeof window.onPdfChosen === 'function') window.onPdfChosen(type, path);
}

// ==================== 分批渲染 ====================
// 一次只向后端要 PAGE_BATCH 页。一次性渲染全文必然带截断上限，而截断结果一旦被
// 当成页面清单使用，保存时就会写出缺页的文件；分批既能拿到全部页面，
// 单次经过 js 桥的数据量也可控。
const PAGE_BATCH = 24;
const VIEWER_DPI = 100;
// 查看 PDF 的页码清单给到这么多页（后端 api/viewer.py 的 MAX_VIEWER_PAGES 同步）。
// 以前这里是「一次到底、最多 600 页」的硬顶，因为整页堆叠滚动会把每一张图都留在
// 页面里；现在是按视口窗口出图、滚过的远处回收，清单和滚动条都靠便宜的页尺寸撑，
// 所以五千页只是清单长，不是内存长。
const VIEWER_MAX_PAGES = 5000;
// 视口上下各多备多少屏的图：用户往下滚时不会立刻看到白块
const VIEWER_OVERSCAN_PX = 1.5;
// 窗口外还留几批不回收（来回小幅滚动不必重新出图）
const VIEWER_KEEP_PAD = 2;
// 左边那条缩略图栏的出图精度：格子才 118px 宽，64dpi 已经过采样
const VIEWER_THUMB_DPI = 64;
// 页面之间的那道缝，和 style.css 里 .pdf-view img 的 margin-bottom 一致
const VIEWER_PAGE_GAP = 12;
// 缩略图精度：屏幕上一个格子只有 120~150px 宽，64dpi 的 A4 图（567x803）已经是
// 三四倍过采样，看不出差别，但比 80dpi 每页快一半、体积只有三分之一。
// 64 同时是「保底档」：预读边距里那几批、以及配置读不出来时，一律按它出图。
// 三档可选（F25）：96 是屏幕观看舒适档，128 能读小字；单页字节数按面积涨（约 2.25× / 4×）
const PM_THUMB_DPI_MIN = THUMB_DPI_DEFAULT;
// 档位列表来自 js/common.js，设置窗口那个下拉框用的是同一串
const PM_THUMB_DPI_OPTIONS = THUMB_DPI_OPTIONS;
// 用户选的档位，来自配置 thumb_dpi；只有真正在视口里的那几批按它出图
let pmThumbDpi = PM_THUMB_DPI_MIN;
// 与 pmImagesCache 同下标：那一格的图是按哪个档位出的。
// 图片本身仍直接存字符串——验收脚本按 pmImagesCache.filter(Boolean) 数驻留页数，
// 把它换成对象会让那几条断言全部读到 0，看着像「图全没了」。
let pmImagesDpi = [];
// 缩略图跟着滚动画，一页都不落下——用户要对着图才能判断这页是不是要删/要转的那页，
// 「后面的页不画图」等于那部分没法编辑。内存靠只留住离屏幕最近的这几批来兜底：
// 千页以上的书不会把全部 base64 都攥在手里，滚回去时那批会重新出一次图。
const PM_THUMB_KEEP_BATCHES = 8;
// 一页缩略图超过这么多字节就按「扫描件」处理：标清也改小口取（见 pmBatchSlices）。
// 本机实测文字页约 7KB/页、扫描页 66KB/页，阈值取中间的 24KB，两边都差着成倍，不会抖。
const PM_THUMB_HEAVY_PAGE_BYTES = 24 * 1024;
// 上一段真正到手的平均字节数决定的口径，换文件时不沿用（首批还没数据，一律按文字页取）
let pmThumbBytesHeavy = false;
// 撤销栈深度：一份快照就是 slots 的一次拷贝，千页文档约几十 KB
const PM_HISTORY_MAX = 30;


// ==================== Viewer 预览 ====================
/** 空状态与预览区互斥：空的 preview-container 会显示成一条白色横条，很难看。 */
function setViewerLoaded(loaded) {
    const box = document.getElementById('viewer-preview');
    const empty = document.getElementById('viewer-empty');
    const dock = document.getElementById('viewer-bar-dock');
    if (box) box.classList.toggle('hidden', !loaded);
    if (empty) empty.classList.toggle('hidden', !!loaded);
    // 那条浮标和预览区一起出现：没有文档时留一条什么都不干的工具条更难看
    if (dock) dock.classList.toggle('hidden', !loaded);
}

/**
 * 那条浮在可见区底端的工具条：平时收起，鼠标移上去展开，点右边那颗胶囊标「固定展开」，
 * 再点一下取消固定。展开/收起由这儿管，不用 CSS 的 :hover：
 * 胶囊标和条之间哪怕只有一两像素的缝，鼠标爬过去那一下就会被判成「移开」而当场收起
 * （用户截图里骂的就是这个）。所以移开给 320ms 宽限，期间回到条上就取消收起。
 */
let viewerBarPinned = false;
let viewerBarInside = false;
let viewerBarCollapseTimer = 0;

function viewerBarSetOpen(open) {
    const dock = document.getElementById('viewer-bar-dock');
    if (dock) dock.classList.toggle('collapsed', !open);
}

function viewerBarInit() {
    const dock = document.getElementById('viewer-bar-dock');
    if (!dock || dock.__barBound) return;
    dock.__barBound = true;
    dock.addEventListener('mouseenter', () => {
        viewerBarInside = true;
        clearTimeout(viewerBarCollapseTimer);
        viewerBarSetOpen(true);
    });
    dock.addEventListener('mouseleave', () => {
        viewerBarInside = false;
        if (viewerBarPinned) return;
        clearTimeout(viewerBarCollapseTimer);
        viewerBarCollapseTimer = setTimeout(() => {
            // 焦点还在条里就不收（键盘 Tab 进去的人得能继续用）
            if (!viewerBarPinned && !dock.contains(document.activeElement)) viewerBarSetOpen(false);
        }, 320);
    });
}

function viewerBarTogglePinned() {
    viewerBarPinned = !viewerBarPinned;
    const dock = document.getElementById('viewer-bar-dock');
    const toggle = document.getElementById('viewer-bar-toggle');
    if (!dock) return;
    clearTimeout(viewerBarCollapseTimer);
    dock.classList.toggle('pinned', viewerBarPinned);
    if (viewerBarPinned) {
        viewerBarSetOpen(true);
    } else if (!viewerBarInside) {
        viewerBarSetOpen(false);
    }
    // 鼠标还压在胶囊上时先留着条，等它移开再由 mouseleave 收：
    // 当场收掉的话用户看到的是「我手还放在上面它就没了」
    if (toggle) {
        toggle.setAttribute('aria-expanded', viewerBarPinned ? 'true' : 'false');
        toggle.title = viewerBarPinned ? uiText('dyn_1419', '取消固定（移开鼠标就收起）') : uiText('dyn_1004', '固定展开这条工具栏');
        // 取消固定那一下，胶囊还带着焦点：不 blur 的话 :focus-within 会让它收不回去，
        // 用户看到的就是「我明明点收回了」
        if (!viewerBarPinned && document.activeElement === toggle) toggle.blur();
    }
}

// 一次查看会话的状态。图只活在窗口里：滚出窗口的槽位连 src 一起摘掉，
// 所以五千页的册子和六十页的册子，攥在手里的图是同一个量级。
let viewer = null;

function viewerNewState(path, gen) {
    return {
        path: path, gen: gen,
        total: 0, count: 0,
        sizes: [], widths: [], heights: [], lefts: [], tops: [], span: 0,
        thumbHeights: [], thumbTops: [], thumbSpan: 0,
        // 缩放：width=适应页宽（默认，和以前一模一样）、page=适应整页、
        // one=1:1 实际大小、pct=按百分比（25~400，100% 就是 1:1）
        zoom: 'width', pct: 100, page: 0,
        // 左边那条里当前用描边标着的那一格；-1 表示还没有
        activeThumb: -1,
        slots: new Map(), thumbs: new Map(),
        images: new Map(), thumbImages: new Map(),
        // 可选文字层：页序 → 后端那一页的 {items, width, ...}。和图同进同退，
        // 换文件时跟着这个新对象一起空掉，不留上一页的字
        layers: new Map(),
        // 已经说过「这一页没有可选取的文字」的页，一份文档内不重复说
        noTextHinted: new Set(),
        pending: new Set(),
        bound: false,
    };
}

// 100% 那一档每 pt 占多少 px：PDF 的 pt 是 1/72 英寸，屏幕按 96 dpi 画，
// 所以 1:1 的换算系数是 96/72。WPS 的百分比框也是这个口径。
const VIEWER_PX_PER_PT = 96 / 72;
const VIEWER_ZOOM_MIN = 25;
const VIEWER_ZOOM_MAX = 400;

/** 这一页在当前缩放下该占多宽（px）。 */
function viewerPageWidth(size, vw, vh) {
    const v = viewer;
    const ptW = size[0] > 0 ? size[0] : 595;
    const ptH = size[1] > 0 ? size[1] : 842;
    if (v.zoom === 'width') return vw;
    if (v.zoom === 'one') return Math.round(ptW * VIEWER_PX_PER_PT);
    if (v.zoom === 'page') return Math.max(60, Math.round(Math.min(vw, vh * ptW / ptH)));
    const pct = Math.max(VIEWER_ZOOM_MIN, Math.min(VIEWER_ZOOM_MAX, v.pct));
    return Math.max(60, Math.round(ptW * VIEWER_PX_PER_PT * pct / 100));
}

/**
 * 按当前缩放算每页的宽/高/左边距与前缀和。
 *
 * 出图精度跟着缩放走（见 viewerRenderDpi），版面尺寸也在这儿一次性定死——
 * 滚动条长度和「跳到第 N 页」都靠它，不能等图回来再改，否则一跳就是几百像素的跳动。
 */
function viewerCalcLayout() {
    const v = viewer;
    const box = document.getElementById('viewer-pdf');
    const spacer = document.getElementById('viewer-spacer');
    const thumbSpacer = document.getElementById('viewer-thumb-spacer');
    if (!box || !spacer || !thumbSpacer) return;
    // 宽按空壳自己量到的来；高按可见区来（「适应整页」要的是这一屏能放多大），
    // 藏起来时量不到，由 showPage / setViewerLoaded 之后补算一次
    const vp = viewerViewport(spacer);
    const vw = Math.max(120, Math.round(spacer.getBoundingClientRect().width) || 600);
    const vh = vp.size;
    v.widths = new Array(v.count);
    v.heights = new Array(v.count);
    v.lefts = new Array(v.count);
    for (let i = 0; i < v.count; i++) {
        const size = v.sizes[i] || [595, 842];
        const w = viewerPageWidth(size, vw, vh);
        v.widths[i] = w;
        v.heights[i] = Math.max(24, Math.round(w * (size[1] || 842) / (size[0] || 595)));
        v.lefts[i] = Math.max(0, Math.round((vw - w) / 2));
    }
    v.tops = viewerPrefix(v.heights, VIEWER_PAGE_GAP);
    v.span = v.tops[v.count];
    spacer.style.height = v.span + 'px';
    const railWidth = Math.max(60, Math.round(thumbSpacer.getBoundingClientRect().width) || 118);
    v.thumbHeights = new Array(v.count);
    for (let i = 0; i < v.count; i++) {
        v.thumbHeights[i] = Math.max(20, Math.round(railWidth * v.heights[i] / v.widths[i]));
    }
    v.thumbTops = viewerPrefix(v.thumbHeights, 10);
    v.thumbSpan = v.thumbTops[v.count];
    thumbSpacer.style.height = v.thumbSpan + 'px';
    v.slots.forEach((node, i) => viewerPlaceSlot(node, i));
    // 版面宽度一变，每页「pt 占多少 px」就变了：只改那一个变量，层里的框自己跟着重算
    v.slots.forEach((node, i) => viewerLayerRelayout(node, i));
    v.thumbs.forEach((node, i) => {
        node.style.top = v.thumbTops[i] + 'px';
        node.style.height = v.thumbHeights[i] + 'px';
    });
}

function viewerPlaceSlot(node, index) {
    const v = viewer;
    node.style.left = v.lefts[index] + 'px';
    node.style.width = v.widths[index] + 'px';
    node.style.top = v.tops[index] + 'px';
    node.style.height = v.heights[index] + 'px';
}

/**
 * 出图精度按缩放换算，夹在 60~300 dpi。
 *
 * 适应页宽时算出来正好是以前那个 100 dpi 上下（1.4 倍过采样，字看着不糊）；
 * 放大到 400% 时按原精度出图就是一张马赛克，所以必须跟着涨；300 是后端
 * `_clamp_dpi` 的上限，再高就是拿内存换一点点锐度。
 */
function viewerRenderDpi(start) {
    const v = viewer;
    const index = Math.max(0, Math.min(v.count - 1, start));
    const size = v.sizes[index] || [595, 842];
    const ratio = v.widths[index] / ((size[0] || 595) * VIEWER_PX_PER_PT);
    return Math.max(60, Math.min(300, Math.round(96 * ratio * 1.05)));
}

/** 前缀和：tops[i] 是第 i 页顶部的偏移，最后一项是整个版面高度。 */
function viewerPrefix(heights, gap) {
    const tops = new Array(heights.length + 1);
    tops[0] = 0;
    for (let i = 0; i < heights.length; i++) tops[i + 1] = tops[i] + heights[i] + gap;
    return tops;
}

/** 某个偏移落在第几页（tops 单调递增，二分）。 */
function viewerIndexAt(tops, y) {
    let lo = 0, hi = tops.length - 2;
    while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (tops[mid] <= y) lo = mid; else hi = mid - 1;
    }
    return Math.max(0, Math.min(lo, Math.max(0, tops.length - 2)));
}

/** 版面宽度变了就要重算：槽位、缩略图格、滚动条长度全部跟着改。 */
/** 版面重算的对外名字：窗口改大小、收导航栏、切回这一页、换缩放都走它。 */
function viewerRelayout() {
    const v = viewer;
    if (!v || !v.count) return;
    viewerCalcLayout();
}

/** 取一批页面图；到手时若会话已换（换了文件或重开），整批丢掉不画。 */
function viewerStartFetch(target, start) {
    const v = viewer;
    const key = target + ':' + start;
    if (v.pending.has(key)) return;
    v.pending.add(key);
    viewerInflight += 1;
    const done = () => {
        viewerInflight = Math.max(0, viewerInflight - 1);
        if (viewer) viewerPumpQueue();
    };
    const gen = v.gen;
    const dpi = target === 'thumb' ? VIEWER_THUMB_DPI : viewerRenderDpi(start);
    const count = Math.min(PAGE_BATCH, v.count - start);
    callApi('render_page_range', v.path, start, count, dpi).then(list => {
        if (!viewer || viewer.gen !== gen) { done(); return; }
        viewer.pending.delete(key);
        if (Array.isArray(list) && list.length) {
            list.forEach((src, k) => {
                const index = start + k;
                if (index >= viewer.count) return;
                const store = target === 'thumb' ? viewer.thumbImages : viewer.images;
                store.set(index, src);
                viewerPaint(target, index);
            });
        }
        done();
    }).catch(() => {
        if (viewer && viewer.gen === gen) viewer.pending.delete(key);
        done();
    });
}

/** 把已经到手的图挂到那一格的节点上（节点还没建就先不挂，建的时候再取）。 */
function viewerPaint(target, index) {
    const v = viewer;
    const store = target === 'thumb' ? v.thumbImages : v.images;
    const src = store.get(index);
    if (!src) return;
    const node = (target === 'thumb' ? v.thumbs : v.slots).get(index);
    if (!node) return;
    const img = node.firstElementChild;
    if (img && img.getAttribute('src') !== src) img.setAttribute('src', src);
}

// ==================== 查看页的可选文字层（像 WPS 那样按住左键拖就能选字）====================
// 后端 api/viewer.py 的 get_text_layer_range 给的是「每个文字串一个框 + pt 坐标」，
// 坐标系和那张图完全同一套（左上原点、y 向下、/Rotate 已转正），所以前端只要
// 一个系数 k = 这一格占的 px 宽 ÷ 这一页的 pt 宽，就能把框铺在图上。
// 三层设计都是冲着「别拖慢滚动」来的：
//   ① 只给窗口里那几页铺（和图同一套回收）；
//   ② 铺的时候把几何全写成 calc(var(--vtl-k) * 常数)，缩放/改窗口只改 --vtl-k 这一个变量，
//      不重建 DOM、不回后端——一次重排都不多花；
//   ③ 横向缩放比值与 k 无关（分子分母都随 k 线性变），所以每页只在铺的时候量一次自然宽。
let viewerTextLayerOn = true;
let viewerLayerQueue = [];
let viewerLayerInflight = 0;
const viewerLayerInflightMax = 2;

/**
 * 后台补数据的桥调用：不占「加载中」那条，也不弹错。
 *
 * 文字层没到，那一页只是暂时选不了字，图照旧在。走 callApi 会让每次滚动都闪一下
 * 遮罩、桥还没齐时更弹一句「接口未就绪」——那是把一件后台动作当成用户操作在报错。
 */
function callApiBackground(name) {
    const bridge = (window.pywebview || {}).api;
    const args = Array.prototype.slice.call(arguments, 1);
    if (!bridge || typeof bridge[name] !== 'function') return Promise.resolve(null);
    try {
        return Promise.resolve(bridge[name].apply(bridge, args)).catch(() => null);
    } catch (e) {
        return Promise.resolve(null);
    }
}

/** 这一格每 pt 占多少 px。图按它出，框按它铺，两边必须同一个基数。 */
function viewerLayerK(index) {
    const v = viewer;
    const size = v.sizes[index] || [595, 842];
    const ptW = size[0] > 0 ? size[0] : 595;
    const pxW = v.widths[index] || 0;
    if (!(pxW > 0)) return 0;
    return pxW / ptW;
}

/** 只改系数：缩放、改窗口、收导航栏都走这条，不重建 DOM、也不回后端。 */
function viewerLayerScale(node, index) {
    const host = node.__vtl;
    if (!host) return;
    const k = viewerLayerK(index);
    if (k > 0) host.style.setProperty('--vtl-k', k.toFixed(6) + 'px');
}

/**
 * 版面重算那一段走这条：先改系数，再把「量不到自然宽」的那一页补铺一次。
 *
 * 格子藏在别的页里（rect 全是 0）时 offsetWidth 读回来是 0，scaleX 算不出来，
 * 那一页的字就会按浏览器自己的宽度铺，和纸上的位置错开。铺的时候标一下，
 * 等这一页真的看得见（showPage / 改窗口都会走 viewerCalcLayout）再补铺一次。
 */
function viewerLayerRelayout(node, index) {
    viewerLayerScale(node, index);
    const host = node.__vtl;
    if (host && host.__vtlStale) viewerPaintLayer(index);
}

/**
 * 单元 → 起点角 + 沿字前进方向的长 + 字高那一边。
 *
 * 竖排向上那一列第一个字在框底、横排倒过来那串从右边起，都在这张表里；
 * 表错一个角，整列字就会飞出页面外面（不是偏一点，是整列看不见）。
 * 非 90 倍数的斜排（很少见）退回左上角，仍按 r 转：位置差一点，但不会崩。
 */
function viewerLayerBox(item) {
    const r = Math.round(item.r || 0);
    const x = item.x, y = item.y, w = item.w, h = item.h;
    if (r === 90) return { x: x + w, y: y, flow: h, cross: w, rot: 90 };
    if (r === 180) return { x: x + w, y: y + h, flow: w, cross: h, rot: 180 };
    if (r === 270) return { x: x, y: y + h, flow: h, cross: w, rot: 270 };
    return { x: x, y: y, flow: w, cross: h, rot: r };
}

/** 把某一格的文字层铺出来（或撤掉）。数据没到就什么都不做，到了再铺。 */
function viewerPaintLayer(index) {
    const v = viewer;
    const node = v.slots.get(index);
    if (!node) return;
    const host = node.__vtl;
    const page = viewerTextLayerOn ? v.layers.get(index) : null;
    const items = page && Array.isArray(page.items) ? page.items : [];
    // 三种「铺不了」：没开、这一页没字、字体没编码（读回来全是占位符，
    // 铺上去选中的是一串问号，比选不中更误导人）
    if (!page || !page.has_text || page.encoding_suspect || !items.length) {
        if (host) { host.textContent = ''; host.__built = ''; }
        return;
    }
    let box = host;
    if (!box) {
        box = document.createElement('div');
        box.className = 'vtl';
        node.appendChild(box);
        node.__vtl = box;
    }
    // 同一页同一批字不重铺：滚回来时 Map 里还是那份，重铺等于白逼一次重排
    const stamp = [page.page_index, items.length, page.truncated ? 't' : ''].join(':');
    if (box.__built === stamp && box.childElementCount) { viewerLayerScale(node, index); return; }
    box.__built = stamp;
    box.__vtlStale = false;
    box.textContent = '';
    const frag = document.createDocumentFragment();
    const spans = [];
    let lastLine = null;
    items.forEach(item => {
        if (!item || typeof item.t !== 'string' || !item.t) return;
        if (lastLine !== null) {
            // 行间补换行、行内带空格的补空格：跨框选出来那句才是通顺的
            frag.appendChild(document.createTextNode(
                item.l !== lastLine ? '\n' : (item.g ? ' ' : '')));
        }
        lastLine = item.l;
        const geo = viewerLayerBox(item);
        const el = document.createElement('span');
        el.textContent = item.t;
        el.style.left = 'calc(var(--vtl-k) * ' + geo.x + ')';
        el.style.top = 'calc(var(--vtl-k) * ' + geo.y + ')';
        const size = item.s > 0 ? item.s : (geo.cross > 0 ? geo.cross : 10);
        el.style.fontSize = 'calc(var(--vtl-k) * ' + size + ')';
        if (geo.rot) el.style.transform = 'rotate(' + geo.rot + 'deg)';
        frag.appendChild(el);
        spans.push({ el: el, flow: geo.flow, rot: geo.rot });
    });
    viewerLayerScale(node, index);
    box.appendChild(frag);
    // 先全写完再一次性读：读 offsetWidth 会逼一次重排，穿插着读就是每框逼一次
    spans.forEach(s => { s.natural = s.el.offsetWidth; });
    const k = viewerLayerK(index) || 1;
    let unmeasured = 0;
    spans.forEach(s => {
        if (!(s.natural > 0) || !(s.flow > 0)) {
            if (!(s.natural > 0)) unmeasured += 1;
            return;
        }
        const ratio = (s.flow * k) / s.natural;
        if (!(ratio > 0) || Math.abs(ratio - 1) < 0.002) return;
        s.el.style.setProperty('--vtl-sx', ratio.toFixed(5));
        s.el.style.transform = (s.rot ? 'rotate(' + s.rot + 'deg) ' : '')
            + 'scaleX(var(--vtl-sx))';
    });
    // 有框量不到宽（多半是这一格还藏着）就留个记号，等它可见时补铺一次
    box.__vtlStale = unmeasured > 0;
}

function viewerPumpLayers() {
    while (viewerLayerInflight < viewerLayerInflightMax && viewerLayerQueue.length) {
        viewerFetchLayer(Number(viewerLayerQueue.shift().slice(6)));
    }
}

/** 只给窗口里还没到手的批排文字层的队；和图分开两条队，字不许把图的额度吃掉。 */
function viewerQueueLayers(lo, hi) {
    const v = viewer;
    if (!viewerTextLayerOn || !v.count) { viewerLayerQueue = []; return; }
    const wanted = [];
    for (let start = Math.floor(lo / PAGE_BATCH) * PAGE_BATCH; start <= hi; start += PAGE_BATCH) {
        if (start >= v.count) break;
        const key = 'layer:' + start;
        if (v.layers.has(Math.min(start, v.count - 1)) || v.pending.has(key)) continue;
        wanted.push({ key: key, dist: Math.abs(start - lo) });
    }
    const inside = index => index >= lo - PAGE_BATCH && index <= hi + PAGE_BATCH;
    viewerLayerQueue = viewerLayerQueue.filter(key => inside(Number(key.slice(6))));
    wanted.forEach(item => {
        if (viewerLayerQueue.indexOf(item.key) < 0) viewerLayerQueue.push(item.key);
    });
    viewerLayerQueue.sort((a, b) => {
        const da = Math.abs(Number(a.slice(6)) - lo), db = Math.abs(Number(b.slice(6)) - lo);
        return da === db ? a.localeCompare(b) : da - db;
    });
    viewerPumpLayers();
}

function viewerFetchLayer(start) {
    const v = viewer;
    const key = 'layer:' + start;
    if (v.pending.has(key)) return;
    v.pending.add(key);
    viewerLayerInflight += 1;
    const gen = v.gen;
    const count = Math.min(PAGE_BATCH, v.count - start);
    const done = () => {
        viewerLayerInflight = Math.max(0, viewerLayerInflight - 1);
        if (viewer) viewerPumpLayers();
    };
    callApiBackground('get_text_layer_range', v.path, start, count).then(res => {
        if (!viewer || viewer.gen !== gen) { done(); return; }
        viewer.pending.delete(key);
        if (!res || !res.success || res.unit !== 'pt' || !Array.isArray(res.pages)) { done(); return; }
        // 按页自带的 page_index 归位，不按数组下标：后端会跳过单页失败的页，
        // 照下标摆会让后面每一页都串到上一页的框上
        res.pages.forEach(page => {
            if (!page || typeof page.page_index !== 'number') return;
            if (page.page_index >= viewer.count) return;
            viewer.layers.set(page.page_index, page);
            viewerPaintLayer(page.page_index);
        });
        done();
    });
}

/** 撤掉窗口外那几页的文字层数据；节点跟着格子一起走，这里只清 Map。 */
function viewerTrimLayers(lo, hi) {
    const pad = VIEWER_KEEP_PAD * PAGE_BATCH;
    viewer.layers.forEach((_page, index) => {
        if (index < lo - pad || index > hi + pad) viewer.layers.delete(index);
    });
}

/** 开关：关掉就把这一层的 DOM 整个撤掉，省掉几百上千个 span。 */
function viewerToggleTextLayer() {
    viewerTextLayerOn = !viewerTextLayerOn;
    const btn = document.getElementById('viewer-select-toggle');
    if (btn) {
        btn.classList.toggle('active', viewerTextLayerOn);
        btn.setAttribute('aria-pressed', String(viewerTextLayerOn));
    }
    viewerHideCopy();
    const v = viewer;
    // 关与开都要把已有格子重铺一遍：数据多半还在 Map 里，
    // 只靠 viewerUpdate 的话「还没到手的批」才排队，已经在手的那几页就没人画了
    if (v) v.slots.forEach((_node, index) => viewerPaintLayer(index));
    if (!viewerTextLayerOn) {
        viewerLayerQueue = [];
        return;
    }
    viewerUpdate();
}

// 选完之后浮出来的那颗「复制」。WebView2 里 Ctrl+C 本来就能用，
// 但「拖完看见一个按钮」才是 WPS 那个手感，也才让第一次用的人知道这里能选。
let viewerCopyBtn = null;

function viewerHideCopy() {
    if (viewerCopyBtn && viewerCopyBtn.parentNode) viewerCopyBtn.parentNode.removeChild(viewerCopyBtn);
}

function viewerCopySelected() {
    const sel = window.getSelection();
    const text = sel ? String(sel.toString() || '') : '';
    if (!text) { viewerHideCopy(); return; }
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    if (!ok && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
            () => showToast(uiText('dyn_1487', '已复制所选文字')),
            () => showToast(uiText('dyn_1488', '复制失败')));
        viewerHideCopy();
        return;
    }
    showToast(ok ? uiText('dyn_1487', '已复制所选文字') : uiText('dyn_1488', '复制失败'));
    viewerHideCopy();
}

/** 选区落在文字层里才浮按钮；落在别处（标题、状态行、别的页）一律撤掉。 */
function viewerTrackSelection(event) {
    if (!viewer || !viewerTextLayerOn) { viewerHideCopy(); return; }
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) {
        viewerHideCopy();
        viewerNoteNoText(event);
        return;
    }
    const range = sel.getRangeAt(0);
    const anchor = range.commonAncestorContainer;
    const el = anchor && anchor.nodeType === 1 ? anchor : (anchor || {}).parentElement;
    if (!el || !el.closest || !el.closest('.vtl')) { viewerHideCopy(); return; }
    const spacer = document.getElementById('viewer-spacer');
    if (!spacer) return;
    const rect = range.getBoundingClientRect();
    if (!(rect.width > 0) && !(rect.height > 0)) { viewerHideCopy(); return; }
    if (!viewerCopyBtn) {
        viewerCopyBtn = document.createElement('button');
        viewerCopyBtn.type = 'button';
        viewerCopyBtn.className = 'btn ghost vtl-copy';
        viewerCopyBtn.textContent = uiText('dyn_1490', '复制');
        // 按钮自己不吃掉选区：按下不默认清空，否则点一下就变成「复制了个空」
        viewerCopyBtn.addEventListener('mousedown', event => event.preventDefault());
        viewerCopyBtn.addEventListener('click', viewerCopySelected);
    }
    if (viewerCopyBtn.parentNode !== spacer) spacer.appendChild(viewerCopyBtn);
    const base = spacer.getBoundingClientRect();
    viewerCopyBtn.style.left = Math.max(0, Math.round(rect.right - base.left) + 6) + 'px';
    viewerCopyBtn.style.top = Math.max(0, Math.round(rect.bottom - base.top) + 4) + 'px';
}

/**
 * 在「确实没有字」的那一页上拖了一把却什么都没选中，就说一句为什么。
 *
 * 只在数据已经到手、而且后端明说这一页没字（或字体没编码）时才吭声：
 * 层还在路上就喊「没有可选取的文字」是骗人，用户等半秒其实选得中。
 * 同一页只说一次，换文档跟着 viewer 一起清空。
 */
function viewerNoteNoText(event) {
    const v = viewer;
    const slot = event && event.target && event.target.closest
        ? event.target.closest('.viewer-slot') : null;
    if (!v || !slot) return;
    let index = -1;
    v.slots.forEach((node, i) => { if (node === slot) index = i; });
    if (index < 0) return;
    const page = v.layers.get(index);
    if (!page || (page.has_text && !page.encoding_suspect)) return;
    if (!v.noTextHinted) v.noTextHinted = new Set();
    if (v.noTextHinted.has(index)) return;
    v.noTextHinted.add(index);
    showToast(uiText('dyn_1489', '这一页没有可选取的文字'), 2600);
}

function viewerEnsureNode(target, index) {
    const v = viewer;
    const map = target === 'thumb' ? v.thumbs : v.slots;
    let node = map.get(index);
    if (node) return node;
    node = document.createElement('div');
    node.className = target === 'thumb' ? 'thumb-item viewer-thumb' : 'viewer-slot';
    if (target === 'thumb') {
        node.style.top = v.thumbTops[index] + 'px';
        node.style.height = v.thumbHeights[index] + 'px';
        node.onclick = () => viewerScrollToPage(index);
        if (index === v.page) {
            const stale = v.thumbs.get(v.activeThumb);
            if (stale && stale !== node) stale.classList.remove('active');
            node.classList.add('active');
            v.activeThumb = index;
        }
    } else {
        viewerPlaceSlot(node, index);
    }
    const img = document.createElement('img');
    img.alt = '';
    node.appendChild(img);
    const host = document.getElementById(target === 'thumb' ? 'viewer-thumb-spacer' : 'viewer-spacer');
    if (host) host.appendChild(node);
    map.set(index, node);
    viewerPaint(target, index);
    // 格子是刚建的，但文字层的数据可能早就到了（先取后建那一趟），这里补铺一次
    if (target !== 'thumb') viewerPaintLayer(index);
    return node;
}

/**
 * 只留窗口里的格子，远处的连节点带图一起摘掉。
 *
 * 这一步是「五千页也流畅」的全部凭据：base64 字符串和 WebView2 解出来的位图
 * 都挂在节点上，节点不走、图就一直攥着。回收后往回滚会重新出一次图，
 * 那比让内存在第几百页时涨死划算。
 */
function viewerTrimWindow(map, store, lo, hi) {
    const pad = VIEWER_KEEP_PAD * PAGE_BATCH;
    const keep = index => index >= lo - pad && index <= hi + pad;
    map.forEach((node, index) => {
        if (keep(index)) return;
        const img = node.firstElementChild;
        if (img) img.removeAttribute('src');
        node.remove();
        map.delete(index);
    });
    // 一批是 24 页，窗口只用到其中几页：剩下的字符串不在这儿删的话会一直攒着，
    // 实测跳三屏就攥了一千四百多张 base64（约 5 MB 纯字符串），越滚越大
    store.forEach((_src, index) => {
        if (!keep(index)) store.delete(index);
    });
}

/**
 * 可见区在「空壳内容坐标」里的位置和高度。
 *
 * 两个坑都在这儿：① 滚动容器可能是外层 .main，也可能是 .pdf-view 自己（版面有定高时就是它），
 * 所以一律先问 `viewerScroller()` 再算，别拿 `window.innerHeight` 凑；
 * ② `getBoundingClientRect().top` 是相对视口的，在嵌套容器里滚动必须再减掉容器自己的 top，
 * 否则「滚到第 N 页」会算成「滚到容器顶往下那点」，怎么点都停在第 1 页（实测就是这条）。
 */
function viewerViewport(spacer) {
    const host = viewerScroller();
    if (!spacer || !host) return { offset: 0, size: 600, host: host };
    return { offset: host.getBoundingClientRect().top - spacer.getBoundingClientRect().top,
             size: viewerVisibleHeight(host), host: host };
}

/**
 * 这一块到底能看见多高。
 *
 * 不能直接信 `getBoundingClientRect().height`：预览区和缩略图栏都没有定高，
 * 它们的矩形等于「整册内容的高度」（五千页那一档实测 88 万 px），
 * 拿它当可见区就等于说「整本都在眼前」，于是给每一页都建一个格子。
 * 真正能看见的不超过窗口本身，所以按窗口高度夹一下。
 */
function viewerVisibleHeight(host) {
    const h = Math.round(host.getBoundingClientRect().height) || 600;
    return Math.max(120, Math.min(h, window.innerHeight || 800));
}

/**
 * 左栏跟随右侧，但只有右侧能命令左栏——反过来不行。
 *
 * 用户要的是「右侧翻页时左侧那一页的预览要看得见」，同时「我拿鼠标滚左侧，右侧不许跟着动」。
 * 麻烦在于左栏被自己滚动也会冒 scroll 事件，如果不认出来，代码会把它当成「用户翻页」再把
 * 左栏拽回当前页，用户的手就一直在跟软件抢那条栏。所以左栏自己滚一下先挂 700ms 的「别抢」，
 * 期间右侧一律不拽；右侧一滚（包括点缩略图、跳页、换缩放）就把这段时间清零，恢复跟随。
 */
let viewerRailHoldUntil = 0;
// 上一次由代码写进左栏的那个 scrollTop：滚回来的数还是它，就是自己滚的那一下，不算用户动手
let viewerRailSetTop = -1;

function viewerRailScrolled() {
    const rail = document.getElementById('viewer-thumbs');
    if (!rail) return;
    if (Math.abs(rail.scrollTop - viewerRailSetTop) <= 2) return;
    viewerRailHoldUntil = Date.now() + 700;
}

function viewerRailReleased() {
    viewerRailHoldUntil = 0;
}

/** 把当前页的预览带进左栏可见区；已经在里面就一动不动，避免滚动时来回抖。 */
function viewerFollowRail() {
    const v = viewer;
    if (!v || !v.count) return;
    viewerMarkActiveThumb();
    if (Date.now() < viewerRailHoldUntil) return;
    const rail = document.getElementById('viewer-thumbs');
    if (!rail || rail.scrollHeight <= rail.clientHeight + 4) return;
    const gap = 8;
    const top = v.thumbTops[v.page] || 0;
    const height = v.thumbHeights[v.page] || 0;
    const view = rail.clientHeight;
    let next = rail.scrollTop;
    if (top < next + gap) next = top - gap;
    else if (top + height > next + view - gap) next = top + height - view + gap;
    next = Math.max(0, Math.min(next, rail.scrollHeight - view));
    if (Math.abs(next - rail.scrollTop) < 1) return;
    const behavior = rail.style.scrollBehavior;
    rail.style.scrollBehavior = 'auto';
    viewerRailSetTop = next;
    rail.scrollTop = next;
    if (behavior) rail.style.scrollBehavior = behavior; else rail.style.removeProperty('scroll-behavior');
}

/** 当前页那一格描边：左栏被单独滚走时，用户还得一眼认出「我在看这页」。 */
function viewerMarkActiveThumb() {
    const v = viewer;
    if (v.activeThumb === v.page) return;
    const prev = v.thumbs.get(v.activeThumb);
    if (prev) prev.classList.remove('active');
    const cur = v.thumbs.get(v.page);
    if (cur) cur.classList.add('active');
    v.activeThumb = v.page;
}

/** 主列或缩略图栏：按各自的容器矩形算可见带 → 页码区间。 */
function viewerBandOf(hostEl, spacerEl, tops, span, overscanFactor) {
    if (!hostEl || !spacerEl) return [0, 0];
    const size = viewerVisibleHeight(hostEl);
    const offset = hostEl.getBoundingClientRect().top - spacerEl.getBoundingClientRect().top;
    return viewerBand(tops, span, offset, size, Math.round(size * overscanFactor));
}

/** 空壳坐标下的一段可见范围 → 页码区间。 */
function viewerBand(tops, span, offset, size, overscan) {
    const from = Math.max(0, Math.min(span, offset - overscan));
    const to = Math.max(0, Math.min(span, offset + size + overscan));
    return [viewerIndexAt(tops, from), viewerIndexAt(tops, to)];
}

/** 主列与缩略图栏各自按自己那块的实际可见带刷一遍窗口。 */
function viewerUpdate() {
    const v = viewer;
    if (!v || !v.count) return;
    // 版面一动，那颗「复制」就不再对着选区了，先撤掉；下一次 mouseup 再按新位置浮
    viewerHideCopy();
    const page = document.getElementById('page-viewer');
    // 藏起来的页面量不到位置（rect 全 0），这时不刷窗口，切回来由 showPage 补算
    if (page && !page.classList.contains('active')) return;
    const spacer = document.getElementById('viewer-spacer');
    const thumbSpacer = document.getElementById('viewer-thumb-spacer');
    const rail = document.getElementById('viewer-thumbs');
    if (spacer) {
        const vp = viewerViewport(spacer);
        const [lo, hi] = viewerBand(v.tops, v.span, vp.offset, vp.size,
                                    Math.round(vp.size * VIEWER_OVERSCAN_PX));
        for (let i = lo; i <= hi; i++) viewerEnsureNode('main', i);
        viewerTrimWindow(v.slots, v.images, lo, hi);
        viewerTrimLayers(lo, hi);
        viewerQueueFetch('main', lo, hi);
        // 文字层跟着同一套窗口走：图铺到哪儿、字就铺到哪儿，滚过去才有得选
        viewerQueueLayers(lo, hi);
        // 页码框认「压着可见区上沿那一页」。取点往下让 16px：跳页时故意留了 8px 呼吸，
        // 正好卡在两页交界上会把「已经到第 2 页」读成第 1 页，连着按下一页就一直不动（实测踩过）
        v.page = viewerIndexAt(v.tops, Math.max(0, Math.min(v.span - 1, vp.offset + 16)));
        v.vh = vp.size;
        viewerRefreshBar();
    }
    if (rail && thumbSpacer) {
        const [lo, hi] = viewerBandOf(rail, thumbSpacer, v.thumbTops, v.thumbSpan, 1.5);
        for (let i = lo; i <= hi; i++) viewerEnsureNode('thumb', i);
        viewerTrimWindow(v.thumbs, v.thumbImages, lo, hi);
        viewerQueueFetch('thumb', lo, hi);
        viewerFollowRail();
    }
}

/**
 * 底部那条的数：页码框、共几页、四颗翻页按钮的可用性、三档预设哪一档亮着。
 *
 * 页码框正在被用户打字时不覆盖他写的数字（否则输入一半就被滚动改成别的页）。
 */
function viewerRefreshBar() {
    const v = viewer;
    if (!v) return;
    const jump = document.getElementById('viewer-jump');
    const focused = document.activeElement === jump;
    if (jump && !focused) jump.value = String(v.page + 1);
    const info = document.getElementById('viewer-page-info');
    if (info) info.textContent = uiTextf('dyn_1005', '/ 共 {0} 页', v.total);
    const set = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.disabled = !on;
    };
    set('viewer-first', v.page > 0);
    set('viewer-prev', v.page > 0);
    set('viewer-next', v.page < v.count - 1);
    set('viewer-last', v.page < v.count - 1);
    ['one', 'page', 'width'].forEach(mode => {
        const btn = document.getElementById('viewer-zoom-' + mode);
        if (btn) btn.classList.toggle('active', v.zoom === mode);
    });
    const pct = document.getElementById('viewer-zoom-pct');
    const slider = document.getElementById('viewer-zoom-slider');
    const shown = viewerZoomPercent();
    if (pct && document.activeElement !== pct) pct.value = String(shown);
    if (slider && document.activeElement !== slider) slider.value = String(shown);
}

/** 当前版面折算成百分比给框子看（预设档也算得出来：1:1 就是 100%）。 */
function viewerZoomPercent() {
    const v = viewer;
    if (!v || !v.count) return 100;
    if (v.zoom === 'pct') return Math.round(v.pct);
    const size = v.sizes[0] || [595, 842];
    const ratio = v.widths[0] / ((size[0] || 595) * VIEWER_PX_PER_PT);
    return Math.max(VIEWER_ZOOM_MIN, Math.min(VIEWER_ZOOM_MAX, Math.round(ratio * 100)));
}

/**
 * 只给窗口里那些「还没到手的批」排队，一次最多发两批，离视口近的先发。
 *
 * 两条都要：并发不夹住的话，一次把两百多批全推给 js 桥会互相挤到超时、整屏空白；
 * 不按距离排的话，跳页后目标那一格排在几十批早已滚过去的后面，等十几秒才出图。
 */
let viewerQueue = [];
let viewerInflight = 0;
const viewerInflightMax = 2;

function viewerQueueFetch(target, lo, hi) {
    const wanted = [];
    for (let start = Math.floor(lo / PAGE_BATCH) * PAGE_BATCH; start <= hi; start += PAGE_BATCH) {
        if (start >= viewer.count) break;
        const store = target === 'thumb' ? viewer.thumbImages : viewer.images;
        const key = target + ':' + start;
        if (store.has(Math.min(start, viewer.count - 1)) || viewer.pending.has(key)) continue;
        wanted.push({ key: key, dist: Math.abs(start - lo) });
    }
    // 窗口外的旧条目直接丢：用户已经滚走/跳走了，那些图再发回来也没人看
    const inside = index => index >= lo - PAGE_BATCH && index <= hi + PAGE_BATCH;
    viewerQueue = viewerQueue.filter(key => {
        const at = key.indexOf(':');
        if (key.slice(0, at) !== target) return true;
        return inside(Number(key.slice(at + 1)));
    });
    wanted.forEach(item => {
        if (viewerQueue.indexOf(item.key) < 0) viewerQueue.push(item.key);
    });
    viewerQueue.sort((a, b) => {
        const da = viewerQueueDist(a, lo), db = viewerQueueDist(b, lo);
        return da === db ? a.localeCompare(b) : da - db;
    });
    viewerPumpQueue();
}

function viewerQueueDist(key, anchor) {
    const at = key.indexOf(':');
    return Math.abs(Number(key.slice(at + 1)) - anchor);
}

function viewerPumpQueue() {
    while (viewerInflight < viewerInflightMax && viewerQueue.length) {
        const key = viewerQueue.shift();
        const at = key.indexOf(':');
        viewerStartFetch(key.slice(0, at), Number(key.slice(at + 1)));
    }
}

let viewerFrame = 0;
function viewerSchedule() {
    if (viewerFrame) return;
    viewerFrame = requestAnimationFrame(() => {
        viewerFrame = 0;
        viewerUpdate();
    });
}

/** 滚动只挂一次；捕获阶段听，才能不管滚动实际发生在哪一层。 */
function viewerBind() {
    const v = viewer;
    if (v.bound) return;
    v.bound = true;
    window.addEventListener('scroll', viewerSchedule, { passive: true, capture: true });
    // 拖完这一下才把「复制」浮出来。只听 mouseup，不在 selectionchange 里做：
    // 拖动过程中每变一次就挪一次按钮，看着像在手忙脚乱地跟着跑
    document.addEventListener('mouseup', viewerTrackSelection, { passive: true });
    const rail = document.getElementById('viewer-thumbs');
    const main = document.getElementById('viewer-pdf');
    // 左栏自己滚 → 挂住跟随（右侧一动不动）；右栏一滚 → 立刻恢复跟随
    if (rail) rail.addEventListener('scroll', viewerRailScrolled, { passive: true });
    if (main) main.addEventListener('scroll', viewerRailReleased, { passive: true });
    viewerBarInit();
}

/** 跳到第 index 页（0 基）：主列滚过去，缩略图栏跟着对齐。 */
function scrollToPage(index) {
    return viewerScrollToPage(index);
}

/**
 * 真正的滚动容器：从空壳往上找第一个能滚的祖先，找不到就当整页在滚。
 *
 * 查看页这块到底滚在哪一层，取决于卡片高和窗口高谁大；四千多万像素的空壳撑起来之后，
 * 猜错一层就会变成「代码以为滚了、画面一动没动」。
 */
function viewerScroller() {
    let el = document.getElementById('viewer-spacer');
    while (el && el !== document.body) {
        const cs = getComputedStyle(el);
        if (/auto|scroll/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4) return el;
        el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
}

function viewerScrollToPage(index) {
    const v = viewer;
    if (!v || !v.count) return;
    const i = Math.max(0, Math.min(index, v.count - 1));
    const spacer = document.getElementById('viewer-spacer');
    const vp = viewerViewport(spacer);
    const host = vp.host;
    if (!spacer || !host) return;
    // 必须绕过 CSS 的 scroll-behavior: smooth：四千三百万像素那一跳要是「滑」过去，
    // 中间几百屏每屏都触发一次取图排队，目标那页反而排在两百多批后面（实测等 19 秒）。
    const behavior = host.style.scrollBehavior;
    host.style.scrollBehavior = 'auto';
    // 增量按「可见区上沿在内容里的位置」算：滚多少，那个位置就跟着变多少，
    // 这样容器是不是从视口顶部开始、前面有没有别的元素都不影响
    host.scrollTop += (v.tops[i] - 8) - vp.offset;
    if (behavior) host.style.scrollBehavior = behavior; else host.style.removeProperty('scroll-behavior');
    // 用户点名要的那一页说了算：滚到文档末尾时浏览器会把 scrollTop 夹住（最后一页
    // 露不全），这时按可见区反推会少一页，连着按「下一页」就卡住不动
    v.page = i;
    const jump = document.getElementById('viewer-jump');
    if (jump) jump.value = String(i + 1);
    viewerRailReleased();
    viewerUpdate();
    v.page = i;
    viewerRefreshBar();
}

/** 界面上那个「跳到第 N 页」：填超出清单的范围会被夹到最后一页。 */
function viewerJumpToPage() {
    const v = viewer;
    const input = document.getElementById('viewer-jump');
    if (!v || !v.count || !input) return;
    const typed = parseInt(input.value, 10);
    if (!Number.isFinite(typed)) { input.value = '1'; return; }
    const page = Math.max(1, Math.min(typed, v.count));
    input.value = String(page);
    viewerScrollToPage(page - 1);
}

/** 首页 / 上一页 / 下一页 / 末页。±1 是「一页」，不是「一屏」。 */
function viewerStepPage(delta) {
    const v = viewer;
    if (!v || !v.count) return;
    const target = delta === Infinity ? v.count - 1
        : (delta === -Infinity || delta === Number.NEGATIVE_INFINITY) ? 0
            : v.page + delta;
    viewerScrollToPage(Math.max(0, Math.min(v.count - 1, target)));
}

/** 三档预设缩放。 */
function viewerSetZoom(mode) {
    const v = viewer;
    if (!v || !v.count) return;
    v.zoom = mode;
    viewerApplyZoom();
}

/** 百分比框：回车或失焦时认它，越界夹到 25%~400%。 */
function viewerZoomTyped() {
    const v = viewer;
    const input = document.getElementById('viewer-zoom-pct');
    if (!v || !v.count || !input) return;
    const typed = parseInt(input.value, 10);
    v.pct = Number.isFinite(typed) ? Math.max(VIEWER_ZOOM_MIN, Math.min(VIEWER_ZOOM_MAX, typed)) : 100;
    v.zoom = 'pct';
    viewerApplyZoom();
}

/** 滑块：拖动会连着发几十次 input，攒一下再重排，不然每格一下就把整版面重算一遍。 */
let viewerZoomTimer = 0;
function viewerZoomSlid(value) {
    const v = viewer;
    if (!v || !v.count) return;
    v.pct = Math.max(VIEWER_ZOOM_MIN, Math.min(VIEWER_ZOOM_MAX, parseInt(value, 10) || 100));
    v.zoom = 'pct';
    if (viewerZoomTimer) clearTimeout(viewerZoomTimer);
    viewerZoomTimer = setTimeout(() => { viewerZoomTimer = 0; viewerApplyZoom(); }, 90);
}

/**
 * 换缩放：版面重算 + 眼前这批按新精度重出一遍。
 *
 * 位置按「原来那一页还在原来那条线上」复原：先记住当前页离视口上沿还差多少 px，
 * 重算完再按新的版面高度补回去，不然一放大就跳回那页顶部，看着像整屏在乱跳。
 */
function viewerApplyZoom() {
    const v = viewer;
    if (!v || !v.count) return;
    const before = viewerViewport(document.getElementById('viewer-spacer'));
    const keep = before.offset - v.tops[v.page];
    v.gen += 1;                       // 旧精度的在途批次到手也不画
    v.pending.clear();
    v.images.clear();
    viewerQueue = [];
    v.slots.forEach(node => {
        const img = node.firstElementChild;
        if (img) img.removeAttribute('src');
    });
    viewerRelayout();
    const after = viewerViewport(document.getElementById('viewer-spacer'));
    if (after.host) {
        const behavior = after.host.style.scrollBehavior;
        after.host.style.scrollBehavior = 'auto';
        after.host.scrollTop += (v.tops[v.page] + keep) - after.offset;
        if (behavior) after.host.style.scrollBehavior = behavior;
        else after.host.style.removeProperty('scroll-behavior');
    }
    viewerRailReleased();
    viewerUpdate();
}

function viewerStatusText() {
    const v = viewer;
    if (!v || !v.count) return '';
    const capped = v.total > v.count
        ? uiTextf('dyn_1420', '（查看页给到前 {0} 页，后面的页请用页面管理或按页码范围处理）', v.count) : '';
    return uiTextf('dyn_1006', '✓ 共 {0} 页，可翻到任意一页{1}', v.total, capped);
}

async function loadViewerPreview(path) {
    const statusEl = document.getElementById('viewer-status');
    statusEl.innerText = uiText('dyn_1007', '⏳ 正在读取页数…');
    const gen = (viewer ? viewer.gen : 0) + 1;
    viewer = viewerNewState(path, gen);
    // 容器和那两个撑滚动条的空壳是 HTML 里常驻的，换文档只清空壳里的格子
    const spacer = document.getElementById('viewer-spacer');
    const thumbSpacer = document.getElementById('viewer-thumb-spacer');
    if (spacer) spacer.innerHTML = '';
    if (thumbSpacer) thumbSpacer.innerHTML = '';
    // 已经开始加载了，就别再对着用户喊「您还没有打开文件」
    document.getElementById('viewer-empty').classList.add('hidden');

    const total = await callApi('get_pdf_page_count', path);
    if (viewer.gen !== gen) return;
    if (!Number.isInteger(total) || total < 1) {
        statusEl.innerText = uiText('dyn_1008', '✗ 读取失败，请查看日志');
        showToast(uiText('dyn_1023', '无法读取该 PDF，可能已加密或已损坏'), 4000);
        return;
    }

    const sizes = await callApi('get_page_sizes', path, VIEWER_MAX_PAGES);
    if (viewer.gen !== gen) return;
    viewer.total = total;
    if (sizes && sizes.success && Array.isArray(sizes.sizes) && sizes.sizes.length) {
        viewer.sizes = sizes.sizes;
        viewer.count = Math.min(total, sizes.sizes.length);
    } else {
        // 读不到逐页尺寸（老配置或某个文件量不动）时按 A4 铺版面：
        // 图照样是后端出的真尺寸，只是没出图的格子占位可能不太准
        viewer.count = Math.min(total, VIEWER_MAX_PAGES);
        viewer.sizes = new Array(viewer.count).fill([595, 842]);
    }
    // 先让预览区和底部那条现身再量版面：藏着的容器 rect 全是 0，
    // 那时候量到的宽度只能落到兜底值，「适应页宽」会算成 600px 而不是真实宽度
    setViewerLoaded(true);
    viewerRelayout();
    viewerBind();
    statusEl.innerText = viewerStatusText();
    const jump = document.getElementById('viewer-jump');
    if (jump) { jump.min = '1'; jump.max = String(viewer.count); jump.value = '1'; }
    // 换文档一律从第 1 页看起：上一份滚到第几千页的位置不该带过来
    const host = viewerScroller();
    if (host) {
        const behavior = host.style.scrollBehavior;
        host.style.scrollBehavior = 'auto';
        host.scrollTop = 0;
        if (behavior) host.style.scrollBehavior = behavior; else host.style.removeProperty('scroll-behavior');
    }
    const railBox = document.getElementById('viewer-thumbs');
    if (railBox) {
        viewerRailSetTop = 0;
        railBox.scrollTop = 0;
    }
    viewerRailHoldUntil = 0;
    viewerUpdate();

    // 首屏那一批还没到手时别说「已就绪」：让调用方能等到第一页出现
    if (!viewer.images.size) {
        await new Promise(resolve => {
            const started = Date.now();
            const tick = () => {
                if (viewer && viewer.gen === gen && viewer.images.size) return resolve();
                if (viewer && viewer.gen === gen && Date.now() - started < 20000) return setTimeout(tick, 50);
                resolve();
            };
            tick();
        });
        if (viewer && viewer.gen === gen) statusEl.innerText = viewerStatusText();
    }
    if (viewer && viewer.gen === gen && !viewer.images.size) {
        statusEl.innerText = uiText('dyn_1022', '✗ 渲染失败，请查看日志');
        showToast(uiText('dyn_1011', '渲染失败，请确认选择的是有效的 PDF'), 4000);
    }
}

// ==================== 打印（按钮与 Ctrl+P → 打印预览页）====================
// 压在上面的是软件自己的打印预览页（index.html 的 #print-modal），不调系统的打印框：
// 打印框一出来就抢走焦点、盖住窗口，后台验收量不到任何东西；更要紧的是
// 「交给别的 PDF 程序去打」这一步不该由我们替用户做主。
// 真正送页走 api/printing.py：能内置就内置（本程序直接把每一页画到打印机），
// 只在连不上打印机/这台机器不支持时才退回系统打印服务，返回里的 method 说明走的哪条。
// 页码范围和拆分、导文本是同一套写法（api/_range.py），空着就是整本。
//
// 预览出图用的是查看页那套按页出 data URL 的接口（render_page_range，文字页 PNG、
// 含图页 JPEG），不新造第二条渲染路径；版面几何按 api/printing.py 里
// nup_grid / cell_rects / scale_factor / sheet_math 的同一条算式在界面侧算一遍，
// 两边算的都是「格子相对页面的比例」，所以预览里的落位和纸上的落位是一套东西。
let printBusy = false;
// 上一次读回来的打印机列表：换选中项时要在下面那行显示驱动和端口，
// 省得用户对着「导出为WPS PDF」这种名字猜它到底会不会出纸
let printPrinters = [];
// 预览页的出图精度：和查看页缩略图同一档（120 dpi 下 A4 一页 992×1403，
// 版面够看清，base64 也不会大到拖慢 js 桥）。要看得更清用顶部那排缩放，不是抬 dpi。
const PP_PAGE_DPI = 120;
const PP_CACHE_MAX = 30;       // 手里最多攥多少页的图（三千页的册子也不能整本往内存里搬）
const PP_BATCH = 24;           // 一次 render_page_range 最多要多少页，和查看页那一批同量
const PP_WALL_MAX = 24;        // 「整册铺开」最多画多少面
const PP_ZOOM_MIN = 10;
const PP_ZOOM_MAX = 400;
const PP_ZOOM_START = 60;      // 一上来还没量过窗子时先给这一档，⤢ 再按窗子重算
// 纸面相对屏幕 pt→px 的换算：预览页的 100% 就是「按 96 dpi 屏显纸的真实大小」，
// 和查看页那条缩放（1:1 = 793 px 宽的 A4）同一个口径，用户不需要猜两把尺。
const PP_SCREEN_PPI = 96 / 72;

let pp = null;

function printViewerActive() {
    const page = document.getElementById('page-viewer');
    return !!page && page.classList.contains('active');
}

function printModalOpen() {
    const mask = document.getElementById('print-modal');
    return !!mask && !mask.classList.contains('hidden');
}

/** 读右边那一排版面选项（送打和算版面用的是同一份，不另存一份「界面上的选择」）。 */
function printLayout() {
    const pick = (id, fallback) => {
        const el = document.getElementById(id);
        return el ? String(el.value || fallback) : fallback;
    };
    const nupEl = document.getElementById('pp-nup');
    const scaleEl = document.getElementById('pp-scale');
    const marginEl = document.getElementById('pp-margin');
    return {
        duplex: pick('pp-duplex', 'off'),
        nup: parseInt(nupEl && nupEl.value, 10) || 1,
        fit: pick('pp-fit', 'fit'),
        scale: parseInt(scaleEl && scaleEl.value, 10) || 100,
        margin_mm: parseFloat(marginEl && marginEl.value) || 0,
        reverse: pick('pp-order', 'normal') === 'reverse',
    };
}

async function openPrintPanel() {
    const pdf = selectedFiles['viewer'];
    if (!pdf) {
        showToast(uiText('dyn_1017', '请先打开一个 PDF'));
        return;
    }
    const mask = document.getElementById('print-modal');
    const select = document.getElementById('print-printer');
    const hint = document.getElementById('print-hint');
    if (!mask || !select) return;
    document.getElementById('print-file').innerText = pdf.split(/[\\/]/).pop();
    const mode = document.getElementById('print-mode');
    if (mode) mode.innerText = uiText('dyn_1013', '⏳ 正在看这份 PDF 有没有文字层…');
    select.innerHTML = '';
    printPrinters = [];
    hint.innerText = uiText('dyn_1014', '⏳ 正在读取本机打印机…');
    mask.classList.remove('hidden');
    // 每次打开都从头起一个会话：换文件、关了再开，都不该看见上一份的版面
    pp = { path: pdf, gen: (pp ? pp.gen : 0) + 1, plan: null, planError: '',
           sides: [], at: 1, sizes: null,
           mode: 'one', zoom: PP_ZOOM_START, cache: new Map(), pending: new Set() };
    const gen = pp.gen;
    ppDraw();

    const res = await callApi('get_printers');
    // 读打印机那一个来回里用户可能已经关了面板、或换了一份文件：那一趟就不必再往下走
    if (!pp || pp.gen !== gen) return;
    const printers = (res && Array.isArray(res.printers)) ? res.printers : [];
    printPrinters = printers;
    printers.forEach(item => {
        const option = document.createElement('option');
        option.value = item.name;
        option.innerText = item.name + (item.is_default ? uiText('dyn_1421', '（默认）') : '');
        select.appendChild(option);
    });
    // 预选：本机默认打印机排第一，读不到默认时退回列表里第一台
    const wanted = printers.findIndex(item => item.name === (res && res['default']));
    select.selectedIndex = wanted >= 0 ? wanted : 0;
    printPrinterChanged();
    if (!printers.length) {
        hint.innerText = uiTextf('dyn_1015', '⚠ 本机没有可用的打印机。点「打印」会交给系统打印服务，{0}', ((res && res.message) || ''));
    }
}

function closePrintPanel() {
    const mask = document.getElementById('print-modal');
    if (mask) mask.classList.add('hidden');
    // 预览的图是一份册子的几十页 base64，收起来就没理由再攥着（三千页那台机器只有 3.8 GB）
    pp = null;
}

/** 下拉框换了打印机：下面那行说清这台机器是什么驱动、走哪个端口。 */
function printPrinterChanged() {
    const hint = document.getElementById('print-hint');
    const select = document.getElementById('print-printer');
    if (!hint || !select) return;
    if (!printPrinters.length) {
        if (!hint.innerText) hint.innerText = '';
        return;
    }
    const item = printPrinters.find(p => p.name === select.value);
    if (!item) { hint.innerText = ''; printPlanChanged(); return; }
    const bits = [item.driver, item.port].filter(Boolean);
    hint.innerText = (item.is_default ? uiText('dyn_1422', '默认打印机 · ') : '') + bits.join(' · ');
    printPlanChanged();
}

/** 「按百分比」那一档只在选中它时才露出来：另外两档没有比例可填。 */
function printFitChanged() {
    const cell = document.getElementById('pp-scale-cell');
    const fit = document.getElementById('pp-fit');
    if (cell && fit) cell.classList.toggle('hidden', fit.value !== 'custom');
    printPlanChanged();
}

/**
 * 「这一趟会怎么打」预告 + 版面重排：换打印机、改页码、改份数、改双面/拼版/页序/缩放/边距
 * 都重新问一次后端。
 *
 * 走的是 print_plan —— 它只读文件、只算路数和张数，不建设备上下文也不把文件交出去，
 * 一张纸都不会出来。判据是文件有没有文字层：有就按文件原样打（字还是字），
 * 取不出文字（扫描件那种）才用内置出图。这件事必须在按下打印之前说得清，
 * 否则用户只能等纸出来才发现一份文字版 PDF 被打成了图片、边缘发毛。
 * 张数（几页折成几张纸）后端已经算好了（sheet_math），界面不再自己凑一套，
 * 免得「屏幕上两张、纸上三张」这种对不上。
 */
let printPlanTimer = null;
let printPlanSeq = 0;

function printPlanChanged() {
    if (printPlanTimer) clearTimeout(printPlanTimer);
    // 打字的时候别一个字问一次：这份 PDF 打开一下午也改不了几次，400 ms 够了
    printPlanTimer = setTimeout(() => { printPlanTimer = null; printPlanRefresh(); }, 400);
}

async function printPlanRefresh() {
    const line = document.getElementById('print-mode');
    if (!line) return;
    const pdf = selectedFiles['viewer'];
    if (!pdf) { line.innerText = ''; return; }
    printPlanSeq += 1;
    const seq = printPlanSeq;
    const res = await callApi('print_plan', pdf,
        document.getElementById('print-printer').value,
        document.getElementById('print-pages').value.trim(),
        document.getElementById('print-copies').value || 1,
        printLayout());
    // 只有最后那一次请求的结果才配写进这一行，不然用户看到的可能是半个改动之前的判断
    if (!pp || seq !== printPlanSeq) return;
    if (selectedFiles['viewer'] !== pdf) return;
    if (!res || !res.success) {
        line.innerText = '⚠ ' + ((res && res.message) || uiText('dyn_1016', '读不到这份 PDF 的打印方式'));
        // 版面不能继续挂着上一版的纸：那一屏看起来像「已经算好了」，而这一趟其实一页都不送
        pp.plan = null;
        pp.sides = [];
        pp.planError = (res && res.message) || uiText('dyn_1016', '读不到这份 PDF 的打印方式');
        ppDraw();
        return;
    }
    pp.planError = '';
    pp.plan = res;
    pp.sides = ppBuildSides(res);
    // 改范围/份数之后面数会变：眼前这一面要是没在新版面里，就退回第一面，
    // 不许让页码框停在一个已经不存在的面上（那会画出一片空白）
    pp.at = Math.min(Math.max(1, pp.at), Math.max(1, pp.sides.length));
    const prefix = res.method === 'file' ? '📄 ' : '🖼️ ';
    // 一句话里三件事：走哪条路（后端那句判据）、这一趟几页几张纸、这些版面选项谁说了算。
    // 「按文件原样交出去」那一路双面与拼版不归本程序定（printto 只带文件与打印机），
    // 不写明白就等于替用户向那台打印机许愿。
    const tally = uiTextf('pp_plan_tally', '这一趟 {0} 页 · {1} 份 · {2} 张纸',
        res.page_total, res.copies, res.sheets);
    const who = res.method === 'file'
        ? uiText('pp_plan_by_program', '（双面与拼版由接手的 PDF 程序说了算，张数是按你选的估的）')
        : uiText('pp_plan_by_us', '（内置出图这一路会照你选的版面送纸）');
    line.innerText = prefix + res.message + ' ｜ ' + tally + ' ' + who;
    ppDraw();
}

/** 后端给的页码清单折成「纸的一面」：一页一面、拼版几页一面、双面两张面合成一张纸。 */
function ppBuildSides(plan) {
    const lay = plan.layout || { nup: 1, duplex: 'off', reverse: false };
    const order = (plan.pages || []).slice();
    if (lay.reverse) order.reverse();
    const grid = ppGridCells(lay.nup);
    const perSide = grid.length;
    const faces = [];
    for (let start = 0; start < order.length; start += perSide) {
        faces.push(order.slice(start, start + perSide));
    }
    if (!faces.length) faces.push([]);
    const perSheet = (lay.duplex === 'off') ? 1 : 2;
    return faces.map((pages, index) => ({
        pages: pages,
        grid: grid,
        sheet: Math.floor(index / perSheet) + 1,
        side: index % perSheet,
        index: index + 1,
    }));
}

/** 和 api/printing.py 的 nup_grid 同一张表：几页一张对应几列几行，两边一处写。 */
function ppGridSpec(nup) {
    return { 1: [1, 1], 2: [2, 1], 4: [2, 2], 6: [3, 2], 9: [3, 3], 16: [4, 4] }[nup] || [1, 1];
}

/** 和 api/printing.py 的 cell_rects 同口径：格子的位置与大小都写成 0~1 的比例（千分位取整）。 */
function ppGridCells(nup) {
    const spec = ppGridSpec(nup);
    const cols = spec[0];
    const rows = spec[1];
    const cells = [];
    for (let row = 0; row < rows; row++) {
        const y = Math.floor(row * 1000 / rows) / 1000;
        const h = (Math.floor((row + 1) * 1000 / rows) - Math.floor(row * 1000 / rows)) / 1000;
        for (let col = 0; col < cols; col++) {
            const x = Math.floor(col * 1000 / cols) / 1000;
            const w = (Math.floor((col + 1) * 1000 / cols) - Math.floor(col * 1000 / cols)) / 1000;
            cells.push({ x: x, y: y, w: w, h: h });
        }
    }
    return cells;
}

/** 页幅面（pt）：plan.page_sizes 与 plan.pages 同序，摊成一张表，别在画每一格时都去 indexOf。 */
function ppSizeMap(plan) {
    if (pp && pp.sizes && pp.sizes.plan === plan) return pp.sizes.map;
    const map = {};
    (plan.pages || []).forEach((page, at) => {
        const size = plan.page_sizes && plan.page_sizes[at];
        if (size && size[0] > 0 && size[1] > 0) map[page] = { w: size[0], h: size[1] };
    });
    if (pp) pp.sizes = { plan: plan, map: map };
    return map;
}

function ppSizeOf(plan, page) {
    const hit = ppSizeMap(plan)[page];
    // 后端没给到这一页的尺寸（读 rect 失败那种）就按 A4 竖排画：
    // 版面不能因为量不到一页就整面空白，但也不能假装它是任意大小
    return hit || { w: 595, h: 842 };
}

/** 这一面「纸」的幅面：预览把纸面当成这一面最大的那页（后端是驱动定纸，这里只能示意）。 */
function ppPaperOf(face, plan) {
    let w = 0;
    let h = 0;
    (face.pages.length ? face.pages : [1]).forEach(page => {
        const size = ppSizeOf(plan, page);
        // 横竖各自取最长：A3 和 A4 混排的册子按最大的那张纸示意，小的那页居中留白，
        // 这样「这页会不会被裁」在预览里就看得见，而不是跟着上一页的比例装不下。
        w = Math.max(w, size.w);
        h = Math.max(h, size.h);
    });
    return { w: w, h: h };
}

/** 和 api/printing.py 的 scale_factor 同一口径：算的都是相对这一格的比例，两边一套数。 */
function ppScaleFactor(lay, cellW, cellH, pageW, pageH) {
    if (!pageW || !pageH || !cellW || !cellH) return 1;
    const fit = Math.min(cellW / pageW, cellH / pageH);
    if (lay.fit === 'actual') return 1;
    if (lay.fit === 'custom') {
        const pct = Math.min(PP_ZOOM_MAX, Math.max(20, parseInt(lay.scale, 10) || 100));
        return fit * pct / 100;
    }
    return fit;
}

function ppSidesShown() {
    if (!pp) return 0;
    if (pp.mode === 'wall') return Math.min(PP_WALL_MAX, pp.sides.length || 1);
    return pp.mode === 'spread' ? 2 : 1;
}

function ppVisibleFaces() {
    if (!pp || !pp.sides.length) return [];
    if (pp.mode === 'wall') return pp.sides.slice(0, PP_WALL_MAX);
    const shown = [];
    for (let at = pp.at; shown.length < ppSidesShown() && at <= pp.sides.length; at++) {
        shown.push(pp.sides[at - 1]);
    }
    return shown;
}

/**
 * 画一遍版面：每张纸一个 .pp-paper，里面按拼版档切格，页图按 scale 落位。
 *
 * 高度 = 宽度 × 纸的长宽比，所以缩放只写一个宽度数；格子位置全是比例，
 * 窗口变窄、侧边栏收起都跟着变，不必再算第二遍。
 */
function ppDraw() {
    const canvas = document.getElementById('print-canvas');
    if (!canvas || !pp) return;
    const plan = pp.plan;
    canvas.innerHTML = '';
    if (!plan) {
        const tip = document.createElement('p');
        tip.className = 'pp-empty';
        // 算不出来就把那句原因原样摊在版面窗里，而不是留一片看着像「排好了」的白
        tip.innerText = pp.planError ? '⚠ ' + pp.planError
            : uiText('pp_wait_plan', '⏳ 正在算这一趟要过几张纸…');
        canvas.appendChild(tip);
        ppSyncControls();
        return;
    }
    const faces = ppVisibleFaces();
    if (!faces.length) {
        const tip = document.createElement('p');
        tip.className = 'pp-empty';
        tip.innerText = uiText('pp_no_page', '这个范围里没有要打的页');
        canvas.appendChild(tip);
        ppSyncControls();
        return;
    }
    const lay = plan.layout || {};
    const pxPerPt = PP_SCREEN_PPI * (pp.zoom / 100);
    const marginPt = Math.max(0, parseFloat(lay.margin_mm) || 0) / 25.4 * 72;
    faces.forEach(face => {
        const paper = ppPaperOf(face, plan);
        const paperPtW = Math.max(1, paper.w + 2 * marginPt);
        const paperPtH = Math.max(1, paper.h + 2 * marginPt);
        const box = document.createElement('div');
        box.className = 'pp-paper';
        box.dataset.sheet = String(face.sheet);
        box.dataset.side = String(face.side);
        box.style.width = Math.round(paperPtW * pxPerPt) + 'px';
        box.style.height = Math.round(paperPtH * pxPerPt) + 'px';
        const print = document.createElement('div');
        print.className = 'pp-print';
        // 内缩那一段就是用户填的四边留白：预览里看得见「留白吃掉多少」，
        // 就不用等纸出来才发现 A3 塞进 A4 少了一半
        const inset = (marginPt / paperPtW * 100).toFixed(3);
        const insetY = (marginPt / paperPtH * 100).toFixed(3);
        print.style.left = inset + '%';
        print.style.right = inset + '%';
        print.style.top = insetY + '%';
        print.style.bottom = insetY + '%';
        face.grid.forEach((cell, slot) => {
            const holder = document.createElement('div');
            holder.className = 'pp-cell';
            holder.style.left = (cell.x * 100).toFixed(3) + '%';
            holder.style.top = (cell.y * 100).toFixed(3) + '%';
            holder.style.width = (cell.w * 100).toFixed(3) + '%';
            holder.style.height = (cell.h * 100).toFixed(3) + '%';
            const page = face.pages[slot];
            if (!page) return;
            const size = ppSizeOf(plan, page);
            holder.dataset.page = String(page);
            // 落位尺寸在这里换成 px 写死（和纸面那个框同一个换算），img 不再自己按 pt 猜一遍
            const factor = ppScaleFactor(lay, cell.w * paper.w, cell.h * paper.h, size.w, size.h);
            holder.dataset.w = String(Math.max(1, Math.round(size.w * factor * pxPerPt)));
            holder.dataset.h = String(Math.max(1, Math.round(size.h * factor * pxPerPt)));
            ppPaintCell(holder, page);
            print.appendChild(holder);
        });
        const tag = document.createElement('span');
        tag.className = 'pp-tag';
        tag.innerText = uiTextf('pp_face_tag', '第 {0} 张 · {1}', face.sheet,
            (lay.duplex && lay.duplex !== 'off')
                ? (face.side === 0 ? uiText('pp_face_front', '正面') : uiText('pp_face_back', '背面'))
                : uiTextf('pp_face_only', '共 {0} 面里的第 {1} 面', pp.sides.length, face.index));
        box.appendChild(print);
        // 标签挂在纸下沿外面，不占版面；纸与纸之间那段 gap 正好放得下
        box.style.marginBottom = '18px';
        box.appendChild(tag);
        canvas.appendChild(box);
    });
    ppSyncControls();
    ppNeed(faces);
}

/** 图到手了就挂上去（一格一次），没到手就在格子里写清楚是「还在路上」还是「这页出不了图」。 */
function ppPaintCell(holder, page) {
    const hit = pp && pp.cache.get(page);
    holder.innerHTML = '';
    if (!hit) {
        const tip = document.createElement('span');
        tip.className = 'pp-noimg';
        tip.innerText = uiText('pp_loading', '⏳ 这一页在出图…');
        holder.appendChild(tip);
        return;
    }
    if (hit === '') {
        const tip = document.createElement('span');
        tip.className = 'pp-noimg';
        tip.innerText = uiTextf('pp_page_failed', '第 {0} 页没出成图', page);
        holder.appendChild(tip);
        return;
    }
    const img = document.createElement('img');
    img.src = hit;
    img.alt = '';
    img.dataset.page = String(page);
    img.style.width = (holder.dataset.w || '10') + 'px';
    img.style.height = (holder.dataset.h || '10') + 'px';
    img.style.left = '50%';
    img.style.top = '50%';
    img.style.transform = 'translate(-50%, -50%)';
    holder.appendChild(img);
}

/**
 * 眼前这几面缺哪几页：把页码收成连续的一段，一段一次 render_page_range 要回来。
 *
 * 按页发请求（一页一个来回）在这个界面上是错的：一屏铺开 24 面就是 24 个桥来回，
 * 每个来回 0.4~1.5 s，看着就是「预览卡死了」。查看页早就是「连续一段一次」
 * （render_page_range 一趟最多 60 页），这里照它办，段按眼前这几面拼出来。
 */
function ppNeed(faces) {
    if (!pp || !pp.path) return;
    const want = [];
    faces.forEach(face => face.pages.forEach(page => {
        if (!pp.cache.has(page) && want.indexOf(page) < 0) want.push(page);
    }));
    if (!want.length) return;
    want.sort((a, b) => a - b);
    const head = want.slice(0, PP_BATCH);
    const start = head[0];
    // 段中间那几页眼前没要，也一并取回来：多要一页图的代价远小过多一趟桥
    const count = Math.min(PP_BATCH, head[head.length - 1] - start + 1);
    ppFetchRun(start, count, pp.gen);
}

function ppFetchRun(start, count, gen) {
    const key = start + ':' + count;
    if (pp.pending.has(key)) return;
    pp.pending.add(key);
    const settle = list => {
        if (!pp || pp.gen !== gen) return;
        const got = Array.isArray(list) ? list : [];
        for (let i = 0; i < count; i++) ppCacheSet(start + i, got[i] || '');
        ppDraw();
    };
    callApi('render_page_range', pp.path, start - 1, count, PP_PAGE_DPI).then(list => {
        if (!pp || pp.gen !== gen) return;
        pp.pending.delete(key);
        settle(list);
    }).catch(() => {
        if (!pp || pp.gen !== gen) return;
        pp.pending.delete(key);
        settle([]);
    });
}

function ppCacheSet(page, src) {
    pp.cache.set(page, src);
    // 超出上限就从最早那一页开始放手（Map 按插入序，删第一个键就是最旧的）
    while (pp.cache.size > PP_CACHE_MAX) {
        const oldest = pp.cache.keys().next();
        if (oldest.done) break;
        pp.cache.delete(oldest.value);
    }
}

/** 顶部标签、页码框、那行计数都跟着版面走一遍（画一次同步一次，不各写各的）。 */
function ppSyncControls() {
    if (!pp) return;
    Array.prototype.forEach.call(document.querySelectorAll('.pp-tab'), tab => {
        tab.classList.toggle('active', tab.dataset.ppMode === pp.mode);
    });
    const total = pp.sides.length;
    const jump = document.getElementById('pp-jump');
    const at = document.getElementById('pp-at');
    const tally = document.getElementById('pp-tally');
    const zoom = document.getElementById('pp-zoom-label');
    const note = document.getElementById('pp-note');
    const plan = pp.plan || {};
    const lay = plan.layout || {};
    if (jump) {
        jump.max = String(Math.max(1, total));
        // 正在页码框里打字时不覆盖它：写完按一下回车才算数
        if (document.activeElement !== jump) jump.value = String(pp.at);
    }
    if (at) at.innerText = uiTextf('pp_at_face', '第 {0} / {1} 面', pp.at, total);
    if (zoom) zoom.innerText = pp.zoom + '%';
    if (tally) {
        tally.innerText = uiTextf('pp_tally', '{0} 页 → {1} 张纸（{2} 面）',
            plan.page_total || 0, plan.sheets || 0, total);
    }
    const bits = [uiText('pp_preview_note', '预览按「纸 = 页面幅面」示意，驱动打不出的那一圈边没算进来')];
    if (lay.duplex && lay.duplex !== 'off') {
        // 键和兜底文案都要是字面量：把三元塞进 uiText 的两个参数里，
        // 「还没挂键的中文」那道闸就分不出哪个是键、哪个是兜底，整句会被当成漏网的串。
        bits.push(lay.duplex === 'short'
            ? uiText('pp_note_short', '双面短边翻：背面上下对折')
            : uiText('pp_note_long', '双面长边翻：背面左右对折'));
    }
    if ((lay.nup || 1) > 1) {
        bits.push(uiTextf('pp_note_nup', '一张纸 {0} 页，排成 {1} 列 × {2} 行',
            lay.nup, ppGridSpec(lay.nup)[0], ppGridSpec(lay.nup)[1]));
    }
    if ((lay.margin_mm || 0) > 0) {
        bits.push(uiTextf('pp_note_margin', '四边留白 {0} mm', lay.margin_mm));
    }
    if (lay.fit && lay.fit !== 'fit') {
        bits.push(lay.fit === 'actual'
            ? uiText('pp_note_actual', '按纸上的实际大小放，装不下的部分会被裁掉')
            : uiTextf('pp_note_custom', '版面按 {0}% 缩', lay.scale));
    }
    if (lay.reverse) bits.push(uiText('pp_note_reverse', '页序从后往前'));
    if (note) note.innerText = bits.join(' ｜ ');
    const lastStart = Math.max(1, total - (pp.mode === 'spread' ? 1 : 0));
    ['pp-first', 'pp-prev', 'pp-next', 'pp-last'].forEach(id => {
        const btn = document.getElementById(id);
        if (!btn) return;
        btn.disabled = (id === 'pp-first' || id === 'pp-prev') ? pp.at <= 1 : pp.at >= lastStart;
    });
}

/** 翻页：一面一面走（双面时「下一面」就是纸的另一面，第三面是第二张纸的正面）。 */
function printStepSide(step) {
    if (!pp || !pp.sides.length) return;
    const total = pp.sides.length;
    const span = pp.mode === 'spread' ? 2 : 1;
    let at = pp.at + step * span;
    if (step === -Infinity) at = 1;
    if (step === Infinity) at = total;
    pp.at = Math.min(Math.max(1, at), Math.max(1, total - (span - 1)));
    ppDraw();
}

function printJumpSide() {
    const jump = document.getElementById('pp-jump');
    if (!pp || !pp.sides.length || !jump) return;
    const want = parseInt(jump.value, 10);
    pp.at = Math.min(Math.max(1, want || 1), pp.sides.length);
    ppDraw();
}

function printSetMode(mode) {
    if (!pp) return;
    pp.mode = mode;
    pp.at = 1;
    // 换档就是换「一屏看几面」，缩放跟着窗子重算一次，不然铺开那档会一屏只剩一角
    printZoomFit();
}

/** 缩放两档一档一档走（×1.25 / ÷1.25），⤢ 那顆见 printZoomFit。 */
function printZoomStep(dir) {
    if (!pp) return;
    const base = dir > 0 ? 1.25 : 0.8;
    pp.zoom = Math.min(PP_ZOOM_MAX, Math.max(PP_ZOOM_MIN, Math.round(pp.zoom * base)));
    ppDraw();
}

/**
 * 「一屏装得下」：按眼前这几面要占的行列，反推一个百分比出来。
 *
 * 换算只有一处：纸面 px = 纸面 pt × (96/72) × 缩放/100，
 * 所以这里的 100% 就是查看页那档「实际大小」，界面上那一行字也这么写。
 */
function printZoomFit() {
    if (!pp) return;
    const canvas = document.getElementById('print-canvas');
    const plan = pp.plan;
    if (!canvas || !plan) {
        pp.zoom = PP_ZOOM_START;
        ppDraw();
        return;
    }
    const faces = ppVisibleFaces();
    const lay = plan.layout || {};
    const marginPt = Math.max(0, parseFloat(lay.margin_mm) || 0) / 25.4 * 72;
    let paperW = 0;
    let paperH = 0;
    faces.forEach(face => {
        const paper = ppPaperOf(face, plan);
        paperW = Math.max(paperW, paper.w);
        paperH = Math.max(paperH, paper.h);
    });
    if (!paperW || !paperH) { paperW = 595; paperH = 842; }
    paperW += 2 * marginPt;
    paperH += 2 * marginPt;
    const shown = Math.max(1, faces.length);
    // 铺开那档是「接近正方的一坨」，其余两档就是一排放下几面
    const cols = pp.mode === 'wall' ? Math.max(1, Math.ceil(Math.sqrt(shown))) : shown;
    const rows = Math.ceil(shown / cols);
    const gap = 18;                       // 和 .pp-canvas 那段 gap 同档
    const availW = Math.max(60, canvas.clientWidth - 32);
    const availH = Math.max(60, canvas.clientHeight - 40);
    const perPt = Math.min((availW - (cols - 1) * gap) / (cols * paperW),
                           (availH - (rows - 1) * gap) / (rows * paperH));
    const pct = perPt * 100 / PP_SCREEN_PPI;
    pp.zoom = Math.min(PP_ZOOM_MAX, Math.max(PP_ZOOM_MIN, Math.floor(pct)));
    ppDraw();
}

async function doPrint() {
    const pdf = selectedFiles['viewer'];
    const statusEl = document.getElementById('viewer-status');
    if (!pdf) {
        showToast(uiText('dyn_1017', '请先打开一个 PDF'));
        return;
    }
    if (printBusy) return;
    const go = document.getElementById('print-go');
    const label = go ? go.innerText : uiText('print_go', '打印');
    printBusy = true;
    if (go) { go.disabled = true; go.innerText = uiText('dyn_1019', '⏳ 正在打印…'); }
    const res = await callApi('print_pdf',
        pdf,
        document.getElementById('print-printer').value,
        document.getElementById('print-pages').value.trim(),
        document.getElementById('print-copies').value || 1,
        printLayout());
    printBusy = false;
    if (go) { go.disabled = false; go.innerText = label; }

    if (res && res.success) {
        closePrintPanel();
        if (statusEl) statusEl.innerText = '✓ ' + res.message;
        showToast(res.message);
        return;
    }
    // 失败要把原因留在界面上：打印是慢活，用户不会去翻日志确认纸出来没有
    const msg = (res && res.message) || uiText('dyn_1020', '打印失败，请查看日志');
    if (statusEl) statusEl.innerText = '✗ ' + msg;
    showToast(msg, 5000);
}

/**
 * Ctrl+P 只在查看页抢这个键，并把 WebView2 自带的打印预览按掉。
 *
 * 不 preventDefault 的话，页面收到按键的同时 Chromium 也会去开它自己的打印框，
 * 那正是这里刻意避开的东西。别的页面按 Ctrl+P 一律不拦，让用户照常翻页搜索。
 */
function printHandleShortcut(event) {
    if (event.key === 'Escape' && printModalOpen()) {
        event.preventDefault();
        closePrintPanel();
        return;
    }
    const ctrl = event.ctrlKey || event.metaKey;
    if (!ctrl || (event.key !== 'p' && event.key !== 'P')) return;
    if (!printViewerActive()) return;
    event.preventDefault();
    if (printModalOpen()) { closePrintPanel(); return; }
    openPrintPanel();
}

// ==================== 页面管理（缩略图 + 暂存改动） ====================
// 页面清单必须来自真实总页数，绝不能由「渲染出来了几张图」推导：
// 一旦渲染被截断，清单就短了，保存时照着短清单写就会丢页。
//
// 旋转、删除、提取、调序在这里都只改内存中的 pmSlots，直到点「保存为新文件」
// 才一次性写盘。以前每点一下就用 xxx_rotated.pdf 另存一个文件，想「转两页再删
// 一页」必须存两次，点错了还退不回去。
function pmCell(id) {
    return document.querySelector(`#pm-grid .pm-thumb[data-id="${id}"]`);
}

/**
 * 把某一格滚进视野：离得远的那一跳当场到位，离得近才留平滑动画。
 *
 * .main 是 scroll-behavior: smooth，只写 {block: 'center'} 会照着它一路滑过去——1200 页
 * 从第 1 页跳到第 601 页，光动画就走 1.4s，三千页那一跳走 3~4s。这一路扫过的格子一批批
 * 挤进取图流水线（每批一趟桥 0.4~1.5s），用户真正等的落地那一屏只能排在最后：
 * 实测它到 4.7s 才开始取图，中间那几批画的全是用户一眼没看的过路页。
 * 书签跳转的语义是「去第 N 页」，中间那些页不需要看，所以超过两屏就直接落定；
 * 两屏之内还留着动画，用户能看出格子是从哪儿挪过来的。
 * behavior 必须写死：'instant' 才盖得住 CSS 的 smooth，不写就等于没写。
 */
function pmScrollCellIntoView(cell, block) {
    if (!cell) return;
    const sc = document.querySelector('.main');
    const vh = (sc && sc.clientHeight) || window.innerHeight || 800;
    const top = cell.getBoundingClientRect().top;
    cell.scrollIntoView({block: block || 'center',
                         behavior: (top < -2 * vh || top > 3 * vh) ? 'instant' : 'smooth'});
}

async function loadPMPreview(path) {
    const statusEl = document.getElementById('pm-status');
    const grid = document.getElementById('pm-grid');
    pmCloseMenu();
    // 先记下「网格接下来要装的是这份文件」：读页数失败也算装载过，
    // 切回标签时就不会对同一份坏文件反复重来（见 pmTaskShown）。
    pmLoadedPath = path;
    statusEl.innerText = uiText('dyn_1021', '⏳ 正在读取页数...');
    grid.innerHTML = '';
    // 换文件时把上一份的监听彻底断开，别让观察器攥着已经删掉的格子
    if (pmThumbObserver) {
        pmThumbObserver.disconnect();
        pmThumbObserver = null;
    }

    const total = await callApi('get_pdf_page_count', path);
    if (!Number.isInteger(total) || total < 1) {
        statusEl.innerText = uiText('dyn_1022', '✗ 渲染失败，请查看日志');
        showToast(uiText('dyn_1023', '无法读取该 PDF，可能已加密或已损坏'), 4000);
        pmTotalPages = 0;
        pmSlots = [];
        pmSyncToolbar();
        return;
    }

    pmTotalPages = total;
    pmImagesCache = [];
    pmImagesDpi = [];
    // 换文档：上一份是不是扫描件不算数了，首批按文字页整批取（到手后自己纠正）
    pmThumbBytesHeavy = false;
    pmBatchDpi.clear();
    pmViewportCells.clear();
    pmRotCache.clear();
    pmRotPending.clear();
    pmRequestedBatches.clear();
    pmPendingBatches.clear();
    pmVisibleBatches.clear();
    pmVisibleCells.clear();
    pmEverDrawn = new Set();
    pmStatusNotice = '';
    pmHistory = [];
    pmSelectedIds = new Set();
    pmDirty = false;
    pmSlotSeq = 0;
    pmDragSrcPos = -1;
    pmAnchorPos = pmActivePos = -1;
    // 换文件：上一份文件在途的取图请求全部作废，否则旧文档的图会画到新文档的格子上
    pmRequestGen++;
    pmInflightBatch = -1;
    pmDraftKey = '';
    if (pmDraftTimer) { clearTimeout(pmDraftTimer); pmDraftTimer = null; }
    pmHideDraftBar();
    pmSlots = [];
    for (let i = 0; i < total; i++) pmSlots.push({ id: ++pmSlotSeq, orig: i, rot: 0 });

    await pmLoadThumbDpi();
    pmBindGrid();
    pmRenderGrid();
    await pmEnsureBatch(0);
    pmCheckDraft();
}

/**
 * 切到「页面管理」那一格时补一次账。
 *
 * 这一格藏在「排版与整理」里，不在前台的时候观察器一条都不会回报（display:none 的格子
 * 没有面积），所以两件事可能落后：换过文件却没装载，或者装载过但屏幕里这几批的图被回收了。
 * 前者按新文件装载，后者只把在屏的批次重新要一遍——不整表重建，三千页也不卡。
 */
function pmTaskShown() {
    const path = selectedFiles['pm'];
    if (path && pmLoadedPath !== path) {
        loadPMPreview(path);
        return;
    }
    pmSyncToolbar();
    pmPumpSoon();
}

function pmEl(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.innerText = text;
    return el;
}

function pmPlaceholder() {
    // 流光动画表示「滚到这儿就会画出来」，每一页迟早都会轮到
    return pmEl('div', 'pm-thumb-ph');
}

function pmRotLabel(rot) {
    if (rot === 270) return '↺90°';
    return (rot === 180 ? '⇅' : '↻') + rot + '°';
}

function pmCreateCell(slot, pos) {
    const cell = pmEl('div', 'pm-thumb' + (pmSelectedIds.has(slot.id) ? ' selected' : ''));
    cell.dataset.id = String(slot.id);
    cell.dataset.orig = String(slot.orig);
    cell.dataset.pos = String(pos);
    cell.dataset.rot = String(slot.rot);
    cell.draggable = true;
    const media = pmEl('div', 'pm-thumb-media');
    media.appendChild(pmPlaceholder());
    cell.appendChild(media);
    cell.appendChild(pmEl('span', 'pm-page-num', String(pos + 1)));
    cell.appendChild(pmEl('span', 'pm-check', '✓'));
    if (slot.rot) cell.appendChild(pmEl('span', 'pm-rot-badge', pmRotLabel(slot.rot)));
    cell.appendChild(pmEl('span', 'pm-drag-hint', uiTextf('dyn_1024', '原第 {0} 页', (slot.orig + 1))));
    return cell;
}

/** 整表重建只在换文件、撤销、放弃改动时用；日常改一格就动一格。 */
function pmRenderGrid() {
    const grid = document.getElementById('pm-grid');
    if (!grid) return;
    if (pmThumbObserver) {
        pmThumbObserver.disconnect();
        pmThumbObserver = null;
    }
    pmSyncCellRowHeight();
    const frag = document.createDocumentFragment();
    pmSlots.forEach((slot, pos) => frag.appendChild(pmCreateCell(slot, pos)));
    grid.replaceChildren(frag);
    pmUpdateStatus();
    pmSeedViewportBatches();
    pmWatchThumbs();
    pmSyncToolbar();
}

/**
 * 量一次「一格真实高度」，写给网格当行高用。
 *
 * 样式里 .pm-thumb 挂了 content-visibility: auto：屏幕外那些格子不排版不绘制，
 * 尺寸统一由 .pm-grid 的行高给。这一个数错一点，整张网的总高就跟着错
 * ——五千页每格差 1px 是几百 px 的滚动条误差，跳页落点、缩略图导轨、滚到底
 * 那几条判断全废。所以这个数必须是「这一列多宽 → 图区 1:1.414 撑出多高」的真值，
 * 而它跟窗口宽度、导航栏收起与否、DPI 都有关，只能现场量，写死是不成立的。
 * 这个数必须给**行**而不是给格子自己（contain-intrinsic-size 那一套改不动已经排过版的
 * 离屏元素，理由与对照实验写在 style.css 与 HANDOVER 第 4 节第 23 条）。
 *
 * 量法是往网格里临时塞一颗带 .pm-probe 的格子（那颗不走 content-visibility，
 * 见 style.css 的说明），读它 getBoundingClientRect() 的高度再当场摘掉——整个过程
 * 在同一个任务里做完，一帧都不会被画出来。要的是**带边框的那一个数**：它写进
 * grid-auto-rows 就是行高，行高本来就该等于一格的真实高度；早先按
 * contain-intrinsic-size 的口径去量「内容盒」（图区那一块）是另一套算法，
 * 现在尺寸由行给，量格子本身就对了。小数不能抹：clientHeight 只给整数，
 * 五千行累下来还是几百 px。
 */
function pmSyncCellRowHeight() {
    const grid = document.getElementById('pm-grid');
    if (!grid || !pmSlots.length) return;
    const probe = pmCreateCell(pmSlots[0], 0);
    probe.classList.add('pm-probe');
    const keep = grid.firstElementChild;
    grid.insertBefore(probe, keep || null);
    const height = probe.getBoundingClientRect().height;
    probe.remove();
    if (height > 0) grid.style.setProperty('--pm-row-h', height.toFixed(3) + 'px');
}

/**
 * 网格宽度一变就重新量行高（只认宽度）。
 *
 * 行高是「这一列多宽」推出来的，而列宽跟着的不只是窗口宽度：收起/展开导航栏、
 * 悬停临时展开、切进这一格之前窗口被拖窄，全都会改列宽。漏掉任何一条，
 * 那些「从来没画过」的远格子就会按旧宽度占位，整张网的总高跟着偏
 * （收起导航栏只把列宽推了 0.63px，所以这里不能取整再比——取整会把这一档吞掉）。
 *
 * 只认宽度不认高度是必须的吗？是：写行高本身就会改网格高度，
 * 按高度再触发一次就成了自己追自己、停不下来。
 */
let pmGridWidth = -1;

function pmBindGridSizer() {
    if (typeof ResizeObserver !== 'function') return;
    const grid = document.getElementById('pm-grid');
    if (!grid || grid.__pmSizer) return;
    grid.__pmSizer = new ResizeObserver(entries => {
        const width = entries[entries.length - 1].contentRect.width;
        if (width === pmGridWidth) return;
        pmGridWidth = width;
        pmSyncCellRowHeight();
    });
    grid.__pmSizer.observe(grid);
}

/**
 * 刚铺完网格那一刻观察器还没来得及回报（它的回调要等下一帧才发），
 * 而第一批图马上就要按「哪几批真在屏里」决定清晰度档位——不先补这一笔账，
 * 用户选了高清、首屏却还是按标清出图（F25）。这里算的是同一套账，
 * 只算真落在视口里的那些格子；预读边距里的那几批仍然交给观察器（它们只配标清）。
 */
function pmSeedViewportBatches() {
    const vh = window.innerHeight || 800;
    document.querySelectorAll('#pm-grid .pm-thumb').forEach(cell => {
        const orig = Number(cell.dataset.orig);
        if (!Number.isInteger(orig) || orig < 0 || orig >= pmTotalPages) return;
        const box = cell.getBoundingClientRect();
        if (box.bottom <= 0 || box.top >= vh) return;
        const batch = pmBatchStart(orig);
        if (!pmVisibleCells.has(batch)) pmVisibleCells.set(batch, new Set());
        pmVisibleCells.get(batch).add(orig);
        if (!pmViewportCells.has(batch)) pmViewportCells.set(batch, new Set());
        pmViewportCells.get(batch).add(orig);
        pmVisibleBatches.add(batch);
    });
}

/**
 * 位置变了以后同步 data-pos 和角标数字，只扫 [fromPos, toPos] 那一段。
 *
 * 拖拽和删除都是直接搬 DOM 节点，搬完数组下标和格子上的 data-pos 必须一致，
 * 否则下一次拖拽就会插错位置。
 *
 * 为什么要区间：「把第 3 页拖到第 40 页」真正改到的只是中间那 38 格，而原来是无条件
 * 重扫整张网——五千页就是 5000 次属性写 + 5000 次子树查找，拖一下卡一下。
 * 数字一律写 textContent：innerText 要先知道布局结果才能序列化，等于逼着每格重排一次。
 */
function pmSyncPositions(fromPos, toPos) {
    const grid = document.getElementById('pm-grid');
    if (!grid) return;
    const cells = grid.children;
    const from = Math.max(0, fromPos === undefined ? 0 : fromPos);
    const to = (toPos === undefined || toPos < 0)
        ? cells.length - 1 : Math.min(toPos, cells.length - 1);
    for (let pos = from; pos <= to; pos++) {
        const cell = cells[pos];
        if (!cell) continue;
        cell.dataset.pos = String(pos);
        const num = cell.querySelector('.pm-page-num');
        if (num) num.textContent = String(pos + 1);
    }
}

function pmPaintCell(cell) {
    if (!cell) return;
    const orig = Number(cell.dataset.orig);
    const rot = Number(cell.dataset.rot) || 0;
    const media = cell.querySelector('.pm-thumb-media');
    if (!media) return;
    const src = pmThumbSrc(orig, rot);
    const img = media.querySelector('img');
    if (!src) {
        if (img) img.remove();
        if (!media.firstElementChild) media.appendChild(pmPlaceholder());
        return;
    }
    let target = img;
    if (!target) {
        if (media.firstElementChild) media.firstElementChild.remove();
        target = document.createElement('img');
        target.alt = 'page ' + (orig + 1);
        media.appendChild(target);
    }
    if (target.dataset.src !== src) {
        target.src = src;
        target.dataset.src = src;
    }
}

function pmThumbSrc(orig, rot) {
    const base = pmImagesCache[orig];
    if (!base) return '';
    if (!rot) return base;
    // 键里必须带上底图的档位：转过的缩略图是从底图现算的，切档之后底图换了分辨率，
    // 只按 orig:rot 查就会把旧档转出来的那张当新的用（§4.12 点名的坑）。
    const key = orig + ':' + rot + ':' + (pmImagesDpi[orig] || PM_THUMB_DPI_MIN);
    if (pmRotCache.has(key)) return pmRotCache.get(key);
    pmRotateThumb(orig, rot, key);
    return '';
}

/**
 * 转过的缩略图在浏览器里画，不回头找后端。
 *
 * 后端那一侧要重开 PDF、重新渲染编码一遍；这里一次 canvas 就完事，
 * 而且转错了还能立刻撤销回原样，不用等第二次出图。
 */
function pmRotateThumb(orig, rot, key) {
    if (pmRotPending.has(key)) return;
    pmRotPending.add(key);
    const img = new Image();
    img.onload = () => {
        const swap = rot === 90 || rot === 270;
        const canvas = document.createElement('canvas');
        canvas.width = swap ? img.naturalHeight : img.naturalWidth;
        canvas.height = swap ? img.naturalWidth : img.naturalHeight;
        const ctx = canvas.getContext('2d');
        ctx.translate(canvas.width / 2, canvas.height / 2);
        ctx.rotate(rot * Math.PI / 180);
        ctx.drawImage(img, -img.naturalWidth / 2, -img.naturalHeight / 2);
        try {
            pmRotCache.set(key, canvas.toDataURL('image/png'));
        } catch (error) {
            console.error('生成旋转缩略图失败', error);
        }
        pmRotPending.delete(key);
        pmRepaintOrig(orig);
    };
    img.onerror = () => {
        pmRotPending.delete(key);
        pmRepaintOrig(orig);
    };
    img.src = pmImagesCache[orig];
}

function pmRepaintOrig(orig) {
    document.querySelectorAll(`#pm-grid .pm-thumb[data-orig="${orig}"]`)
        .forEach(cell => pmPaintCell(cell));
}

function pmBatchStart(orig) {
    return Math.floor(orig / PAGE_BATCH) * PAGE_BATCH;
}

function pmWatchThumbs() {
    if (pmThumbObserver) pmThumbObserver.disconnect();
    pmThumbObserver = new IntersectionObserver(pmOnIntersect, { root: null, rootMargin: '500px 0px' });
    // 每一页都要能被画出来，所以每一页都要被盯着，而且一直盯着不松手：
    // 谁进视口就请求它那一批，图被回收后再滚回来还会再触发一次。
    document.querySelectorAll('#pm-grid .pm-thumb')
        .forEach(cell => pmThumbObserver.observe(cell));
}

async function pmOnIntersect(entries) {
    for (const entry of entries) {
        const orig = Number(entry.target.dataset.orig);
        if (!Number.isInteger(orig) || orig < 0 || orig >= pmTotalPages) continue;
        const batch = pmBatchStart(orig);
        let seen = pmVisibleCells.get(batch);
        if (!seen) {
            seen = new Set();
            pmVisibleCells.set(batch, seen);
        }
        if (entry.isIntersecting) {
            seen.add(orig);
            const box = entry.boundingClientRect;
            pmVisibleBatches.add(batch);
            // 观察器报的「相交」是连 500px 预读边距一起算的，档位不能按这个来：
            // 预读那几批只配标清，真进了视口才值得按用户选的高档出图。
            if (box.bottom > 0 && box.top < (window.innerHeight || 800)) {
                let onScreen = pmViewportCells.get(batch);
                if (!onScreen) {
                    onScreen = new Set();
                    pmViewportCells.set(batch, onScreen);
                }
                onScreen.add(orig);
            } else {
                pmForgetViewportCell(batch, orig);
            }
            continue;
        }
        // 一批里从视野上沿退出去的那几格，不代表整批离开了屏幕：同一次回调里 true/false
        // 的先后没有保证，按「整批」删会把正在显示的那一批踢出候选，它就永远等不到图
        //（装机版实测：跳到第 1006 页时请求的是它下面那一批，这一批 11s 没出图）。
        // 数到一格不剩才删，平滑滚动路上扫过的那些批照样会退出候选。
        seen.delete(orig);
        if (seen.size === 0) {
            pmVisibleCells.delete(batch);
            pmVisibleBatches.delete(batch);
        }
        pmForgetViewportCell(batch, orig);
    }
    pmCancelStaleInflight();
    pmPumpSoon();
}

/** 这一批还有一格落在视口里吗（不含预读边距）——决定它该按哪个清晰度出图。 */
function pmBatchOnScreen(batch) {
    const onScreen = pmViewportCells.get(batch);
    return !!(onScreen && onScreen.size);
}

function pmForgetViewportCell(batch, orig) {
    const onScreen = pmViewportCells.get(batch);
    if (!onScreen) return;
    onScreen.delete(orig);
    if (onScreen.size === 0) pmViewportCells.delete(batch);
}

/** 这一批该用的清晰度：只有真在视口里的批才升到用户选的档，预读的一律标清（F25）。 */
function pmBatchWantedDpi(batch) {
    if (pmThumbDpi === PM_THUMB_DPI_MIN) return PM_THUMB_DPI_MIN;
    return pmBatchOnScreen(batch) ? pmThumbDpi : PM_THUMB_DPI_MIN;
}

/**
 * 一批切几段、每段几页（§4.12 点名的「网格图走桥要按档限流」）。
 *
 * 渲染耗时跟着像素面积走：128dpi 每页像素是标清的 4 倍，本机实测每页 67ms 对 17ms，
 * 一批 24 页整取要 1.8s 才见第一张图，用户点完「高清」看到的是整个界面卡住。
 * 高清档先小口取 8 页把屏幕上半部分点亮，剩下的一次取完——每多一趟桥后端就多开一次文档
 * （实测约 0.14s），切成三段不值当。
 *
 * 标清原本一律整批一次取，因为文字页一批才 170KB，多切只是多走一趟桥。扫描件不适用：
 * 一页 JPEG 就有 66KB，一批 1.6MB，而桥的耗时按字节是**超线性**的（同一份 300 页扫描件
 * 实测：整批 24 页 4.5s，切两段各 12 页 1.2s + 1.2s）。所以标清只在「这一份文档的缩略图
 * 确实很大」时改小口取，判据用上一段真正到手的字节数（pmThumbBytesHeavy），文字页不受影响。
 */
function pmBatchSlices(dpi, size) {
    if (size <= 8) return [size];
    if (dpi <= PM_THUMB_DPI_MIN) {
        if (!pmThumbBytesHeavy || size <= 12) return [size];
        return [12, size - 12];
    }
    return [8, size - 8];
}

/**
 * 把正在等后端返回的那一批作废掉（F07）。
 *
 * 判定只看「这批已经整个离开屏幕（连预读边距一起出去）」：还在屏里就不能撤，
 * 撤了用户眼前会留下一格永远空着的占位。批记录一撤，图回来时对不上代际号就被丢弃，
 * 既不写缓存也不重排 DOM；之后用户真滚回来，这批不在记录里，会正常重新请求。
 */
function pmCancelStaleInflight() {
    if (pmInflightBatch < 0) return;
    if (pmVisibleBatches.has(pmInflightBatch)) return;
    const batch = pmInflightBatch;
    pmInflightBatch = -1;
    pmRequestGen++;
    pmRequestedBatches.delete(batch);
    pmBatchDpi.delete(batch);
}

/**
 * 格子序号按「原页码」建索引：拖过顺序之后 orig 顺序和格子顺序会不一致，只能现问 DOM。
 * 挑「此刻最近的那一批」和回收远处那几批都要用同一套账，所以抽出来共用。
 */
function pmPosByOrig(grid) {
    const posByOrig = [];
    Array.from(grid.children).forEach((cell, pos) => {
        const orig = Number(cell.dataset.orig);
        if (Number.isInteger(orig)) posByOrig[orig] = pos;
    });
    return posByOrig;
}

/**
 * 这一批离屏幕中线最近还差几像素；整批在格子里找不到（拖没了）返回 null。
 *
 * 必须现问 rect，不能用观察器早前存下的距离：观察器只在「相交状态变化」那一帧回报，
 * 平滑滚动路过的那几批都在屏幕中央留过一个极小的旧值，之后再怎么重新排队都排不过
 * 落地那一屏。300 页扫描件实测：跳到末页时目标那批排在第三趟，等图 16.5s。
 */
function pmBatchScreenDist(grid, posByOrig, batch) {
    const mid = (window.innerHeight || 800) / 2;
    let best = null;
    for (let i = batch; i < batch + PAGE_BATCH; i++) {
        const pos = posByOrig[i];
        if (pos === undefined) continue;
        const cell = grid.children[pos];
        if (!cell) continue;
        const box = cell.getBoundingClientRect();
        const d = Math.abs(box.top + box.height / 2 - mid);
        if (best === null || d < best) best = d;
    }
    return best;
}

/**
 * 候选里「最该先画」的那一批：先比谁铺满屏幕的格子多，一样多再比谁离中线近；没有就返回 null。
 *
 * pmVisibleBatches 只当「哪些批还在视野里」的候选集，远近一律按当下格子位置现算。
 *
 * 为什么先比格子数而不是只比距离：网格一行四格，居中某一格时，同一行左右那几格属于
 * 相邻的另一批，两批到屏幕中线的距离**一样小**。只比距离就得由集合的插入顺序决定谁先，
 * 1200 页实测跳到第 601 页时先取走了「只露三格」的上面那批（[576,600)），
 * 真在眼前的 12 格排在后面，一批 1.3s 就这么多等了一趟桥（B5 因此 4.2s）。
 */
function pmNearestWanted() {
    const grid = document.getElementById('pm-grid');
    if (!grid) return null;
    const posByOrig = pmPosByOrig(grid);
    let best = null;
    for (const batch of pmVisibleBatches) {
        // 在途的批不当候选：它已经在等后端了，再排一次就是同一批请求两遍
        if (pmPendingBatches.has(batch)) continue;
        if (pmRequestedBatches.has(batch) && pmBatchDpi.get(batch) === pmBatchWantedDpi(batch)) continue;
        const dist = pmBatchScreenDist(grid, posByOrig, batch);
        if (dist === null) continue;
        // 只在视口里数到的格子才算覆盖量：预读边距里那几批是 0，自然排到最后
        const onScreen = pmViewportCells.get(batch);
        const cover = onScreen ? onScreen.size : 0;
        if (best === null || cover > best.cover || (cover === best.cover && dist < best.dist)) {
            best = {batch, dist, cover};
        }
    }
    return best === null ? null : best.batch;
}

/**
 * 全局只有一个取图动作在跑，每画完一批都重新挑「此刻离屏幕最近的那一批」。
 *
 * .main 是 scroll-behavior: smooth，一次跳几百页会平滑扫过中间的几十批格子；
 * 若像以前那样每个回调各自排一串请求，跳一次页就真要渲染十几批（实测 12 批 / 4.3s），
 * 用户等的是刚跳到的那一页，中间那些根本一眼带过。
 */
async function pmPump() {
    if (pmPumping) {
        pmPumpWake = true;
        return;
    }
    pmPumping = true;
    try {
        let batch = pmNearestWanted();
        while (batch !== null) {
            await pmEnsureBatch(batch);
            batch = pmNearestWanted();
        }
    } finally {
        pmPumping = false;
        // 等后端的那几秒里用户又跳走了：观察器那会儿叫醒不了泵，现在补一次。
        // 挑的是「此刻」离中心最近的那一批，已经画好的不会被重复取。
        if (pmPumpWake) {
            pmPumpWake = false;
            pmPumpSoon();
        }
    }
}

/**
 * 等视野停住再挑「此刻离屏幕最近的那一批」（快跳不渲染路过的那几屏）。
 *
 * 一次跳几百页时 .main 是平滑滚动，路上每个观察器回调都会叫醒泵，泵就照着
 * 「当下最近的那批」一批一批取图——用户真正等的第 120 页要排在路过的那五批后面。
 * 300 页扫描件实测每一批（后端 1.25s + 走桥 1.5s）要 2.5~14s，跳一屏因此变成 7~43s。
 * 改成停 140ms 才开工：中途那几批一张都不渲染，落地那一屏第一批直接到场。
 * 上限 700ms 是给用户一直滚的情况兜底——一直滚也每 700ms 出一屏，不至于全程空白。
 */
function pmPumpSoon() {
    const now = performance.now();
    if (pmPumpTimer) clearTimeout(pmPumpTimer);
    else pmPumpSince = now;
    if (pmPumpTimer === null || now - pmPumpSince >= PM_PUMP_MAX_WAIT_MS) {
        pmPumpTimer = null;
        pmPump();
        return;
    }
    pmPumpTimer = setTimeout(() => {
        pmPumpTimer = null;
        pmPump();
    }, PM_PUMP_QUIET_MS);
}

/**
 * 远处那批缩略图交出去：图片、旋转后的那一份、批记录一起清掉。
 *
 * 一次留住 8 批（192 页）远超一屏能看到的量，滚回去时观察器会再报一次、那批重新出图；
 * 不这么做的话三千页的书会把上百 MB 的 base64 全攥在页面里。
 */
function pmEvictFarBatches() {
    if (pmRequestedBatches.size <= PM_THUMB_KEEP_BATCHES || !pmThumbObserver) return;
    const grid = document.getElementById('pm-grid');
    if (!grid) return;
    const posByOrig = pmPosByOrig(grid);
    // 回收对象只看「已经滚出屏幕（连预读边距一起出去）而且图也不在请求路上」的那几批：
    // 正在请求中的批被踢出批记录后，图回来时既没人再清它，也再也排不进最近 8 批，
    // 就成了越滚越大的孤儿图；屏幕里那几批被回收则会留下永远空着的格子。
    // 找不着格子的批（拖没了）按最远处理，先把它交出去。
    const ordered = [...pmRequestedBatches]
        .filter(batch => !pmPendingBatches.has(batch) && !pmVisibleBatches.has(batch))
        .map(batch => {
            const near = pmBatchScreenDist(grid, posByOrig, batch);
            return {batch, dist: near === null ? Number.MAX_SAFE_INTEGER : near};
        })
        .sort((a, b) => a.dist - b.dist)
        .map(item => item.batch);
    for (const batch of ordered.slice(PM_THUMB_KEEP_BATCHES)) {
        pmRequestedBatches.delete(batch);
        pmBatchDpi.delete(batch);
        for (let i = batch; i < batch + PAGE_BATCH && i < pmTotalPages; i++) {
            if (pmImagesCache[i]) pmDropCachedThumb(i);
        }
    }
    pmUpdateStatus();
}

/**
 * 收回去一页的缩略图：底图、档位、转过的那几份、格子一起清。
 *
 * 只清图片不够——批记录是回收的唯一线索，一页的图脱离了批记录就再没人管它，
 * 攒着攒着就成了「滚得越久内存越大」的那个漏。
 */
function pmDropCachedThumb(orig) {
    pmImagesCache[orig] = null;
    pmImagesDpi[orig] = 0;
    for (const key of [...pmRotCache.keys()]) {
        // 键是 orig:rot:dpi，第一段才是页码
        if (Number(key.split(':')[0]) === orig) pmRotCache.delete(key);
    }
    pmRepaintOrig(orig);
}

async function pmEnsureBatch(start) {
    const path = selectedFiles['pm'];
    const batchStart = pmBatchStart(start);
    const dpi = pmBatchWantedDpi(batchStart);
    if (!path || pmTotalPages <= 0 || batchStart >= pmTotalPages ||
        pmPendingBatches.has(batchStart)) return;
    // 同批同档才算「已经有了」；换了档这一批必须重新出一遍图，不然切档只是切了个名字
    if (pmRequestedBatches.has(batchStart) && pmBatchDpi.get(batchStart) === dpi) return;
    pmRequestedBatches.add(batchStart);
    pmPendingBatches.add(batchStart);
    const gen = pmRequestGen;
    pmInflightBatch = batchStart;
    const size = Math.min(PAGE_BATCH, pmTotalPages - batchStart);
    let filled = 0;
    let failed = false;
    try {
        // 分段取、分段画：高清晰度下每段都当场把格子填满，用户看着是「从上往下长出来」，
        // 不是盯着一条空白等整批
        for (const span of pmBatchSlices(dpi, size)) {
            const take = Math.min(span, size - filled);
            if (take <= 0) break;
            pmRenderCalls++;
            const part = await callApi('render_page_range', path, batchStart + filled, take, dpi);
            if (!Array.isArray(part) || part.length === 0) {
                failed = true;
                break;
            }
            // 代际对不上＝等图的这几秒里用户又跳走了；批记录被回收＝这一批已经不属于眼前。
            // 两种情况画了一半的格子都要原样还回去：批记录没了，这些图之后没人再清。
            if (gen !== pmRequestGen || !pmRequestedBatches.has(batchStart)) break;
            // 缓存按原始页码存放，与当前显示顺序无关，所以拖拽、旋转之后都不必重新要图
            // 多回来的几项按超出这一批处理，直接丢掉：filled 越过 size 就没法再判断取没取满
            part.slice(0, take).forEach((src, k) => {
                const orig = batchStart + filled + k;
                if (src && !pmEverDrawn.has(orig)) pmEverDrawn.add(orig);
                pmApplyThumbImage(orig, src, dpi);
            });
            filled += Math.min(part.length, take);
            // 按这一段真正到手的平均字节数决定下一批怎么切（扫描件整批 1.6MB 过桥，
            // 实测比拆两段取慢一倍；文字页 170KB 整批取最划算）。少于 4 页的样本不作数，
            // 免得文档末尾那一小批把口径翻过去。
            if (part.length >= 4) {
                const avg = part.reduce((a, s) => a + (s ? s.length : 0), 0) / part.length;
                pmThumbBytesHeavy = avg > PM_THUMB_HEAVY_PAGE_BYTES;
            }
            pmUpdateStatus();
        }
    } finally {
        pmPendingBatches.delete(batchStart);
        if (pmInflightBatch === batchStart) pmInflightBatch = -1;
    }
    if (filled < size) {
        pmCancelledBatches++;
        // 画了一半的格子先收回去：这一批的档位没记上，之后重新取图是整批重来，
        // 留着旧图就会跟新档的图错位在同一个格子里。
        for (let i = batchStart; i < batchStart + filled; i++) pmDropCachedThumb(i);
        pmRequestedBatches.delete(batchStart);
        pmBatchDpi.delete(batchStart);
        // 只有后端真的取失败才退出候选：那种情况不退出，取图循环会对着同一批失败请求转圈。
        // 用户跳走导致的作废不能退出——这一批可能还在屏幕上，退出就留下永远空着的格子。
        if (failed) pmVisibleBatches.delete(batchStart);
        return;
    }
    pmAppliedBatches++;
    pmBatchDpi.set(batchStart, dpi);
    pmUpdateStatus();
    pmEvictFarBatches();
}

function pmApplyThumbImage(orig, src, dpi) {
    if (orig >= pmTotalPages) return;
    pmImagesCache[orig] = src;
    pmImagesDpi[orig] = dpi || PM_THUMB_DPI_MIN;
    pmRepaintOrig(orig);
}

// ---------- 清晰度档位（F25）----------
/** 只认这三档；配置里是别的值（或读不到）都退回标清，别拿一个不认识的 dpi 去出图。 */
function pmNormalizeDpi(value) {
    const dpi = Number(value);
    return PM_THUMB_DPI_OPTIONS.includes(dpi) ? dpi : PM_THUMB_DPI_MIN;
}

/** 把档位同时写到状态和工具条上：控件和实际出图用的值必须是一个。 */
function pmSetThumbDpi(dpi) {
    pmThumbDpi = pmNormalizeDpi(dpi);
    const sel = document.getElementById('pm-dpi');
    if (sel) sel.value = String(pmThumbDpi);
    return pmThumbDpi;
}

/** 从配置读缩略图档位。 */
async function pmLoadThumbDpi() {
    const config = await callApi('load_config');
    return pmSetThumbDpi((config || {}).thumb_dpi);
}

/** 走既有的 save_config 通道存 thumb_dpi，不新增桥方法。 */
async function pmStoreThumbDpi(dpi) {
    const config = await callApi('load_config');
    if (!config || typeof config !== 'object') return false;
    config.thumb_dpi = dpi;
    return await callApi('save_config', config);
}

/** 工具条上换了档位：记进配置，并把此刻在屏里的那几批按新档重取一次。 */
async function pmThumbDpiChanged() {
    const sel = document.getElementById('pm-dpi');
    const dpi = pmNormalizeDpi(sel && sel.value);
    pmThumbDpi = dpi;
    await pmStoreThumbDpi(dpi);
    if (!pmTotalPages) return;
    // 撤掉屏内批的档记录，取图循环自己会把它们重新排队；只在预读边距里的批不动，
    // 省得为几格还没露出来的页白出一次高档图。
    for (const batch of [...pmVisibleBatches]) {
        if (!pmBatchOnScreen(batch)) continue;
        pmRequestedBatches.delete(batch);
        pmBatchDpi.delete(batch);
    }
    pmPump();
}

// ---------- 选中 ----------
/**
 * 选中态一律「按新的那一组重画」。
 *
 * 这里原来是给每个选中的 id 各跑一次 `querySelector('[data-id=…]')`，而那一次查找
 * 要把整张网扫一遍——五千页按「全选」就是五千趟整表遍历，实测 2083 ms
 * （tests/out/smooth_probe.py）。改成只顺着一趟 children 判「这个 id 在不在集合里」，
 * 同一件事变成 O(N) 而不是 O(N×选中数)，全选从 2.1 s 落到几十毫秒。
 */
function pmSetSelection(ids) {
    pmSelectedIds = new Set(ids);
    const grid = document.getElementById('pm-grid');
    if (grid) {
        for (const cell of grid.children) {
            cell.classList.toggle('selected', pmSelectedIds.has(Number(cell.dataset.id)));
        }
    }
    pmUpdateStatus();
    pmSyncToolbar();
}

function pmToggleSelect(id) {
    const cell = pmCell(id);
    if (!cell) return;
    if (cell.dataset.justDragged === '1') {
        delete cell.dataset.justDragged;
        return;
    }
    if (pmSelectedIds.has(id)) pmSelectedIds.delete(id);
    else pmSelectedIds.add(id);
    // 只改这一个元素：整表重建会把已渲染好的缩略图全部销毁重来，千页时明显卡顿
    cell.classList.toggle('selected', pmSelectedIds.has(id));
    pmUpdateStatus();
    pmSyncToolbar();
}

// ---------- 暂存改动 ----------
/**
 * 压一步撤销快照，顺带记下「这一步改了什么」。
 *
 * 快照给撤销栈用（内存里，退出就没了），操作序列给草稿用（落盘，只有几十 KB）。
 * 分两份是有意为之：三千页 × 三十步的全表快照有几十 MB，写进草稿等于把内存问题
 * 搬到磁盘上；而只存操作序列的话，恢复时还得能重放出撤销栈的深度（见 pmReplayDraft）。
 */
function pmSnapshot(op) {
    pmHistory.push({ slots: pmSlots.map(slot => ({ ...slot })), selected: new Set(pmSelectedIds), op: op || null });
    if (pmHistory.length > PM_HISTORY_MAX) pmHistory.shift();
}

/** 选中格在「此刻网格顺序」里的下标：操作序列按位置记，重放时才走得通。 */
function pmSelectedPositions() {
    const positions = [];
    pmSlots.forEach((slot, pos) => { if (pmSelectedIds.has(slot.id)) positions.push(pos); });
    return positions;
}

function pmHasChanges() {
    if (pmSlots.length !== pmTotalPages) return true;
    return pmSlots.some((slot, i) => slot.orig !== i || slot.rot !== 0);
}

/** 每改一笔都重算「待保存」提示和按钮可用性，用户随时知道有没有落盘。 */
function pmMarkDirty() {
    pmDirty = pmHasChanges();
    pmUpdateStatus();
    pmSyncToolbar();
    // 改动只暂存在内存里，那就要防「没保存就崩了」：停手一会儿自动落一份草稿（F08）
    pmScheduleDraft();
}

function pmSyncToolbar() {
    const hasFile = pmSlots.length > 0;
    // 工具条在「排版与整理 → 页面管理」那一格里，选择器按格子来，不再认整页
    document.querySelectorAll('#pane-layout-pm [data-pm-needs-file]')
        .forEach(btn => { btn.disabled = !hasFile; });
    document.querySelectorAll('#pane-layout-pm [data-pm-needs-selection]')
        .forEach(btn => { btn.disabled = pmSelectedIds.size === 0; });
    document.querySelectorAll('#pane-layout-pm [data-pm-needs-undo]')
        .forEach(btn => { btn.disabled = pmHistory.length === 0; });
    document.querySelectorAll('#pane-layout-pm [data-pm-needs-dirty]')
        .forEach(btn => { btn.disabled = !pmDirty; });
}

function pmRotateSelected(delta) {
    if (!pmSelectedIds.size) { showToast(uiText('dyn_1055', '请先点击缩略图选中页面')); return; }
    pmSnapshot({ op: 'rotate', pages: pmSelectedPositions(), delta: delta });
    pmSlots.forEach(slot => {
        if (!pmSelectedIds.has(slot.id)) return;
        slot.rot = (slot.rot + delta + 360) % 360;
        const cell = pmCell(slot.id);
        if (!cell) return;
        cell.dataset.rot = String(slot.rot);
        pmSetRotBadge(cell, slot.rot);
        pmPaintCell(cell);
    });
    pmMarkDirty();
}

function pmSetRotBadge(cell, rot) {
    const old = cell.querySelector('.pm-rot-badge');
    if (old) old.remove();
    if (!rot) return;
    const badge = pmEl('span', 'pm-rot-badge', pmRotLabel(rot));
    const hint = cell.querySelector('.pm-drag-hint');
    cell.insertBefore(badge, hint || null);
}

function pmDeleteSelected() {
    if (!pmSelectedIds.size) { showToast(uiText('dyn_1055', '请先点击缩略图选中页面')); return; }
    if (pmSelectedIds.size >= pmSlots.length) {
        showToast(uiText('dyn_1027', '不能删除全部页面，至少要保留一页'));
        return;
    }
    const removedAt = pmSelectedPositions();
    pmSnapshot({ op: 'delete', pages: removedAt });
    const doomed = pmSelectedIds;
    pmSlots = pmSlots.filter(slot => !doomed.has(slot.id));
    doomed.forEach(id => {
        const cell = pmCell(id);
        if (cell) cell.remove();
    });
    pmSelectedIds = new Set();
    // 摘掉之后只有「第一颗被删的那格」往后要重新编号：删末尾几页不必扫全表
    pmSyncPositions(removedAt.length ? removedAt[0] : 0);
    pmMarkDirty();
}

/** 「提取选中」在这里等于「只留下选中的页」，和其他操作一样只是暂存。 */
function pmExtractSelected() {
    if (!pmSelectedIds.size) { showToast(uiText('dyn_1055', '请先点击缩略图选中页面')); return; }
    if (pmSelectedIds.size === pmSlots.length) { showToast(uiText('dyn_1029', '已经全部选中，无需提取')); return; }
    pmSnapshot({ op: 'extract', pages: pmSelectedPositions() });
    const dropped = pmSlots.filter(slot => !pmSelectedIds.has(slot.id));
    pmSlots = pmSlots.filter(slot => pmSelectedIds.has(slot.id));
    dropped.forEach(slot => {
        const cell = pmCell(slot.id);
        if (cell) cell.remove();
    });
    pmSelectedIds = new Set();
    pmSyncPositions();
    pmMarkDirty();
}

function pmUndo() {
    const last = pmHistory.pop();
    if (!last) { showToast(uiText('dyn_1030', '没有可撤销的改动')); return; }
    pmSlots = last.slots;
    pmSelectedIds = last.selected;
    // 撤销可能把删掉的页面加回来、又打乱位置，格子对不上就整表重建一次
    pmRenderGrid();
    pmDirty = pmHasChanges();
    pmUpdateStatus();
    pmSyncToolbar();
    pmScheduleDraft();
}

function pmResetEdits() {
    if (!pmTotalPages) return;
    if (!pmDirty) { showToast(uiText('dyn_1047', '没有待保存的改动')); return; }
    showMessageDialog(uiText('dyn_1032', '放弃全部改动？'),
        uiText('dyn_1033', '暂存的顺序、旋转和删除都会回到刚打开这份文件时的样子；已经保存过的文件不受影响。'),
        [
            { label: uiText('dyn_1034', '再想想'), primary: false },
            { label: uiText('dyn_1039', '放弃改动'), primary: true, onClick: pmApplyReset }
        ]);
}

/**
 * 换文件前的确认。弹窗只有按钮能关掉（点遮罩、按 Esc 都不会关闭），
 * 所以这个 Promise 一定会被 resolve，不会把 selectPdf 卡死。
 */
function pmConfirmDiscard() {
    return new Promise(resolve => {
        showMessageDialog(uiText('dyn_1036', '有未保存的改动'),
            uiText('dyn_1037', '现在换文件会丢掉暂存的顺序、旋转和删除，已经保存过的文件不受影响。'),
            [
                { label: uiText('dyn_1038', '继续编辑'), primary: false, onClick: () => resolve(false) },
                { label: uiText('dyn_1039', '放弃改动'), primary: true, onClick: () => resolve(true) }
            ]);
    });
}

function pmApplyReset() {
    pmHistory = [];
    pmSelectedIds = new Set();
    pmSlots = [];
    for (let i = 0; i < pmTotalPages; i++) pmSlots.push({ id: ++pmSlotSeq, orig: i, rot: 0 });
    pmRenderGrid();
    pmDirty = false;
    pmUpdateStatus();
    pmSyncToolbar();
    // 已经没有改动了，草稿也就该跟着撤掉（pmFlushDraft 见到干净状态会去 clear_page_draft）
    pmScheduleDraft();
    showToast(uiText('dyn_1040', '已恢复到打开时的样子'), 2600);
}

// ---------- 草稿（F08）：改动还没保存就崩了 / 关了，下次能接上 ----------
// 存进去的只有「打算怎么改」：页序、增量旋转、做过哪几笔、选了哪几格。
// 缩略图一张都不落盘——1200 页会话的图有几百 MB，那等于把内存问题搬到磁盘上；
// 恢复后按现有出图管线重新画一遍，代价是几秒，换来的是草稿永远和源文件对得上。
// 保存成品这条路一个字都不改：仍然只有「保存为新文件」会调 apply_page_edits。
const PM_DRAFT_DEBOUNCE_MS = 800;

/** 拼一份草稿的内容。selected 存原始页号：格子 id 是每个会话自己发的，换一次会话就不认了。 */
function pmDraftPayload() {
    return {
        slots: pmSlots.map(slot => ({ orig: slot.orig, rot: slot.rot })),
        // 只要有一笔快照没记下操作（比如上一次恢复失败留下的保底快照），整条操作序列就不存了：
        // 存半截会让恢复时的重放对不上号，宁可只还原改动、不还原撤销深度。
        history: pmHistory.every(entry => entry.op) ? pmHistory.map(entry => entry.op) : [],
        selected: pmSlots.filter(slot => pmSelectedIds.has(slot.id)).map(slot => slot.orig),
        dirty: pmDirty,
    };
}

/** 改动之后防抖落盘：用户手不停就一笔都不写，停手 0.8 秒才写一次。 */
function pmScheduleDraft() {
    if (!pmTotalPages || !selectedFiles['pm']) return;
    if (pmDraftTimer) clearTimeout(pmDraftTimer);
    pmDraftTimer = setTimeout(() => {
        pmDraftTimer = null;
        pmFlushDraft();
    }, PM_DRAFT_DEBOUNCE_MS);
}

/**
 * 立刻落一次盘（防抖到期、或用户放弃改动/保存成功时撤掉草稿）。
 *
 * 没有改动就不该留草稿：留着的话下次打开还会提示「有未保存改动」，那才是脏数据。
 */
async function pmFlushDraft() {
    const pdf = selectedFiles['pm'];
    if (!pdf || !pmTotalPages) return;
    if (pmHasChanges()) {
        const res = await callApi('save_page_draft', pdf, pmDraftPayload());
        if (res && res.success) pmDraftKey = res.key || pmDraftKey;
        return;
    }
    await pmDropDraft();
}

/** 撤掉当前文件的草稿：保存成功后必须调，此时页码已按新文件重编号，旧草稿是脏数据。 */
async function pmDropDraft() {
    if (pmDraftTimer) { clearTimeout(pmDraftTimer); pmDraftTimer = null; }
    pmDraftCache = null;
    pmHideDraftBar();
    if (!pmDraftKey) return;
    const key = pmDraftKey;
    pmDraftKey = '';
    await callApi('clear_page_draft', key);
}

/** 比较两个路径是不是同一份文件：Windows 的大小写和分隔符不该算两份。 */
function pmSamePath(left, right) {
    const norm = value => String(value || '').replace(/\\/g, '/').toLowerCase();
    return !!norm(left) && norm(left) === norm(right);
}

function pmDraftTimeLabel(ts) {
    if (!ts) return '';
    const date = new Date(ts * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
        + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 打开文件后问一句：这份文件有没有留下的草稿。
 *
 * 提示做成网格上方的横幅而不是弹窗：弹窗会压住整个界面，验收脚本和「先看看再说」的用户
 * 都被它挡住；横幅不动网格，用户不理会也不影响继续编辑。
 */
async function pmCheckDraft() {
    pmDraftCache = null;
    pmHideDraftBar();
    const pdf = selectedFiles['pm'];
    if (!pdf || !pmTotalPages || pmHasChanges()) return;
    const list = await callApi('list_page_drafts');
    if (!Array.isArray(list) || !list.length) return;
    const hit = list.find(item => pmSamePath(item.source, pdf) && item.slot_count);
    if (!hit) return;
    const res = await callApi('load_page_draft', hit.key);
    if (!res || !res.found || !res.draft) return;
    pmDraftCache = res;
    pmDraftKey = res.key || '';
    const slots = Array.isArray(res.draft.slots) ? res.draft.slots : [];
    const rotated = slots.filter(item => item && item.rot).length;
    const when = pmDraftTimeLabel(res.draft.saved_at);
    if (!res.restorable) {
        pmShowDraftBar(uiTextf('dyn_1041', '⚠ 这份文件有 {0} 格的草稿，但{1}，不能安全套用', slots.length, res.message || uiText('dyn_1120', '和当前文件对不上')), false);
        return;
    }
    pmShowDraftBar(uiTextf('dyn_1042', '⚠ 这份文件有未保存的改动：{0} 格{1}{2}，要接上吗？', slots.length, (rotated ? uiTextf('dyn_1423', ' / 其中旋转 {0} 页', rotated) : ''), (when ? ` · ${when}` : '')), true);
}

function pmShowDraftBar(text, canRestore) {
    const bar = document.getElementById('pm-draft');
    if (!bar) return;
    document.getElementById('pm-draft-text').innerText = text;
    document.getElementById('pm-draft-restore').classList.toggle('hidden', !canRestore);
    bar.classList.remove('hidden');
}

function pmHideDraftBar() {
    const bar = document.getElementById('pm-draft');
    if (bar) bar.classList.add('hidden');
}

/** 把一笔操作作用在「重放中的页序」上；只碰 orig/rot，格子 id 由外面统一发。 */
function pmApplyOpToWork(work, op) {
    if (!op || (op.op !== 'move' && !Array.isArray(op.pages))) return false;
    const positions = new Set((op.pages || []).map(Number));
    if (op.op === 'rotate') {
        const delta = Number(op.delta) || 0;
        for (const pos of positions) {
            const item = work[pos];
            if (!item) return false;
            item.rot = (item.rot + delta + 360) % 360;
        }
        return true;
    }
    if (op.op === 'delete' || op.op === 'extract') {
        // 删除是「这些位置不要了」，提取是「只留下这些位置」，同一份位置两种取法
        const keep = [];
        work.forEach((item, pos) => {
            const wanted = op.op === 'extract' ? positions.has(pos) : !positions.has(pos);
            if (wanted) keep.push(item);
        });
        if (!keep.length) return false;
        work.length = 0;
        work.push(...keep);
        return true;
    }
    if (op.op === 'move') {
        const from = Number(op.from);
        const to = Number(op.to);
        if (from < 0 || from >= work.length || to < 0 || to >= work.length) return false;
        const [moved] = work.splice(from, 1);
        work.splice(to, 0, moved);
        return true;
    }
    return false;
}

function pmSameOrder(work, slots) {
    if (work.length !== slots.length) return false;
    return work.every((item, pos) => item.orig === slots[pos].orig && item.rot === slots[pos].rot);
}

/**
 * 用草稿里的操作序列把撤销栈一层层搭回来。
 *
 * 草稿存的是操作序列而不是每次的全表快照（三千页 × 三十步有几十 MB），
 * 所以恢复时从「刚打开的样子」出发按顺序重放，每重放一笔前把那一笔之前的状态
 * 压进撤销栈——用户按 Ctrl+Z 就能一笔笔退回去。
 * 重放结果和草稿的最终页序对不上时（草稿来自别的版本、或中途文件被换过），
 * 只留一个「退回到打开时」的保底快照：改动照样还原，撤销深度不硬编。
 */
function pmApplyDraftHistory(draft, idByOrig) {
    const ops = Array.isArray(draft.history) ? draft.history : [];
    const work = [];
    for (let i = 0; i < pmTotalPages; i++) work.push({ orig: i, rot: 0 });
    const pristine = work.map(item => ({ ...item }));
    const entries = [];
    for (const op of ops) {
        entries.push({
            slots: work.map(item => ({ id: idByOrig[item.orig], orig: item.orig, rot: item.rot })),
            selected: new Set(),
            op: op,
        });
        if (!pmApplyOpToWork(work, op)) {
            pmHistory = [{ slots: pristine.map(item => ({ ...item, id: idByOrig[item.orig] })),
                selected: new Set(), op: null }];
            return;
        }
    }
    if (!pmSameOrder(work, pmSlots)) {
        pmHistory = [{ slots: pristine.map(item => ({ ...item, id: idByOrig[item.orig] })),
            selected: new Set(), op: null }];
        return;
    }
    pmHistory = entries.slice(Math.max(0, entries.length - PM_HISTORY_MAX));
}

/** 点横幅上的「恢复改动」：把草稿的页序、旋转、选中和撤销栈一起接回来。 */
function pmRestoreDraft() {
    const res = pmDraftCache;
    if (!res || !res.restorable || !pmTotalPages) return;
    const slots = (res.draft && res.draft.slots) || [];
    if (!slots.length) { pmHideDraftBar(); return; }
    // 格子 id 先按原始页号统一发一遍：草稿里没有 id，也不该有——它是上一个会话的编号
    const idByOrig = [];
    for (let i = 0; i < pmTotalPages; i++) idByOrig[i] = ++pmSlotSeq;
    pmSlots = slots.map(item => ({ id: idByOrig[item.orig], orig: item.orig, rot: item.rot || 0 }));
    pmApplyDraftHistory(res.draft, idByOrig);
    const live = new Set(pmSlots.map(slot => slot.orig));
    const selected = Array.isArray(res.draft.selected) ? res.draft.selected : [];
    pmSelectedIds = new Set(selected.filter(orig => live.has(orig)).map(orig => idByOrig[orig]));
    pmRenderGrid();
    pmDirty = pmHasChanges();
    pmUpdateStatus();
    pmSyncToolbar();
    pmHideDraftBar();
    showToast(uiText('dyn_1043', '已接上上次未保存的改动，可以继续编辑或直接保存'), 3400);
}

/** 点横幅上的「丢弃」：草稿删掉，下次打开同一份文件就不再提示。 */
function pmDismissDraft() {
    pmDropDraft();
}

// ---------- 一次保存 ----------
function pmBaseName(path) {
    return String(path || '').split(/[\\/]/).pop() || 'output.pdf';
}

function pmSuggestedName(path) {
    return pmBaseName(path).replace(/\.pdf$/i, '') + uiText('dyn_1044', '_编辑后.pdf');
}

async function pmSave() {
    const pdf = selectedFiles['pm'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    if (!pmSlots.length) { showToast(uiText('dyn_1046', '没有可保存的页面')); return; }
    if (!pmDirty) { showToast(uiText('dyn_1047', '没有待保存的改动')); return; }

    const out = await callApi('save_file_dialog', pmSuggestedName(pdf));
    if (!out || typeof out !== 'string') return;

    const slots = pmSlots.map(slot => ({ page: slot.orig + 1, rotation: slot.rot }));
    const res = await callApi('apply_page_edits', pdf, out, slots);
    if (!res || !res.success) {
        reportFailure(res, document.getElementById('pm-status'), uiText('dyn_1048', '保存失败'));
        return;
    }
    // 成品已经写出去了，草稿必须当场撤掉：此刻页序按新文件重编号，留着就是脏数据
    await pmDropDraft();
    await pmAdoptSaved(out, res);
}

/**
 * 保存成功后切到成品文件。
 *
 * 用户要亲眼看到改动落地，后续操作也该接在这个文件上；继续停在旧文件会让
 * 「已保存」和界面不一致，下一次保存就把刚做的改动又写回原样了。
 */
async function pmAdoptSaved(path, res) {
    selectedFiles['pm'] = path;
    await loadPMPreview(path);
    pmStatusNotice = uiTextf('dyn_1049', '✓ 已保存 {0} 页', res.pages);
    pmUpdateStatus();
    showToast(uiTextf('dyn_1050', '已保存为 {0}', pmBaseName(path)), 3400);
    await callApi('add_recent_file', path);
}

// ---------- 右键菜单 ----------
function pmOpenMenu(cell, x, y) {
    const id = Number(cell.dataset.id);
    // 右键没选中的格子时只针对这一页；右键已选中的格子时对整组生效
    if (!pmSelectedIds.has(id)) pmSetSelection([id]);
    const count = pmSelectedIds.size;
    document.getElementById('pm-menu-title').innerText = count > 1
        ? uiTextf('dyn_1051', '已选中 {0} 页', count)
        : uiTextf('dyn_1052', '第 {0} 页（原第 {1} 页）', Number(cell.dataset.pos) + 1, Number(cell.dataset.orig) + 1);
    const menu = document.getElementById('pm-menu');
    menu.classList.remove('hidden');
    // 先亮出来再量尺寸：藏着的浮层量到的是 0x0，贴边时就会算错位置
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 4)) + 'px';
    menu.style.top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 4)) + 'px';
}

function pmCloseMenu() {
    const menu = document.getElementById('pm-menu');
    if (menu) menu.classList.add('hidden');
}

function pmMenuAction(action) {
    pmCloseMenu();
    if (!pmSelectedIds.size) return;
    if (action === 'ccw') pmRotateSelected(-90);
    else if (action === 'cw') pmRotateSelected(90);
    else if (action === 'delete') pmDeleteSelected();
    else if (action === 'zoom') pmZoomSelected();
}

/**
 * 打开高清浏览窗（F24）：从指定那一格开始，可以连着往后翻。
 *
 * 以前一次只能看一页，多选时还得先取消选择——用户想连着核对几页就得反复点。
 * 现在把网格当前的顺序和每格的暂存角度一起交给后端：翻页翻的是「这份文件打算
 * 长什么样」，不是原始页序；窗口仍然复用同一个实例，不叠窗。
 * 窗口里的左转/右转只转预览（和缩放、适应窗口一样是看的手段），不写进暂存改动——
 * 待保存的旋转只从网格这边改，两处都能改就会出现「角标和成品不一致」。
 */
async function pmZoomFrom(id) {
    const pdf = selectedFiles['pm'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    if (!pmSlots.length) return;
    let index = pmSlots.findIndex(slot => slot.id === id);
    if (index < 0) index = 0;
    const slot = pmSlots[index];
    const sequence = pmSlots.map(item => [item.orig, item.rot]);
    const res = await callApi('open_page_zoom', pdf, slot.orig, slot.rot, sequence);
    if (!res || !res.success) {
        reportFailure(res, document.getElementById('pm-status'), uiText('dyn_1054', '放大预览失败'));
    }
}

/** 右键菜单的「放大预览」：多选时从选中的第一格开始连着翻，不再要求只选一页。 */
function pmZoomSelected() {
    const first = pmSlots.find(slot => pmSelectedIds.has(slot.id));
    if (!first) { showToast(uiText('dyn_1055', '请先点击缩略图选中页面')); return; }
    return pmZoomFrom(first.id);
}

/**
 * 格子是动态增删的，事件全部挂在网格上，一次绑好。
 *
 * 千页文档挂一千个监听器既慢又难清理，而委托只要五个：点击、右键、拖进拖出。
 */
function pmBindGrid() {
    if (pmGridBound) return;
    const grid = document.getElementById('pm-grid');
    if (!grid) return;
    pmGridBound = true;
    pmBindGridSizer();

    grid.addEventListener('click', event => {
        const cell = event.target.closest('.pm-thumb');
        if (cell) pmToggleSelect(Number(cell.dataset.id));
    });
    grid.addEventListener('contextmenu', event => {
        const cell = event.target.closest('.pm-thumb');
        if (!cell) return;
        event.preventDefault();
        pmOpenMenu(cell, event.clientX, event.clientY);
    });
    grid.addEventListener('dragstart', event => {
        const cell = event.target.closest('.pm-thumb');
        if (!cell) return;
        pmDragSrcPos = Number(cell.dataset.pos);
        event.dataTransfer.effectAllowed = 'move';
        cell.classList.add('dragging');
    });
    grid.addEventListener('dragover', event => {
        const cell = event.target.closest('.pm-thumb');
        if (!cell) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        cell.classList.add('drag-over');
    });
    grid.addEventListener('dragleave', event => {
        const cell = event.target.closest('.pm-thumb');
        if (cell) cell.classList.remove('drag-over');
    });
    grid.addEventListener('drop', event => {
        const cell = event.target.closest('.pm-thumb');
        if (cell) pmDrop(cell, event);
    });
    grid.addEventListener('dragend', event => {
        const cell = event.target.closest('.pm-thumb');
        if (cell) cell.classList.remove('dragging');
        grid.querySelectorAll('.drag-over').forEach(node => node.classList.remove('drag-over'));
        pmDragSrcPos = -1;
    });

    document.addEventListener('click', event => {
        if (!event.target.closest('#pm-menu')) pmCloseMenu();
    });
    document.addEventListener('keydown', pmHandleShortcut);
    // 双击缩略图进高清浏览（F24）。前面那两次单击已经把选中取消掉了，
    // 所以这里顺手把这一格重新选中，界面和正在看的那页保持一致。
    grid.addEventListener('dblclick', event => {
        const cell = event.target.closest('.pm-thumb');
        if (!cell) return;
        const id = Number(cell.dataset.id);
        pmSetSelection([id]);
        pmZoomFrom(id);
    });
    // 关窗前尽力再落一次盘：强杀进程走不到这儿，主机制是停手 0.8 秒的防抖，
    // 这条只补「刚改完就点 ✕、防抖还没到期」那不到一秒的尾巴。
    window.addEventListener('pagehide', () => {
        if (!pmDraftTimer || !pmHasChanges()) return;
        clearTimeout(pmDraftTimer);
        pmDraftTimer = null;
        pmFlushDraft();
    });
    // 一滚起来，浮在原地的那份菜单就指错页面了，直接收起
    window.addEventListener('scroll', pmCloseMenu, true);
}

// ---------- 快捷键（F23）----------
/**
 * 页面管理这会儿在前台吗：不在就别抢键，别的页面正用输入框填页码范围。
 *
 * 合并之后它是「排版与整理」里的一格，所以光看页面还不够——同一页里切到裁剪那一格时，
 * Delete 该删掉输入框里的字符，而不是删页面。
 */
function pmPageActive() {
    const pane = document.getElementById('pane-layout-pm');
    if (!pane || pane.classList.contains('hidden')) return false;
    const page = document.getElementById('page-layout');
    return !!(page && page.classList.contains('active'));
}

/** 焦点在输入控件里一律不抢键：在页码框里按 Delete 是删字符，不是删页面。 */
function pmTypingIn(target) {
    if (!target) return false;
    const tag = String(target.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
}

/** 向导或确认弹窗压在上面时不响应快捷键：那两种界面只认按钮。 */
function pmModalOpen() {
    return ['#wizard', '#message-modal', '#print-modal'].some(selector => {
        const mask = document.querySelector(selector);
        return !!mask && !mask.classList.contains('hidden');
    });
}

/** 网格现在几列：按算出来的栅格数，不按像素猜（列数跟着窗口宽度变）。 */
function pmGridColumns() {
    const grid = document.getElementById('pm-grid');
    if (!grid) return 1;
    const tracks = getComputedStyle(grid).getPropertyValue('grid-template-columns');
    const cols = tracks.split(' ').filter(Boolean).length;
    return cols > 0 ? cols : 1;
}

/** 光标现在在哪一格：有选中时取选中的最后一格，没选中时从第一格开始。 */
function pmCaretPos() {
    for (let pos = pmSlots.length - 1; pos >= 0; pos--) {
        if (pmSelectedIds.has(pmSlots[pos].id)) return pos;
    }
    return 0;
}

/**
 * 方向键挪光标。
 *
 * 走的是「活动端」：普通按一下就是活动端移动一格（选择只剩那一格），
 * 按住 Shift 则是把锚点钉在原地、活动端往前推，中间整段都是选中。
 * 锚点必须自己记着——之前从「选中的最后一格」往前推，来回拖选时锚点会跳到另一端，
 * 第二下 Shift+→ 就把刚扩出去的那格又缩回来了。
 *
 * 移动之后把那一格滚进视野：键盘走格子时如果不动屏幕，光标就走出屏外了，
 * 而且这也顶替了「方向键滚动页面」——两者本来就要同一件事，选一个更实用的。
 */
function pmMoveCaret(step, extend) {
    const count = pmSlots.length;
    if (!count) return;
    const live = pos => pos >= 0 && pos < count && pmSelectedIds.has(pmSlots[pos].id);
    // 选中集被鼠标改动过（锚点或活动端已经不在里面）就接不上上一段选择，重新起一段
    const growing = extend && live(pmAnchorPos) && live(pmActivePos);
    if (!growing) {
        pmAnchorPos = pmActivePos = pmCaretPos();
    }
    const to = Math.max(0, Math.min(count - 1, pmActivePos + step));
    pmActivePos = to;
    if (!growing) pmAnchorPos = to;
    const lo = Math.min(pmAnchorPos, to);
    const hi = Math.max(pmAnchorPos, to);
    const ids = [];
    for (let pos = lo; pos <= hi; pos++) ids.push(pmSlots[pos].id);
    pmSetSelection(ids);
    const cell = pmCell(pmSlots[to].id);
    if (cell) cell.scrollIntoView({ block: 'nearest' });
}

function pmHandleShortcut(event) {
    // Esc 先给浮层：菜单开着就先收菜单（这条是 6.0.4 就有的行为，不能改掉）
    const menu = document.getElementById('pm-menu');
    if (event.key === 'Escape' && menu && !menu.classList.contains('hidden')) {
        pmCloseMenu();
        return;
    }
    if (!pmPageActive() || !pmSlots.length || pmTypingIn(event.target) || pmModalOpen()) return;
    const key = event.key;
    const ctrl = event.ctrlKey || event.metaKey;
    if (ctrl && (key === 'a' || key === 'A')) {
        event.preventDefault();
        pmSetSelection(pmSlots.map(slot => slot.id));
        return;
    }
    if (ctrl && (key === 'z' || key === 'Z')) {
        event.preventDefault();
        pmUndo();
        return;
    }
    if (key === 'Delete') {
        if (!pmSelectedIds.size) return;
        event.preventDefault();
        pmDeleteSelected();
        return;
    }
    if (key === 'Escape') {
        if (!pmSelectedIds.size) return;
        event.preventDefault();
        pmSetSelection([]);
        return;
    }
    const step = { ArrowLeft: -1, ArrowRight: 1,
        ArrowUp: -pmGridColumns(), ArrowDown: pmGridColumns() }[key];
    if (step === undefined) return;
    event.preventDefault();
    pmMoveCaret(step, event.shiftKey);
}

function pmDrop(targetCell, event) {
    event.preventDefault();
    targetCell.classList.remove('drag-over');
    const pos = Number(targetCell.dataset.pos);
    if (pmDragSrcPos < 0 || pmDragSrcPos === pos) {
        pmDragSrcPos = -1;
        return;
    }

    pmSnapshot({ op: 'move', from: pmDragSrcPos, to: pos });
    const moved = pmSlots.splice(pmDragSrcPos, 1)[0];
    pmSlots.splice(pos, 0, moved);

    // 只搬这一个 DOM 节点：整表重建会把已经渲染好的缩略图全部销毁重来，
    // 千页文档拖一次要等好几秒，还会闪一下
    const grid = document.getElementById('pm-grid');
    const nodes = Array.from(grid.children);
    const node = nodes[pmDragSrcPos];
    nodes.splice(pmDragSrcPos, 1);
    grid.insertBefore(node, pos < nodes.length ? nodes[pos] : null);
    // 改到位置的只有「起点到落点」那一段，两头之间的格子下标各自挪了一格
    pmSyncPositions(Math.min(pmDragSrcPos, pos), Math.max(pmDragSrcPos, pos));
    pmDragSrcPos = -1;

    // Chromium 拖拽结束不会再补发 click，这个标记只用来吃掉紧随拖拽的那一次点击；
    // 必须自动过期，否则会一直留在元素上、吞掉用户下一次真正的点击
    node.dataset.justDragged = '1';
    setTimeout(() => delete node.dataset.justDragged, 0);
    pmMarkDirty();
}

function pmUpdateStatus() {
    const statusEl = document.getElementById('pm-status');
    if (!statusEl) return;
    const rotated = pmSlots.reduce((count, slot) => count + (slot.rot ? 1 : 0), 0);
    const removed = pmTotalPages - pmSlots.length;
    const moved = pmSlots.some((slot, i) => slot.orig !== i);
    const pending = [];
    if (moved) pending.push(uiText('dyn_1415', '调整顺序'));
    if (rotated) pending.push(uiTextf('dyn_1416', '旋转 {0} 页', rotated));
    if (removed > 0) pending.push(uiTextf('dyn_1417', '删除 {0} 页', removed));

    // 「已出图」按画过的页累计，远处的图为了省内存会回收、滚回去再画一次，
    // 所以这里不能说「只剩这么多」，那会让用户以为后面的页画不出来
    let text = (pmStatusNotice ? pmStatusNotice + ' · ' : '')
        + uiTextf('dyn_1056', '✓ 共 {0} 页，缩略图已出 {1} / {2} 页（跟着滚动画，每页都能编辑）', pmTotalPages, pmEverDrawn.size, pmTotalPages);
    if (pmSelectedIds.size) text += uiTextf('dyn_1057', ' · 已选中 {0} 页', pmSelectedIds.size);
    text += pending.length ? uiTextf('dyn_1424', ' · 待保存：{0}', pending.join('、')) : uiText('dyn_1058', ' · 没有待保存的改动');
    statusEl.innerText = text;
}


// ==================== 拆分 ====================
function toggleSplitMode() {
    const mode = document.querySelector('input[name="split-mode"]:checked').value;
    document.getElementById('split-every-n').classList.toggle('hidden', mode !== 'every_n');
    document.getElementById('split-range').classList.toggle('hidden', mode !== 'range');
}

async function doSplit() {
    const pdf = selectedFiles['split'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    const mode = document.querySelector('input[name="split-mode"]:checked').value;
    const every_n = document.getElementById('split-n').value;
    const range_str = document.getElementById('split-range-input').value;
    const res = await callApi('split_pdf', pdf, mode, every_n, range_str);
    if (res && res.success) {
        document.getElementById('split-status').innerText = '✓ ' + res.files.join(', ');
        showToast(uiText('dyn_1060', '拆分完成'));
    } else {
        reportFailure(res, document.getElementById('split-status'), uiText('dyn_1061', '拆分失败'));
    }
}

// ==================== 合并 ====================
async function addMergeFiles() {
    const path = await callApi('open_file_dialog');
    if (!path || typeof path !== 'string') return;
    if (mergeFilesList.includes(path)) { showToast(uiText('dyn_1062', '该文件已在列表中')); return; }
    mergeFilesList.push(path);
    renderMergeList();
}

function clearMergeFiles() {
    if (mergeFilesList.length === 0) return;
    mergeFilesList = [];
    renderMergeList();
}

function removeMergeFile(index) {
    mergeFilesList.splice(index, 1);
    renderMergeList();
}

function moveMergeFile(index, delta) {
    const newIndex = index + delta;
    if (newIndex < 0 || newIndex >= mergeFilesList.length) return;
    const tmp = mergeFilesList[index];
    mergeFilesList[index] = mergeFilesList[newIndex];
    mergeFilesList[newIndex] = tmp;
    renderMergeList();
}

function renderMergeList() {
    const list = document.getElementById('merge-list');
    if (!list) return;
    if (mergeFilesList.length === 0) { list.innerHTML = ''; return; }
    list.innerHTML = mergeFilesList.map((f, i) => {
        const name = f.split(/[\\/]/).pop();
        const isFirst = i === 0;
        const isLast = i === mergeFilesList.length - 1;
        const escaped = f.replace(/"/g, '&quot;');
        return `
            <div class="merge-item" draggable="true" data-index="${i}"
                 ondragstart="onMergeDragStart(event, ${i})"
                 ondragover="onMergeDragOver(event, ${i})"
                 ondrop="onMergeDrop(event, ${i})"
                 ondragend="onMergeDragEnd(event)">
                <span class="drag-handle" title="${uiText('dyn_1403', '拖拽排序')}">⋮⋮</span>
                <span class="merge-index">${i + 1}.</span>
                <span class="merge-name" title="${escaped}">${name}</span>
                <button class="action-btn" ${isFirst ? 'disabled' : ''} onclick="moveMergeFile(${i}, -1)">↑</button>
                <button class="action-btn" ${isLast ? 'disabled' : ''} onclick="moveMergeFile(${i}, 1)">↓</button>
                <button class="action-btn remove-btn" onclick="removeMergeFile(${i})">✕</button>
            </div>`;
    }).join('');
}

function onMergeDragStart(e, index) {
    dragSrcIndex = index;
    e.dataTransfer.effectAllowed = 'move';
    e.currentTarget.classList.add('dragging');
}

function onMergeDragOver(e, index) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    e.currentTarget.classList.add('drag-over');
}

function onMergeDrop(e, index) {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    if (dragSrcIndex < 0 || dragSrcIndex === index) return;
    const item = mergeFilesList.splice(dragSrcIndex, 1)[0];
    mergeFilesList.splice(index, 0, item);
    dragSrcIndex = -1;
    renderMergeList();
}

function onMergeDragEnd(e) {
    e.currentTarget.classList.remove('dragging');
    document.querySelectorAll('.merge-item').forEach(el => el.classList.remove('drag-over'));
}

async function doMerge() {
    if (mergeFilesList.length === 0) { showToast(uiText('dyn_1063', '请添加文件')); return; }
    const out = await callApi('save_file_dialog', 'merged.pdf');
    if (!out || typeof out !== 'string') return;
    const statusEl = document.getElementById('merge-status');
    statusEl.innerText = uiText('dyn_1064', '⏳ 合并中...');
    const res = await callApi('merge_pdfs', mergeFilesList, out);
    if (res && res.success) {
        statusEl.innerText = '✓ ' + out;
        showToast(uiText('dyn_1065', '合并完成'));
    } else {
        statusEl.innerText = '✗ ' + (res && res.message ? res.message : '');
    }
}

// ==================== 预览窗（翻页 / 缩放，印章与文字水印共用） ====================
// 120dpi 约为屏幕的 1.67 倍：放大到 200% 仍不发虚，又不至于像 150dpi 那样
// 每次换页都往 js 桥塞两三 MB。换页才请求后端，缩放只改 CSS 宽度。
const PREVIEW_DPI = 120;
const PDF_POINTS_PER_INCH = 72;

const PREVIEW_PANES = {
    watermark: {
        viewport: 'wm-viewport', zoom: 'wm-zoom', prev: 'wm-prev', next: 'wm-next',
        pageInput: 'wm-page-input', pageInfo: 'wm-page-info'
    },
    stamp: {
        viewport: 'stamp-viewport', zoom: 'stamp-zoom', prev: 'stamp-prev', next: 'stamp-next',
        pageInput: 'stamp-page-input', pageInfo: 'stamp-page-info'
    },
    // 排版与整理那四件工具（加页码 / 页眉页脚 / 裁剪 / 缩放）各有一个单页预览窗。
    // 键名和 PANEL_TOOLS.layout 里的工具名一律对齐，翻页、缩放就复用上面这一套：
    // 差别只在出图的接口（那四格走 preview_page_effect，见 tools.js 的 layRenderPreview）
    number: {
        viewport: 'lay-number-viewport', zoom: 'lay-number-zoom', prev: 'lay-number-prev',
        next: 'lay-number-next', pageInput: 'lay-number-page-input',
        pageInfo: 'lay-number-page-info', note: 'lay-number-note'
    },
    header: {
        viewport: 'lay-header-viewport', zoom: 'lay-header-zoom', prev: 'lay-header-prev',
        next: 'lay-header-next', pageInput: 'lay-header-page-input',
        pageInfo: 'lay-header-page-info', note: 'lay-header-note'
    },
    crop: {
        viewport: 'lay-crop-viewport', zoom: 'lay-crop-zoom', prev: 'lay-crop-prev',
        next: 'lay-crop-next', pageInput: 'lay-crop-page-input',
        pageInfo: 'lay-crop-page-info', note: 'lay-crop-note'
    },
    scale: {
        viewport: 'lay-scale-viewport', zoom: 'lay-scale-zoom', prev: 'lay-scale-prev',
        next: 'lay-scale-next', pageInput: 'lay-scale-page-input',
        pageInfo: 'lay-scale-page-info', note: 'lay-scale-note'
    },
};
const previewState = {
    watermark: {page: 0, count: 0, zoom: 'fit'},
    stamp: {page: 0, count: 0, zoom: 'fit'},
    number: {page: 0, count: 0, zoom: 'fit'},
    header: {page: 0, count: 0, zoom: 'fit'},
    crop: {page: 0, count: 0, zoom: 'fit'},
    scale: {page: 0, count: 0, zoom: 'fit'},
};

function paneEl(key, part) {
    const cfg = PREVIEW_PANES[key];
    return cfg && cfg[part] ? document.getElementById(cfg[part]) : null;
}

function paneImage(key) {
    const vp = paneEl(key, 'viewport');
    return vp ? vp.querySelector('img.page-img') : null;
}

/**
 * 按当前档位设置页面图的显示宽度。
 *
 * 百分比档位指「实际打印尺寸」：图是按 PREVIEW_DPI 渲染的，
 * naturalWidth * 72 / dpi 才是它在纸上占的 CSS 像素。图尚未解码完时
 * naturalWidth 为 0，此时先按适应宽度顶着，onload 里会再校正一次。
 *
 * 'page'（整页可见）不由宽度定，而是取「按窗子高度放得下整页」的那个宽度：
 * 竖版页在矮窗子里先被高度夹住，左右留出空，页面下沿不再被切掉。
 */
function applyPreviewZoom(key) {
    const img = paneImage(key);
    const vp = paneEl(key, 'viewport');
    const st = previewState[key];
    if (!img || !vp || !st) return;
    const avail = Math.max(120, vp.clientWidth);
    const actual = img.naturalWidth * PDF_POINTS_PER_INCH / PREVIEW_DPI;
    let width = avail;
    if (st.zoom === 'page') {
        const actualH = img.naturalHeight * PDF_POINTS_PER_INCH / PREVIEW_DPI;
        if (actual > 0 && actualH > 0) {
            width = Math.min(avail, Math.max(120, vp.clientHeight) * actual / actualH);
        }
    } else if (st.zoom !== 'fit' && actual > 0) {
        width = actual * (parseFloat(st.zoom) / 100);
    }
    img.style.width = Math.round(width) + 'px';
    if (key === 'stamp') placeStampOverlay();
}

function previewZoomChanged(key) {
    const select = paneEl(key, 'zoom');
    if (!select || !previewState[key]) return;
    previewState[key].zoom = select.value;
    applyPreviewZoom(key);
}

/** 页数已知后回写导航区；总数变小时把当前页夹回来。 */
function setPreviewCount(key, count) {
    const st = previewState[key];
    if (!st) return;
    const total = Math.max(0, parseInt(count, 10) || 0);
    st.count = total;
    if (st.page >= total) st.page = Math.max(0, total - 1);
    syncPreviewNav(key);
}

function syncPreviewNav(key) {
    const st = previewState[key];
    if (!st) return;
    const total = st.count;
    const input = paneEl(key, 'pageInput');
    if (input) {
        const shown = String(st.page + 1);
        if (input.value !== shown) input.value = shown;
        input.max = total > 0 ? String(total) : '1';
        input.disabled = total <= 1;
    }
    const info = paneEl(key, 'pageInfo');
    if (info) info.innerText = total > 1 ? uiTextf('dyn_1005', '/ 共 {0} 页', total) : '';
    const prev = paneEl(key, 'prev');
    if (prev) prev.disabled = st.page <= 0;
    const next = paneEl(key, 'next');
    // total 为 0（还没选文件）时也必须禁用，否则空态下能一直点下一页
    if (next) next.disabled = total === 0 || st.page >= total - 1;
}

function previewGoToPage(key, index) {
    const st = previewState[key];
    if (!st) return;
    let target = parseInt(index, 10);
    if (isNaN(target)) target = 1;
    target = Math.max(1, target);
    if (st.count > 0) target = Math.min(target, st.count);
    if (target - 1 === st.page) { syncPreviewNav(key); return; }
    st.page = target - 1;
    syncPreviewNav(key);
    renderPreviewPane(key);
}

function previewStep(key, delta) {
    const st = previewState[key];
    if (!st) return;
    previewGoToPage(key, st.page + 1 + delta);
}

function previewInputPage(key) {
    const input = paneEl(key, 'pageInput');
    if (!input) return;
    previewGoToPage(key, input.value);
}

function showPreviewHint(key, text, textKey) {
    const vp = paneEl(key, 'viewport');
    if (!vp) return;
    vp.innerHTML = '';
    const tip = document.createElement('p');
    tip.className = 'page-empty';
    // 给了 textKey 就登记下来：这一格已经画好在屏上时用户才改语言，
    // 不登记的话那句提示会一直留着上一次语言。
    if (textKey) setDynamicText(tip, textKey, text);
    else tip.textContent = text;
    vp.appendChild(tip);
}

function renderPreviewPane(key) {
    if (key === 'stamp') {
        renderStampPane();
        return;
    }
    if (key !== 'watermark') {
        // 排版与整理那四格（number / header / crop / scale）：出图要按当前表单先把这一页改一遍，
        // 只有 tools.js 知道各家的字段，所以翻页也交回它那一层
        if (typeof layRenderPreview === 'function') layRenderPreview(key);
        return;
    }
    // 翻页要的是「立刻按当前页渲染一次」。之前排队的防抖任务如果不撤掉，
    // 会在 200ms 后把同一页再渲染一遍，白跑一趟后端。
    if (wmPreviewTimer) {
        clearTimeout(wmPreviewTimer);
        wmPreviewTimer = null;
    }
    renderWatermarkPreview();
}

/** 换了 PDF 后从第 1 页看起；总页数要等下一次渲染的响应才知道。 */
function resetPreviewPage(key) {
    const st = previewState[key];
    if (!st) return;
    st.page = 0;
    st.count = 0;
    syncPreviewNav(key);
}

// 「适应宽度」和页面图的实际显示宽度绑在容器尺寸上，窗口一改就得重新量。
//
// 但不能来一次重算一次：拖动窗边时 resize 是一帧好几条，而这一串重算实测一次 90 ms
// （tests/out/smooth_probe.py 的 resize_ms），六个预览窗格各重排一遍再加查看页整版重排，
// 拖动的过程就变成 10 帧一秒。攒到最后一次之后再算：拖动时窗口自己跟着鼠标走不卡，
// 松手 100 ms 内预览跟上新宽度，用户看不出中间那一段。
let resizeSettleTimer = null;
const RESIZE_SETTLE_MS = 100;

window.addEventListener('resize', () => {
    if (resizeSettleTimer) clearTimeout(resizeSettleTimer);
    resizeSettleTimer = setTimeout(settleWindowResize, RESIZE_SETTLE_MS);
});

function settleWindowResize() {
    resizeSettleTimer = null;
    Object.keys(previewState).forEach(key => {
        const vp = paneEl(key, 'viewport');
        // 看不见的窗格 clientWidth 是 0，会被 applyPreviewZoom 的下限夹成 120px 的小页，
        // 切回来时没人重算就一直那么小 —— 所以量不到宽度的窗格直接跳过，交给 showPage 补
        if (vp && vp.clientWidth > 0) applyPreviewZoom(key);
    });
    // 查看页那套是按宽度算的占位高度，窗口一改宽度整版面高度就变了，必须重排
    if (typeof viewerRelayout === 'function' && viewer) { viewerRelayout(); viewerUpdate(); }
    // 页面管理那一网的格子高度是按列宽算的（缩略图按 1:1.414 撑高），换了宽度
    // 网格兜底高度也要跟着换，否则离屏那几千格会按旧宽度占位、滚动条长度就不对
    pmSyncCellRowHeight();
}

// ==================== 印章 ====================
function toggleStampSource() {
    const source = document.querySelector('input[name="stamp-source"]:checked').value;
    document.getElementById('stamp-file-source').classList.toggle('hidden', source !== 'file');
    document.getElementById('stamp-draw-source').classList.toggle('hidden', source !== 'draw');
}

function toggleStampPageMode() {
    const mode = document.getElementById('stamp-page-mode').value;
    document.getElementById('stamp-page-range').classList.toggle('hidden', mode !== 'range');
}

// 与后端 api/stamp.py 的 _STAMP_BASE_WIDTH_RATIO 保持一致
const STAMP_BASE_WIDTH_RATIO = 0.30;
// 印章落点存成「相对页面图的比例」，翻页或改缩放后才能回到同一个位置
const STAMP_DEFAULT_POS = {x: 0.03, y: 0.03};
let stampPosNorm = null;
let stampRenderSeq = 0;

async function loadStampPreview(path) {
    // 先作废上一份文档还在路上的渲染：不抬序号的话它的响应回来会重新填满刚清空的窗格
    stampRenderSeq += 1;
    // 换 PDF 时必须把已选印章一起清掉：否则预览里看不到印章，
    // 但 stampImagePath 还在，点「开始盖印」会在看不见的位置盖上它
    stampImageElement = null;
    stampImagePath = '';
    stampPosNorm = null;
    const st = previewState.stamp;
    st.page = 0;
    st.count = 0;
    const viewport = paneEl('stamp', 'viewport');
    if (viewport) viewport.innerHTML = '';
    syncPreviewNav('stamp');

    const total = await callApi('get_pdf_page_count', path);
    if (selectedFiles['stamp'] !== path) return;
    setPreviewCount('stamp', total > 0 ? total : 0);
    await renderStampPane();
}

async function renderStampPane() {
    const viewport = paneEl('stamp', 'viewport');
    if (!viewport) return;
    const path = selectedFiles['stamp'];
    if (!path) {
        stampRenderSeq += 1;
        showPreviewHint('stamp', uiText('dyn_1072', '请先选择要盖印的 PDF'), 'dyn_1072');
        return;
    }

    const seq = ++stampRenderSeq;
    const imgSrc = await callApi('render_page', path, previewState.stamp.page, PREVIEW_DPI);
    if (seq !== stampRenderSeq) return;

    if (!imgSrc || typeof imgSrc !== 'string') {
        viewport.innerHTML = '';
        // 页面都渲染不出来，印章更没法落位；连引用一起清掉，避免「看不见却照样盖」
        stampImageElement = null;
        stampImagePath = '';
        document.getElementById('stamp-status').innerText = uiText('dyn_1067', '✗ 无法预览该 PDF');
        return;
    }

    const img = document.createElement('img');
    img.className = 'page-img';
    img.id = 'stamp-base-image';
    img.alt = '';
    img.src = imgSrc;

    viewport.innerHTML = '';
    viewport.appendChild(img);
    // 换页会重写视口内容，绝对定位的印章得跟着放回来
    if (stampImageElement) viewport.appendChild(stampImageElement);
    img.onload = () => applyPreviewZoom('stamp');
    applyPreviewZoom('stamp');
}

// 用宽度表达缩放（而不是 transform: scale），这样预览大小与实际输出一致，
// 位置归一化也不会被 transform 的缩放中心影响
function applyStampScale() {
    if (!stampImageElement) return;
    const baseImg = document.getElementById('stamp-base-image');
    if (!baseImg || baseImg.offsetWidth <= 0) return;
    const scale = document.getElementById('stamp-scale').value / 100;
    stampImageElement.style.width =
        (baseImg.offsetWidth * STAMP_BASE_WIDTH_RATIO * scale) + 'px';
    stampImageElement.style.height = 'auto';
}

/**
 * 把选中的印章图叠到预览窗，并让它成为当前生效的印章。
 *
 * 界面跑在 http://127.0.0.1，直接引用 file:/// 会被 WebView2 拦掉（图永远解码
 * 不出来，预览里只剩一个能拖的空框），所以图片由后端读成 data URL。
 * stampImagePath 只在这里赋值：路径、预览、导出三者必须同时换。
 */
async function attachStampOverlay(path) {
    const viewport = paneEl('stamp', 'viewport');
    if (!viewport) return;
    const res = await callApi('load_stamp_image', path);
    if (!res || !res.success || !res.data_url) {
        reportFailure(res, document.getElementById('stamp-status'), uiText('dyn_1068', '印章图片读不出来'));
        return;
    }
    // 每次都重新建元素：签名可能被覆盖成同名文件，按路径缓存会显示旧图
    const stampImg = document.createElement('img');
    stampImg.src = res.data_url;
    stampImg.className = 'stamp-image';
    stampImg.alt = '';
    makeDraggable(stampImg, syncStampPosNorm);
    stampImageElement = stampImg;
    stampImagePath = path;
    // 换图之后之前拖出来的落点不再有意义，回到默认位置
    stampPosNorm = null;
    stampImageElement.style.opacity = document.getElementById('stamp-opacity').value / 100;
    viewport.appendChild(stampImageElement);
    placeStampOverlay();
}

/**
 * 把印章的像素位置换算回相对比例（拖动结束、摆放之后各调用一次）。
 *
 * 限的是「整枚印章都在页面上」而不是「左上角在页面上」：后端落印时会把出界的
 * 印章整枚夹回页内，前端只夹左上角的话，拖到右边缘/下边缘就会看到预览盖一半、
 * 成品却完整，翻页后还会被摆回一个用户没选过的位置。这里顺手把元素夹回来，
 * 保证屏幕上看到的就是导出的结果。
 */
function syncStampPosNorm() {
    const el = stampImageElement;
    const base = document.getElementById('stamp-base-image');
    if (!el || !base || base.offsetWidth <= 0 || base.offsetHeight <= 0) return;
    const minLeft = base.offsetLeft;
    const minTop = base.offsetTop;
    // 印章比页面还宽时（放大到极限）区间会反向，取 min 兜一下，退化成贴着左上角
    const maxLeft = Math.max(minLeft, minLeft + base.offsetWidth - el.offsetWidth);
    const maxTop = Math.max(minTop, minTop + base.offsetHeight - el.offsetHeight);
    const left = Math.min(Math.max(el.offsetLeft, minLeft), maxLeft);
    const top = Math.min(Math.max(el.offsetTop, minTop), maxTop);
    if (left !== el.offsetLeft) el.style.left = Math.round(left) + 'px';
    if (top !== el.offsetTop) el.style.top = Math.round(top) + 'px';
    stampPosNorm = {
        x: (left - minLeft) / base.offsetWidth,
        y: (top - minTop) / base.offsetHeight
    };
}

/**
 * 按相对比例摆放印章。
 *
 * 分母必须是「页面图像」而不是预览容器：容器只有几百像素高，页面图却有一千多
 * 像素，比例被放大后又被 clamp 到 1，结果无论拖到哪里印章都会盖到页面最底部。
 * 页面图居中时会带 offsetLeft 偏移，取和放两边都要连着它一起算。
 */
function placeStampOverlay() {
    const el = stampImageElement;
    const base = document.getElementById('stamp-base-image');
    if (!el || !base || base.offsetWidth <= 0 || base.offsetHeight <= 0) return;
    const pos = stampPosNorm || STAMP_DEFAULT_POS;
    el.style.left = Math.round(base.offsetLeft + pos.x * base.offsetWidth) + 'px';
    el.style.top = Math.round(base.offsetTop + pos.y * base.offsetHeight) + 'px';
    applyStampScale();
    // 摆完再夹一次：翻页/改印章缩放后可能压出页面边缘，夹回来的值同时写回 stampPosNorm，
    // 这样「没拖动过」的默认落点也会以真实比例传给后端，而不是让后端按自己的默认角落印
    syncStampPosNorm();
}

async function selectStampImage() {
    // 必须传完整的 "描述 (*.ext)" 写法，pywebview 只认这一种
    const path = await callApi('open_file_dialog', ['Image Files (*.png;*.jpg;*.jpeg;*.bmp)']);
    if (!path || typeof path !== 'string') return;

    if (!selectedFiles['stamp']) {
        // 没有底页就没法摆印章；只记路径不显示，会让人以为选好了却看不见
        const tip = uiText('dyn_1072', '请先选择要盖印的 PDF');
        document.getElementById('stamp-status').innerText = '✗ ' + tip;
        showToast(tip, 3000);
        return;
    }
    await attachStampOverlay(path);
    if (stampImagePath === path) {
        document.getElementById('stamp-status').innerText = '✓ ' + path;
    }
}

function makeDraggable(el, onEnd) {
    // 用位移增量，而不是 clientX - offsetLeft：后者把视口坐标和
    // 相对父元素的偏移量混在一起，父容器一旦有偏移就会跳位
    let startX = 0, startY = 0, originLeft = 0, originTop = 0;
    el.addEventListener('mousedown', (e) => {
        e.preventDefault();
        startX = e.clientX;
        startY = e.clientY;
        originLeft = el.offsetLeft;
        originTop = el.offsetTop;
        document.addEventListener('mousemove', drag);
        document.addEventListener('mouseup', endDrag);
    });
    function drag(e) {
        el.style.left = (originLeft + e.clientX - startX) + 'px';
        el.style.top = (originTop + e.clientY - startY) + 'px';
    }
    function endDrag() {
        document.removeEventListener('mousemove', drag);
        document.removeEventListener('mouseup', endDrag);
        if (onEnd) onEnd();
    }
}

function updateStampOpacityAndScale() {
    if (!stampImageElement) return;
    stampImageElement.style.opacity = document.getElementById('stamp-opacity').value / 100;
    applyStampScale();
}

function clearSign() {
    const canvas = document.getElementById('sign-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
}

/** 画布上有没有落笔：空白签名存成印章后是透明的，盖出来什么都没有。 */
function canvasHasInk(canvas) {
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    for (let i = 3; i < data.length; i += 4) {
        if (data[i] > 0) return true;
    }
    return false;
}

async function saveSign() {
    const canvas = document.getElementById('sign-canvas');
    if (!canvasHasInk(canvas)) {
        const tip = uiText('dyn_1070', '请先在签名框里手写，空白签名保存不了');
        document.getElementById('stamp-status').innerText = '✗ ' + tip;
        showToast(tip, 3000);
        return;
    }
    const dataUrl = canvas.toDataURL('image/png');
    const out = await callApi('save_file_dialog', 'signature.png', ['PNG Image (*.png)']);
    if (!out || typeof out !== 'string') return;
    const res = await callApi('save_signature_canvas', dataUrl, out);
    if (res && res.success) {
        document.getElementById('stamp-status').innerText = '✓ ' + out;
        showToast(uiText('dyn_1071', '签名已保存'));

        // 立刻放进预览并允许拖动，保证 stampImagePath 与预览元素始终一致
        if (selectedFiles['stamp']) {
            await attachStampOverlay(out);
        } else {
            const tip = uiText('dyn_1072', '请先选择要盖印的 PDF');
            document.getElementById('stamp-status').innerText = '✗ ' + tip;
            showToast(tip, 3000);
        }
    } else {
        reportFailure(res, document.getElementById('stamp-status'), uiText('dyn_1073', '保存签名失败'));
    }
}

async function doStamp() {
    const pdf = selectedFiles['stamp'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    if (!stampImagePath) { showToast(uiText('dyn_1075', '请先选择印章图片')); return; }
    const opacity = document.getElementById('stamp-opacity').value / 100;
    const scale = document.getElementById('stamp-scale').value / 100;
    const pageMode = document.getElementById('stamp-page-mode').value;
    const pageRange = document.getElementById('stamp-page-range').value;
    const out = await callApi('save_file_dialog', 'stamped.pdf');
    if (!out || typeof out !== 'string') return;

    // 落点在拖动结束时就已经换算成相对比例了；这里再同步一次，覆盖
    // 「选完印章没拖动过」（stampPosNorm 仍是 null，用默认落点）的情况。
    syncStampPosNorm();

    const res = await callApi('apply_stamp', pdf, out, stampImagePath,
                              opacity, scale, pageMode, pageRange, stampPosNorm);
    if (res && res.success) {
        document.getElementById('stamp-status').innerText = '✓ ' + out;
        showToast(uiText('dyn_1076', '盖印完成'));
    } else {
        reportFailure(res, document.getElementById('stamp-status'), uiText('dyn_1077', '盖印失败'));
    }
}

// ==================== 骑缝印（跨页印章，接 api/qifeng.py）====================
// 后端 apply_cross_page_seal 已登记进 common.js 的 LONG_JOBS：进度条与取消自动出现，
// 这里只管参数装配和结果落话。两条界面侧硬口径：
//   * 章宽的「绝对磅」与「按页宽百分比」是两个独立输入（后端同时给会 width_conflict），
//     只把选中那一档传下去、另一档传 0——别学参考实现一个框两种语义；
//   * 状态行先说后端那句 message，再把 warnings[].text 逐条追加；verified === false
//     时用 ✗ 起头并列出核对不上的页码，不许把「写出来了但核对不对」报成成功。
function toggleCrossSeal() {
    const on = document.getElementById('cross-seal-enable').checked;
    document.getElementById('cross-seal-body').classList.toggle('hidden', !on);
}

function crossSealYChanged() {
    document.getElementById('cross-seal-y-val').innerText =
        document.getElementById('cross-seal-y').value + '%';
}

function crossSealWidthModeChanged() {
    const mode = (document.querySelector('input[name="cross-seal-width-mode"]:checked') || {}).value || 'pt';
    document.getElementById('cross-seal-width-pt').disabled = mode !== 'pt';
    document.getElementById('cross-seal-width-percent').disabled = mode !== 'percent';
}

function crossSealSetStatus(res, statusEl, failFallback) {
    if (res && res.success) {
        const lines = [];
        const head = res.message || uiText('tool_qf_done_plain', '骑缝印已贴好');
        // verified 明确为 false：文件在那儿，但读回核对有页对不上——按 ✗ 说话并点名页码
        lines.push((res.verified === false ? '✗ ' : '✓ ') + head);
        if (res.verified === false && Array.isArray(res.mismatches) && res.mismatches.length) {
            const pages = res.mismatches.map(x => x.page).filter(Boolean);
            lines.push(uiTextf('tool_qf_mismatch_pages', '核对不上的页：{0}',
                               Array.from(new Set(pages)).join('、')));
        }
        (res.warnings || []).forEach(w => {
            if (w && w.text) lines.push('⚠ ' + w.text);
        });
        statusEl.innerText = lines.join('\n');
        if (res.verified !== false) showToast(head, 3600);
        else showToast(res.message || uiText('tool_qf_verify_failed', '请检查后再用'), 5200);
        return true;
    }
    reportFailure(res, statusEl, failFallback);
    // verify_failed 在回包里是 success:false（文件写了但核对不对）——mismatches 也照样点名，
    // 让用户知道是哪几页、差在哪，而不是只有一句「没成功」
    if (res && Array.isArray(res.mismatches) && res.mismatches.length) {
        const pages = Array.from(new Set(res.mismatches.map(x => x && x.page).filter(Boolean)));
        if (pages.length) {
            statusEl.innerText += '\n' + uiTextf('tool_qf_mismatch_pages', '核对不上的页：{0}',
                                                 pages.join('、'));
        }
    }
    return false;
}

async function doCrossSeal() {
    const statusEl = document.getElementById('cross-seal-status');
    const pdf = selectedFiles['stamp'];
    if (!pdf) {
        const tip = uiText('tool_qf_need_pdf', '请先在上方选择要盖骑缝印的 PDF');
        statusEl.innerText = '✗ ' + tip; showToast(tip, 3200); return;
    }
    if (!stampImagePath) {
        const tip = uiText('tool_qf_need_image', '骑缝印与盖印共用上面选的那张印章图片，请先「浏览图片」选一张');
        statusEl.innerText = '✗ ' + tip; showToast(tip, 4200); return;
    }
    const group = parseInt(document.getElementById('cross-seal-group').value, 10);
    if (!group || group < 1) {
        // 后端把 0 / 负数 / 非数字一律拒成 bad_group_size：界面先把 min 卡住，话术自己先说
        const tip = uiText('tool_qf_group_need', '「每组页数」要填 1 或更大的整数（一枚章最多切成几条）');
        statusEl.innerText = '✗ ' + tip; showToast(tip, 3600); return;
    }
    const modeEl = document.querySelector('input[name="cross-seal-width-mode"]:checked');
    const mode = modeEl ? modeEl.value : 'pt';
    const widthPt = mode === 'pt'
        ? (parseFloat(document.getElementById('cross-seal-width-pt').value) || 0) : 0;
    const widthPct = mode === 'percent'
        ? (parseFloat(document.getElementById('cross-seal-width-percent').value) || 0) : 0;
    if (mode === 'percent' && widthPct > 100) {
        const tip = uiText('tool_qf_pct_over', '章宽百分比最大 100%，再多就超出页面了');
        statusEl.innerText = '✗ ' + tip; showToast(tip, 3600); return;
    }
    const range = document.getElementById('cross-seal-range').value.trim();
    const y = Number(document.getElementById('cross-seal-y').value);
    const overlap = document.getElementById('cross-seal-overlap').checked;
    const password = document.getElementById('cross-seal-password').value;
    const base = String(pdf).split(/[\\/]/).pop().replace(/\.[^.]+$/, '');
    const out = await callApi('save_file_dialog',
                              base + uiText('tool_qf_suffix', '_骑缝章.pdf'));
    if (!out || typeof out !== 'string') return;
    statusEl.innerText = uiText('tool_qf_busy', '⏳ 正在逐页贴骑缝印（写完还会逐页读回核对）…');
    const res = await callApi('apply_cross_page_seal', pdf, stampImagePath, range, group,
                              y, widthPt, widthPct, overlap, password, out);
    // 「要口令」单列一条路：只在后端点名 needs_password 时把口令框亮出来，
    // 其余失败一律走状态行 + toast（错误码后端原样带着，界面按码分支，不猜话）
    if (res && res.error === 'needs_password') {
        document.getElementById('cross-seal-pw-box').classList.remove('hidden');
    }
    crossSealSetStatus(res, statusEl, uiText('tool_qf_failed', '骑缝印没有完成'));
}

// ==================== 水印 ====================
// 常用色：让「一键选中」比打开取色器更快，取色器负责剩下的任意色
const WM_PRESET_COLORS = ['#B4232F', '#2F67BC', '#218552', '#422FBC', '#BC6C2F', '#7B828C', '#000000'];
// 对角线和居中两种排法，默认字号差很多（一张铺满、一张只有一处），
// 用户没手动调过大小时才跟着切换，调过之后就不再擅自改动他的选择。
const WM_DEFAULT_SIZE = { diagonal: 20, centered: 40 };
let wmSizeTouched = false;
let wmPreviewTimer = null;
let wmPreviewSeq = 0;

function wmSpacingPt() {
    // 滑块给的是「密度」1~10，后端要的是平铺间距（pt），方向相反。
    // 5 档正好等于旧版写死的 200pt，老用户升级后不动滑块就还是原来的效果。
    const density = parseInt(document.getElementById('wm-density').value, 10) || 5;
    return 300 - (density - 1) * 25;
}

function wmParams() {
    return {
        text: document.getElementById('wm-text').value,
        color: document.getElementById('wm-color').value,
        opacity: document.getElementById('wm-opacity').value / 100,
        diagonal: document.getElementById('wm-diagonal').checked,
        size: parseInt(document.getElementById('wm-size').value, 10) || WM_DEFAULT_SIZE.diagonal,
        spacing: wmSpacingPt(),
        // 本机字体（common.js 的字体下拉那一族）：空串 = 后端内置口径，一个字都不变；
        // 预览与成品传的是同一个值，两边不会两副样子
        font: (document.getElementById('wm-font') || {}).value || '',
        // 按不按 LaTeX 标记解析水印文字（默认关，说明见 index.html 的 latex_switch_desc）；
        // 预览与成品同样共用这一个值
        latex: !!(document.getElementById('wm-latex') || {}).checked
    };
}

function wmSizeChanged() {
    wmSizeTouched = true;
    document.getElementById('wm-size-val').innerText =
        document.getElementById('wm-size').value + 'pt';
    loadWatermarkPreview();
}

function wmDiagonalChanged() {
    const diagonal = document.getElementById('wm-diagonal').checked;
    // 平铺间距只对对角线有意义，取消勾选后把滑块置灰而不是偷偷用默认值
    const density = document.getElementById('wm-density');
    if (density) density.disabled = !diagonal;
    const label = document.getElementById('wm-density-label');
    if (label) label.classList.toggle('disabled', !diagonal);

    const size = document.getElementById('wm-size');
    if (size && !wmSizeTouched) {
        size.value = diagonal ? WM_DEFAULT_SIZE.diagonal : WM_DEFAULT_SIZE.centered;
        document.getElementById('wm-size-val').innerText = size.value + 'pt';
    }
    loadWatermarkPreview();
}

function setWatermarkColor(hex) {
    const input = document.getElementById('wm-color');
    if (!input) return;
    input.value = hex;
    loadWatermarkPreview();
}

function renderWatermarkPresets() {
    const box = document.getElementById('wm-color-presets');
    if (!box) return;
    box.innerHTML = '';
    WM_PRESET_COLORS.forEach(hex => {
        const button = document.createElement('button');
        button.type = 'button';
        button.style.background = hex;
        button.title = hex;
        button.onclick = () => setWatermarkColor(hex);
        box.appendChild(button);
    });
}

function loadWatermarkPreview() {
    // 色号回显是纯本地的事，跟着输入立刻走；预览要后端渲染整页，得合并
    const hexLabel = document.getElementById('wm-color-hex');
    if (hexLabel) hexLabel.innerText = document.getElementById('wm-color').value.toUpperCase();
    // 拖动滑块时 input 事件每帧都来，预览是「渲染整页 PNG」这种重活，
    // 不做合并会把 js 桥塞满、还把最后一次操作挤出队列。200ms 内的连按只渲染末次。
    if (wmPreviewTimer) clearTimeout(wmPreviewTimer);
    wmPreviewTimer = setTimeout(renderWatermarkPreview, 200);
}

async function renderWatermarkPreview() {
    const preview = paneEl('watermark', 'viewport');
    if (!preview) return;
    const p = wmParams();

    const pdf = selectedFiles['watermark'];
    if (!pdf || !String(p.text).trim()) {
        wmPreviewSeq += 1;
        const need = !pdf ? ['dyn_1426', uiText('dyn_1426', '请选择 PDF 文件以预览效果')]
                          : ['dyn_1078', uiText('dyn_1078', '输入水印文字后这里会显示真实页面效果')];
        showPreviewHint('watermark', need[1], need[0]);
        return;
    }

    const seq = ++wmPreviewSeq;
    const res = await callApi('preview_watermark', pdf, p.text, p.color, p.opacity,
                              p.diagonal, p.size, p.spacing,
                              previewState.watermark.page, PREVIEW_DPI, p.font, p.latex);
    // 等回来时用户可能又改了参数或翻了页，这一张已经过时，直接丢掉
    if (seq !== wmPreviewSeq) return;
    if (res && res.success && res.image) {
        setPreviewCount('watermark', res.page_count);
        const img = document.createElement('img');
        img.className = 'page-img';
        img.alt = '';
        img.src = 'data:image/png;base64,' + res.image;
        preview.innerHTML = '';
        preview.appendChild(img);
        img.onload = () => applyPreviewZoom('watermark');
        applyPreviewZoom('watermark');
        // 所选字体读不出来时后端照样出图，但会退回内置字体并带一句 message——
        // 这句必须落在状态行上，不许静默（预览用的是哪把字体，用户得看得见）
        if (res.message) {
            document.getElementById('wm-status').innerText = '⚠ ' + res.message;
        }
    } else {
        showPreviewHint('watermark', (res && res.message) || uiText('dyn_1079', '预览生成失败'),
                        res && res.message ? '' : 'dyn_1079');
    }
}

async function doWatermark() {
    const pdf = selectedFiles['watermark'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    const p = wmParams();
    const out = await callApi('save_file_dialog', 'watermarked.pdf');
    if (!out || typeof out !== 'string') return;
    const res = await callApi('apply_watermark', pdf, out, p.text, p.color, p.opacity,
                              p.diagonal, p.size, p.spacing, p.font, p.latex);
    if (res && res.success) {
        let text = '✓ ' + out;
        if (res.message) {
            // 成品其实是用内置字体出的（所选字体读不出来）：成功也要把那句话说完，
            // 状态行带 ⚠ 后缀并弹一条 toast，不能只留一个 ✓
            text += ' ⚠ ' + res.message;
            showToast(res.message, 5200);
        }
        document.getElementById('wm-status').innerText = text;
        if (!res.message) showToast(uiText('dyn_1081', '水印已应用'));
    } else {
        reportFailure(res, document.getElementById('wm-status'), uiText('dyn_1082', '水印应用失败'));
    }
}

// ==================== 压缩 ====================
async function doCompress() {
    const pdf = selectedFiles['compress'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    const level = document.getElementById('compress-level').value;
    const out = await callApi('save_file_dialog', 'compressed.pdf');
    if (!out || typeof out !== 'string') return;
    const res = await callApi('compress_pdf', pdf, out, level);
    if (res && res.success) {
        // 顺带把体积变化显示出来，用户才知道这次压缩到底有没有效果
        let text = '✓ ' + out;
        if (res.original_size && res.new_size) {
            const before = (res.original_size / 1024 / 1024).toFixed(2);
            const after = (res.new_size / 1024 / 1024).toFixed(2);
            text += `（${before} MB → ${after} MB）`;
        }
        document.getElementById('compress-status').innerText = text;
        showToast(res.message ? res.message : uiText('dyn_1084', '压缩完成'), res.message ? 4200 : 2400);
    } else {
        reportFailure(res, document.getElementById('compress-status'), uiText('dyn_1085', '压缩失败'));
    }
}

// ==================== 批量 ====================
function toggleBatchOptions() {
    const op = document.getElementById('batch-op').value;
    ['split', 'convert', 'compress'].forEach(name => {
        document.getElementById('batch-' + name + '-option')
            .classList.toggle('hidden', name !== op);
    });
    // 按钮跟着上面选的操作改口；文案没取回来时用字典里那几个字兜底
    const btn = document.getElementById('batch-start');
    if (!btn) return;
    const key = 'start_batch_' + op;
    btn.setAttribute('data-i18n', key);
    btn.textContent = (currentTextDict && currentTextDict[key])
        || {split: uiText('start_batch_split', '开始批量拆分'), convert: uiText('start_batch_convert', '开始批量转换'), compress: uiText('start_batch_compress', '开始批量压缩')}[op];
}

async function addBatchFiles() {
    const path = await callApi('open_file_dialog');
    if (!path || typeof path !== 'string') return;
    batchFilesList.push(path);
    document.getElementById('batch-status').innerText = uiTextf('dyn_1089', '✓ 已加入 {0} 个文件', batchFilesList.length);
}

async function doBatch() {
    if (batchFilesList.length === 0) { showToast(uiText('dyn_1090', '请先添加要处理的文件')); return; }
    const op = document.getElementById('batch-op').value;
    const statusEl = document.getElementById('batch-status');
    statusEl.innerText = uiTextf('dyn_1091', '⏳ 正在{0} {1} 个文件…', {split: uiText('batch_split', '批量拆分'), convert: uiText('batch_convert', '批量转换'), compress: uiText('batch_compress', '批量压缩')}[op], batchFilesList.length);
    let res;
    if (op === 'split') {
        res = await callApi('batch_split', batchFilesList,
                            document.getElementById('batch-n').value);
    } else if (op === 'compress') {
        res = await callApi('batch_compress', batchFilesList,
                            document.getElementById('batch-compress-level').value);
    } else {
        res = await callApi('batch_convert', batchFilesList,
                            document.getElementById('batch-format').value);
    }

    // 旧代码把返回值丢掉后无条件显示「完成」，
    // 即使每个文件都失败也照样报成功
    const results = (res && res.results) || [];
    const failed = results.filter(r => !r.success);

    if (!res || !res.success || failed.length) {
        const first = failed.length
            ? (failed[0].message || uiText('dyn_1096', '未知错误'))
            : ((res && res.message) || uiText('dyn_1096', '未知错误'));
        const totalCount = results.length || batchFilesList.length;
        statusEl.innerText = uiTextf('dyn_1097', '✗ {0}/{1} 个文件失败：{2}', failed.length, totalCount, first);
        showToast(uiText('dyn_1098', '批量处理存在失败项'), 4200);
        return;
    }

    statusEl.innerText = uiTextf('dyn_1099', '✓ 完成（{0} 个文件）', results.length);
    if (op === 'compress') {
        // 批量压缩到底值不值，看的是省了多少字节；只报「几个文件完成」用户没概念。
        const mb = n => (n / 1024 / 1024).toFixed(2);
        if (res.original_size && res.new_size) {
            statusEl.innerText = uiTextf('dyn_1100', '✓ 完成（{0} 个文件，{1} MB → {2} MB）', results.length, mb(res.original_size), mb(res.new_size));
        }
        if (res.ineffective) {
            statusEl.innerText += uiTextf('dyn_1101', '，{0} 个没变小', res.ineffective);
            showToast(uiTextf('dyn_1102', '有 {0} 个文件压完体积没减小：本来就是纯文字、或者已经压得很干，可以删掉那几份成品', res.ineffective), 4800);
        }
    }
    showToast(uiText('dyn_1103', '批量处理完成'));
    // 成功后清空列表，避免下一轮把上次的文件又处理一遍
    batchFilesList = [];
}

// ==================== 加密 / 解密 ====================
async function doEncrypt() {
    const pdf = requirePdf('encrypt');
    if (!pdf) return;
    const userPw = document.getElementById('enc-user-pw').value;
    const ownerPw = document.getElementById('enc-owner-pw').value;
    const out = await callApi('save_file_dialog', 'encrypted.pdf');
    if (!out || typeof out !== 'string') return;
    const res = await callApi('encrypt_pdf', pdf, out, userPw, ownerPw);
    if (res && res.success) {
        document.getElementById('encrypt-status').innerText = '✓ ' + out;
        showToast(uiText('dyn_1104', '加密完成'));
    } else {
        reportFailure(res, document.getElementById('encrypt-status'), uiText('dyn_1105', '加密失败'));
    }
}

async function doDecrypt() {
    const pdf = requirePdf('encrypt');
    if (!pdf) return;
    const password = prompt(uiText('dyn_1106', '请输入密码：')) || '';
    const out = await callApi('save_file_dialog', 'decrypted.pdf');
    if (!out || typeof out !== 'string') return;
    const res = await callApi('decrypt_pdf', pdf, out, password);
    if (res && res.success) {
        document.getElementById('encrypt-status').innerText = '✓ ' + out;
        showToast(uiText('dyn_1107', '解密完成'));
    } else {
        reportFailure(res, document.getElementById('encrypt-status'), uiText('dyn_1108', '解密失败'));
    }
}

// ==================== 格式转换 ====================
// 保存对话框的过滤器默认只放开 *.pdf，转成 Office 格式时已存在的 .docx/.xlsx/.pptx
// 会被隐藏，用户既看不见也覆盖不了旧文件，所以按目标格式给对应的过滤器。
const CONVERT_FILTERS = {
    docx: ['Word Document (*.docx)'],
    xlsx: ['Excel Workbook (*.xlsx)'],
    pptx: ['PowerPoint Presentation (*.pptx)'],
};

async function doConvert() {
    // LaTeX 方向先分流：这一格要的是用户选的 .tex（tools.js 的 selectTexFile 记的路径），
    // 不是这一页共用的那份 PDF；放在「请先选择 PDF」之前判，否则选好了 .tex 也会被挡回去
    if (val('convert-direction') === 'latex') { convertLatexToPdf(); return; }
    const pdf = selectedFiles['convert'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    const fmt = document.getElementById('convert-format').value;
    const statusEl = document.getElementById('convert-status');
    let res;
    if (fmt === 'png' || fmt === 'jpeg') {
        const dir = await callApi('select_directory');
        if (!dir || typeof dir !== 'string') return;
        statusEl.innerText = uiText('dyn_1111', '⏳ 转换中...');
        res = await callApi('convert_to_images', pdf, dir, fmt);
    } else {
        const out = await callApi('save_file_dialog', 'converted.' + fmt, CONVERT_FILTERS[fmt] || []);
        if (!out || typeof out !== 'string') return;
        statusEl.innerText = uiText('dyn_1111', '⏳ 转换中...');
        if (fmt === 'docx') res = await callApi('convert_to_docx', pdf, out);
        else if (fmt === 'xlsx') res = await callApi('convert_to_xlsx', pdf, out);
        else if (fmt === 'pptx') res = await callApi('convert_to_pptx', pdf, out);
    }
    if (res && res.success) {
        statusEl.innerText = uiText('dyn_1112', '✓ 完成');
        showToast(uiText('dyn_1113', '转换完成'));
    } else {
        statusEl.innerText = '✗ ' + (res && res.message ? res.message : '');
    }
}

// ==================== 元数据 ====================
async function loadMetadata() {
    const pdf = selectedFiles['metadata'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    const res = await callApi('get_metadata', pdf);
    if (res && res.success) {
        document.getElementById('meta-title').value = res.title || '';
        document.getElementById('meta-author').value = res.author || '';
        document.getElementById('meta-subject').value = res.subject || '';
        document.getElementById('meta-keywords').value = res.keywords || '';
        showToast(uiText('dyn_1115', '元数据已读取'));
    } else {
        reportFailure(res, document.getElementById('metadata-status'), uiText('dyn_1116', '读取元数据失败'));
    }
    // 工具链那一栏和上面这件事各自独立：元数据没读出来（比如加密）也要照自己的回包说话
    await loadTexProvenance(pdf);
}

/**
 * 「生成工具链」那一栏：判这份 PDF 是不是 TeX 工具链排的、走的哪条路、字体内嵌了没有。
 *
 * 话术全部来自后端（engine_label / font_note_texts / warning_texts 都按界面语言解析好了），
 * 前端只拼标签。读不出来就整块收起，不拿「未标注」冒充「没有」，
 * 也不让这一栏的失败打断元数据那件正事。
 */
const TEX_FONT_ROWS = 30;

async function loadTexProvenance(pdf) {
    const box = document.getElementById('meta-tex');
    const engine = document.getElementById('meta-tex-engine');
    const detail = document.getElementById('meta-tex-detail');
    if (!box || !engine || !detail) return;
    const res = await callApi('detect_tex_provenance', pdf);
    if (!res) { box.classList.add('hidden'); return; }
    if (res.success) {
        let head = uiTextf('tex_prov_engine', '排版引擎：{0}', res.engine_label || '');
        if (res.version) {
            head += ' ' + res.version;
        } else if (res.is_tex) {
            // 认出了引擎、字符串里却没写版本：明说「未标注」，不替它编一个
            head += ' ' + uiText('tex_prov_no_version', '未标注版本');
        }
        if (res.via_dvi && res.dviprocessor) {
            head += '（' + uiTextf('tex_prov_via', '经 {0} 转出', res.dviprocessor) + '）';
        }
        engine.textContent = cleanControlChars(head);
    } else {
        engine.textContent = cleanControlChars(res.message || '');
    }
    const rows = [];
    // 展开区里的每一项都是「文件里写的原文 / 字体名」这类外来数据：先去掉控制字符再转义，
    // 只转义不管的话一个 NUL 就能把那一行截断（esc 只管 & < > "）
    const saf = (t) => esc(cleanControlChars(t));
    const push = (html) => rows.push('<div class="caps-item">' + html + '</div>');
    if (res.creator) {
        push(saf(uiTextf('tex_prov_creator', 'Creator：{0}', '')) + saf(res.creator));
    }
    if (res.producer) {
        push(saf(uiTextf('tex_prov_producer', 'Producer：{0}', '')) + saf(res.producer));
    }
    const fonts = res.fonts || [];
    if (fonts.length) {
        push(saf(uiTextf('tex_prov_fonts', '字体 {0} 支', fonts.length)));
        fonts.slice(0, TEX_FONT_ROWS).forEach(function (f) {
            const flags = [];
            if (f.embedded) flags.push(uiText('tex_prov_embedded', '已内嵌'));
            if (f.subset_prefix) flags.push('ABCDEF+');
            push('<span class="caps-cmd">' + saf(f.name || '') + '</span> '
                + saf(f.type || '') + (flags.length ? '（' + saf(flags.join(' / ')) + '）' : ''));
        });
        if (fonts.length > TEX_FONT_ROWS) {
            push(saf(uiTextf('latex_rows_cut', '（下面只列前 {0} 条，共 {1} 条）',
                TEX_FONT_ROWS, fonts.length)));
        }
    }
    (res.font_note_texts || []).concat(res.warning_texts || []).forEach(function (line) {
        if (line) push(saf(line));
    });
    if (res.detail) {
        // 异常摘要只进展开区，不占主行（主行那句已经是能看懂的话）
        push('<span class="caps-line">' + saf(String(res.detail)) + '</span>');
    }
    detail.innerHTML = rows.length
        ? rows.join('')
        : '<div class="caps-item">' + saf(uiText('tex_no_detail', '没有更多信息')) + '</div>';
    box.classList.remove('hidden');
}

/** 文件里的 /Info 字符串是外来数据：控制字符（含 NUL）留在界面上会把那一行截断，先去干净。 */
function cleanControlChars(text) {
    return String(text || '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
}

async function saveMetadata() {
    const pdf = selectedFiles['metadata'];
    if (!pdf) { showToast(uiText('dyn_1117', '请先选择 PDF')); return; }
    const out = await callApi('save_file_dialog', 'metadata.pdf');
    if (!out || typeof out !== 'string') return;
    const res = await callApi('save_metadata',
        pdf, out,
        document.getElementById('meta-title').value,
        document.getElementById('meta-author').value,
        document.getElementById('meta-subject').value,
        document.getElementById('meta-keywords').value
    );
    if (res && res.success) {
        document.getElementById('metadata-status').innerText = '✓ ' + out;
        showToast(uiText('dyn_1118', '元数据已保存'));
    } else {
        reportFailure(res, document.getElementById('metadata-status'), uiText('dyn_1119', '保存元数据失败'));
    }
}

// ==================== 最近文件 ====================
async function loadRecentFiles() {
    const files = await callApi('get_recent_files');
    const container = document.getElementById('recent-files');
    if (!container || !Array.isArray(files)) return;
    container.innerHTML = files.length === 0
        ? `<span style="color:var(--text-secondary);">${currentTextDict['no_recent'] || '暂无最近文件'}</span>`
        : files.map(f => `
            <div class="recent-item">
                <span onclick="openRecent('${f.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')">${f.split(/[\\/]/).pop()}</span>
                <button onclick="removeRecent('${f.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')">✕</button>
            </div>`).join('');
}

async function openRecent(path) {
    selectedFiles['viewer'] = path;
    const viewerBtn = Array.from(document.querySelectorAll('.nav-btn'))
        .find(b => b.querySelector('[data-i18n="viewer"]'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-viewer').classList.add('active');
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    if (viewerBtn) viewerBtn.classList.add('active');
    await loadViewerPreview(path);
}

async function removeRecent(path) {
    await callApi('remove_recent_file', path);
    loadRecentFiles();
}

// ==================== 签名画板 ====================
function initSignCanvas() {
    const canvas = document.getElementById('sign-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    ctx.strokeStyle = '#000';
    ctx.lineWidth = 2;
    ctx.lineCap = 'round';
    let drawing = false;
    canvas.addEventListener('mousedown', (e) => {
        drawing = true;
        ctx.beginPath();
        ctx.moveTo(e.offsetX, e.offsetY);
    });
    canvas.addEventListener('mousemove', (e) => {
        if (!drawing) return;
        ctx.lineTo(e.offsetX, e.offsetY);
        ctx.stroke();
    });
    canvas.addEventListener('mouseup', () => { drawing = false; });
    canvas.addEventListener('mouseleave', () => { drawing = false; });
}

// ==================== 首次启动设置向导 ====================
// 向导是主界面里的一层全屏遮罩，不再另开窗口：多一个 WebView 实例就多一份内存
// 和一套窗口生命周期问题，而这几步都只是改配置。
// 条款是第一屏而且不能跳过：不同意就没有后面的语言、配色和更新选项。
// 语言紧跟在条款后面、排所有选项的第一位：选了它，向导自己就得用那句话说话。
// 右键菜单（shell）排在更新之后当最后一步：它和「自动检查更新」都属于「要不要动这台电脑」
// 那一类决定；步序里插在哪都要跟着 splash2/phase3 的向导断言走，这一步是新加的第五步。
const WIZARD_STEPS = ['license', 'language', 'appearance', 'update', 'shell'];
const WIZARD_TITLE_KEYS = { license: 'wiz_title_license', language: 'wiz_title_language',
                            appearance: 'wiz_title_appearance', update: 'wiz_title_update',
                            shell: 'wiz_title_shell' };
// 这一次实际要走哪几步。老用户覆盖安装/升级时只留条款那一步（见 openWizard 的 licenseOnly）。
let wizardSteps = WIZARD_STEPS;
let wizardIndex = 0;
let wizardLicenseLoaded = false;
// 同步在途：这期间点「同意」等于没看过这一版条款，得拦住
let wizardLicensePending = false;

function wizardTheme() {
    const active = document.querySelector('#wizard-themes .swatch.active');
    return active ? active.dataset.value : 'blue';
}

function wizardRenderThemes(selected) {
    const box = document.getElementById('wizard-themes');
    if (!box) return;
    box.innerHTML = '';
    themeOptions().forEach(option => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'swatch' + (option.value === selected ? ' active' : '');
        button.dataset.value = option.value;
        button.innerHTML = `<i style="background:${option.color}"></i>${option.label}`;
        button.onclick = () => { wizardRenderThemes(option.value); wizardPreviewTheme(); };
        box.appendChild(button);
    });
}

/** 还没点完成也先让用户看到效果 */
function wizardPreviewTheme() {
    toggleDarkModeClass(document.getElementById('wizard-dark').checked);
    applyThemeColor(wizardTheme());
}

function wizardRenderDots() {
    const box = document.getElementById('wizard-dots');
    if (!box) return;
    box.innerHTML = '';
    wizardSteps.forEach((name, i) => {
        const dot = document.createElement('span');
        dot.className = 'wizard-dot' + (i === wizardIndex ? ' active' : (i < wizardIndex ? ' done' : ''));
        box.appendChild(dot);
    });
}

function wizardShowStep(index) {
    wizardIndex = Math.max(0, Math.min(index, wizardSteps.length - 1));
    const current = wizardSteps[wizardIndex];
    document.querySelectorAll('#wizard .wizard-step').forEach(step => {
        step.classList.toggle('hidden', step.dataset.step !== current);
    });
    const title = document.getElementById('wizard-title');
    if (title) title.innerText = uiText(WIZARD_TITLE_KEYS[current] || '', uiText('app_title', 'PDF Toolkit'));
    wizardRenderDots();
    const page = document.getElementById('wizard-page');
    if (page) page.innerText = `${wizardIndex + 1} / ${wizardSteps.length}`;
    // 第一屏只有同意 / 不同意，后两屏才是上一步 / 稍后再说 / 下一步
    const accept = document.getElementById('wizard-foot-accept');
    const nav = document.getElementById('wizard-foot-nav');
    if (accept) accept.classList.toggle('hidden', wizardIndex !== 0);
    if (nav) nav.classList.toggle('hidden', wizardIndex === 0);
    const next = document.getElementById('wizard-next');
    if (next) next.innerText = wizardIndex === wizardSteps.length - 1
        ? uiText('wiz_finish', '完成设置') : uiText('wiz_next', '下一步');
    if (current === 'license') wizardLoadLicense();
    if (current === 'language') wizardRenderLanguages();
}

// ==================== 向导里的「界面语言」这一步 ====================
// 语言清单和「这台电脑是什么语言」都问后端（core/i18n.py 认 Windows 的 UI 语言），
// 前端只负责列出来。选完立刻把整套文案换掉，包括向导自己这几句 —— 用户点下去
// 就该看到自己那门语言，而不是等点「完成设置」。
let wizardLangInfo = null;

function nameOfLanguage(info, code) {
    const hit = (info.available || []).find(a => a.code === code);
    return hit ? hit.name : (code || '');
}

async function wizardRenderLanguages() {
    const box = document.getElementById('wizard-lang');
    if (!box) return;
    if (!wizardLangInfo) wizardLangInfo = await callApi('get_language_info');
    const info = wizardLangInfo || {};
    const config = await callApi('load_config');
    box.innerHTML = '';
    (info.available || []).forEach(item => {
        const option = document.createElement('option');
        option.value = item.code;
        // 「跟随系统」这一项后面括注它实际会得到什么，免得选了却不知道变成哪种语言
        option.innerText = item.code === 'auto'
            ? uiText('lang_auto', '跟随系统') + '（' + nameOfLanguage(info, info.system) + '）'
            : item.name;
        box.appendChild(option);
    });
    const stored = (config && config.language) || info.stored || 'auto';
    box.value = (info.available || []).some(a => a.code === stored) ? stored : 'auto';
    const note = document.getElementById('wizard-lang-note');
    if (note) {
        note.innerText = box.value === 'auto'
            ? uiText('lang_auto_desc', '自动使用这台电脑的系统语言') + '：' + nameOfLanguage(info, info.system)
            : uiText('language_desc', '选一种界面语言，改完立即生效；下一次启动仍然用它。');
    }
}

/** 选语言：立刻落盘并刷一次文案，向导自己也跟着换语言（其余几步仍是点「完成设置」才写）。 */
async function wizardLanguageChanged(code) {
    const config = await callApi('load_config');
    if (config && typeof config === 'object') {
        config.language = code;
        if (!await callApi('save_config', config)) {
            showToast(uiText('language_changed_fail', '界面语言写入配置失败，请查看日志'), 4000);
        }
    }
    await applyLanguage();
    await wizardRenderLanguages();
    const title = document.getElementById('wizard-title');
    if (title) title.innerText = uiText(WIZARD_TITLE_KEYS[wizardSteps[wizardIndex]] || '', uiText('app_title', 'PDF Toolkit'));
    const next = document.getElementById('wizard-next');
    if (next) next.innerText = wizardIndex === wizardSteps.length - 1
        ? uiText('wiz_finish', '完成设置') : uiText('wiz_next', '下一步');
    const skip = document.getElementById('wizard-skip');
    if (skip) skip.innerText = uiText('wiz_later', '稍后再说');
}
window.wizardLanguageChanged = wizardLanguageChanged;

async function wizardLoadLicense() {
    if (wizardLicenseLoaded || wizardLicensePending) return;
    const box = document.getElementById('wizard-license-text');
    if (!box) return;
    /*
     * 第一次启动（含覆盖安装升级）后端会真去联网取当前版本的条款，最坏几秒。
     * 这期间把「同意并继续」按住：让人在空白/旧文案上点同意等于没签这份条款。
     */
    const accept = document.getElementById('wizard-accept');
    wizardLicensePending = true;
    if (accept) accept.disabled = true;
    box.innerText = uiText('dyn_1442', '正在同步最新使用条款…');
    let res = null;
    try {
        res = await callApi('get_license_text', false);
    } finally {
        wizardLicensePending = false;
        if (accept) accept.disabled = false;
    }
    if (res && res.success) {
        /* i18n-log 这三句进的是日志，不跟着界面换语言（日志一直是简体中文才有人查得动） */
        const source = res.bundled ? '内置副本' : (res.cached ? '本地缓存' : '更新源');
        callApi('wizard_event', 'license', source + (res.stale ? '（离线）' : ''));
        /* i18n-log-end */
        box.innerHTML = renderMarkdown(res.text) + wizardLicenseNote(res);
        wizardLicenseLoaded = true;
    } else {
        callApi('wizard_event', 'license', '获取失败');
        box.innerText = (res && res.message) || uiText('dyn_1447', '暂时无法获取使用条款，请检查网络后重试。');
    }
}

/** 离线退回旧正文时在文末补一句说明，别让「内置那份」冒充最新版本。 */
function wizardLicenseNote(res) {
    if (!res || !res.stale) return '';
    const what = res.bundled ? uiText('dyn_1448', '随程序内置的条款') : uiText('dyn_1449', '上次缓存的条款');
    return '<p class="wizard-license-note">' + uiTextf('dyn_1450', '当前离线，以上为{0}；恢复联网后重开向导即可同步最新版本。', what) + '</p>';
}

/**
 * 打开向导。条款是第一屏且不能跳过，不同意就退出程序。
 *
 * licenseOnly=true 用于「老用户覆盖安装 / 版本升级」：只把这一版条款摆出来问同意不同意，
 * 不再走外观和更新两步——人家的设置已经调好了，重装一次就被改配色是不可接受的。
 * 首次安装（配置里 first_run_completed 还没置上）走完整三步。
 */
function openWizard(licenseOnly) {
    const mask = document.getElementById('wizard');
    if (!mask) return;
    wizardSteps = licenseOnly ? ['license'] : WIZARD_STEPS;
    if (!licenseOnly) {
        callApi('load_config').then(config => {
            const cfg = (config && typeof config === 'object') ? config : {};
            wizardRenderThemes(cfg.theme_color || 'blue');
            document.getElementById('wizard-dark').checked = !!cfg.dark_mode;
            document.getElementById('wizard-notes').checked = cfg.show_feature_notes !== false;
            document.getElementById('wizard-update').checked = cfg.check_update_on_startup !== false;
            // 右键菜单出厂是「关」：这一项动的是用户自己的资源管理器，向导不许先斩后奏，
            // 初值照配置里存的现状画（和设置窗口 #opt-shell-menu 同一个键）
            document.getElementById('wizard-shell').checked = !!cfg.shell_context_menu;
            wizardPreviewTheme();
        });
    }
    mask.classList.remove('hidden');
    wizardShowStep(0);
    callApi('wizard_event', 'shown', licenseOnly ? '只确认条款' : '完整五步');
}
window.openWizard = openWizard;

function wizardPrev() { wizardShowStep(wizardIndex - 1); }
function wizardNext() {
    if (wizardIndex < wizardSteps.length - 1) wizardShowStep(wizardIndex + 1);
    else wizardFinish(true);
}

function wizardDisagree() {
    // 条款没同意就不存在「先看看再说」的中间态：不同意只能退出，哪条路径都一样
    callApi('wizard_event', 'declined', '不同意使用条款');
    callApi('quit_app', '不同意使用条款');
}

/**
 * 同意条款的那一刻就把「这个版本已经走完向导」写进去：
 * 后面两步允许跳过，但不能因为用户随后关了程序就再拿条款拦他一次。
 */
async function wizardRecordAgreement() {
    const config = await callApi('load_config');
    if (!config || typeof config !== 'object') return false;
    return await markWizardDone(config);
}

/** 记下走完向导时的版本号和安装批次；下次启动只有换了版本或重装过才会再弹。 */
async function markWizardDone(config) {
    const info = await getAppInfo();
    config.first_run_completed = true;
    if (info.current_version) config.wizard_version = info.current_version;
    if (info.install_stamp) config.wizard_stamp = info.install_stamp;
    return await callApi('save_config', config);
}

async function wizardAccept() {
    if (wizardLicensePending) {
        showToast(uiText('dyn_1451', '正在同步使用条款，请稍候…'), 2000);
        return;
    }
    if (!wizardLicenseLoaded) {
        // 条款没落到屏上就不算看过：这时点「同意」等于签一份空白的条款，改成重试同步
        showToast(uiText('dyn_1452', '使用条款还没加载出来，正在重试…'), 2500);
        wizardLoadLicense();
        return;
    }
    const saved = await wizardRecordAgreement();
    if (!saved) showToast(uiText('dyn_1453', '条款已同意，但设置写入失败，请查看日志'), 4000);
    callApi('wizard_event', 'agreed', saved ? '条款已同意' : '条款同意但写入失败');
    // 只确认条款的那一种走法到这里就到底了：applySettings=false，用户原有的配色和更新开关一个字都不改
    if (wizardIndex >= wizardSteps.length - 1) wizardFinish(false);
    else wizardShowStep(wizardIndex + 1);
}

/** applySettings=false 就是「稍后再说」：只记已完成，不动用户现有设置。 */
async function wizardFinish(applySettings) {
    const mask = document.getElementById('wizard');
    let config = await callApi('load_config');
    if (!config || typeof config !== 'object') config = {};
    if (applySettings) {
        const lang = document.getElementById('wizard-lang');
        if (lang && lang.value) config.language = lang.value;
        config.theme_color = wizardTheme();
        config.dark_mode = document.getElementById('wizard-dark').checked;
        config.show_feature_notes = document.getElementById('wizard-notes').checked;
        config.check_update_on_startup = document.getElementById('wizard-update').checked;
        // 右键菜单：只有这一步的目标和配置里存的现状不一样才真的去动注册表
        // （反复走完向导不该一遍遍敲用户的 HKCU；和设置窗口 applySettings 同一口径）。
        // 落盘以 set_context_menu 回读注册表的 enabled 为准——没写成的那一次不许存「假的 true」。
        const shellBox = document.getElementById('wizard-shell');
        if (shellBox) {
            const want = !!shellBox.checked;
            if (want !== !!config.shell_context_menu) {
                const shellRes = await callApi('set_context_menu', want);
                config.shell_context_menu = (shellRes && typeof shellRes.enabled === 'boolean')
                    ? shellRes.enabled : !!config.shell_context_menu;
                shellBox.checked = config.shell_context_menu;
                if (shellRes && !shellRes.success) {
                    showToast(shellRes.message || uiText('set_shell_menu_fail',
                        '右键菜单设置失败，详见运行日志'), 5000);
                }
            } else {
                config.shell_context_menu = !!config.shell_context_menu;
            }
        }
    }
    const saved = await markWizardDone(config);
    if (!saved) {
        callApi('wizard_event', 'save_failed', applySettings ? '完成设置时写入失败' : '跳过时写入失败');
        showToast(uiText('dyn_1454', '设置写入失败，请查看日志'), 4000);
        return;
    }
    callApi('wizard_event', applySettings ? 'completed' : 'skipped',
        applySettings ? '完成设置' : '稍后再说');
    if (applySettings) {
        wizardPreviewTheme();
        await callApi('broadcast_settings');
    }
    if (mask) mask.classList.add('hidden');
    if (applySettings) showToast(uiText('dyn_1455', '设置已完成，开始使用吧'));
    // 启动时因为向导而压住的更新检查，等界面真正露出来再做
    checkUpdateOnStartup();
}

function wizardSkip() { wizardFinish(false); }

// ==================== 启动时静默检查更新 ====================
// 只有「确实发现新版本」才弹窗；已是最新、本地版本更高、断网、
// 超时、解析失败等情况一律静默，不打扰用户。
async function checkUpdateOnStartup() {
    const config = await callApi('load_config');
    if (config && config.check_update_on_startup === false) return;

    const res = await callApi('check_update_silent');
    if (res && res.update_available) {
        showUpdateDialog(res.version, res.url);
    }
}

// ==================== 初始化 ====================
async function init() {
    await applyLanguage();
    const version = await applyVersion();

    const info = await getAppInfo();
    const label = document.getElementById('app-version');
    if (label) {
        const credit = info.author ? ` ©${info.author}` : '';
        label.innerText = version ? `v${version}${credit}` : credit.trim();
    }

    const config = await applyStoredSettings();
    // 配置里那两项（收没收起、准不准悬停展开）落定之后才挂，早挂会在「还没读到配置」那一瞬乱浮
    bindSidebarHover();

    // 首页卡片的置顶顺序已经由 applyStoredSettings 按配置排过（广播时也走同一条）；
    // 清晰度控件还得单独点名，不然没打开文件时它一直指着 64，用户以为那是临时值而不是自己的设置。
    pmSetThumbDpi((config || {}).thumb_dpi);

    toggleSplitMode();
    toggleStampSource();
    toggleStampPageMode();
    toggleBatchOptions();
    initSignCanvas();
    loadRecentFiles();
    renderWatermarkPresets();
    // 预览窗一进来就该是空态：翻页按钮禁用、页码框只读，印章页给出「请先选择 PDF」的提示，
    // 否则用户能在没有文档的窗口上点 ◀ ▶、往页码框里敲数字。
    syncPreviewNav('watermark');
    syncPreviewNav('stamp');
    renderPreviewPane('stamp');
    // 启动时按「对角线平铺」的初始勾选状态同步密度滑块的可用性，顺带排一次预览
    wmDiagonalChanged();
    setViewerLoaded(false);
    // 页面管理一进来就没有文档，工具条上的操作按钮该是禁用态，而不是点一下才提示
    pmBindGrid();
    pmSyncToolbar();
    // 新页面（内容提取/排版与整理/安全与整理、转换页新增方向）的控件初值。放在 applyLanguage
    // 之后，因为它们要用文案字典；tools.js 在 app.js 后面加载，这里只认这一个入口。
    if (typeof toolsInit === 'function') toolsInit();
    // 本机字体清单：冷扫描 3 秒上下（热 0.3 s），绝不能压在开窗口那一下——界面就绪后
    // 延迟排一次异步预热，回来自动把四个「字体」下拉填上（common.js fontsWarmup）。
    setTimeout(fontsWarmup, 1200);

    const wmText = document.getElementById('wm-text');
    const wmColor = document.getElementById('wm-color');
    if (wmText) wmText.addEventListener('input', loadWatermarkPreview);
    if (wmColor) wmColor.addEventListener('input', loadWatermarkPreview);
    // Ctrl+P 打印（查看页）：绑在 document 上，页面管理那套按键各管各的，互不抢
    document.addEventListener('keydown', printHandleShortcut);

    // 向导按「版本 + 安装批次」判定：配置里记的版本和当前程序版本不一样，或者这台机器
    // 又装过一次（安装器每次装完都换一个新的 install.stamp），就说明这一回还没走过向导。
    // 只比版本号的话，同一个版本反复重装永远只弹第一次；读不到配置或拿不到版本号时不弹——
    // 那种情况弹了也记不住，只会每次启动拦一次。
    const newVersion = config && config.wizard_version !== info.current_version;
    const newInstall = !!(config && info.install_stamp && config.wizard_stamp !== info.install_stamp);
    const needWizard = !!(config && info.current_version && (newVersion || newInstall));
    // 分两种走法：从没做完过首次设置的是新装，走完整三步；已经用着的（升级或覆盖安装）
    // 只弹条款确认，外观和更新两步不出现，原有设置一律不动。
    const firstInstall = !(config && config.first_run_completed);
    if (needWizard) openWizard(!firstInstall);
    // 不 await：更新检查不应该拖慢界面就绪；向导挡着时先不检查，免得弹窗在向导背后
    else checkUpdateOnStartup();
}

window.addEventListener('pywebviewready', init);