// ============================================================================
// 设置窗口逻辑。
// callApi / showToast / 主题 / 弹窗 等公共部分在 js/common.js（THEME_OPTIONS 也在那里）。
// ============================================================================

let appInfo = {};
let licenseLoaded = false;
let changesLoaded = false;
// 右键菜单开关「设置窗口打开时读到的那份现状」：null = 还没读到配置。
// applySettings 只在目标状态和它不一样时才去动注册表——反复点「应用」
// 不该一遍遍敲用户的注册表（不擅自改用户的东西，从少动手开始）。
let shellMenuCurrent = null;

function setStatus(text) {
    const el = document.getElementById('settings-status');
    if (el) el.innerText = text || '';
}

// ==================== 标签页 ====================
function switchTab(name, event) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    const panel = document.getElementById('tab-' + name);
    if (panel) panel.classList.add('active');

    const button = (event && event.currentTarget)
        || document.querySelector(`.tab[data-tab="${name}"]`);
    if (button) button.classList.add('active');
}

/**
 * 标签页内部的「小页」切换。三个标签页各有一组 .subtabs / .subpanel，
 * 所以清理和查找都必须圈在自己那一页里：全局 querySelectorAll 会让
 * 「关于」的小页按钮把基础设置刚点亮的那格按灭（反过来也一样）。
 * 面板 id 一律是 `{页}-{小页}`，和 HTML 里 data-sub 对上。
 */
function switchSub(page, name, event) {
    const scope = document.getElementById('tab-' + page);
    if (!scope) return;
    scope.querySelectorAll('.subtab').forEach(t => t.classList.remove('active'));
    scope.querySelectorAll('.subpanel').forEach(p => p.classList.remove('active'));

    const panel = document.getElementById(page + '-' + name);
    if (panel) panel.classList.add('active');

    const button = (event && event.currentTarget)
        || scope.querySelector(`.subtab[data-sub="${name}"]`);
    if (button) button.classList.add('active');
}

/** 基础 / 高级两页的小页切换（外观·界面行为·首次设置 / 更新·性能与暂存·日志与诊断）。 */
function switchSettingSub(page, name, event) {
    switchSub(page, name, event);
}

/**
 * 「关于」里的三个子页：基本信息 / 更新日志 / 使用条款。
 * 联网正文第一次切到才拉，打开设置窗口时不多花两次网络等待。
 */
function switchAboutSub(name, event) {
    switchSub('about', name, event);

    if (name === 'changes' && !changesLoaded) {
        changesLoaded = true;
        loadChanges(false, true);
    }
    if (name === 'license' && !licenseLoaded) {
        licenseLoaded = true;
        loadLicense(false, true);
    }
}

/** 使用条款、更新日志都按「本机这一版同步过没有」决定要不要联网，同步过直接吃本地缓存；
 *  离线才退回旧副本。两者展示逻辑一样，共用一份。 */
function renderFetchedText(res, box, meta, label) {
    if (res && res.success) {
        box.innerHTML = renderMarkdown(res.text);
        if (res.stale && res.refreshing) {
            // 后端只等了一小会儿就把本地这份端出来，联网那趟还在后台跑：
            // 说清楚「这份是本地的，最新的正在后台取」，别让人以为界面卡住了或就是这份了
            meta.textContent = uiTextf('md_refreshing',
                '网络较慢，先显示本地的{0}；正在后台获取最新版本，下次打开即为最新', label);
        } else if (res.stale) {
            meta.textContent = res.bundled
                ? uiTextf('dyn_1334', '当前离线，显示的是随程序内置的{0}', label)
                : uiTextf('dyn_1335', '当前离线，显示的是上次缓存的{0}', label);
        } else if (res.bundled) {
            meta.textContent = uiTextf('dyn_1336', '随程序内置的{0}', label);
        } else if (res.cached) {
            meta.textContent = uiTextf('dyn_1337', '本地缓存的{0}', label);
        } else {
            meta.textContent = uiText('dyn_1338', '已从更新源获取最新版本');
        }
    } else {
        box.textContent = (res && res.message) || uiTextf('dyn_1339', '暂时无法获取{0}，请点击「在浏览器中查看」。', label);
        meta.textContent = '';
    }
}

// ==================== 使用条款 ====================
async function loadLicense(forceRefresh, firstLoad) {
    const box = document.getElementById('license-text');
    const meta = document.getElementById('license-meta');
    // 第一次切进来时后端可能真要走网络（缓存不是为当前版本同步的），先给一句等待提示
    if (forceRefresh || firstLoad) {
        box.textContent = uiText('dyn_1340', '正在从更新源获取最新版本…');
        meta.textContent = '';
    }

    const res = await callApi('get_license_text', !!forceRefresh);
    renderFetchedText(res, box, meta, uiText('wiz_title_license', '使用条款'));
}

