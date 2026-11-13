"""子进程一律不许再开控制台窗口。

成品是 console=False 的窗口程序，它自己没有控制台：只要有任何库 spawn 一个子进程，
Windows 就给那个子进程现开一个控制台——Paddle 在 import 时跑 `where nvcc` 和
`where ccache` 两条探测，用户看到的就是「加载 OCR 时闪过的两个 cmd 窗口」。
第三方库里那些调用点追不完（以后升级还会再加），所以在这一层统一兜住：
调用方没有显式要求「新控制台 / 脱离控制台」时，补上 CREATE_NO_WINDOW。
"""

import inspect
import subprocess
import sys

_installed = False
# creationflags 在 Popen.__init__ 的形参表里排第几（含 self），按位置传参时要用
_FLAGS_SLOT = None

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
# 这两条是调用方自己要求的窗口形态，不能被我们改掉
_KEEP_CONSOLE = (getattr(subprocess, 'CREATE_NEW_CONSOLE', 0x00000010)
                 | getattr(subprocess, 'DETACHED_PROCESS', 0x00000008))


def install():
    """装兜底；重复调用只生效一次，非 Windows 上什么都不做。"""
    global _installed, _FLAGS_SLOT
    if _installed or sys.platform != 'win32':
        return
    _installed = True
    names = list(inspect.signature(subprocess.Popen.__init__).parameters)
    _FLAGS_SLOT = names.index('creationflags') if 'creationflags' in names else None
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        # args 里没有 self，所以位置比形参表少一格
        if _FLAGS_SLOT is not None and len(args) >= _FLAGS_SLOT:
            if not (args[_FLAGS_SLOT - 1] or 0) & _KEEP_CONSOLE:
                args = args[:_FLAGS_SLOT - 1] + (
                    (args[_FLAGS_SLOT - 1] or 0) | NO_WINDOW,) + args[_FLAGS_SLOT:]
        elif not (kwargs.get('creationflags') or 0) & _KEEP_CONSOLE:
            kwargs['creationflags'] = (kwargs.get('creationflags') or 0) | NO_WINDOW
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched
