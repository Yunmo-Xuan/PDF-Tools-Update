"""启动画面的后端。

这里刻意只依赖标准库：主界面的 Api 要 import pymupdf / pdfplumber / PIL，
冷启动好几秒，而启动画面必须在那之前就出现。
版本号和主题在建窗口时就读好、挂在 url 上（见 splash_query），
所以 splash.html 只需要这份最小接口报一个首帧信号，
真正的 Api 等后台线程把重依赖加载完再建主窗口。
"""

import json
import logging
import threading
import time
from urllib.parse import urlencode

from core.constants import CURRENT_VERSION, DEFAULT_CONFIG
from core.i18n import lang_texts, resolve_language
from core.lang_zh_cn import TEXTS as BASE_TEXTS
from core.paths import CONFIG_FILE

# 启动画面自己要说的那几句话。版本号/主题/这几句文案都挂在 url 上（见 splash_query），
# 因为 js_api 注入得等页面跑起来，而启动画面统共只停两三秒。
SPLASH_KEYS = (
    'splash_title', 'splash_status', 'splash_slow', 'splash_failed',
    'splash_stage_core', 'splash_stage_ui', 'splash_stage_comp',
    'splash_stage_near', 'splash_stage_enter',
    # 软件名和那句副标题也归启动画面管：这一页要在第一帧就是用户那门语言的名字。
    'app_title', 'app_tagline',
)


def read_splash_theme():
    """
    直接读配置文件里的主题键和语言键，够用就行。

    不走 BaseApi.load_config：那是 api 包的一部分，一 import 就把整包的重依赖拖进来，
    启动画面又得等好几秒。文件缺失（全新安装）或内容损坏都退回默认风格、语言退回系统。
    """
    theme = {
        "theme_color": DEFAULT_CONFIG["theme_color"],
        "dark_mode": bool(DEFAULT_CONFIG["dark_mode"]),
        "language": DEFAULT_CONFIG.get("language", "auto"),
    }
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            if stored.get("theme_color"):
                theme["theme_color"] = stored["theme_color"]
            theme["dark_mode"] = bool(stored.get("dark_mode"))
            if stored.get("language"):
                theme["language"] = stored["language"]
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("读取启动画面主题失败")
    return theme


def splash_texts(settings=None):
    """启动画面要用的那几句，按配置里的语言取（缺键自动回简体中文）。"""
    settings = settings or read_splash_theme()
    lang = resolve_language(settings.get("language"))
    table = dict(BASE_TEXTS)
    table.update(lang_texts(lang))
    return {key: table[key] for key in SPLASH_KEYS}


# 启动画面面板的渐变起点色，必须和 assets/js/common.js 的 themeGradients、
# assets/splash.html 的 PALETTE 三处保持一致；改主题色时一起改。
# 窗口底色取的是「起点色淡化之后的那个值」，也就是页面画出来之前板子的颜色，
# 两边算的是同一个式子，交接时才看不出有一次换色。
PANEL_START_LIGHT = {
    "blue": "#728db5", "green": "#72b593", "cyan": "#72b0b5", "teal": "#72b5b0",
    "purple": "#7c72b5", "pink": "#b57297", "red": "#b57380", "orange": "#b58f72",
    "amber": "#b59772", "slate": "#8090a8",
}
PANEL_START_DARK = {
    "blue": "#141b24", "green": "#14241c", "cyan": "#142224", "teal": "#142422",
    "purple": "#171424", "pink": "#24141d", "red": "#241418", "orange": "#241b14",
    "amber": "#241d14", "slate": "#181b20",
}


def _mix(color_a, color_b, weight_b):
    """两个 #rrggbb 按比例混合（weight_b 是 color_b 的占比），返回 #rrggbb。"""
    try:
        a = color_a.lstrip('#')
        b = color_b.lstrip('#')
        parts = [round(int(a[i:i + 2], 16) * (1 - weight_b) + int(b[i:i + 2], 16) * weight_b)
                 for i in (0, 2, 4)]
        return '#%02x%02x%02x' % tuple(max(0, min(255, p)) for p in parts)
    except Exception:
        return color_a