async function refreshLicense() {
    await loadLicense(true);
}

async function openLicensePage() {
    const url = (appInfo && appInfo.license_page)
        || 'https://github.com/Yunmo-Xuan/PDF-Tools-Update/blob/main/LICENSE.md';
    await callApi('open_url', url);
}

// ==================== 更新日志 ====================
async function loadChanges(forceRefresh, firstLoad) {
    const box = document.getElementById('changes-text');
    const meta = document.getElementById('changes-meta');
    if (forceRefresh || firstLoad) {
        box.textContent = uiText('dyn_1342', '正在从更新源获取最新日志…');
        meta.textContent = '';
    }

    const res = await callApi('get_release_notes', !!forceRefresh);
    renderFetchedText(res, box, meta, uiText('set_changelog', '更新日志'));
    // 后端只留「本机大版本及以后」的条目，更新源还没写到这一版时过滤结果就是空的：
    // 给一句说明，别留个空白框
    if (res && res.success && !(res.text || '').trim()) {
        const major = res.major_version || '';
        box.textContent = uiTextf('dyn_1344', 'v{0}.x 及以后暂无更新记录，点「在浏览器中查看」可阅读完整更新日志。', major);
        meta.textContent = uiTextf('dyn_1345', '只展示 v{0} 及以后的更新记录', major);
    }
}

async function refreshChanges() {
    await loadChanges(true);
}

// ==================== 主题 ====================
function renderThemeSwatches(selected) {
    const box = document.getElementById('theme-swatches');
    if (!box) return;
    box.innerHTML = '';

    themeOptions().forEach(option => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'swatch' + (option.value === selected ? ' active' : '');
        button.dataset.value = option.value;
        button.innerHTML = `<i style="background:${option.color}"></i>${option.label}`;
        button.onclick = () => {
            renderThemeSwatches(option.value);
            previewTheme();
        };
        box.appendChild(button);
    });
}

/** 还没保存也先让用户看到效果 */
function previewTheme() {
    const active = document.querySelector('.swatch.active');
    toggleDarkModeClass(document.getElementById('opt-dark').checked);
    applyThemeColor(active ? active.dataset.value : 'blue');
}

// ==================== 载入 / 保存 ====================
async function loadAppInfo(preloaded) {
    appInfo = preloaded || await getAppInfo();

    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.innerText = value || '—';
    };
    setText('about-name', uiText('app_title', appInfo.name));
    setText('about-version', appInfo.version);
    setText('about-author', appInfo.author);
    setText('about-update-url', appInfo.update_page);
    setText('about-config', appInfo.config_file);
    setText('log-path', appInfo.log_dir || appInfo.log_file);
    setText('log-usage', describeLogUsage(appInfo.log_usage));
    setText('log-protected', describeProtectedLogUsage(appInfo.protected_log_usage));
}

async function loadSettings() {
    const config = await applyStoredSettings();

    renderThemeSwatches((config && config.theme_color) || 'blue');
    document.getElementById('opt-dark').checked = !!(config && config.dark_mode);
    document.getElementById('opt-notes').checked =
        !(config && config.show_feature_notes === false);
    document.getElementById('opt-nav-hover').checked =
        !(config && config.sidebar_hover_expand === false);
    document.getElementById('opt-ocr-preload').checked =
        !(config && config.ocr_preload === false);
    await renderLanguageOptions((config || {}).language);
    document.getElementById('opt-update').checked =
        !(config && config.check_update_on_startup === false);
    // 右键菜单出厂是「关」，和 DEFAULT_CONFIG 一致，所以这里用 !! 而不是别的开关那种「取非」
    shellMenuCurrent = !!(config && config.shell_context_menu);
    document.getElementById('opt-shell-menu').checked = shellMenuCurrent;
    // 这两项都按「认不出来就回到默认」处理：配置是手改过的也不能让下拉框显示空白
    selectOrDefault('opt-thumb-dpi', THUMB_DPI_OPTIONS, (config || {}).thumb_dpi, THUMB_DPI_DEFAULT);
    selectOrDefault('opt-draft-policy', DRAFT_POLICY_OPTIONS,
                    (config || {}).draft_policy, DRAFT_POLICY_DEFAULT);

    await loadAppInfo();
    setStatus('');
    // 控件里的勾/下拉这时才真的等于用户存的那份配置。验收脚本靠它判断
    // 「可以开始读了」，否则慢机器上量到的是出厂默认值而不是用户的设置。
    window.settingsLoaded = true;
}

