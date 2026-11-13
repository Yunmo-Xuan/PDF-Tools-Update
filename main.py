"""PDF Toolkit入口。

启动画面与主界面都用 webview 渲染：两者外观同源，
也省掉了一整套 GUI 工具包（tkinter）的依赖与线程问题。

启动顺序是有讲究的：核心模块（pymupdf/pdfplumber/PIL）要 load 好几秒，
所以先只建启动画面，重活丢给 webview.start 的后台线程去干；
启动画面则等自己画出首帧后再计时，保证用户真的看得到它。
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import random
import sys
import threading
import time

# 从这里开始计时：PyInstaller 的引导代码已经跑完，剩下的 import webview / 重依赖
# 才是真正的启动开销，所以这一行必须排在所有慢 import 之前
PROCESS_STARTED_AT = time.monotonic()

# 必须排在 webview / paddle 这些会 spawn 子进程的库之前：成品是窗口程序，
# 任何一次子进程调用都会闪出一个 cmd 窗口（Paddle 探测 nvcc/ccache 就是两下）。
from core.noconsole import install as _install_noconsole

_install_noconsole()

import webview

from core import webview2
from core import lifecycle
from core import winicon
from core import winclose
from core.constants import APP_NAME, CURRENT_VERSION
from core.i18n import resolve_language, texts
from core.logger import LOG_FILE
from core.paths import ensure_dirs, get_asset_path
from core.splash import SplashApi, read_splash_theme, splash_background_color, splash_query

# 首帧之后至少让启动画面停留这么久，免得主界面秒开时它一闪而过
MIN_SPLASH_VISIBLE_SECONDS = 1.4
# 首帧信号最多等这么久：等不到也继续，别让最短显示时间从进程启动那一刻起算
SPLASH_FIRST_FRAME_SECONDS = 10.0
# 核心模块 + 主界面加载的总预算，超时就直接把主界面放出来（并留在日志里）
MAIN_READY_SECONDS = 45.0
# 进度条走到 100% 之后再留这么久才切主界面，让人真的看见它走完
# （0.32s 实测不够：条子刚跳满窗口就没了，看上去仍像没走完）
SPLASH_FINISH_SECONDS = 0.55
# 主界面放出来之后，隔这么久才开始后台加载 OCR 引擎
OCR_PRELOAD_DELAY_SECONDS = 4.0
# SetThreadPriority 的「后台模式」：CPU / IO / 页面优先级一起降
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
# 缺 WebView2 时的退出码：非 0，让安装器、脚本和日志能分清「正常关掉」和「跑不起来」。
# 用 3 是因为 1 太容易被解释成 Python 自身的报错，2 是 argparse 的用法错误。
RUNTIME_MISSING_EXIT_CODE = 3

MAIN_WINDOW_SIZE = (1400, 850)
MAIN_WINDOW_MIN_SIZE = (1100, 700)
SPLASH_WINDOW_SIZE = (480, 320)
# 窗口区域的圆角半径。splash.html 的 .panel 往外多铺 3px、自己的 border-radius 是 21px，
# 弧线正好落在这条裁切线外侧，四角不会漏出底色或窗口描边。
SPLASH_CORNER_RADIUS = 18
# 主窗口只关一次，副窗口清理只做一次；重复触发会往日志里泼 destroy 异常
_SECONDARY_CLEANUP_DONE = False


def _set_app_user_model_id():
    """让任务栏图标与窗口正确归组（仅 Windows 有效）。"""
    if sys.platform != 'win32':
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f'pdf.toolkit.v{CURRENT_VERSION}'
        )
    except Exception:
        pass


def _install_excepthooks():
    """任何线程里的未捕获异常都要进日志，否则打包后无从排查。"""
    original = sys.excepthook

    def global_hook(exc_type, exc_value, exc_tb):
        logging.error("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        original(exc_type, exc_value, exc_tb)

    def thread_hook(args):
        logging.error(
            f"线程 {getattr(args.thread, 'name', '?')} 未捕获异常",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = global_hook
    threading.excepthook = thread_hook


def _centered_position(width, height):
    """
    算出把窗口摆在屏幕正中央所需的 (x, y)。

    pywebview 的 create_window 在 x/y 缺省时会用 (0, 0)，也就是屏幕左上角，
    启动画面看起来就像是从角落冒出来的。这里自己算居中位置；
    取不到屏幕尺寸时返回 (None, None)，交回给 pywebview 决定。
    """
    if sys.platform != 'win32':
        return None, None
    try:
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)   # SM_CXSCREEN
        screen_h = user32.GetSystemMetrics(1)   # SM_CYSCREEN
        if screen_w <= 0 or screen_h <= 0:
            return None, None
        return max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 2)
    except Exception:
        logging.exception("计算窗口居中位置失败")
        return None, None


def _splash_hwnd(launch):
    """
    取启动画面顶层窗口的 HWND。

    pywebview 把 BrowserView（.NET 控件）挂在 window.native 上，圆角要裁的是它外面的
    那个顶层窗体，所以取 GA_ROOT；控件本身还没建出来时返回 None。
    """
    window = launch.splash
    native = getattr(window, 'native', None) if window is not None else None
    handle = getattr(native, 'Handle', None)
    if handle is None:
        return None
    try:
        # .NET 的 IntPtr 不能直接 int()，要先 ToInt64（窗口还没建出来时这里会抛）
        hwnd = ctypes.windll.user32.GetAncestor(handle.ToInt64(), 2)  # GA_ROOT
        return hwnd or None
    except Exception:
        return None


def _round_splash_corners(launch):
    """
    把启动画面的窗口本身裁成圆角。

    它是 frameless 的矩形窗口：WebView2 在页面画出首帧之前会先把整块客户区刷成
    background_color，用户于是先看到一块直角矩形，三四秒之后圆角卡片才盖上来——
    那块底什么也不显示，只是难看。窗口裁圆之后底色和卡片是同一个形状，交接就顺了。
    """
    hwnd = _splash_hwnd(launch)
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    try:
        client = ctypes.wintypes.RECT()
        outer = ctypes.wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client)):
            return False
        if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
            return False
        width, height = client.right, client.bottom
        if width <= 0 or height <= 0:
            return False
        if (outer.right - outer.left, outer.bottom - outer.top) != (width, height):
            # 标题栏/边框还没去掉：SetWindowRgn 会把窗口缩到区域的包围盒，
            # 这时按客户区裁就等于把窗口切掉一圈（实测 480x320 变成 464x281）。
            return False
        try:
            # 圆角按 CSS 像素写，窗口区域按物理像素算，所以跟着 DPI 缩放
            scale = (user32.GetDpiForWindow(hwnd) or 96) / 96.0
        except Exception:
            scale = 1.0
        radius = max(1, int(round(SPLASH_CORNER_RADIUS * scale)))
        region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        if not region:
            return False
        # 成功后区域归窗口所有，不能再 DeleteObject；失败才需要自己回收
        if user32.SetWindowRgn(hwnd, region, True) == 0:
            gdi32.DeleteObject(region)
            return False
        logging.info("启动画面窗口已裁成 %dpx 圆角 (%dx%d)", radius, width, height)
        return True
    except Exception:
        logging.debug("裁剪启动画面圆角失败", exc_info=True)
        return False


def _round_splash_corners_when_ready(launch):
    """
    窗口刚建好时客户区尺寸还可能不准，圆角要重试到真的裁上为止。

    重试上限取首帧那一瞬：页面画出来之前必须已经裁好，否则用户还是会看见直角。
    """
    def loop():
        deadline = time.monotonic() + SPLASH_FIRST_FRAME_SECONDS
        while time.monotonic() < deadline:
            if launch.splash_closed.is_set():
                return
            if _round_splash_corners(launch):
                return
            time.sleep(0.02)
        logging.warning("启动画面圆角没裁上，退回直角窗口")

    threading.Thread(target=loop, name='splash-corners', daemon=True).start()


class _Launch:
    """
    启动阶段的共享状态。

    splash_closed 是必需的：启动画面现在要陪着用户等完核心模块加载，
    这期间要是被关掉（frameless 也能 Alt+F4），而主窗口又还没显示，
    进程就变成一个看不见也关不掉的窗口。
    """

    def __init__(self):
        self.bridge = SplashApi()
        self.splash = None
        self.main_window = None
        self.splash_closed = threading.Event()
        # 已经在把主界面交给用户了：此时收掉启动画面属于正常交接
        self.handing_over = threading.Event()


def _main_window_title():
    """主窗口标题栏那一句：软件名跟着界面语言走。

    页面里的 `document.title` 顶不掉原生标题栏（装机成品实测：界面是中文，
    标题栏还钉着创建时传进去的那句英文），所以这一句必须在建窗口之前由后端按
    配置文件里的语言算好。语言在设置里改过之后由 broadcast_settings 再刷一次。
    """
    lang = resolve_language(read_splash_theme()['language'])
    return texts(lang)['dyn_1319'].format(CURRENT_VERSION)


def _create_splash(launch):
    width, height = SPLASH_WINDOW_SIZE
    x, y = _centered_position(width, height)
    theme = read_splash_theme()
    launch.splash = webview.create_window(
        APP_NAME,
        get_asset_path('splash.html') + '?' + splash_query(theme),
        js_api=launch.bridge,
        width=width,
        height=height,
        x=x,
        y=y,
        frameless=True,
        easy_drag=True,
        on_top=True,
        resizable=False,
        # pywebview 默认 shadow=True，会在建窗口时把 DWM 框架延伸进客户区并强制打开
        # 非客户区绘制（DWMWA_NCRENDERING_POLICY=ENABLED）——那圈白线就是它描的，
        # 描在窗口区域之外，SetWindowRgn 裁不掉。启动画面不要这个投影。
        shadow=False,
        background_color=splash_background_color(theme),
    )
    launch.splash.events.closed += lambda *args: _on_splash_closed(launch)
    winicon.attach(launch.splash, 'splash')
    _round_splash_corners_when_ready(launch)
    _start_stage_pusher(launch)
    lifecycle.log_first_run('启动画面已创建')
    return launch.splash


def _on_splash_closed(launch):
    launch.splash_closed.set()
    if launch.handing_over.is_set():
        return
    window = launch.main_window
    if window is None:
        return  # 主窗口还没建出来，此时关掉启动画面就是关掉整个程序
    # 这个回调跑在 GUI 线程上，而 destroy() 要等那个线程把事件处理完：
    # 直接在原地调会把界面冻住最多 20s，得丢给别的线程去做
    threading.Thread(
        target=_destroy_main_window, args=(window,), name='splash-closed-cleanup', daemon=True
    ).start()


def _destroy_main_window(window):
    try:
        window.destroy()
        logging.info("启动画面被关掉，主窗口一并退出")
    except Exception:
        logging.exception("退出时销毁主窗口失败")


def _on_main_window_closed(main_window):
    """
    关掉主窗口就是关掉整个程序。

    pywebview 默认要等所有窗口都关了才让 start() 返回，所以放大预览或设置窗口开着时
    关主窗口，进程会留下来陪那个副窗口——用户看到的是「程序没退干净」。

    两步走，顺序不能反：副窗口的销毁要 Invoke 回 GUI 线程、还要等 WebView2 拆完，
    在 GUI 线程正忙着收尾的那一刻根本排不上号。用户等的是「屏幕上什么时候没有它」，
    所以先当场地把它们从屏幕上撤掉（一个 ShowWindow，不排队），销毁再丢给别的线程。
    """
    global _SECONDARY_CLEANUP_DONE
    if _SECONDARY_CLEANUP_DONE:
        return
    _SECONDARY_CLEANUP_DONE = True

    rest = [window for window in list(webview.windows) if window is not main_window]
    # 它正在关自己，再 hide/destroy 一次只会往日志里泼异常，所以已经排除掉了
    hidden = winclose.hide_all(rest)
    winclose.destroy_later(rest, why='主窗口已关闭')
    logging.info("主窗口已关闭：残留副窗口 %d 个，已当场隐藏 %d 个", len(rest), hidden)


def _start_stage_pusher(launch):
    """
    里程碑一变就写给启动画面。

    单独一个线程，而且要等首帧之后才动手：evaluate_js 是同步等 GUI 线程的，
    核心模块正在加载时 GUI 线程可能好几秒腾不出来，放在启动流程里就是自己卡自己。
    推失败也不要紧——splash.html 那边同时还在轮询 startup_stage()。
    """
    def loop():
        last = None
        deadline = time.monotonic() + MAIN_READY_SECONDS
        while time.monotonic() < deadline and not launch.splash_closed.is_set():
            stage = launch.bridge.stage()
            if stage != last and launch.bridge.first_painted.is_set():
                script = 'window.setStartupStage && window.setStartupStage(%d, %s);' % (
                    stage[0], json.dumps(stage[1], ensure_ascii=False))
                try:
                    launch.splash.evaluate_js(script)
                    last = stage
                except Exception:
                    logging.debug("推送启动进度失败，交给页面轮询兜底", exc_info=True)
            time.sleep(0.1)

    threading.Thread(target=loop, name='splash-progress', daemon=True).start()


def _tell_splash_failure(launch, message):
    """
    核心模块加载失败时，把原因写在启动画面上。

    等首帧是为了确保页面已经在跑 JS——窗口刚建好就 evaluate_js 会被 pywebview 丢掉。
    """
    launch.bridge.first_painted.wait(SPLASH_FIRST_FRAME_SECONDS)
    script = 'window.showStartupFailure && window.showStartupFailure({!r});'.format(message)
    try:
        launch.splash.evaluate_js(script)
    except Exception:
        logging.exception("向启动画面写入失败信息失败")


# 每次启动给界面一点点不一样：色相 ±5 度，再按时段加一点冷暖。这两个数只喂
# body::before 那两段装饰渐变（assets/css/style.css 的 --hue-offset / --decor-warmth），
# 主色、卡片描边、按钮颜色都不经过它们，所以换一次开机不会让任何一处控件变色。
MOOD_HUE_JITTER = 5
# 时段（左闭右开）→ 冷暖偏移、问候语。深夜没有得体的问候，留空：
# 界面那个位置是 :empty 就不占版的，宁可不写也不凑一句。
MOOD_BY_HOUR = (
    ((5, 8), 6, 'greet_morning'),
    ((8, 11), 4, 'greet_forenoon'),
    ((11, 14), 0, 'greet_noon'),
    ((14, 18), -3, 'greet_afternoon'),
    ((18, 23), -6, 'greet_evening'),
)


def _launch_mood():
    """凑出这一次启动的色偏与问候，交给页面去落地（common.js 的 applyLaunchMood）。"""
    hour = time.localtime().tm_hour
    warmth, greeting_key = -8, ''
    for (start, end), w, key in MOOD_BY_HOUR:
        if start <= hour < end:
            warmth, greeting_key = w, key
            break
    return {
        'hueOffset': random.randint(-MOOD_HUE_JITTER, MOOD_HUE_JITTER),
        'warmth': warmth,
        # 发键不发句子：句子在后端定死的话，英文界面右上角就留着「下午好」。
        'greetingKey': greeting_key,
    }


def _tell_main_mood(main_window):
    """
    把本次启动的状态推给主界面，推不到就算了——界面按 style.css 里的默认值显示。

    放在 show() 之后：窗口还没露出来时 WebView2 会省着不跑脚本，早推的那一下经常白给。
    """
    mood = _launch_mood()
    script = 'window.applyLaunchMood && window.applyLaunchMood(%s);' % json.dumps(
        mood, ensure_ascii=False)
    try:
        main_window.evaluate_js(script)
        logging.info("本次启动的界面色偏：hue=%+d warmth=%+d",
                     mood['hueOffset'], mood['warmth'])
    except Exception:
        logging.debug("推送本次启动的界面色偏失败，界面按默认值显示", exc_info=True)


def _reveal(launch):
    """
    主界面加载完后收起启动画面。

    两个条件都要满足：主界面 loaded，且启动画面从真正画出首帧起至少停留了
    MIN_SPLASH_VISIBLE_SECONDS。只有前者的话，加载快时启动画面会短到看不见。
    """
    splash, main_window = launch.splash, launch.main_window
    loaded = threading.Event()

    def _on_main_loaded(*args):
        launch.bridge.set_stage(92, 'splash_stage_near')
        loaded.set()

    main_window.events.loaded += _on_main_loaded

    launch.bridge.first_painted.wait(SPLASH_FIRST_FRAME_SECONDS)
    painted_at = launch.bridge.painted_at or time.monotonic()

    deadline = time.monotonic() + MAIN_READY_SECONDS
    while not loaded.wait(0.1):
        if launch.splash_closed.is_set():
            return
        if time.monotonic() >= deadline:
            logging.error("主界面在 %.0fs 内没有加载完成，仍然把它放出来", MAIN_READY_SECONDS)
            break

    remaining = MIN_SPLASH_VISIBLE_SECONDS - (time.monotonic() - painted_at)
    if remaining > 0:
        time.sleep(remaining)

    if launch.splash_closed.is_set():
        return

    logging.info(
        "启动画面首帧后停留 %.2fs，进程启动至今 %.2fs",
        time.monotonic() - painted_at,
        time.monotonic() - PROCESS_STARTED_AT,
    )
    launch.handing_over.set()

    # 先把条子走到 100% 再切换：不等这一下的话，用户看到的永远是「差一点就满了」
    launch.bridge.set_stage(100, 'splash_stage_enter')
    _push_final_stage(launch, splash)
    time.sleep(SPLASH_FINISH_SECONDS)

    try:
        main_window.show()
    except Exception:
        logging.exception("显示主窗口失败")
    _tell_main_mood(main_window)
    # 先撤掉启动画面再销毁：它是 on_top 的，destroy 要排 GUI 线程和 WebView2 拆除的队，
    # 那几百毫秒里主窗口已经露出来了却还被这一块「已经完成了使命」的画面压着正中，
    # 用户读到的就是「开完还在卡一下」。
    winclose.hide(splash)
    try:
        splash.destroy()
    except Exception:
        # 这里失败不影响主界面，但静默吞掉的话启动画面窗口可能残留在那儿，
        # 排查时一句线索都没有。
        logging.exception("关闭启动画面失败")
    logging.info("主界面已就绪")
    lifecycle.log_first_run('主界面已就绪，进程启动至今 %.2fs', time.monotonic() - PROCESS_STARTED_AT)


def _push_final_stage(launch, splash):
    """收尾这一步直接推一次：120ms 的轮询周期可能来不及，进度就停在 92%。"""
    script = 'window.setStartupStage && window.setStartupStage(100, %s);' % json.dumps(
        '正在进入主界面…', ensure_ascii=False)
    try:
        splash.evaluate_js(script)
        return True
    except Exception:
        logging.debug("推送启动画面收尾进度失败", exc_info=True)
        return False


def _bootstrap(launch):
    """webview.start 之后在后台线程跑：加载重依赖、建主界面、收尾切换。"""
    started_at = time.monotonic()
    lifecycle.log_first_run('开始加载核心模块')
    launch.bridge.set_stage(12, 'splash_stage_core')

    # 把实际选中的渲染引擎留在日志第一行：现场报「白屏、点了没反应」时，
    # 这一句就能一眼分清是 WebView2 的问题还是页面本身的问题
    renderer = getattr(webview, 'renderer', '') or '未知'
    logging.info("渲染引擎: %s", renderer)
    if renderer != 'edgechromium':
        logging.error("渲染引擎退回 %s，界面将无法渲染；见 core/webview2.py", renderer)

    try:
        from api import Api
        api = Api()
    except Exception:
        logging.exception("核心模块加载失败")
        _tell_splash_failure(
            launch,
            launch.bridge.text('splash_fail_core', log=LOG_FILE),
        )
        return

    logging.info("核心模块就绪，耗时 %.2fs", time.monotonic() - started_at)
    lifecycle.log_first_run('核心模块加载完成，耗时 %.2fs', time.monotonic() - started_at)
    launch.bridge.set_stage(45, 'splash_stage_ui')

    try:
        main_width, main_height = MAIN_WINDOW_SIZE
        main_x, main_y = _centered_position(main_width, main_height)
        main_window = webview.create_window(
            _main_window_title(),
            get_asset_path('index.html'),
            js_api=api,
            width=main_width,
            height=main_height,
            x=main_x,
            y=main_y,
            min_size=MAIN_WINDOW_MIN_SIZE,
            hidden=True,
            background_color='#f0f7ff',
        )
    except Exception:
        logging.exception("创建主窗口失败")
        _tell_splash_failure(
            launch,
            launch.bridge.text('splash_fail_window', log=LOG_FILE),
        )
        return

    launch.main_window = main_window
    main_window.events.closed += lambda *args: _on_main_window_closed(main_window)
    winicon.attach(main_window, 'main')
    lifecycle.log_first_run('主窗口已创建')
    launch.bridge.set_stage(68, 'splash_stage_comp')
    if launch.splash_closed.is_set():
        # 加载期间用户就关掉了启动画面，别把他已经关掉的那个程序再变回来
        _on_splash_closed(launch)
        return

    # 向导要从设置页跳回主界面，先把主窗口登记给后端，别靠 url 猜
    from api.wizard import set_main_window
    set_main_window(main_window)

    _reveal(launch)
    _schedule_ocr_preload(api)


def _schedule_ocr_preload(api):
    """主界面交给用户之后，按设置在后台把 OCR 引擎先建好。

    引擎一次要 8~13 秒（Paddle 导入 + 模型进内存）。以前只有打开「提取 → OCR」那一页
    才预热，所以每回重开程序，用户都要在点开那一页的那一刻重新等一遍。
    延迟几秒 + 后台优先级是为了不跟首屏渲染、缩略图出图和启动计时那几段抢同一段时间。
    """
    try:
        preload = bool(api.load_config().get('ocr_preload', True))
    except Exception:
        logging.exception('读取「OCR 后台预加载」配置失败')
        return
    if not preload:
        logging.info('OCR 后台预加载：按设置关闭')
        return

    def loop():
        time.sleep(OCR_PRELOAD_DELAY_SECONDS)
        try:
            # 后台模式把这条线程的 CPU / IO / 换页优先级一起降下来，
            # 用户正在做的事永远排在它前面；代价是它自己可能慢几秒，无所谓。
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)
        except Exception:
            logging.debug('降低 OCR 预加载线程优先级失败', exc_info=True)
        started = time.monotonic()
        try:
            from core import ocr
            result = ocr.warmup(ocr.DEFAULT_QUALITY)
        except Exception:
            logging.exception('OCR 后台预加载失败')
            return
        if result.get('success') is False:
            logging.warning('OCR 后台预加载失败：%s', result.get('message'))
            return
        logging.info('OCR 后台预加载完成，耗时 %.2fs', time.monotonic() - started)

    threading.Thread(target=loop, name='ocr-preload', daemon=True).start()


def _runtime_gate():
    """
    缺网页渲染组件就不要开界面。

    pywebview 在本机找不到 WebView2 时会退回 WinForms/MSHTML（IE）：界面框架照样
    「加载成功」，但现代 CSS 不生效、脚本在解析阶段就报错，用户拿到的是
    一片白底加几行裸文字、点什么都没反应，而进程正常、日志里一个错误码都没有。
    判断口径与 pywebview 自己的选择一致（见 core/webview2.py），
    所以这里说能跑就真能跑；说不能跑时一个窗口都不建，只弹系统消息框。
    """
    info = webview2.probe()
    if info['ok']:
        lifecycle.log_first_run('WebView2 运行时: %s', info.get('runtime') or '可用')
        return True
    lifecycle.log_first_run('启动门禁: 未通过，缺少 %s', '、'.join(info['missing']))
    logging.critical("WebView2 运行时不可用，本机缺少：%s", '、'.join(info['missing']))
    action = '已打开官方下载页' if webview2.show_missing_dialog(info['missing']) else '未打开下载页'
    logging.critical("启动门禁拦下本次启动：%s；退出码 %d", action, RUNTIME_MISSING_EXIT_CODE)
    return False


def main():
    ensure_dirs()
    lifecycle.begin_session()

    logging.info("=" * 60)
    logging.info(f"{APP_NAME}启动 (v{CURRENT_VERSION})")
    logging.info(f"日志文件: {LOG_FILE}")
    logging.info(f"运行模式: {'安装版' if getattr(sys, 'frozen', False) else '源码'}")
    logging.info(f"Python: {sys.version}")

    # 门禁必须排在任何窗口之前：启动画面本身也是 WebView2 渲染的，
    # 组件缺失时它同样是一片纯色矩形，那时用户连「程序在跑」都看不出来。
    if not _runtime_gate():
        logging.info("程序退出")
        logging.shutdown()
        return RUNTIME_MISSING_EXIT_CODE

    launch = _Launch()
    _create_splash(launch)

    # 除了启动画面，其余窗口都在 GUI 起来之后再建：
    # 主界面依赖的 Api 要 load 好几秒，放在这里会让启动画面迟迟出不来。
    webview.start(_bootstrap, (launch,))

    logging.info("程序退出")
    # webview.start 返回不等于进程该继续活着：WebView2 的后台线程会把 Python 解释器
    # 吊在窗口已经关闭之后，表现为任务管理器里还留着 PDFToolkit，重装时报「文件被占用」。
    # logging.shutdown 先落盘，再直接结束进程。
    logging.shutdown()
    os._exit(0)


if __name__ == '__main__':
    _set_app_user_model_id()
    _install_excepthooks()
    # 门禁拦下来的那次要带着退出码走，方便安装器和日志分辨「正常退出」
    sys.exit(main())
