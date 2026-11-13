r"""打印 PDF（查看页的 🖨️ 与 Ctrl+P）。

按「这份 PDF 有没有文字层」分两条路，两条都不把用户的文件交给网络：
  * 文字型：把 PDF 原样交给本机注册的 PDF 程序去打（Windows 的 printto 动词，
    带上界面选中的那台打印机）。字还是字、矢量还是矢量，由驱动按打印机的
    最高分辨率出结果。这条路绝不自己出图。
  * 扫描件（取不出文字）：内置打印——自己把每一页出成位图，用 Windows 的 GDI
    直接送进打印机的设备上下文。好处是不看机器上装了什么 PDF 阅读器的脸色，
    页码范围、份数都在软件里说清楚，打出来的就是他要的那几页。
    扫描件本来就只是一张图，出成位图不损失任何东西。

系统兜底还剩一种场合：扫描件那条内置路「还没开始送页」就建不上设备上下文
（这台机器没有 GDI、找不到打印机、驱动连不上），才退回系统打印服务。
作业已经送了一半再兜底，等于同一份文件打两遍。

四个实测出来的坑决定了下面的写法：
  1. EnumPrintersW 的 Level 要用 2。写成 4 也能返回成功，但那是另一个结构
     （PRINTER_INFO_4，一条只有 24 字节），照 PRINTER_INFO_2 去读就会读出空列表，
     看起来像「这台机器没装打印机」。
  2. ctypes 取回来的句柄默认按 32 位整数截断，而 64 位系统上 HDC 是指针。
     所以每个用到 hdc 的 API 都要显式写 restype/argtypes，不然在有的机器上是
     「偶尔能用、偶尔把图打到别处去」。
  3. 出图精度不能直接跟着打印机走：600 dpi 的 A4 一页位图就是 139 MB。
     上限压到 300 dpi，剩下的交给 StretchDIBits 放大——纸面上看不出来，内存看得出来。
  4. printto 是「把文件交到别的程序手上」，交完就返回：本程序看不到作业打成什么样，
     所以成功只能说「已交给」，不能说「已打完」。临时副本也不能交完就删——
     那边可能还没读，删早了打印就坏，见 _print_tmp_dir。
"""

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
import sys
import threading
import time

from ._range import select_pages
from core.i18n import t as _text

# 内置打印的出图上限，见模块开头第 3 条
MAX_PRINT_DPI = 300
MIN_PRINT_DPI = 72
# 份数不设上限：打几份是用户自己的事，界面填多少就送多少

# 文字层判定：最多均匀抽 12 页，其中任何一页攒够 20 个非空白字符就算文字型。
# 阈值不取「有一个字就算」：扫描件常带一层水印文字或 OCR 残渣，一两个字谈不上
# 「文字型」，而正文页不可能连 20 个字符都没有。
TEXT_SAMPLE_LIMIT = 12
TEXT_CHARS_MIN = 20

# 打印预览页上那一排版面选项（单双面 / 每张几页 / 页序 / 缩放 / 边距）。
# 这些值只有「内置出图」那一路是本站说了算的（下面 _print_job 真的按它们排版）；
# 「按文件原样交给别的 PDF 程序」那一路，双面与拼版归那边定，界面必须把这句说出来。
DUPLEX_CHOICES = ('off', 'long', 'short')
NUP_CHOICES = (1, 2, 4, 6, 9, 16)
FIT_CHOICES = ('fit', 'actual', 'custom')
SCALE_MIN, SCALE_MAX = 20, 400
MARGIN_MM_MIN, MARGIN_MM_MAX = 0.0, 25.0
# Windows DEVMODE 里翻转方式的取值（wingdi.h）
DMDUP_SIMPLEX = 1
DMDUP_VERTICAL = 2      # 短边翻转（上下翻）
DMDUP_HORIZONTAL = 3    # 长边翻转（左右翻，像书一样）
DMDUP_LOOKUP = {'off': DMDUP_SIMPLEX, 'short': DMDUP_VERTICAL, 'long': DMDUP_HORIZONTAL}
DM_DUPLEX = 0x00001000
DM_FIELDS = 0x00000020
DM_IN_BUFFER = 0x00000001
DM_OUT_BUFFER = 0x00000002

_get_printers_lock = threading.Lock()
# 同一份文件连点两次打印会攒出两个作业，这里串行处理：打印本来就是慢活
_print_lock = threading.Lock()

LOGPIXELSX = 88
LOGPIXELSY = 90
PHYSICALWIDTH = 110
PHYSICALHEIGHT = 111
PHYSICALOFFSETX = 112
PHYSICALOFFSETY = 113
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
PRINTER_ENUM_LOCAL = 2
PRINTER_ENUM_CONNECTIONS = 4
PRINTER_ATTRIBUTE_DEFAULT = 4
ERROR_INSUFFICIENT_BUFFER = 122


class _PrinterInfo2W(ctypes.Structure):
    """winspool.h 里的 PRINTER_INFO_2W，字段顺序和个数都不能改。"""
    _fields_ = [
        ("pServerName", wt.LPWSTR),
        ("pPrinterName", wt.LPWSTR),
        ("pShareName", wt.LPWSTR),
        ("pPortName", wt.LPWSTR),
        ("pDriverName", wt.LPWSTR),
        ("pComment", wt.LPWSTR),
        ("pLocation", wt.LPWSTR),
        ("pDevMode", ctypes.c_void_p),
        ("pSepFile", wt.LPWSTR),
        ("pPrintProcessor", wt.LPWSTR),
        ("pDatatype", wt.LPWSTR),
        ("pParameters", wt.LPWSTR),
        ("pSecurityDescriptor", ctypes.c_void_p),
        ("Attributes", wt.DWORD),
        ("Priority", wt.DWORD),
        ("DefaultPriority", wt.DWORD),
        ("StartTime", wt.DWORD),
        ("Status", wt.DWORD),
        ("cJobs", wt.DWORD),
        ("AveragePPM", wt.DWORD),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _DocInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wt.LPWSTR),
        ("lpszOutput", wt.LPWSTR),
        ("lpszDatatype", wt.LPWSTR),
        ("fwType", wt.DWORD),
    ]