/** 把配置值写进下拉框；值不认识（或压根没有）就选到默认项，别留一个空选中。 */
function selectOrDefault(id, values, value, fallback) {
    const el = document.getElementById(id);
    if (!el) return;
    const hit = values.map(String).includes(String(value));
    el.value = hit ? String(value) : String(fallback);
}

/** 读下拉框；控件不在了就退回默认值，别让配置里冒出一个 undefined。 */
function selectedValue(id, fallback) {
    const el = document.getElementById(id);
    return el && el.value ? el.value : fallback;
}

// 语言清单（含「这台电脑是什么语言」）来自后端 core/i18n.py，只问一次
let languageInfo = null;

/**
 * 填语言下拉框，并选中配置里存的那一档。
 *
 * 认不出来的值（配置文件被手改坏）一律退回「跟随系统」，而不是留一个空选中；
 * 「跟随系统」后面括注它实际会得到哪种语言，用户才知道这一项意味着什么。
 */
async function renderLanguageOptions(stored, preloadedInfo) {
    const box = document.getElementById('opt-language');
    if (!box) return;
    if (preloadedInfo) languageInfo = preloadedInfo;
    if (!languageInfo) languageInfo = await callApi('get_language_info');
    const info = languageInfo || {};
    box.innerHTML = '';
    (info.available || []).forEach(item => {
        const option = document.createElement('option');
        option.value = item.code;
        option.innerText = item.code === 'auto'
            ? uiText('lang_auto', '跟随系统') + '（' + languageLabel(info, info.system) + '）'
            : item.name;
        box.appendChild(option);
    });
    const wanted = stored || info.stored || 'auto';
    box.value = (info.available || []).some(a => a.code === wanted) ? wanted : 'auto';
}

async function applySettings(extra) {
    const config = await callApi('load_config');
    if (!config || typeof config !== 'object') {
        setStatus(uiText('dyn_1347', '✗ 读取配置失败'));
        showToast(uiText('set_load_config_fail', '读取配置失败'), 4000);
        return;
    }
    // 「语言到底变了没有」要拿**改之前**的那一档来比：这行必须在下面覆盖
    // config.language 之前取。它原来写在覆盖之后，langChanged 永远是 false，
    // 「关于→更新日志/使用条款」两行 meta（JS 现写的文字）在切语言后留着中文——
    // phase3 的 J18 就是逮这一处的（r55 轮实测红过一次，改回取真·旧值后回到绿）。
    const langBefore = config.language;

    const active = document.querySelector('.swatch.active');
    config.theme_color = active ? active.dataset.value : 'blue';
    config.dark_mode = document.getElementById('opt-dark').checked;
    config.show_feature_notes = document.getElementById('opt-notes').checked;
    config.sidebar_hover_expand = document.getElementById('opt-nav-hover').checked;
    config.ocr_preload = document.getElementById('opt-ocr-preload').checked;
    const langBox = document.getElementById('opt-language');
    if (langBox && langBox.value) config.language = langBox.value;
    config.check_update_on_startup = document.getElementById('opt-update').checked;
    config.thumb_dpi = Number(selectedValue('opt-thumb-dpi', THUMB_DPI_DEFAULT));
    config.draft_policy = selectedValue('opt-draft-policy', DRAFT_POLICY_DEFAULT);
    // 资源管理器右键菜单：先落控件值（恢复默认/验收都按这一行认这项被覆盖了）。
    // 只有这一次的目标和窗口打开时读到的现状不一样，才真的去写/删注册表；
    // 写不写得成不算界面说了算——按后端回读注册表后的 enabled 落盘，
    // 没写成的那一次配置里存的是实际状态，不许有一项「假的 true」。
    config.shell_context_menu = document.getElementById('opt-shell-menu').checked;
    if (shellMenuCurrent !== null && config.shell_context_menu !== shellMenuCurrent) {
        const shellRes = await callApi('set_context_menu', config.shell_context_menu);
        if (shellRes && typeof shellRes.enabled === 'boolean') {
            config.shell_context_menu = shellRes.enabled;
            document.getElementById('opt-shell-menu').checked = shellRes.enabled;
            shellMenuCurrent = shellRes.enabled;
        }
        if (shellRes && !shellRes.success) {
            showToast(shellRes.message || uiText('set_shell_menu_fail', '右键菜单设置失败，详见运行日志'), 5000);
        }
    }
    // 「恢复默认」要顺带清掉的那几项从参数里进来：这个窗口里没有它们的控件，
    // 平时点「应用」不许碰（否则一次应用就把用户的首页置顶和栏宽悄悄改了）。
    if (extra && typeof extra === 'object') Object.assign(config, extra);

    // 一次调用把「保存 + 合并后的配置 + 文案包 + 语言清单 + 关于信息」一起拿回来，
    // 省掉原来串行的四次桥往返（实测那一下就是用户嫌的一秒里的大头）
    const bundle = await callApi('save_config_and_bundle', config);
    const ok = bundle && bundle.success;
    if (ok) {
        // 本窗口先跟着换语言，再写那声「已保存」：
        // 只把新语言推给主界面、设置窗口自己留着中文，用户会以为这一项没生效。
        await applyLanguage(bundle.texts);
        await renderLanguageOptions(config.language, bundle.language_info);
        // 「运行日志占多大」「副标题」这类是 JS 写进去的文字，不在 data-i18n 覆盖范围里，
        // 换完语言要重新渲染一遍，否则那一行还是上一次的语言。
        await loadAppInfo(bundle.app_info);
        // 「关于 → 更新日志 / 使用条款」那两格上面那行小字（本地缓存的更新日志…）同样是 JS 写的，
        // 切了语言不重渲染就留着上一次的语言。但这两格要读正文（还可能触发联网），
        // 改个主题色、换个开关不该把它们再走一遍——所以只在语言真的变了这一次才走。
        const langChanged = (config.language || '') !== (langBefore || '');
        if (langChanged && typeof loadChanges === 'function'
                && document.getElementById('changes-text')) {
            await loadChanges();
        }
        if (langChanged && typeof loadLicense === 'function'
                && document.getElementById('license-text')) {
            await loadLicense();
        }
        setStatus(uiText('set_saved', '✓ 设置已保存'));
        showToast(uiText('set_saved_toast', '设置已保存'));
        // 通知主界面立刻套用新主题
        await callApi('broadcast_settings', bundle);
    } else {
        reportFailure(null, document.getElementById('settings-status'),
                      uiText('set_save_fail', '设置无法写入配置文件'));
    }
    return ok;
}

