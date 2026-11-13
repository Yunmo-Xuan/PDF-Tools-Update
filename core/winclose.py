"""主窗口一关，剩下的副窗口要当场从屏幕上消失。

为什么要单独一个模块：用户报的是「关主窗口后附带的窗口有将近 1 秒的延迟才能被关掉」，
实测（tests/out/close_lag_probe.py，源码版三轮）副窗口**不再可见**要 410~884 ms，
彻底消失要 1.3~2.4 s。这 1 秒里两段都省不掉：

  ① main.py 原先 threading.Timer(0.3, ...) 那 300 ms 是写死的等待；
  ② window.destroy() 是 Invoke 到 GUI 线程的同步调用，要等那一窗的 WebView2
     控制器拆除完才返回，几个副窗口还是串着来。

②改不掉——拆除本来就得在 GUI 线程上排队做。但用户根本不在意窗口什么时候被
真正销毁，他在意的是「它还挂在屏幕上」。所以这一步先把顶层窗口 ShowWindow 掉
（同进程 GUI 线程上的一个 Win32 调用，不等任何事件循环、不等任何拆除），
再把 destroy() 丢给别的线程慢慢走完。屏幕上那一瞬间从 0.4~0.9 s 变成当场没了。
"""

import ctypes
import logging
import threading

from core import winicon

SW_HIDE = 0

user32 = ctypes.windll.user32
user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = ctypes.c_bool


def hide(window):
    """把一个 pywebview 窗口当场从屏幕上撤掉；句柄还没建出来就算了。"""
    hwnd = winicon.window_hwnd(window)
    if not hwnd:
        return False
    try:
        return bool(user32.ShowWindow(hwnd, SW_HIDE))
    except Exception:
        logging.debug("隐藏窗口失败", exc_info=True)
        return False


def hide_all(windows):
    """依次撤掉；返回真撤掉了几颗。副窗口统共就设置 / 放大预览两三个，不用并发。"""
    return sum(1 for window in windows if hide(window))


def destroy_later(windows, why=''):
    """
    在别的线程把这几个窗口销毁掉，主线程一步都不等。

    destroy() 会 Invoke 到 GUI 线程并同步等返回，所以**绝不能**在窗口自己的
    closed 回调里原地调：那是 GUI 线程自己等自己，pywebview 里这条的旧注释写的
    「冻住最多 20s」就是它。
    """
    def loop():
        for window in windows:
            try:
                window.destroy()
            except Exception:
                logging.exception("销毁残留窗口失败（%s）", why or '未说明')
        logging.info("残留副窗口已销毁 %d 个（%s）", len(windows), why or '未说明')

    threading.Thread(target=loop, name='window-cleanup', daemon=True).start()