class _DevModeW(ctypes.Structure):
    """
    wingdi.h 里的 DEVMODEW，只声明到打印要动的那几个字段，后面用字节尾巴补齐。

    为什么敢截一半：驱动给的缓冲区比这份声明长（dmDriverExtra 那一段是它自己的私有设置），
    我们按驱动的BufferSize 分配、只按前段的偏移读写，尾部原样留着——
    要是把结构体声明成刚好 sizeof(DEVMODEW) 再交给驱动，驱动往尾部写私有数据就踩出缓冲区了。
    """
    _fields_ = [
        ("dmDeviceName", wt.WCHAR * 32),
        ("dmSpecVersion", wt.WORD),
        ("dmDriverVersion", wt.WORD),
        ("dmSize", wt.WORD),
        ("dmDriverExtra", wt.WORD),
        ("dmFields", wt.DWORD),
        ("dmOrientation", wt.SHORT),
        ("dmPaperSize", wt.SHORT),
        ("dmPaperLength", wt.SHORT),
        ("dmPaperWidth", wt.SHORT),
        ("dmScale", wt.SHORT),
        ("dmCopies", wt.SHORT),
        ("dmDefaultSource", wt.SHORT),
        ("dmPrintQuality", wt.SHORT),
        ("dmColor", wt.SHORT),
        ("dmDuplex", wt.SHORT),
        ("dmYResolution", wt.SHORT),
        ("dmTTOption", wt.SHORT),
        ("dmCollate", wt.SHORT),
        ("dmFormName", wt.WCHAR * 32),
    ]


_gdi32 = None
_winspool = None
_api_failed = ''


def _win_apis():
    """
    取 gdi32 / winspool 两个 dll，取不到就返回 None 并把原因留下。

    延迟到真要用的时候再取：非 Windows 上这些库压根不存在，写在模块顶层
    会让 import api.printing 直接炸，连带把整个打印入口一起带走。
    """
    global _gdi32, _winspool, _api_failed
    if _gdi32 is not None and _winspool is not None:
        return _gdi32, _winspool
    if sys.platform != 'win32':
        _api_failed = f'当前系统不是 Windows（{sys.platform}）'
        return None
    try:
        gdi32 = ctypes.windll.gdi32
        winspool = ctypes.windll['winspool.drv']
    except Exception as error:  # pragma: no cover - 只在缺库的机器上走到
        _api_failed = f'加载打印相关的系统库失败：{error}'
        return None
    # 句柄是指针：不把 restype/argtypes 写全，64 位上就会被按 32 位整数截断
    gdi32.CreateDCW.restype = wt.HDC
    gdi32.CreateDCW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.GetDeviceCaps.restype = ctypes.c_int
    gdi32.GetDeviceCaps.argtypes = [wt.HDC, ctypes.c_int]
    gdi32.StartDocW.restype = ctypes.c_int
    gdi32.StartDocW.argtypes = [wt.HDC, ctypes.POINTER(_DocInfoW)]
    gdi32.StartPage.argtypes = [wt.HDC]
    gdi32.EndPage.argtypes = [wt.HDC]
    gdi32.EndDoc.argtypes = [wt.HDC]
    gdi32.AbortDoc.argtypes = [wt.HDC]
    gdi32.StretchDIBits.restype = ctypes.c_int
    gdi32.StretchDIBits.argtypes = [
        wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.POINTER(_BitmapInfoHeader), wt.UINT, wt.DWORD,
    ]
    winspool.EnumPrintersW.restype = wt.BOOL
    winspool.EnumPrintersW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
    ]
    winspool.GetDefaultPrinterW.restype = wt.BOOL
    winspool.GetDefaultPrinterW.argtypes = [wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    # 问驱动要/改一份 DEVMODE（双面那一档要写进这里，见 _duplex_devmode）
    winspool.DocumentPropertiesW.restype = ctypes.c_int
    winspool.DocumentPropertiesW.argtypes = [
        ctypes.c_void_p, wt.LPCWSTR, wt.LPCWSTR,
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
    ]
    _gdi32, _winspool = gdi32, winspool
    return gdi32, winspool


def _read_wstring_at(address):
    """从缓冲区里读一个以 NUL 结尾的 UTF-16 串；地址为 0 时给空串。"""
    if not address:
        return ''
    return ctypes.wstring_at(address)


def enum_printers():
    """
    列出本机打印机：[{'name', 'port', 'driver', 'is_default'}, ...]，拿不到就返回空列表。

    默认标记以 GetDefaultPrinterW 为准（那才是「这台机器上这个用户现在用哪台」）；
    PRINTER_INFO_2 自带的 PRINTER_ATTRIBUTE_DEFAULT 在很多机器上并不跟着变，
    只信它会把界面预选错一台。
    """
    if not _win_apis():
        return []
    winspool = _winspool
    needed = wt.DWORD(0)
    returned = wt.DWORD(0)
    flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    # 第一次调用注定失败（缓冲区为 0），只要把需要的字节数问出来就行
    winspool.EnumPrintersW(flags, None, 2, None, 0, ctypes.byref(needed), ctypes.byref(returned))
    if ctypes.GetLastError() not in (0, ERROR_INSUFFICIENT_BUFFER) or not needed.value:
        logging.info(f"枚举打印机没有返回内容，错误码 {ctypes.GetLastError()}")
        return []
    buffer = ctypes.create_string_buffer(needed.value)
    if not winspool.EnumPrintersW(flags, None, 2, buffer, needed.value,
                                  ctypes.byref(needed), ctypes.byref(returned)):
        logging.info(f"枚举打印机失败，错误码 {ctypes.GetLastError()}")
        return []

    item_size = ctypes.sizeof(_PrinterInfo2W)
    count = returned.value
    # pcReturned 是「台数」。这一句要读越界就成了访问别的内存，所以按缓冲区容量夹一道
    count = min(count, len(buffer) // item_size)
    default = default_printer()
    base = ctypes.addressof(buffer)
    printers = []
    for index in range(count):
        info = _PrinterInfo2W.from_address(base + index * item_size)
        name = _read_wstring_at(info.pPrinterName)
        if not name:
            continue
        printers.append({
            "name": name,
            "port": _read_wstring_at(info.pPortName),
            "driver": _read_wstring_at(info.pDriverName),
            "is_default": bool(name == default) or bool(info.Attributes & PRINTER_ATTRIBUTE_DEFAULT),
        })
    return printers


def default_printer():
    """本机默认打印机名；没有默认打印机或不在 Windows 上时返回空串。"""
    if not _win_apis():
        return ''
    size = wt.DWORD(0)
    _winspool.GetDefaultPrinterW(None, ctypes.byref(size))
    if not size.value:
        return ''
    buffer = ctypes.create_unicode_buffer(size.value)
    if _winspool.GetDefaultPrinterW(buffer, ctypes.byref(size)):
        return buffer.value
    return ''


# ==================== 交给本机 PDF 程序按文件打印（文字型走这条路）====================
# ShellExecuteEx 的返回值只有大于 32 才是成功，32 及以下是一批系统错误码
SHELL_OK_MIN = 32
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102
# 把文件交到别的程序手上之后，等它读一会儿再往下走：多份连着交要按顺序，
# 临时副本也要等对方读完才敢删。等不到不算失败——作业通常已经进了后台队列。
HANDOFF_WAIT_MS = 10000
# 打印临时副本最多在机器上留多久，下一次打印时清掉过期的
PRINT_TMP_KEEP_SECONDS = 3600

# 常见的几个失败码翻成人话：这一路看不到对方程序的脸色，能说的只有系统给的回执
_SHELL_ERRORS = {
    2: '要打印的文件找不到了',
    3: '文件所在的路径有问题',
    5: '当前用户没有权限做这件事',
    22: '读文件时出错',
    26: '文件正被别的程序占着',
    27: '这台机器上没有登记「打印」这个动作该交给谁',
    28: '和那个 PDF 程序传话没成功',
    29: '那个 PDF 程序正忙，没接手这一次',
    30: '等那个 PDF 程序回话超时',
    31: '这台机器上没有能打印 PDF 的程序（PDF 的文件关联里没指向任何阅读器）',
    32: '系统缺了要用到的一个库',
}

_shell32 = None
_kernel32 = None
_shell_failed = ''


class _ShellExecuteInfoW(ctypes.Structure):
    """shellapi.h 里的 SHELLEXECUTEINFOW，字段顺序和个数都不能改。

    最后那个 hProcess 只有带上 SEE_MASK_NOCLOSEPROCESS 才拿得到值：文件是交给
    别的程序去读的，没有它的进程句柄就不知道读完没有，临时副本只能不敢删。
    """
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", wt.LPCWSTR),
        ("lpFile", wt.LPCWSTR),
        ("lpParameters", wt.LPCWSTR),
        ("lpDirectory", wt.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wt.LPCWSTR),
        ("hKeyClass", ctypes.c_void_p),
        ("dwHotKey", wt.DWORD),
        ("hMonitor", ctypes.c_void_p),
        ("hProcess", wt.HANDLE),
    ]


def _shell_apis():
    """
    取 shell32 / kernel32 并把签名写全；取不到返回 None，原因留在 _shell_failed。

    和 _win_apis() 一样延迟到真要用时才取：非 Windows 上这两个库压根不存在，
    写在模块顶层会让 import api.printing 直接炸，连带把内置打印那条路一起带走。
    """
    global _shell32, _kernel32, _shell_failed
    if _shell32 is not None and _kernel32 is not None:
        return _shell32, _kernel32
    if sys.platform != 'win32':
        _shell_failed = f'当前系统不是 Windows（{sys.platform}）'
        return None
    try:
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
    except Exception as error:  # pragma: no cover - 只在缺库的机器上走到
        _shell_failed = f'加载系统外壳库失败：{error}'
        return None
    shell32.ShellExecuteExW.restype = wt.BOOL
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfoW)]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _shell32, _kernel32 = shell32, kernel32
    return shell32, kernel32


