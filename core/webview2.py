"""网页渲染组件（WebView2 运行时）的检查与缺失提示。

界面整个跑在 WebView2 里，pywebview 在这台机器上找不到运行时会退回
WinForms/MSHTML（也就是 IE 内核）。退回去之后页面看着是「打开了」，
但现代 CSS 全不生效、脚本在解析阶段就抛 SyntaxError，
用户拿到的是一片白底加几行没排版的裸文字，点什么都没反应——
而进程活着、日志里一个错误码都没有，从现场几乎查不出来。
与其把那种界面交出去，不如在开窗之前就说清楚缺什么、去哪儿装。

判定口径与 pywebview 自己的选择保持一致（同一批 EdgeUpdate 客户端注册表键、
同一个最低内核版本、外加 .NET Framework 4.6.2 这条它也会查的前提），
否则会出现「程序说缺、pywebview 却跑得起来」这种互相打脸的分歧。
见 webview/platforms/winforms.py 的 _is_chromium()。
"""

import ctypes
import logging
import os
import platform
import sys
import webbrowser

IS_WINDOWS = sys.platform == 'win32'

# EdgeUpdate 为每个通道各写一个 Clients 键，pv 值就是那个通道的内核版本号。
# 只认 WebView2 的四个通道：Edge 浏览器自己装的键（{F3017245-…}）不代表运行时存在。
_CLIENT_KEYS = (
    ('{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'WebView2 运行时'),
    ('{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}', 'WebView2 Beta'),
    ('{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}', 'WebView2 Developer'),
    ('{65C35B14-6C1D-4122-AC46-7148CC9D6497}', 'WebView2 Canary'),
)
# pywebview 从 86.0.622.0 起才用（对应 WebView2 Runtime 86），低于它就退回 IE
MIN_BUILD = (86, 0, 622, 0)
# .NET Framework 4.6.2 的 Release 值：WinForms 包装层要它
MIN_DOTNET_RELEASE = 394802

# 官方下载页（微软自己那份，含 Evergreen Bootstrapper 与离线安装器），中文站。
# .NET Framework 也在这里给出入口，缺件提示只有一条链接，别让人自己猜。
RUNTIME_PAGE_URL = 'https://developer.microsoft.com/zh-cn/microsoft-edge/webview2/'

_MB_YESNO = 0x04
_MB_ICONERROR = 0x10
_MB_DEFBUTTON2 = 0x200
_IDYES = 6


def _version_tuple(text):
    """"86.0.622.0" -> (86, 0, 622, 0)；解析不出来返回 None（宁可当它没有）。"""
    parts = str(text or '').strip().split('.')
    if not parts or parts == ['']:
        return None
    try:
        return tuple(int(p) for p in parts[:4])
    except ValueError:
        return None


def _client_paths():
    """
    给出要试的 (注册表根, 子键路径) 组合。

    HKCU 里是「当前用户装的运行时」，HKLM 里是「全机器装的」。
    64 位 Python 读 HKLM 的这份键必须走 WOW6432Node（EdgeUpdate 是 32 位程序，
    它写进去的位置就在那儿），32 位进程则已经落在 32 位视图里、不能再加前缀。
    """
    if platform.machine() == 'x86':
        prefix = ''
    else:
        prefix = 'WOW6432Node\\'
    for guid, _label in _CLIENT_KEYS:
        yield 'HKEY_CURRENT_USER', rf'Microsoft\EdgeUpdate\Clients\{guid}'
        yield 'HKEY_LOCAL_MACHINE', rf'{prefix}Microsoft\EdgeUpdate\Clients\{guid}'


def _read_pv(hive_name, sub_key):
    try:
        import winreg
        with winreg.OpenKey(getattr(winreg, hive_name), rf'SOFTWARE\{sub_key}') as key:
            value, _ = winreg.QueryValueEx(key, 'pv')
        return str(value).strip()
    except Exception:
        return ''


def runtime_version():
    """
    本机可用的 WebView2 内核版本号；没有则返回空串。

    固定版本（fixed version）分发是把整个运行时放在一个目录里、
    用 WEBVIEW2_RUNTIME_PATH 告诉 pywebview 去哪找，这种机器注册表里查不到，
    所以先认这个环境变量与 webview.settings 里的值。
    """
    if not IS_WINDOWS:
        return 'non-windows'
    if os.environ.get('WEBVIEW2_RUNTIME_PATH'):
        return 'fixed-version'
    try:
        import webview
        if webview.settings.get('WEBVIEW2_RUNTIME_PATH'):
            return 'fixed-version'
    except Exception:
        pass

    best = ''
    best_tuple = None
    for hive, path in _client_paths():
        pv = _read_pv(hive, path)
        build = _version_tuple(pv)
        if build and (best_tuple is None or build > best_tuple):
            best, best_tuple = pv, build
    return best


def dotnet_release():
    """本机 .NET Framework 4.x 的 Release 值；查不到返回 0。"""
    if not IS_WINDOWS:
        return MIN_DOTNET_RELEASE
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full'
        ) as key:
            value, _ = winreg.QueryValueEx(key, 'Release')
        return int(value)
    except Exception:
        return 0


