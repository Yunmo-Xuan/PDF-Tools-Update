"""给每个顶层窗口按系统度量设图标（标题栏 / 任务栏 / Alt+Tab）。

为什么需要这一模块：pywebview 建窗口时如果没显式传 icon，会走
`ExtractIconW(模块句柄, sys.executable, 0)` 从自己的 exe 里取第一号图标，
本机实测拿到的是 **32×32** 的 HICON（tests/out/icon_size_probe.py），
而标题栏那一格只有 16 px（SM_CXSMICON）。WinForms 于是把 32 的图现场压到 16，
用户看到的就是「图标糊了」——原图是全的，糊在尺寸取错这一步。

这里按窗口自己的 DPI 量出小图标与大图标各该多大，用 LoadImage 从 .ico 里
**按尺寸取对应那一档**（16/32/48/64 四档都是重做过的清晰版，见 tests/out/icon_repack.py），
再用 WM_SETICON 分别塞给 ICON_SMALL / ICON_SMALL2 / ICON_BIG，Windows 就不必再缩放。
"""

import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import wintypes

from core.paths import get_base_path

GA_ROOT = 2
GCLP_HICON = -14
GCLP_HICONSM = -34
ICON_BIG = 1
ICON_SMALL = 0
ICON_SMALL2 = 2
IMAGE_ICON = 1  # winuser.h：IMAGE_BITMAP=0 / IMAGE_ICON=1 / IMAGE_CURSOR=2
LR_LOADFROMFILE = 0x0010
LR_SHARED = 0x00008000
SM_CXICON = 11
SM_CYICON = 12
SM_CXSMICON = 49
SM_CYSMICON = 50
WM_GETICON = 0x007F
WM_SETICON = 0x0080
# 窗口句柄要等了 GUI 线程才建得出来，最多等这么久
APPLY_TIMEOUT_SECONDS = 6.0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClassLongPtrW.restype = ctypes.c_ssize_t
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = ctypes.c_uint
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.LoadImageW.restype = wintypes.HANDLE
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
user32.GetIconInfo.restype = wintypes.BOOL
gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
gdi32.GetObjectW.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL


class IconInfo(ctypes.Structure):
    _fields_ = [('fIcon', wintypes.BOOL), ('xHotspot', wintypes.DWORD),
                ('yHotspot', wintypes.DWORD), ('hbmMask', wintypes.HBITMAP),
                ('hbmColor', wintypes.HBITMAP)]


class Bitmap(ctypes.Structure):
    _fields_ = [('bmType', wintypes.LONG), ('bmWidth', wintypes.LONG),
                ('bmHeight', wintypes.LONG), ('bmWidthBytes', wintypes.LONG),
                ('bmPlanes', wintypes.WORD), ('bmBitsPixel', wintypes.WORD),
                ('bmBits', ctypes.c_void_p)]


def icon_file():
    """.ico 的落点（源码 = 项目根 resources，打包后 = _internal/resources）。"""
    return os.path.join(get_base_path(), 'resources', 'pdf_icon.ico')


def icon_side(handle):
    """量一个 HICON 实际是多少像素，取不到返回 None。验收断言靠它判「是不是 1:1 那一档」。"""
    if not handle:
        return None
    info = IconInfo()
    if not user32.GetIconInfo(handle, ctypes.byref(info)):
        return None
    bmp = Bitmap()
    got = gdi32.GetObjectW(info.hbmColor or info.hbmMask, ctypes.sizeof(bmp), ctypes.byref(bmp))
    for owned in (info.hbmColor, info.hbmMask):
        if owned:
            gdi32.DeleteObject(owned)
    if not got:
        return None
    if not info.hbmColor:
        # 只剩 AND 掩码那一面时，位图高度是图像高度的两倍
        return bmp.bmWidth, bmp.bmHeight // 2
    return bmp.bmWidth, bmp.bmHeight


def root_hwnd(handle):
    """从控件句柄往上找到真正带标题栏的那个顶层窗口。"""
    if not handle:
        return None
    found = user32.GetAncestor(handle, GA_ROOT)
    return found or None


def window_hwnd(window):
    """pywebview 窗口的顶层句柄；窗口还没建出来时返回 None。"""
    native = getattr(window, 'native', None)
    if native is None:
        return None
    try:
        # .NET 的 IntPtr 不能直接 int()，要先 ToInt64
        return root_hwnd(native.Handle.ToInt64())
    except Exception:
        logging.debug("取窗口顶层句柄失败", exc_info=True)
        return None


def wanted_sides(hwnd):
    """这个窗口的标题栏图标与任务栏图标各该多大（跟着窗口自己的 DPI 走）。"""
    try:
        dpi = user32.GetDpiForWindow(hwnd) or 96
    except Exception:
        dpi = 96
    scale = dpi / 96.0

    def metric(index, fallback):
        try:
            got = user32.GetSystemMetrics(index)
        except Exception:
            got = 0
        return got or int(round(fallback * scale))

    return max(1, metric(SM_CXSMICON, 16)), max(1, metric(SM_CXICON, 32))


def load_icon(side, path=None):
    """
    从 .ico 里按尺寸取那一档。

    LR_SHARED 让系统自己缓存这些句柄，所以调用方**不要** DestroyIcon——
    窗口要一直用到它，销毁了标题栏就空了。
    """
    handle = user32.LoadImageW(None, path or icon_file(), IMAGE_ICON,
                               side, side, LR_LOADFROMFILE | LR_SHARED)
    return handle if handle else None


def apply_to_hwnd(hwnd, path=None):
    """给一个顶层窗口设好大小两档图标，返回是否都设上了。"""
    if not hwnd or sys.platform != 'win32':
        return False
    source = path or icon_file()
    if not os.path.isfile(source):
        logging.warning("图标文件不存在，窗口图标沿用系统取值: %s", source)
        return False
    small_side, big_side = wanted_sides(hwnd)
    small = load_icon(small_side, source)
    big = load_icon(big_side, source)
    if not small or not big:
        logging.warning("按尺寸取图标失败（%dpx / %dpx），沿用系统取值", small_side, big_side)
        return False
    # ICON_SMALL2 是 Vista 之后标题栏真正读的那一档，只设 ICON_SMALL 会被它盖过去
    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, small)
    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
    logging.info("窗口图标已按尺寸设置：标题栏 %dpx、任务栏 %dpx", small_side, big_side)
    return True


def current_sides(hwnd):
    """读回这个窗口现在实际用的两档图标尺寸，给验收断言用。"""
    small = user32.SendMessageW(hwnd, WM_GETICON, ICON_SMALL2, 0) or \
        user32.GetClassLongPtrW(hwnd, GCLP_HICONSM)
    big = user32.SendMessageW(hwnd, WM_GETICON, ICON_BIG, 0) or \
        user32.GetClassLongPtrW(hwnd, GCLP_HICON)
    return {'small': icon_side(small or None), 'big': icon_side(big or None)}


def attach(window, name=''):
    """
    等窗口建出来，然后给它设图标。

    设置窗口和放大预览是「点了才建」的，句柄什么时候才有没法预知，所以开一条
    有限次轮询的守护线程（最多 6 秒，设上就退）。设不上只影响标题栏那 16 像素，
    不值得为它加常驻轮询，也不值得挂在某一两个事件上赌时序。
    """
    def loop():
        deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            hwnd = window_hwnd(window)
            if hwnd and apply_to_hwnd(hwnd):
                return
            time.sleep(0.1)
        logging.warning("窗口 %s 的图标没设上，标题栏沿用系统取值", name or '?')

    threading.Thread(target=loop, name='winicon-%s' % (name or 'window'), daemon=True).start()