def _launch_with_shell(verb, path, printer='', wait_ms=HANDOFF_WAIT_MS):
    """
    让系统按文件关联把这个文件交给某个程序去做一件事，返回 {'ok', 'message', 'done'}。

    printer 非空时用 printto 动词并把打印机名当参数递过去（和「发送到」菜单同一个
    机制），选中的那台才真被打；不给打印机名就退回 print 动词，由系统按默认打印机走。
    nShow 给 SW_HIDE：打印是后台活，不该在用户屏幕上蹦出阅读器窗口。
    wait_ms 是「愿意为知道对方读完没有而等多久」，给 0 就是交完就走。
    """
    apis = _shell_apis()
    if not apis:
        return {"ok": False, "message": _shell_failed, "done": False}
    shell32, kernel32 = apis
    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(_ShellExecuteInfoW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = verb
    info.lpFile = path
    info.lpParameters = ('"%s"' % printer) if printer else None
    info.lpDirectory = None
    info.nShow = SW_HIDE
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.GetLastError()
        detail = _SHELL_ERRORS.get(code, f'系统返回错误码 {code}')
        logging.info(f"ShellExecuteExW({verb}) 失败: {path} code={code}")
        return {"ok": False, "message": detail, "done": False}
    handle = info.hProcess
    if not handle:
        # 拿不到进程句柄（目标程序把这活转给了别的已在跑的实例）：
        # 只能当成「已交接」，临时副本交给超时清理那一步兜
        return {"ok": True, "message": '', "done": False}
    try:
        if wait_ms <= 0:
            return {"ok": True, "message": '', "done": False}
        state = kernel32.WaitForSingleObject(handle, wait_ms)
        return {"ok": True, "message": '', "done": state == _WAIT_OBJECT_0}
    finally:
        kernel32.CloseHandle(handle)


def _print_tmp_dir():
    """打印临时副本的落点（Local AppData\\PDFToolkit\\printfiles）。

    不放系统临时目录：一是和草稿同一套口径，验收时挪沙盒目录就能整体挪走；
    二是这里的文件要等对面程序读走，不能指望系统的 %TEMP% 清理器替你收尾。
    """
    from core.paths import PRINT_TMP_DIR
    try:
        os.makedirs(PRINT_TMP_DIR, exist_ok=True)
    except Exception:
        logging.exception("创建打印临时目录失败")
    return PRINT_TMP_DIR


def _sweep_print_tmp(keep=''):
    """
    清掉上一次打印留下的过期副本，返回删掉的个数。

    为什么不交完就删：那个程序可能还在读这份文件，删早了打印直接坏掉。
    为什么不能不删：这是用户的机器，每打一次留一个副本等于永久攒垃圾。
    所以只删超过 PRINT_TMP_KEEP_SECONDS 的，而且只删这个目录里我们自己命名的文件。
    """
    directory = _print_tmp_dir()
    now = time.time()
    removed = 0
    try:
        names = os.listdir(directory)
    except Exception:
        logging.exception(f"读打印临时目录失败: {directory}")
        return 0
    for name in names:
        if not (name.startswith('print-') and name.endswith('.pdf')):
            continue
        path = os.path.join(directory, name)
        if keep and os.path.normcase(path) == os.path.normcase(keep):
            continue
        try:
            if now - os.path.getmtime(path) < PRINT_TMP_KEEP_SECONDS:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            # 还在被读、或已经被别的清理干掉了：都不是错误，记一条就行
            logging.info(f"打印临时副本没删掉（先不管它）: {path}")
    if removed:
        logging.info(f"打印临时副本清理: 删掉 {removed} 个过期文件")
    return removed


def _write_range_copy(doc, indexes, path):
    """
    把要打印的那几页原样攒成一份临时 PDF（内容流照搬，字还是字）。

    printto 这个动词不带页码范围，只能交整本；要「只打 3-5 页」又不把字糊成图，
    就只能给它一份只含这几页的文件。insert_pdf 是逐页复制对象，不做任何栅格化。
    """
    import pymupdf as fitz
    out = fitz.open()
    try:
        for index in indexes:
            out.insert_pdf(doc, from_page=index, to_page=index)
        out.save(path, deflate=True)
    finally:
        out.close()
    return path


def _system_print(pdf_path, printer='', launch=None, wait_ms=HANDOFF_WAIT_MS):
    """
    把文件交给系统的打印服务：Windows 走文件关联的 print/printto 动词，POSIX 走 lp。

    这一步完全交给别的程序：本程序看不到作业打成什么样，所以成功只代表「已交接」，
    消息里必须这么说。launch 是注入点，验收用它替掉真启动（真跑会真的出纸或弹窗）。
    """
    verb = 'printto' if printer else 'print'
    if sys.platform != 'win32':
        command = ['lp'] + (['-d', printer] if printer else []) + [pdf_path]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=30)
            return {"success": True, "method": "system", "done": True}
        except Exception as error:
            return {"success": False, "method": "system", "message": _text('api_print_sys_failed') % error}
    result = (launch or _launch_with_shell)(verb, pdf_path, printer, wait_ms)
    if not result.get("ok"):
        logging.warning(f"系统打印交接失败: {pdf_path}（{result.get('message')}）")
        return {
            "success": False,
            "method": "system",
            "message": _text('api_print_use_app') % (result.get('message') or _text('api_print_sys_not_started')),
            "done": False,
        }
    return {"success": True, "method": "system", "done": bool(result.get("done"))}


