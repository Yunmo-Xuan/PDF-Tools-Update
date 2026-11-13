"""首次启动向导的后端入口。

向导本身画在主界面里（一层遮罩），这里只负责两件事：
把设置窗口让位关掉，再让主窗口把向导显示出来。
"""

import logging

import webview

from core import lifecycle
from core.i18n import t as _text

# 主窗口由 main.py 建好之后登记进来。
# 只靠遍历 webview.windows 猜 url 并不可靠：pywebview 在各平台上回填的
# url 形式不一样（有时还是传入的本地路径），认不出来向导按钮就点了没反应。
_main_window = None


def set_main_window(window):
    global _main_window
    _main_window = window


def _describe_windows():
    items = []
    for w in list(webview.windows):
        items.append('%s | url=%s' % (getattr(w, 'title', '?'), getattr(w, 'url', '?')))
    return ' ; '.join(items) or '(无)'


def _find_main_window():
    if _main_window is not None:
        return _main_window

    candidates = list(reversed(webview.windows))
    for window in candidates:
        blob = '%s %s' % (getattr(window, 'url', '') or '', getattr(window, 'location', '') or '')
        if 'index.html' in blob.lower():
            return window
    for window in candidates:
        # 主窗口的标题栏跟着界面语言走，所以这里认的是当前语言那一句软件名，不是写死的英文
        if str(getattr(window, 'title', '') or '').startswith(_text('app_title')):
            return window
    return None


class WizardMixin:
    def wizard_event(self, event, detail=''):
        """界面把向导走到哪一步告诉后端：首次运行日志要留下这条线。"""
        event = str(event or '')[:32]
        detail = str(detail or '')[:160]
        text = f'向导: {event}' + (f'（{detail}）' if detail else '')
        logging.info(text)
        lifecycle.log_first_run(text)
        return {"success": True}

    def open_wizard(self):
        """从设置页重新运行向导：先关设置窗口，再让主界面显示遮罩。"""
        from api.settings import _settings_window

        window = _find_main_window()
        if window is None:
            logging.warning("打开设置向导失败：找不到主界面窗口，当前窗口 [%s]", _describe_windows())
            return {"success": False, "message": _text('api_wizard_no_main')}

        try:
            if _settings_window is not None:
                self.close_settings_window()
            try:
                window.show()
                window.restore()
            except Exception:
                # 窗口状态接口在不同平台上不完全一致，失败不影响后面的调用
                pass
            window.evaluate_js('window.openWizard && window.openWizard();')
            logging.info("已请求主界面显示设置向导")
            return {"success": True}
        except Exception as error:
            logging.exception("打开设置向导失败")
            return {"success": False, "message": str(error)}