def splash_background_color(theme=None):
    """
    窗口底色 = 启动画面面板左上角那一档颜色。

    WebView2 在页面画出首帧之前会把整块客户区刷成这个颜色，所以它既是「板子」的
    底色，也是深色模式用户不该看到浅色块的那一处。圆角外那四个角同样用它。
    """
    theme = theme or read_splash_theme()
    dark = bool(theme.get("dark_mode"))
    name = theme.get("theme_color") or "blue"
    if dark:
        return _mix(PANEL_START_DARK.get(name, PANEL_START_DARK["blue"]), "#1b1d33", .7)
    return _mix(PANEL_START_LIGHT.get(name, PANEL_START_LIGHT["blue"]), "#ffffff", .9)


def splash_query(theme=None):
    """
    版本号、主题和界面文案挂在启动画面的 url 上，让页面第一帧就是终态。

    只靠 js_api 的话，页面要等 pywebview 把 api 注入进来、再等一次跨进程往返才拿得到主题；
    打包成品实测那一下要 1s 上下，而启动画面统共只停两三秒——
    深色模式的用户于是全程看着一块默认蓝。配置里读出来的值一律 url 编码，
    免得有人手改过 theme_color 就把后面的参数拼坏。
    """
    theme = theme or read_splash_theme()
    texts = splash_texts(theme)
    return urlencode({
        'v': CURRENT_VERSION,
        't': theme['theme_color'],
        'd': '1' if theme['dark_mode'] else '0',
        'x': json.dumps({k: texts[k] for k in
                         ('splash_title', 'splash_status', 'splash_slow', 'splash_failed',
                          'app_title', 'app_tagline')},
                        ensure_ascii=False),
    })


class SplashApi:
    """
    暴露给 splash.html 的最小 js_api：首帧信号的接收端，也是真实进度的来源。

    进度只认后端里程碑，不认时间：set_stage 由启动流程在几个真实节点上调用，
    页面读到的永远是「已经做到的那一步」。以前是页面自己按墙上时钟爬到 98%，
    实际主界面 1.4 秒最短停留一到就切走了，条子每次停在五成出头——那是假的。
    """

    def __init__(self, settings=None):
        self.first_painted = threading.Event()
        self.painted_at = None
        self._settings = settings
        self._texts = None
        self._stage_lock = threading.Lock()
        self._percent = 0
        self._label = self.text('splash_status')

    @property
    def texts(self):
        if self._texts is None:
            self._texts = splash_texts(self._settings)
        return self._texts

    def text(self, key, **fmt):
        """按当前语言取一句；里程碑标签和失败提示都走这里，后端不再硬写中文。"""
        value = self.texts.get(key, key)
        return value.format(**fmt) if fmt else value

    def set_stage(self, percent, key):
        """记录一个里程碑：只会向前，不会被后面的慢节点拽回去。"""
        label = self.text(key)
        with self._stage_lock:
            if percent > self._percent:
                self._percent = min(100, int(percent))
                self._label = label

    def stage(self):
        with self._stage_lock:
            return self._percent, self._label

    def report_painted(self):
        """
        页面自己报「已经画出首帧」，并顺手把当前进度带回去。

        启动画面的最短显示时间从首帧算，而不是从进程启动算——
        以前窗口要等重依赖加载完才出现，从进程启动计时等于没计时。
        首帧之前后端早就跑完几个里程碑了，返回值让页面不必从 0 再爬一遍。
        """
        if not self.first_painted.is_set():
            self.painted_at = time.monotonic()
            self.first_painted.set()
        return self.startup_stage()

    def startup_stage(self):
        """页面轮询用：evaluate_js 推不过来（GUI 线程被重依赖占住）时的兜底通道。"""
        percent, label = self.stage()
        return {"success": True, "percent": percent, "label": label}
