"""独立设置窗口：打开/复用窗口、广播设置、读取许可协议与更新日志。"""

import json
import logging
import os
import re
import threading

import webview

from core import winicon
from core import winclose
from core.constants import (
    CURRENT_VERSION,
    LICENSE_PAGE_URL,
    LICENSE_URLS,
    README_PAGE_URL,
    README_URLS,
)
from core.i18n import t
from core.mirror import fetch_all, parse_version
from core.paths import CONFIG_DIR, get_asset_path

# 设置窗口是懒创建的：第一次点齿轮才建，关掉后下次再建。
# 这样常驻内存里不会多一个 WebView 实例。
_settings_window = None
# js_api 的每次调用都在新线程上跑，连点两下齿轮会同时通过「窗口为空」的判断，
# 于是建出两个设置窗口；关掉其中一个又会把全局引用清掉，剩下的那个就成了孤儿。
_settings_lock = threading.Lock()

# 「条款 / 更新日志」的后台同步去重表：同一类正文同时只允许一趟在跑
_MD_SYNC_INFLIGHT = set()
_MD_SYNC_LOCK = threading.Lock()
# 正常打开时最多等多久就先用本地那份顶上（联网那趟仍在后台跑完并替换本地）
_MD_WAIT_BUDGET_SECONDS = 2.5
# 用户点名强刷（设置里的按钮、发现新版本的弹窗）可以多等一会儿，照原来的 9s 口径
_MD_FORCE_BUDGET_SECONDS = 9.0

# 联网拉到的正文缓存在配置目录，离线时也能查看上次拉到的内容
_LICENSE_CACHE = os.path.join(CONFIG_DIR, 'LICENSE.md')
# 随程序分发的副本：保证断网、首次运行时也能立刻看到内容
_LICENSE_BUNDLED = get_asset_path('license.md')
_README_CACHE = os.path.join(CONFIG_DIR, 'README.md')
_README_BUNDLED = get_asset_path('readme.md')
# 缓存旁边再记一份「这份缓存是为哪个版本同步的」。
# 光看缓存有没有会出问题：老用户抱着的可能是上个版本拉到的条款，
# 全新安装则只有随程序的内置副本，两者都不能当成「已经是最新的」。
_LICENSE_VER = _LICENSE_CACHE + '.ver'
_README_VER = _README_CACHE + '.ver'

# GitHub 的 README 会用 HTML 包装折叠块和 shield 徽标，
# 这些标签在应用内的 markdown 渲染器里既解析不出来、又会把尖括号露给用户。
# details/summary 整行丢掉（<summary> 里的「点击展开」是网页折叠控件的文案，不是内容）；
# 其它标签只脱壳、保留里面的文字；徽标图片删掉。
_FOLD_LINE = re.compile(r'^\s*</?(?:details|summary)\b[^>]*>.*$')
_HTML_TAG = re.compile(
    r'</?(?:div|a|img|br|p|table|thead|tbody|tr|th|td|kbd|sub|sup|b|strong|em|span|center|h[1-6])\b[^>]*>',
    re.I,
)
_SHIELD_IMAGE = re.compile(r'!\[[^\]]*\]\([^)\s]*img\.shields\.io[^)]*\)')
_VERSION_HEADING = re.compile(r'^\s*#{2,6}\s+v?(\d+(?:\.\d+){1,3})\b.*$')
_MAJOR_NUMBER = re.compile(r'^\s*v?(\d+)')


def _heading_level(line):
    stripped = line.strip()
    return len(stripped) - len(stripped.lstrip('#'))


def _clean_readme_line(line):
    """返回清洗后的行；整行被丢弃时返回 None。"""
    if _FOLD_LINE.match(line):
        return None
    cleaned = _SHIELD_IMAGE.sub('', line)
    if _HTML_TAG.search(cleaned):
        cleaned = _HTML_TAG.sub('', cleaned)
    if cleaned != line and not cleaned.strip():
        return None
    return cleaned


def _extract_changelog(text):
    """
    从 README 正文里切出「更新日志」一节。

    整篇 README 是项目介绍，弹窗里只该看到「这次更新了什么」；
    找不到该节时返回 None，由调用方决定退回什么。
    """
    lines = (text or '').splitlines()
    start = None
    boundary = 2
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('#') and '更新日志' in stripped:
            start = i + 1
            # 只有一级标题的 README 也认：二级及更高级的下一个标题就是本节结束
            boundary = max(2, _heading_level(stripped))
            break
    if start is None:
        return None

    body = []
    for line in lines[start:]:
        if line.strip().startswith('#') and _heading_level(line) <= boundary:
            break
        cleaned = _clean_readme_line(line)
        if cleaned is None:
            continue
        body.append(cleaned)
    while body and body[-1].strip() in ('', '---'):
        body.pop()
    return '\n'.join(body).strip() or None