def probe():
    """
    返回 {'ok', 'runtime', 'dotnet', 'missing'}，missing 是缺件说明列表。

    ok 为真就等价于「pywebview 会选 edgechromium」；为假时界面必然退回 IE，
    那时不该开窗，见 main.py 的 _runtime_gate。
    """
    runtime = runtime_version()
    release = dotnet_release()
    missing = []

    build = _version_tuple(runtime)
    usable = runtime in ('non-windows', 'fixed-version') or (
        build is not None and build >= MIN_BUILD)
    if not usable:
        missing.append('Microsoft Edge WebView2 运行时')
    if IS_WINDOWS and release < MIN_DOTNET_RELEASE:
        missing.append('.NET Framework 4.6.2 或更高版本')

    return {
        'ok': not missing,
        'runtime': runtime,
        'dotnet': release,
        'missing': missing,
    }


def build_message(missing):
    """弹窗正文：缺什么、后果是什么、去哪儿装。"""
    lines = ['PDF Toolkit需要下面的系统组件才能显示界面：', '']
    for item in missing:
        lines.append('    · ' + item)
    lines += [
        '',
        '缺少它时程序只能退回过时的 IE 内核，那个内核跑不动本程序的界面，',
        '打开后是一片空白、所有按钮都没有反应，所以程序在这里就停下来了。',
        '',
        '装完不用卸载或重装本程序，直接再启动一次即可。',
        '缺 WebView2 运行时的话，本程序安装包里已带微软官方的离线安装器，重新运行安装即可装上；',
        '也可以去官方下载页面自行下载。',
        '是否现在打开官方下载页面？',
    ]
    return '\n'.join(lines)


def open_download_page():
    """打开官方下载页；浏览器调不起来时把地址写进日志，让用户自己复制。"""
    try:
        if webbrowser.open(RUNTIME_PAGE_URL):
            return True
    except Exception:
        logging.exception('调用默认浏览器打开下载页失败')
    logging.error('请手动在浏览器中打开：%s', RUNTIME_PAGE_URL)
    return False


def show_missing_dialog(missing):
    """
    用系统消息框说明缺件，用户同意时顺手打开下载页，返回是否打开了。

    这里不能用界面弹窗——界面正是坏掉的那一环；也不能 print，
    打包成 --windowed 的 exe 没有控制台。消息框是 Win32 自带的，不引新依赖。
    """
    try:
        choice = ctypes.windll.user32.MessageBoxW(
            None, build_message(missing), 'PDF Toolkit - 缺少必需的组件',
            _MB_YESNO | _MB_ICONERROR | _MB_DEFBUTTON2)
    except Exception:
        logging.exception('弹出缺件提示失败，只留日志')
        return False
    return open_download_page() if choice == _IDYES else False