/**
 * 「恢复默认」= 把 DEFAULT_CONFIG 里那些**用户能改的设置**一次带回出厂值。
 *
 * 三条口径：
 * ① 窗口里有控件的八项（主题色 / 深色 / 功能说明 / 悬停展开 / 语言 / 自动更新 /
 *    缩略图清晰度 / 草稿策略）先把控件按回默认，再走一次保存，界面与配置不会各改一半；
 * ② 窗口里没有控件、但确实存在配置里的两项也一起带回：导航栏收起状态（出厂展开）、
 *    首页常用功能置顶（出厂没人置顶）——漏掉它们就是「点了恢复默认，栏还是收着的」；
 * ③ 使用痕迹不动：最近文件、条款同意记录、向导走过哪一版都不是「设置」，
 *    清空它们等于替用户做了一次删记录，那是各自入口该干的事。
 */
async function restoreDefaults() {
    renderThemeSwatches('blue');
    document.getElementById('opt-dark').checked = false;
    document.getElementById('opt-notes').checked = true;
    document.getElementById('opt-nav-hover').checked = true;
    document.getElementById('opt-ocr-preload').checked = true;
    // 语言下拉是异步填的（第一次要等 get_language_info 回来）：不等它就点保存，
    // 写回去的还是上一次那门语言，表现就是「这一项没恢复」。
    await renderLanguageOptions('auto');
    document.getElementById('opt-update').checked = true;
    // 右键菜单回出厂「关」：顺手把注册表里那两棵自己写的树撤掉（applySettings 里
    // 现状与目标不一致才会去动，恢复默认这一趟正好触发注销）
    document.getElementById('opt-shell-menu').checked = false;
    document.getElementById('opt-thumb-dpi').value = String(THUMB_DPI_DEFAULT);
    document.getElementById('opt-draft-policy').value = DRAFT_POLICY_DEFAULT;
    previewTheme();
    await applySettings({ sidebar_collapsed: false, pinned_pages: [] });
    showToast(uiText('set_restored', '已恢复默认设置'));
}

// ==================== 更新 / 日志 ====================
async function checkUpdateNow() {
    const box = document.getElementById('update-result');
    box.innerText = uiText('dyn_1353', '⏳ 正在检查…');

    // 用户主动请求：先确认网络，再按版本号给出三种明确结论
    const res = await callApi('check_update_manual');
    const status = res && res.status;
    const message = (res && res.message) || uiText('set_update_fail', '检查更新失败');

    if (status === 'newer') {
        box.innerText = '⬆ ' + message;
        // 走带更新日志的弹窗，比一句「发现新版本」更能让用户决定要不要升
        showUpdateDialog(res.version, res.url);
    } else if (status === 'latest') {
        box.innerText = '✓ ' + message;
        showMessageDialog(uiText('check_update', '检查更新'), message);
    } else if (status === 'ahead') {
        box.innerText = '⚠ ' + message;
        showMessageDialog(uiText('check_update', '检查更新'), message);
    } else {
        box.innerText = '✗ ' + message;
        showMessageDialog(uiText('check_update', '检查更新'), message);
    }
}

async function openLog() {
    const res = await callApi('open_log_folder');
    if (!res || !res.success) {
        reportFailure(res, null, uiText('dyn_1358', '打开日志目录失败'));
    }
}

/** 字节数说成人话：日志本来就几十 KB 到几 MB，报「1468006 字节」没人看得懂。 */
function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 「运行日志」那一行的第二句：现在占多大、由几个文件组成。 */
function describeLogUsage(usage) {
    if (!usage || !usage.files) return uiText('dyn_1359', '当前没有日志文件');
    const copies = usage.files - 1;
    return uiTextf('dyn_1360', '共 {0}，{1} 个文件{2}', formatBytes(usage.bytes), usage.files,
                   (copies > 0 ? uiTextf('dyn_1438', '（写满自动轮转留下的 {0} 份旧副本）', copies) : ''));
}

/** 安装/首次运行与报错日志：固定留存，不参与「清理日志」。 */
function describeProtectedLogUsage(protectedUsage) {
    const data = protectedUsage || {};
    const parts = [];
    if (data.install) parts.push(uiTextf('dyn_1412', '安装/首次运行 {0}', formatBytes(data.install.bytes)));
    if (data.error) parts.push(uiTextf('dyn_1413', '报错 {0}', formatBytes(data.error.bytes)));
    return uiTextf('dyn_1361', '单独留存、不参与清理：{0}',
                   (parts.length ? parts.join(' · ') : uiText('dyn_1362', '暂无')));
}

/**
 * 清空运行日志。
 *
 * 这是一次删用户自己的东西，而且删的可能正是他此刻要找的现场，所以先确认再动手；
 * 确认框里把「清理的是什么、清完还会不会继续写」说清楚，不写成一句「确定吗」。
 */
function clearLog() {
    showMessageDialog(uiText('dyn_1363', '清理运行日志'),
        uiText('dyn_1364', '将删除当前的运行日志及其轮转副本；安装/首次运行日志与报错日志会保留。程序会继续写新的，不需要重启。如果正在排查某个问题，建议先「打开日志目录」把日志留一份再清理。'),
        [
            { label: uiText('cancel', '取消') },
            { label: uiText('dyn_1366', '清理'), primary: true, onClick: doClearLog },
        ]);
}

async function doClearLog() {
    const statusEl = document.getElementById('settings-status');
    const res = await callApi('clear_log');
    if (!res || !res.success) {
        // 部分文件删不掉（例如另一个实例正占着日志）也会走到这里：
        // 已经删掉的那部分是真的删了，所以照实报数，不能只说「失败」
        const detail = (res && res.failed && res.failed.length)
            ? uiTextf('dyn_1439', '有 {0} 个文件正被占用，没能删掉', res.failed.length) : '';
        setStatus(uiTextf('dyn_1367', '✗ 清理日志未完成 {0}', detail));
        showToast(res && res.message ? res.message : uiText('dyn_1368', '清理日志失败'), 4000);
    } else {
        setStatus(uiTextf('dyn_1369', '✓ 日志已清理，释放 {0}', formatBytes(res.freed_bytes)));
        showToast(uiText('set_log_cleared', '日志已清理'));
    }
    const usage = await callApi('get_log_usage');
    const el = document.getElementById('log-usage');
    if (el) el.innerText = describeLogUsage(usage);
    if (res && res.protected) {
        const protectedEl = document.getElementById('log-protected');
        if (protectedEl) protectedEl.innerText = describeProtectedLogUsage(res.protected);
    }
}

async function openUpdatePage() {
    if (appInfo.update_page) {
        await callApi('open_url', appInfo.update_page);
    }
}

async function closeSettingsWindow() {
    await callApi('close_settings_window');
}

/** 向导画在主界面里，这里只负责把自己让位出去。 */
async function runWizard() {
    const res = await callApi('open_wizard');
    if (!res || !res.success) {
        reportFailure(res, document.getElementById('settings-status'), uiText('dyn_1371', '无法打开设置向导'));
    }
}

window.addEventListener('pywebviewready', loadSettings);
