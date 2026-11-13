"""Windows 资源管理器右键菜单的桥接层：查询与开关。

真正碰注册表的是 core/shell.py——只写 HKCU、只写自己那两棵树、
失败如实回话，这一层不重复实现也不吞结果：

* ``context_menu_status()``  → {'success', 'registered', 'enabled', 'entries', ...}
* ``set_context_menu(bool)`` → {'success', 'enabled', 'action', 'message', ...}

message 全部走字典键：取话的动作在 core/shell.py 里做（t('api_shell_*') / t('shell_*')），
键名与中文原文清单见 tests/out/shell_menu_report.txt，由维护者统一补进十本字典。
这一层自己一句中文都不写，也不 catch-then-say-ok——桥两头必须同一副口径。
"""

import logging

from core import shell


class ShellMixin:
    def context_menu_status(self):
        """当前用户资源管理器右键菜单的接入状态（设置页显示与验收都读这里）。

        success 说的是「查询这件事办成了」；到底注册没注册看 registered/enabled——
        「没注册」是正常现状，不是失败。
        """
        try:
            result = shell.status()
        except Exception:  # core 已全量兜底；这一道防的是「winreg 被换坏」这类意外
            logging.exception('读取右键菜单状态失败')
            return shell.failure_result('status')
        result.setdefault('success', result.get('reason') != 'error')
        return result

    def set_context_menu(self, enabled):
        """开/关右键菜单。返回里的 enabled 是办完之后回读注册表的真实状态，
        写失败就报 False——配置落盘由界面按这个值来，不带假的。"""
        wanted = bool(enabled)
        try:
            result = shell.register() if wanted else shell.unregister()
        except Exception:  # 同上，最后一道；core 自己从不抛
            logging.exception('设置右键菜单失败（wanted=%s）', wanted)
            return shell.failure_result('register' if wanted else 'unregister')
        logging.info('右键菜单开关：%s（wanted=%s reason=%s）',
                     '成功' if result.get('success') else '未成功',
                     wanted, result.get('reason'))
        return result