def _split_changelog(section):
    """把更新日志拆成 [{version, body}]，顺序沿用 README 里的新→旧。"""
    entries = []
    current = None
    for line in (section or '').splitlines():
        match = _VERSION_HEADING.match(line)
        if match:
            current = {"version": match.group(1), "lines": []}
            entries.append(current)
            continue
        if current is not None:
            current["lines"].append(line)
    return [
        {"version": e["version"], "body": '\n'.join(e["lines"]).strip()}
        for e in entries
    ]


def _changelog_top_version(text):
    """
    这份正文里「更新日志」最上面那条的版本号（元组）；读不出来返回 None。

    README 的日志是按新→旧排的，顶端就是这份快照有多新——
    多个镜像的回答靠它比大小，条款正文里没有这一节，于是比不出来、退回按顺序取。
    """
    entries = _split_changelog(_extract_changelog(text or ''))
    return parse_version(entries[0]["version"]) if entries else None


def _major_of(version):
    """取大版本号："1.2.3" / "v1" -> "1"；解析不出来返回 None。"""
    match = _MAJOR_NUMBER.match(str(version or ''))
    return match.group(1) if match else None


def _major_number(version):
    """把大版本号取成整数；"1.2.3" / "v1" -> 1，解析不出来返回 None。"""
    major = _major_of(version)
    try:
        return int(major)
    except (TypeError, ValueError):
        return None


def _drop_older_majors(entries, major):
    """
    丢掉比本机更旧的大版本条目，本机这一版和比它更新的都留下。

    为什么不写成「只留同大版本」：那样一来发布了 v8.x 之后，还在用 7.x 的用户
    更新日志里永远什么都没有——而那一版正是他们会去翻的内容（要不要升、升了什么）。
    反过来比本机旧的（上个、上上个大版本）仍然不看：一路翻下去全是历史版本，
    太长也没人读，那是「在浏览器中查看」完整日志的活。
    major 解析不出来时不过滤，宁多勿少。
    """
    floor = _major_number(major)
    if floor is None:
        return list(entries)
    # 认不出大版本的条目直接丢掉：宁可少一条，也不能把别的东西冒充本机这一版的记录
    return [e for e in entries
            if (n := _major_number(e.get("version"))) is not None and n >= floor]


def _entries_to_markdown(entries):
    """把条目重新拼成正文，交给前端的 markdown 渲染器。"""
    blocks = []
    for entry in entries:
        body = (entry.get("body") or '').strip()
        heading = f"### v{entry.get('version')}"
        blocks.append(f"{heading}\n\n{body}" if body else heading)
    return '\n\n'.join(blocks).strip()


def _on_settings_closed():
    global _settings_window
    with _settings_lock:
        _settings_window = None


def _settings_title():
    """设置窗口标题栏那一句：软件名和「设置」两截都按界面语言来。

    原生标题是建窗口时由后端定的（页面的 <title> 顶不掉它），所以这两截得在后端取；
    窗口开着的时候改了语言，broadcast_settings 会再刷一次。
    """
    return '%s - %s' % (t('app_title'), t('set_title'))


class SettingsMixin:
    def open_settings_window(self):
        """打开设置窗口；若已打开则聚焦，不重复创建。"""
        global _settings_window
        with _settings_lock:
            if _settings_window is not None:
                try:
                    _settings_window.title = _settings_title()
                    _settings_window.show()
                    _settings_window.restore()
                    return {"success": True, "reused": True}
                except Exception:
                    # 窗口可能已经被销毁，落到下面重新创建
                    logging.info("原设置窗口已失效，重新创建")
                    _settings_window = None

            try:
                _settings_window = webview.create_window(
                    _settings_title(),
                    get_asset_path('settings.html'),
                    js_api=self,
                    width=780,
                    # 580 那一档量出来「外观」小页要滚 35 px（内容 395 / 可视 360）：
                    # 分小页要的就是「一屏读完一类设置」，所以窗口给到 640，让最长那页也不滚。
                    height=640,
                    min_size=(660, 480),
                    resizable=True,
                    background_color='#f0f7ff',
                )
                _settings_window.events.closed += _on_settings_closed
                winicon.attach(_settings_window, 'settings')
            except Exception as error:
                _settings_window = None
                logging.exception("打开设置窗口失败")
                return {"success": False, "message": str(error)}

            logging.info("设置窗口已创建")
            return {"success": True, "reused": False}

    def close_settings_window(self):
        """关闭设置窗口（由窗口内的「关闭」按钮调用）。"""
        global _settings_window
        with _settings_lock:
            window = _settings_window
            _settings_window = None
        try:
            # destroy 可能同步触发 closed 事件，所以必须在锁外调用，避免自锁
            if window is not None:
                # 先当场撤掉再销毁：destroy 要排 GUI 线程和 WebView2 拆除的队，
                # 实测能让这颗已经「关掉了」的窗口多挂在屏幕上小一秒。
                winclose.hide(window)
                window.destroy()
            return {"success": True}
        except Exception as error:
            logging.exception("关闭设置窗口失败")
            return {"success": False, "message": str(error)}

    def broadcast_settings(self, bundle=None):
        """
        设置保存后通知所有窗口重新应用主题，让改动立刻生效。

        `bundle` 是 `save_config_and_bundle` 那一包（配置 + 文案 + 语言表 + 关于信息）：
        带着它就不必每个窗口再回后端取一次配置和整包文案。不带也照样工作（照旧各自去取），
        老调用方与验收脚本都不用改。
        """
        delivered = 0
        failed = 0
        # 原生标题栏不吃页面 DOM：改了语言，主窗口和设置窗口的标题都得由后端重挂一次
        if _settings_window is not None:
            try:
                _settings_window.title = _settings_title()
            except Exception:
                logging.info("设置窗口标题刷新失败（窗口可能正在关闭）")
        for window in list(webview.windows):
            try:
                blob = '%s %s' % (getattr(window, 'url', '') or '',
                                  getattr(window, 'location', '') or '')
                if 'index.html' in blob.lower():
                    window.title = t('dyn_1319').format(CURRENT_VERSION)
                payload = json.dumps(bundle or None, ensure_ascii=True)
                window.evaluate_js(
                    'window.applyStoredSettings && window.applyStoredSettings(%s);' % payload
                )
                delivered += 1
            except Exception:
                failed += 1
                logging.exception("推送设置变更失败")
        # 一个窗口都没推到时不能报成功，否则主题没变却显示"已保存"
        if delivered == 0:
            return {"success": False, "windows": 0, "failed": failed}
        return {"success": True, "windows": delivered, "failed": failed}

    # ---------- 联网正文（许可协议 / 更新日志）----------
    @staticmethod
    def _read_markdown(path, label):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                text = handle.read().strip()
            return text or None
        except FileNotFoundError:
            return None
        except Exception:
            logging.exception(f"读取{label}失败")
            return None

    @staticmethod
    def _write_markdown(path, text, label):
        """写成功返回 True。调用方靠这个返回值决定要不要记版本号。

        先写同目录的临时文件再 `os.replace`：正文是后台线程在写的，用户随时可能打开
        设置窗口去读它，直接 `open(w)` 中间会有一小段「文件在、内容是半截」的窗口。
        """
        tmp_path = path + '.tmp'
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(tmp_path, 'w', encoding='utf-8') as handle:
                handle.write(text)
            os.replace(tmp_path, path)
            return True
        except Exception:
            logging.exception(f"写入{label}失败")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False

    @staticmethod
    def _read_synced_ver(path):
        """读缓存对应的版本号；文件不存在或读失败都算「从没为任何版本同步过」。"""
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return handle.read().strip()
        except FileNotFoundError:
            return ''
        except Exception:
            logging.exception(f"读取{path}失败")
            return ''

    @staticmethod
    def _write_synced_ver(path, label):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(CURRENT_VERSION)
        except Exception:
            # 标记写失败只是下次会多联网一次，不值得让正文读取整个失败
            logging.exception(f"写入{label}失败")

    def _mark_synced(self, cache_path, ver_path, text, label, min_version):
        """
        正文落盘并记下「已为本机版本同步」，返回是否真的记上了。

        两种情况不记标记，宁可下次多联一次网：
          1) 正文没写进去——正文没落盘却把版本号写上，下次就会拿着上个版本的旧缓存
             当「已同步」端出来；
          2) 给了 min_version、而这份正文的日志顶端还不够新——说明远端还没写本机这一版，
             或者所有镜像都还在一起滞后。这种情况不能声称同步完成，
             否则远端补上之后本机反而一直读着旧的、再也看不到自己那一版的记录
             （现场报的「GitHub 已经改了却读不到更新日志」就是这么来的）。
        """
        if not self._write_markdown(cache_path, text, f'{label}缓存'):
            logging.warning(f"{label}缓存写入失败，本次不记同步版本号")
            return False
        if min_version:
            top = _changelog_top_version(text)
            wanted = parse_version(min_version)
            if top is None or (wanted is not None and top < wanted):
                logging.warning("%s里最新一份的日志顶端是 v%s，低于本机 v%s：本次不记同步标记，"
                                "远端补上后下次打开就能读到",
                                label, '.'.join(map(str, top)) if top else '未知', min_version)
                return False
        self._write_synced_ver(ver_path, f'{label}版本号')
        return True

    @staticmethod
    def _download_markdown(urls, label, timeout=8, deadline=9.0, grace=1.2):
        """
        并行问完所有镜像，返回 [(url, 正文), ...]（顺序沿用 urls）；都没回来时是空列表。

        以前是「按顺序试、第一个成功的就用」，那正好会被最快的过期镜像抢走：
        jsDelivr 系缓存着几天前的快照，而 GitHub 上的正文已经改过了。
        deadline 是整轮的总预算，grace 是第一个回答到达后留给其余镜像的时间——
        首次启动的向导不能把人按在一屏上。挑哪一份由 _pick_markdown 按内容决定。
        """
        return fetch_all(urls, label, timeout=timeout, deadline=deadline, grace=grace)

    @staticmethod
    def _pick_markdown(copies, label):
        """
        从各镜像的回答里挑「内容最新」的那一份正文；没有回答时返回 None。

        比的是更新日志顶端那条的版本号（README 的顺序是新→旧，顶端就是最新）。
        条款正文没有版本标记，比不出来时退回按镜像顺序取第一份，
        与原来的取法一致——这里要的只是「别让滞后快照赢」，不是重新排优先级。
        """
        if not copies:
            return None
        best_text, best_version, best_url = None, None, None
        for url, text in copies:
            version = _changelog_top_version(text)
            if version is not None and (best_version is None or version > best_version):
                best_text, best_version, best_url = text, version, url
        if best_text is None:
            logging.info("采用%s: %s（无版本可比，按镜像顺序取第一份）", label, copies[0][0])
            return copies[0][1]
        logging.info("采用%s: %s（日志顶端 v%s，%d 份回答中最新）",
                     label, best_url, '.'.join(str(p) for p in best_version), len(copies))
        return best_text

    def _fetch_markdown_now(self, kind, cache_path, ver_path, urls, label,
                            min_version, budget):
        """
        联网取正文，最多等 `budget` 秒；等不到也照样往下走，那一趟继续在后台跑。

        落盘由这一趟自己做（只有一个写者），所以「超时先端本地、回头拿到最新的
        就静默把本地那份换掉」是天然成立的：后台那趟回来照样写缓存与同步标记。
        同一类正文同时只允许一趟在跑，免得用户连点把四个镜像各刷四遍。
        返回 (正文或 None, 是否还在后台跑)。
        """
        with _MD_SYNC_LOCK:
            if kind in _MD_SYNC_INFLIGHT:
                return None, True
            _MD_SYNC_INFLIGHT.add(kind)

        done = threading.Event()
        box = {}

        def run():
            try:
                text = self._pick_markdown(self._download_markdown(urls, label), label)
                box['synced'] = bool(text) and self._mark_synced(
                    cache_path, ver_path, text, label, min_version)
                box['text'] = text
            except Exception:
                logging.exception("后台同步%s失败", label)
                box['text'] = None
            finally:
                with _MD_SYNC_LOCK:
                    _MD_SYNC_INFLIGHT.discard(kind)
                done.set()

        threading.Thread(target=run, name='md-sync-%s' % kind, daemon=True).start()
        if not done.wait(budget):
            logging.info("%s：%s s 内没等到回话，先用本地那份，联网那趟继续在后台跑",
                         label, budget)
            return None, True, False
        return box.get('text'), False, bool(box.get('synced'))

    def _load_markdown(self, cache_path, bundled_path, urls, label, page_url,
                       force_refresh, ver_path, min_version=None, kind=None):
        """
        取值顺序：为当前版本同步过的缓存 -> 联网（最多等一小会儿）-> 旧缓存/内置副本。

        关键点在版本号比对，而不是「缓存文件在就算有」：
        老用户配置目录里那份可能是上个版本拉到的条款，全新安装则只有随程序打包的内置副本，
        直接端出去等于让用户签旧条款、看旧日志。版本号对不上就强制联网一次，
        同步成功才把版本号写成当前版本，之后每次打开都直接读缓存、不再联网。
        联网失败照旧退回已有内容并标 stale，断网不会开天窗；
        失败时不写版本号，下次打开会再试。

        min_version 是给更新日志用的「证明得了才算同步」：所有镜像里最新那份的日志顶端
        仍然低于本机版本时（远端还没写这一版、或所有镜像都还在滞后），不写版本号标记，
        下次打开会再联网一次——这样远端一补上，程序不必重装就能看到自己这一版的记录。

        等待预算是这一轮改的口径：正常打开只等 `_MD_WAIT_BUDGET_SECONDS`，
        用户点名强刷（设置里那个按钮、发现新版本那个弹窗）才按 `_MD_FORCE_BUDGET_SECONDS` 等。
        没等到的那一下**立刻**端本地，联网那趟在后台跑完并替换本地那份——
        以前是「不等到不回话」，镜像里有一个卡住时首次启动那屏条款能停二十几秒，
        而那份本地副本一直躺在程序目录里没人去端。
        """
        cached = self._read_markdown(cache_path, f'{label}缓存')

        if not force_refresh and cached and self._read_synced_ver(ver_path) == CURRENT_VERSION:
            return {
                "success": True,
                "text": cached,
                "cached": True,
                "bundled": False,
                "synced": True,
                "page_url": page_url,
            }

        budget = _MD_FORCE_BUDGET_SECONDS if force_refresh else _MD_WAIT_BUDGET_SECONDS
        fresh, refreshing, synced = self._fetch_markdown_now(
            kind or label, cache_path, ver_path, urls, label, min_version, budget)
        if fresh:
            return {
                "success": True,
                "text": fresh,
                "cached": False,
                "bundled": False,
                "synced": synced,
                "page_url": page_url,
            }

        fallback = cached or self._read_markdown(bundled_path, f'内置{label}')
        if fallback:
            return {
                "success": True,
                "text": fallback,
                "cached": bool(cached),
                "bundled": not cached,
                "synced": False,
                "stale": True,
                # 界面据此说明「还在后台取最新的」，而不是让人以为就这份了
                "refreshing": refreshing,
                "page_url": page_url,
            }

        if refreshing:
            # 本地一份都没有、这趟又没等到：别报「取不到」，稍后后台那趟会落盘
            return {
                "success": False,
                "synced": False,
                "refreshing": True,
                "page_url": page_url,
                "message": t('api_markdown_fetch_slow') % label,
            }

        return {
            "success": False,
            "synced": False,
            "page_url": page_url,
            "message": t('api_markdown_fetch_failed') % label,
        }

    def get_license_text(self, force_refresh=False):
        """取使用条款正文（仓库里的 LICENSE.md，标题就是「使用条款」）。"""
        return self._load_markdown(
            _LICENSE_CACHE, _LICENSE_BUNDLED, LICENSE_URLS,
            t('set_terms'), LICENSE_PAGE_URL, bool(force_refresh), _LICENSE_VER,
            kind='license',
        )

    def get_release_notes(self, force_refresh=False, major_only=True):
        """
        取更新日志正文，来自仓库 README.md。

        联网读取方式和许可协议一致；这里再把「更新日志」一节单独切出来，
        并按版本拆成条目。

        major_only=True（设置页用）：丢掉比本机更旧的大版本，留本机这一版和比它更新的，
        本机 7.0.1 看 7.x 与 8.x，不看 6.x 及更早——更新源发布了新大版本时，
        还没升上去的旧用户也要能看到那一版的记录；
        major_only=False（新版本弹窗用）：保留全部条目，
        弹窗只渲染它要宣传的那一个版本，跨大版本升级时也要拿得到正文。
        """
        result = self._load_markdown(
            _README_CACHE, _README_BUNDLED, README_URLS,
            t('set_changelog'), README_PAGE_URL, bool(force_refresh), _README_VER,
            CURRENT_VERSION, kind='changes',
        )
        major = _major_of(CURRENT_VERSION)
        section = _extract_changelog(result.get("text") or "")
        entries = _split_changelog(section) if section else []

        if entries:
            picked = _drop_older_majors(entries, major) if major_only else entries
            result["text"] = _entries_to_markdown(picked)
            result["entries"] = picked
        elif section:
            # 切得出「更新日志」这一节、却一条版本标题都没有：正文原样给出
            result["text"] = section
            result["entries"] = []
        else:
            result["entries"] = []

        result["major_version"] = major
        result["current_version"] = CURRENT_VERSION
        return result