def sample_page_indexes(total, wanted=None, limit=TEXT_SAMPLE_LIMIT):
    """
    挑出要看文字层的页（0 起）：给定了要打的页就只看这些页，否则整本均匀取样。

    均匀取样而不是只看开头几页：书的前言几页常常是空的，末尾多是封底，
    只看开头会把带正文的文件判成扫描件。首页和末页一定看得到。
    """
    try:
        count = int(total)
    except (TypeError, ValueError):
        return []
    if count <= 0:
        return []
    pool = []
    if wanted:
        for value in wanted:
            try:
                index = int(value) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < count and index not in pool:
                pool.append(index)
    if not pool:
        pool = list(range(count))
    if limit <= 1 or len(pool) <= limit:
        return pool[:limit] if limit > 0 else []
    step = (len(pool) - 1) / (limit - 1)
    picked = [pool[int(round(index * step))] for index in range(limit)]
    # round 在 step 很接近 0.5 的极端档位上可能撞车，去重后补一页，保证取样数够
    unique = []
    for index in picked:
        if index not in unique:
            unique.append(index)
    for index in pool:
        if len(unique) >= limit:
            break
        if index not in unique:
            unique.append(index)
    return unique


def count_text_chars(page):
    """这一页能取出多少个非空白字符；页面读不动就当 0，不让文字层判定把打印炸掉。"""
    try:
        text = page.get_text() or ''
    except Exception:
        logging.debug("取文字失败，这一页按无文字处理: page=%s",
                      getattr(page, "number", "?"), exc_info=True)
        return 0
    return len(''.join(text.split()))


def text_layer_info(doc, indexes):
    """返回 (是不是文字型, 抽了几页, 抽样里的字符总数)。"""
    sampled = 0
    chars = 0
    for index in indexes:
        found = count_text_chars(doc[index])
        sampled += 1
        chars += found
        if found >= TEXT_CHARS_MIN:
            return True, sampled, chars
    return False, sampled, chars


def normalize_layout(layout=None):
    """
    把界面那一排版面选项收成一份能用的字典；认不出的一律回到默认档。

    默认档就是 7.0.3 及以前的行为（单面、一张一页、正常页序、缩放到可打印区、不加边距），
    所以老调用点（只传四个参数的那几处）和不带版面选项的验收，看到的都是原来的样子。
    这里一律「不认就退回默认」而不是「不认就报错」：这些选项只影响纸怎么排，
    填错了不值得把用户这一趟打印整段拒掉。
    """
    src = layout if isinstance(layout, dict) else {}
    duplex = str(src.get('duplex') or 'off').strip().lower()
    if duplex not in DUPLEX_CHOICES:
        duplex = 'off'
    try:
        nup = int(src.get('nup') or 1)
    except (TypeError, ValueError):
        nup = 1
    if nup not in NUP_CHOICES:
        nup = 1
    fit = str(src.get('fit') or 'fit').strip().lower()
    if fit not in FIT_CHOICES:
        fit = 'fit'
    try:
        scale = int(src.get('scale') or 100)
    except (TypeError, ValueError):
        scale = 100
    scale = max(SCALE_MIN, min(SCALE_MAX, scale))
    try:
        margin = float(src.get('margin_mm') or 0)
    except (TypeError, ValueError):
        margin = 0.0
    if not (MARGIN_MM_MIN <= margin <= MARGIN_MM_MAX):
        margin = 0.0
    return {
        'duplex': duplex,
        'nup': nup,
        'fit': fit,
        'scale': scale,
        'margin_mm': round(margin, 1),
        'reverse': bool(src.get('reverse')),
    }


def nup_grid(nup):
    """每张纸排 nup 页时的 (列, 行)：1→1×1、2→2×1、4→2×2、6→3×2、9→3×3、16→4×4。"""
    return {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3), 16: (4, 4)}.get(nup, (1, 1))


def _layout_is_default(layout):
    """这份版面选项是不是「和 7.0.3 的样子一模一样」：只有动过选项的人才该看到那些补充说明。"""
    return (layout or normalize_layout()) == normalize_layout()


def sheet_math(page_total, copies, nup=1, duplex='off'):
    """
    算「这一趟要过几张纸」：页数 → 每份几面 → 每份几张 → 一共几张。

    双面是一张纸两面（长边/短边只差别在纸怎么翻，张数一样），所以张数只看
    有几面、每面几格；凑不满的那一面照样要吃一张纸，这里向上取整而不是四舍五入——
    报小了用户会当场发现纸不够。
    """
    per_side = max(1, int(nup))
    sides = -(-max(0, int(page_total)) // per_side)
    sheets_per_copy = -(-sides // (2 if duplex in ('long', 'short') else 1))
    copies = max(1, int(copies))
    return {
        "page_total": int(page_total),
        "per_sheet": per_side,
        "sides_per_copy": sides,
        "sheets_per_copy": sheets_per_copy,
        "copies": copies,
        "sheets": sheets_per_copy * copies,
    }


def cell_rects(area_width, area_height, cols, rows):
    """
    把可打印区切成 cols×rows 个格子：[(x, y, 宽, 高), ...]，行优先（先排满上面一行）。

    整除不了的那几 px 摊给前面几列/几行（每格最多差 1 px），目的是不让最后一格
    莫名窄一条——预览和纸面对不上就是从这种地方开始的。
    """
    if area_width <= 0 or area_height <= 0 or cols <= 0 or rows <= 0:
        return []
    rects = []
    for row in range(rows):
        y = (row * area_height) // rows
        h = ((row + 1) * area_height) // rows - y
        for col in range(cols):
            x = (col * area_width) // cols
            w = ((col + 1) * area_width) // cols - x
            rects.append((x, y, w, h))
    return rects


def scale_factor(layout, cell_width, cell_height, image_width, image_height):
    """
    一页在该格子里要画多大：返回乘在「出图像素尺寸」上的倍率。

    出图用的就是设备 dpi，所以那份像素图本身已经是纸上的实际大小，倍率 1 = 100%。
    预览页画版面时调的是同一个函数，纸面上落在哪儿和预览里落在哪儿不会两套算法。

    三档缩放（打印预览页上那三个选项就是它）：
      fit    = 等比缩到刚好放进格子（7.0.3 及以前的唯一行为）；
      actual = 实际大小，一格装不下的部分由驱动吃掉；
      custom = 先按 fit 算，再乘用户填的那个百分比。
    尺寸读不出来（0 或负数）时按 fit 走，不给 0 除。
    """
    if image_width <= 0 or image_height <= 0 or cell_width <= 0 or cell_height <= 0:
        return 1.0
    fit = min(cell_width / image_width, cell_height / image_height)
    mode = (layout or {}).get('fit', 'fit')
    if mode == 'actual':
        return 1.0
    if mode == 'custom':
        pct = (layout or {}).get('scale', 100)
        try:
            pct = int(pct)
        except (TypeError, ValueError):
            pct = 100
        return fit * (max(SCALE_MIN, min(SCALE_MAX, pct)) / 100.0)
    return fit


def build_plan(doc, wanted_pages='', printer='', copies=1, layout=None):
    """
    把「这一趟该怎么打」算清楚：不建设备上下文、不出图、也不交文件。

    校验（页码范围、份数、打印机名）、版面选项和路数判定都收在这里，print_pdf 照这个结果执行，
    print_plan 直接把它还给界面和验收——所以验收能核「这个文件会走哪条路、要过几张纸」，
    而不必真的送出一张纸。返回 (plan, 错误说明)，其中一个必为 None。

    plan 里的 method：'file' = 把 PDF 原样交给本机 PDF 程序（文字型）；
                     'raster' = 内置出图（扫描件那种取不出文字的）。
    """
    total = doc.page_count
    if total <= 0:
        return None, "这个 PDF 没有可打印的页面"
    page_numbers, error = select_pages(wanted_pages, total)
    if error:
        return None, error
    try:
        asked = int(copies)
    except (TypeError, ValueError):
        asked = 1
    copies = max(1, asked)

    name = (str(printer) or '').strip() or default_printer()
    if not name:
        printers = enum_printers()
        if printers:
            name = printers[0]["name"]

    is_text, sampled, chars = text_layer_info(doc, sample_page_indexes(total, page_numbers))
    indexes = [p - 1 for p in page_numbers]
    whole = len(indexes) == total
    if is_text:
        reason = (f"这份 PDF 有文字层（抽样 {sampled} 页取出 {chars} 个字符），"
                  f"按文件原样打印，字不会被转成图片")
    else:
        reason = (f"这份 PDF 取不出文字（抽样 {sampled} 页共 {chars} 个字符，扫描件那种），"
                  f"用软件内置打印出图")
    lay = normalize_layout(layout)
    # 每一面里挤几页只由拼版档决定；双面是一张纸两面，所以「几面」除 2 才是「几张纸」。
    # 页幅面在这里顺手读一次（读 rect 不读像素，一页几微秒），预览页要拿它算版面比例。
    sizes = []
    for index in indexes:
        try:
            rect = doc[index].rect
            sizes.append([round(rect.width, 1), round(rect.height, 1)])
        except Exception:
            sizes.append([0.0, 0.0])
    sheets = sheet_math(len(page_numbers), copies, lay['nup'], lay['duplex'])
    plan = dict(sheets, **{
        "method": "file" if is_text else "raster",
        "printer": name,
        "pages": page_numbers,
        "page_count": total,
        "copies": copies,
        "whole_document": whole,
        "text_layer": is_text,
        "sampled": sampled,
        "chars": chars,
        "layout": lay,
        "page_sizes": sizes,
        "reason": reason,
    })
    return plan, None


def page_to_dib(page, dpi):
    """
    把一页出成 StretchDIBits 收得下的位图字节：(bytes, 宽, 高)。

    32 位 BI_RGB 的字节序是 BGRX，而 PyMuPDF 出的是 RGB —— 直接把它给的字节端上去，
    打出来红蓝互换（彩色图纸、扫描件最明显）。这里过一道 Pillow 换成 BGRX。
    biHeight 取负数表示自顶向下，正好和图像字节顺序一致，省掉再翻一遍。
    """
    import pymupdf as fitz
    from PIL import Image

    zoom = float(dpi) / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return image.tobytes("raw", "BGRX"), pix.width, pix.height


def fit_rect(page_width, page_height, area_width, area_height, offset_x=0, offset_y=0):
    """
    算出一页该落在可打印区域的哪个位置：(x, y, 宽, 高)，等比缩放到刚好放得下并居中。

    可打印区域是驱动给出的物理尺寸减去四边不可打印的边距，单位是设备像素；
    落在这个框外的那部分驱动会直接吃掉，所以「放大到铺满整张纸」不能做。
    """
    if page_width <= 0 or page_height <= 0 or area_width <= 0 or area_height <= 0:
        return offset_x, offset_y, max(0, area_width), max(0, area_height)
    scale = min(area_width / page_width, area_height / page_height)
    width = max(1, int(round(page_width * scale)))
    height = max(1, int(round(page_height * scale)))
    x = offset_x + (area_width - width) // 2
    y = offset_y + (area_height - height) // 2
    return x, y, width, height


def print_dpi(hdc):
    """按打印机的分辨率定出图精度：取两个方向里较小的，再夹到 [72, 300]。"""
    gdi32 = _gdi32
    x = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
    y = gdi32.GetDeviceCaps(hdc, LOGPIXELSY)
    candidates = [v for v in (x, y) if v and v > 0]
    value = min(candidates) if candidates else 300
    return max(MIN_PRINT_DPI, min(value, MAX_PRINT_DPI))


def paint_page(hdc, page, dpi, area_width, area_height, offset_x=0, offset_y=0, layout=None):
    """
    把一页画到设备上下文上（或画进纸上的某一个格子），返回实际落位 (x, y, 宽, 高)。

    出图字节要在 blit 期间活着：ctypes 只交给 GDI 一个指针，
    一旦这个字节串被回收，画出来的就是别的东西，所以这里显式攥着它直到调用返回。

    layout 不给时就是 7.0.3 及以前的行为：等比缩到刚好放进这块区域并居中（fit_rect）。
    给了就按预览页那一套算倍率（见 scale_factor），照样居中——
    「实际大小」那一档会画得比格子大，多出来的是驱动吃掉的部分，纸面上看得见、预览里也看得见。
    """
    raw, width, height = page_to_dib(page, dpi)
    if layout:
        factor = scale_factor(layout, area_width, area_height, width, height)
        draw_w = max(1, int(round(width * factor)))
        draw_h = max(1, int(round(height * factor)))
        x = offset_x + (area_width - draw_w) // 2
        y = offset_y + (area_height - draw_h) // 2
    else:
        x, y, draw_w, draw_h = fit_rect(width, height, area_width, area_height, offset_x, offset_y)
    header = _BitmapInfoHeader()
    header.biSize = ctypes.sizeof(_BitmapInfoHeader)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0
    header.biSizeImage = len(raw)
    done = _gdi32.StretchDIBits(hdc, x, y, draw_w, draw_h, 0, 0, width, height,
                                raw, ctypes.byref(header), DIB_RGB_COLORS, SRCCOPY)
    if done == 0:
        raise OSError('StretchDIBits 没有画出任何内容（驱动拒绝了这一页）')
    return x, y, draw_w, draw_h


def duplex_devmode(name, duplex):
    """
    问驱动要一份默认 DEVMODE、改成用户选的那一档翻转、再让驱动过一遍；返回 (缓冲区, 驱动接受的档)。

    双面这件事本程序说不了不算数：纸是驱动翻的，能不能翻只有驱动知道。所以这里
    「请求 + 读回」两步都走 DocumentPropertiesW，最后以驱动写回来的 dmDuplex 为准，
    界面拿到的那句才敢说「这台机器真会双面」。任何一步不成就给 (None, '')，
    作业照旧建上下文，只是按单面出，界面会说清是哪一种。

    缓冲区一律按驱动报的字节数分配（dmDriverExtra 那段是驱动的私有设置，
    声明成 sizeof(DEVMODEW) 交给它就是把私有数据写到别人的内存上）。
    """
    if duplex not in ('long', 'short'):
        return None, ''
    apis = _win_apis()
    if not apis:
        return None, ''
    winspool = apis[1]
    try:
        size = winspool.DocumentPropertiesW(None, name, name, None, None, 0)
        if size <= 0:
            logging.info(f"读不到「{name}」的默认打印设置，双面请求作罢（返回 {size}）")
            return None, ''
        base = ctypes.create_string_buffer(size)
        if winspool.DocumentPropertiesW(None, name, name, base, None, DM_OUT_BUFFER) < 0:
            logging.info(f"「{name}」不接受读默认打印设置，错误码 {ctypes.GetLastError()}")
            return None, ''
        view = ctypes.cast(base, ctypes.POINTER(_DevModeW))
        want = DMDUP_LOOKUP[duplex]
        view.contents.dmDuplex = want
        view.contents.dmFields |= DM_DUPLEX
        # 第二遍把改过的递给驱动，同时收驱动改回来的那一份：驱动不认这一档就会把它拧回单面
        feed = ctypes.create_string_buffer(size)
        ctypes.memmove(feed, base, size)
        out = ctypes.create_string_buffer(size)
        if winspool.DocumentPropertiesW(None, name, name, out, feed,
                                        DM_IN_BUFFER | DM_OUT_BUFFER) < 0:
            logging.info(f"「{name}」不接受改打印设置，错误码 {ctypes.GetLastError()}")
            return None, ''
        got = ctypes.cast(out, ctypes.POINTER(_DevModeW)).contents.dmDuplex
        return out, ('duplex' if got == want else 'simplex')
    except Exception:
        logging.debug("问驱动要双面设置失败", exc_info=True)
        return None, ''


def _print_job(hdc, doc, page_indexes, copies, job_name, layout=None):
    """
    在已经建好的设备上下文上打一份作业：StartDoc → 逐面 → EndDoc。

    作业级别的错（StartDoc/StartPage/EndPage/EndDoc 失败）会 AbortDoc 后往上抛：
    宁可一份都不出，也不要出半份还继续往下送。
    单页出图失败只是那一格空白，其余照打，失败页号攒下来给调用方报——
    为了一页渲染失败把已经送进去的几十页作废掉，用户只会觉得「按了没反应」。

    layout 是预览页那一排版面选项（见 normalize_layout）：不传就是 7.0.3 及以前的样子
    ——一页一面、铺满可打印区、正常页序、不加边距，返回值也和以前逐个数相同。
    一次 StartPage 交出去的是「纸的一面」，几张纸要按双面与否折算，所以这里数三个量：
    送出多少页、画了多少面、过几张纸。返回 (页数, 面数, 张数, dpi, 出图失败的页号列表)。
    """
    gdi32 = _gdi32
    lay = normalize_layout(layout)
    phys_w = gdi32.GetDeviceCaps(hdc, PHYSICALWIDTH)
    phys_h = gdi32.GetDeviceCaps(hdc, PHYSICALHEIGHT)
    off_x = gdi32.GetDeviceCaps(hdc, PHYSICALOFFSETX)
    off_y = gdi32.GetDeviceCaps(hdc, PHYSICALOFFSETY)
    # 个别驱动在物理尺寸上返回 0：这时按整张可打印区算，至少不至于算出负数
    area_w = phys_w - 2 * off_x if phys_w and phys_w > 2 * off_x else max(1, phys_w)
    area_h = phys_h - 2 * off_y if phys_h and phys_h > 2 * off_y else max(1, phys_h)
    dpi = print_dpi(hdc)
    # 用户加的边距叠在驱动那段不可打印边距之内侧；夹到不把可打印区吃穿为止
    pad = int(round(lay['margin_mm'] / 25.4 * dpi))
    pad = min(pad, max(0, area_w // 2 - 1), max(0, area_h // 2 - 1))
    area_w = max(1, area_w - 2 * pad)
    area_h = max(1, area_h - 2 * pad)
    off_x += pad
    off_y += pad
    cols, rows = nup_grid(lay['nup'])
    cells = cell_rects(area_w, area_h, cols, rows) or [(0, 0, area_w, area_h)]

    order = list(reversed(page_indexes)) if lay['reverse'] else list(page_indexes)
    sides = [order[start:start + len(cells)] for start in range(0, len(order), len(cells))]
    if not sides:
        sides = [[]]
    duplex = lay['duplex'] in ('long', 'short')

    info = _DocInfoW()
    info.cbSize = ctypes.sizeof(_DocInfoW)
    info.lpszDocName = job_name
    if gdi32.StartDocW(hdc, ctypes.byref(info)) <= 0:
        raise OSError(f"StartDoc 失败，错误码 {ctypes.GetLastError()}")

    delivered = 0
    faces = 0
    sheets = 0
    failed = []
    try:
        for _copy in range(copies):
            for side_index, side in enumerate(sides):
                if gdi32.StartPage(hdc) <= 0:
                    raise OSError(f"StartPage 失败（第 {side[0] + 1 if side else 0} 页），"
                                  f"错误码 {ctypes.GetLastError()}")
                for slot, page_index in enumerate(side):
                    cell_x, cell_y, cell_w, cell_h = cells[slot]
                    try:
                        paint_page(hdc, doc[page_index], dpi, cell_w, cell_h,
                                   off_x + cell_x, off_y + cell_y, lay)
                        delivered += 1
                    except Exception:
                        logging.exception(f"打印第 {page_index + 1} 页失败")
                        if page_index + 1 not in failed:
                            failed.append(page_index + 1)
                # 这一面没画完也要走完 EndPage，否则会卡在半页状态里出不来
                if gdi32.EndPage(hdc) <= 0:
                    first = side[0] + 1 if side else 0
                    raise OSError(f"EndPage 失败（第 {first} 页），错误码 {ctypes.GetLastError()}")
                faces += 1
                # 双面由驱动把相邻两面折到同一张纸上；单面就是一面一张
                if not duplex or side_index % 2 == 0:
                    sheets += 1
        if gdi32.EndDoc(hdc) <= 0:
            raise OSError(f"EndDoc 失败，错误码 {ctypes.GetLastError()}")
    except Exception:
        gdi32.AbortDoc(hdc)
        raise
    return delivered, faces, sheets, dpi, failed


class PrintMixin:
    """打印相关的 js_api 入口。"""

    def get_printers(self):
        """列出可选打印机。返回形状稳定：界面拿到空列表就照常渲染下拉框。"""
        with _get_printers_lock:
            printers = enum_printers()
        return {
            "success": True,
            "printers": printers,
            "default": default_printer(),
            # 内置打印不可用的原因只在拿不到的时候给，界面据此决定怎么说
            "builtin": bool(printers),
            "message": '' if printers else (_api_failed or _text('api_no_printer_available')),
        }

    def print_plan(self, pdf_path, printer='', pages='', copies=1, layout=None):
        """
        只算这一趟会怎么打，一张纸都不送。

        界面用它把「按文件还是内置出图、共几页几份几张纸」写在打印预览页底下那行，
        让用户在按下打印之前就知道字会不会被糊成图、要费几张纸；验收也用它核对路数——
        真打印一旦按下就可能出纸，判据不能建立在出纸上。返回形状和 print_pdf 对齐：
        method 是 file / raster，版面选项原样带回来（归一化过的），前端不必再算一遍。
        """
        import pymupdf as fitz

        if not pdf_path or not os.path.isfile(pdf_path):
            return {"success": False, "method": "none", 'message': _text('api_no_print_files')}
        doc = None
        try:
            doc = fitz.open(pdf_path)
            if doc.needs_pass:
                return {"success": False, "method": "none", 'message': _text('api_print_locked')}
            plan, error = build_plan(doc, pages, printer, copies, layout)
            if error:
                return {"success": False, "method": "none", "message": error}
            return dict(plan, success=True, message=plan["reason"])
        except Exception as e:
            logging.exception("print_plan 失败")
            return {"success": False, "method": "none", "message": str(e)}
        finally:
            if doc is not None:
                doc.close()

    def print_pdf(self, pdf_path, printer='', pages='', copies=1, layout=None):
        """
        打印一份 PDF。

        printer 为空就用本机默认打印机；pages 沿用「1-5, 8, 11-13」那套写法，
        空串表示整个文档（和拆分、导文本同一口径，见 api/_range.py）；
        copies 是份数，不设上限；
        layout 是打印预览页那一排版面选项（单双面 / 每张几页 / 页序 / 缩放 / 边距），
        不传或传一份认不出的都退回 7.0.3 的样子（单面、一页一张、正常序、铺满可打印区）。
        内置出图那一路照它排版；按文件交出去那一路只是把选项记在回执里，
        那边程序认不认由它说，本程序不替它答应。
        返回里的 method：file = 按文件原样交给本机 PDF 程序（有文字层时走这条），
        builtin = 内置出图（扫描件），system = 内置那条路还没开始送页就交给了系统，
        none = 校验没过、一趟都没送。
        """
        import pymupdf as fitz

        if not pdf_path or not os.path.isfile(pdf_path):
            return {"success": False, "method": "none", 'message': _text('api_no_print_files')}
        try:
            doc = fitz.open(pdf_path)
        except Exception as error:
            logging.exception(f"打不开要打印的 PDF: {pdf_path}")
            return {"success": False, "method": "none",
                    "message": _text('api_print_open_failed') % (type(error).__name__, error)}
        try:
            if doc.needs_pass:
                return {"success": False, "method": "none", 'message': _text('api_print_locked')}
            plan, error = build_plan(doc, pages, printer, copies, layout)
            if error:
                return {"success": False, "method": "none", "message": error}
            if plan["method"] == "file":
                return self._print_as_file(pdf_path, doc, plan)
            return self._print_raster(pdf_path, doc, plan)
        finally:
            doc.close()

    def _print_as_file(self, pdf_path, doc, plan):
        """
        文字型：把 PDF 原样交给本机注册的 PDF 程序去打（字不当图糊）。

        这一路看不到作业打成什么样，所以成功只能说「已交接」；
        要打的不是整本时先攒一份只含那几页的副本，因为 printto 动词不带页码范围。
        """
        name = plan["printer"]
        copies = plan["copies"]
        count = len(plan["pages"])
        target = pdf_path
        temp = ''
        if not plan["whole_document"]:
            _sweep_print_tmp()
            stem = os.path.splitext(os.path.basename(pdf_path))[0]
            temp = os.path.join(_print_tmp_dir(),
                                f"print-{time.strftime('%Y%m%d-%H%M%S')}-{stem}.pdf")
            try:
                _write_range_copy(doc, [p - 1 for p in plan["pages"]], temp)
            except Exception as error:
                logging.exception("攒打印副本失败")
                if os.path.isfile(temp):
                    try:
                        os.remove(temp)
                    except OSError:
                        pass
                return {"success": False, "method": "none",
                        "message": _text('api_print_temp_failed')
                                   % (type(error).__name__, error)}
            target = temp

        started = time.perf_counter()
        handed = 0
        last_done = True
        for copy_index in range(copies):
            # 只给最后一份留等待的时间：每份都等对方读完，20 份就是几十秒，
            # 而桥层同步调用只有 5 秒的预算（方案 §4.13）。前面几份交完就走，
            # 顺序由阅读器自己的打印队列保（同一台机器、同一个程序，先交的先打）。
            budget = HANDOFF_WAIT_MS if copy_index == copies - 1 else 0
            result = _system_print(target, name, wait_ms=budget)
            if not result.get("success"):
                message = result.get("message") or "交给系统打印失败"
                if temp:
                    message += f"（临时文件 {os.path.basename(temp)} 留在打印目录里，稍后自动清理）"
                return {"success": False, "method": "file", "printer": name,
                        "pages": count, "copies": handed, "text_layer": True,
                        "message": message}
            handed += 1
            last_done = bool(result.get("done"))

        cleaned = True
        if temp and last_done:
            # 对面程序已经退出，这份副本不会再被读，当场删掉；读没读完就不碰它，
            # 删早了等于把用户的打印弄坏，留着交给下次打印的过期清理
            try:
                os.remove(temp)
            except OSError:
                cleaned = False
        elif temp:
            cleaned = False

        where = f"「{name}」" if name else "系统默认打印机"
        message = f"已把文件原样交给{where}打印：{count} 页 ×{copies} 份（有文字层，没转成图片）"
        if not plan["whole_document"]:
            message += f"，只含所选的 {count} 页"
        # 版面选项对这条路说了也不算：printto 只带文件名和打印机，双面/拼版/边距
        # 全凭对面那程序的打印设置。不说清这一句，用户会以为这边选了双面就真能双面。
        if not _layout_is_default(plan.get("layout")):
            message += f"；双面/拼版/边距这几样到了那边归它的打印设置定，张数按你选的估的是 {plan.get('sheets')}"
        if temp and not cleaned:
            message += "；那份临时文件还在打印目录里，下次打印时自动清掉"
        logging.info(f"按文件打印已交接: {pdf_path} {count} 页 ×{copies} 份 打印机={name or '默认'}"
                     f"（{time.perf_counter() - started:.2f}s，临时副本={os.path.basename(temp) if temp else '无'}）")
        return {
            "success": True,
            "method": "file",
            "printer": name,
            "pages": count,
            "copies": copies,
            "sheets": plan.get("sheets", count * copies),
            "per_sheet": plan.get("per_sheet", 1),
            "layout": plan.get("layout") or normalize_layout(),
            "layout_applied": False,
            "text_layer": True,
            "whole_document": plan["whole_document"],
            "temp_file": temp if not cleaned else '',
            "message": message,
        }

    def _print_raster(self, pdf_path, doc, plan):
        """
        扫描件：内置出图，用 GDI 直接画进打印机的设备上下文。

        只有「还没开始送页」的失败才退回系统打印：作业送了一半再兜底等于打两遍。

        预览页那一排版面选项在这一路是真生效的：拼版/边距/缩放走 _print_job 的几何，
        页序在这里倒一遍，双面按驱动接受的档定——驱动拧回单面时结果里就写单面，
        不能替驱动许愿。
        """
        name = plan["printer"]
        copies = plan["copies"]
        lay = plan.get("layout") or normalize_layout()
        indexes = [p - 1 for p in plan["pages"]]
        if not name:
            return self._fallback_to_system(pdf_path, '本机没有可用的打印机', '')
        if not _win_apis():
            return self._fallback_to_system(
                pdf_path, _api_failed or '这台机器不支持内置打印', '')

        devmode, accepted = duplex_devmode(name, lay["duplex"])
        hdc = _gdi32.CreateDCW(name, None, None, devmode)
        if not hdc:
            return self._fallback_to_system(
                pdf_path, f'连不上打印机「{name}」', name)

        try:
            try:
                with _print_lock:
                    delivered, faces, sheets, dpi, failed = _print_job(
                        hdc, doc, indexes, copies, os.path.basename(pdf_path), lay)
            finally:
                _gdi32.DeleteDC(hdc)
        except Exception as error:
            logging.exception(f"内置打印失败: {pdf_path}")
            # 能走到这里的只剩「建好上下文之后的错」。作业可能已经送出去一部分，
            # 这时候再兜底就是把同一份文件再打一遍，所以只报原因，不重复打
            return {
                "success": False,
                "method": "builtin",
                "message": _text('api_print_builtin_failed') % (type(error).__name__, error),
            }

        logging.info(f"内置打印完成: {pdf_path} 送出 {delivered} 页 / {faces} 面 / {sheets} 张"
                     f"（{len(indexes)} 页 ×{copies}，每张 {lay['nup']} 页，"
                     f"{lay['duplex']} 面，{dpi} dpi，打印机 {name}）"
                     + (f"，其中 {failed} 页出图失败" if failed else ''))
        message = f"已送到「{name}」：{len(indexes)} 页 ×{copies}（内置出图 {dpi} dpi）"
        if not _layout_is_default(lay):
            bits = [f"每张 {lay['nup']} 页"] if lay['nup'] > 1 else []
            if lay['duplex'] != 'off':
                bits.append('双面已由驱动接受' if accepted == 'duplex' else '这台机器不接受双面，按单面出')
            if lay['reverse']:
                bits.append('逆序')
            if lay['margin_mm']:
                bits.append(f"边距 {lay['margin_mm']} mm")
            if lay['fit'] != 'fit':
                bits.append('实际大小' if lay['fit'] == 'actual'
                            else f"缩放 {lay['scale']}%")
            message += f"；{'、'.join(bits)}，过了 {sheets} 张纸"
        if failed:
            message += f"；第 {'、'.join(str(p) for p in failed)} 页出图失败，纸上是空白"
        return {
            "success": not failed,
            "method": "builtin",
            "printer": name,
            "pages": len(indexes),
            "copies": copies,
            "sheets": sheets,
            "faces": faces,
            "delivered": delivered,
            "per_sheet": lay["nup"],
            "layout": lay,
            "layout_applied": True,
            "duplex_accepted": accepted,
            "dpi": dpi,
            "failed_pages": failed,
            "text_layer": False,
            "message": message,
        }

    def _fallback_to_system(self, pdf_path, reason, printer=''):
        """内置打印还没开始就把这活交给系统，并把原因带在返回里。"""
        logging.info(f"改用系统打印: {pdf_path}（{reason}）")
        result = _system_print(pdf_path, printer)
        result["reason"] = reason
        if result.get("success"):
            result["method"] = "system"
            result["message"] = _text('api_print_handoff') % reason + \
                                ("（%s）" % printer if printer else _text('api_print_default_printer'))
        return result
