import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser

import webview

from core.constants import (
    APP_AUTHOR,
    APP_NAME,
    CURRENT_VERSION,
    DEFAULT_CONFIG,
    LICENSE_PAGE_URL,
    README_PAGE_URL,
    UPDATE_PAGE_URL,
)
from core.logger import (
    ERROR_LOG_FILE,
    INSTALL_LOG_FILE,
    LOG_DIR,
    LOG_FILE,
    clear_log,
    log_usage,
    protected_log_usage,
)
from core import winclose
from core import jobctl
from core.paths import CONFIG_FILE, INSTALL_STAMP_FILE
from core.i18n import (AUTO, available, detect_system_language, resolve_language,
                       set_language, texts)


def _install_stamp():
    """
    读安装批次标记（安装器写在 exe 旁边的 install.stamp），没有就返回空串。

    便携版和源码模式没有这个文件，向导那时退回只比版本号。
    """
    try:
        with open(INSTALL_STAMP_FILE, 'r', encoding='utf-8') as handle:
            return handle.read().strip()[:64]
    except OSError:
        return ''


def _build_file_types(filetypes):
    """
    把前端传来的 filetypes 统一成 pywebview 要求的 "描述 (*.ext)" 列表。

    pywebview 会对每一项调用 parse_file_type()，它只接受完整的
    "描述 (*.ext)" 写法；遇到裸字符串（例如 "Image files"）会直接抛
    ValueError，整个打开/保存对话框都弹不出来。

    前端历史上传过两种写法，这里都兼容：
        ("PDF files", "*.pdf")     -> 两段式，拼成一段
        ("Image Files (*.png)",)   -> 已是完整写法，原样保留
    并保证列表末尾一定有「所有文件」。
    """
    if not filetypes:
        return ("All files (*.*)",)
    if isinstance(filetypes, str):
        filetypes = [filetypes]

    items = [str(x) for x in filetypes if x]
    if len(items) == 2 and '(' not in items[0]:
        items = [f"{items[0]} ({items[1]})"]

    result = [x for x in items if '(' in x and ')' in x]
    if not result:
        result = ["All files (*.*)"]
    if not any(x.startswith("All files") for x in result):
        result.append("All files (*.*)")
    return tuple(result)


def _target_window():
    """
    当前正在操作的窗口。

    设置窗口是独立窗口，如果仍固定用 webview.windows[0]，
    从设置窗口触发的对话框就可能挂到主窗口上（甚至挂到已销毁的启动画面上）。
    """
    try:
        window = webview.active_window()
        if window is not None:
            return window
    except Exception:
        pass
    return webview.windows[0] if webview.windows else None


class BaseApi:
    CONFIG_FILE = CONFIG_FILE

    # ---------- 配置 ----------
    def load_config(self):
        """读取配置，并与默认值合并（老配置文件缺少的新键自动补上）。"""
        stored = {}
        try:
            with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                stored = loaded
            else:
                logging.warning(f"配置文件内容不是对象，改用默认值: {self.CONFIG_FILE}")
        except FileNotFoundError:
            pass
        except Exception:
            logging.exception(f"读取配置失败: {self.CONFIG_FILE}")

        config = dict(DEFAULT_CONFIG)
        config.update(stored)
        # 后端侧那些「没有界面语言参数可带」的文案（OCR 的报错之类）跟着这份配置走
        set_language(config.get('language'))
        return config

    def save_config(self, config):
        try:
            if not isinstance(config, dict):
                logging.warning(f"保存配置被忽略：收到的不是对象而是 {type(config).__name__}")
                return False
            # 前端可能把它拿到的兜底值（{} 或 {success:false,...}）原样存回来。
            # 只认已知键并据此补齐，异常载荷直接拒绝，否则一次误存就把最近文件全清了。
            known = {k: v for k, v in config.items() if k in DEFAULT_CONFIG}
            if not known:
                logging.warning(f"保存配置被忽略：内容里没有任何已知配置项: {list(config)[:5]}")
                return False
            merged = dict(DEFAULT_CONFIG)
            merged.update(known)
            # 配置目录可能被清理工具删掉，写之前先确保它存在
            os.makedirs(os.path.dirname(self.CONFIG_FILE), exist_ok=True)
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            set_language(merged.get('language'))
            return True
        except Exception:
            logging.exception(f"保存配置失败: {self.CONFIG_FILE}")
            return False

    # ---------- 元信息 ----------
    def get_version(self):
        """界面显示用版本号。"""
        return CURRENT_VERSION

    def get_log_path(self):
        return LOG_FILE

    def save_config_and_bundle(self, config):
        """
        存配置，并把界面「应用设置」之后马上要用的四样一次回齐：
        合并后的配置、这一档语言的整包文案、语言清单、「关于」那几行需要的信息。

        加这一条只为省往返：原来点一次「保存并应用」，界面要连着发
        load_config → save_config → get_all_text → get_language_info → get_app_info
        五次桥调用，再广播给各窗口（每个窗口又各自 load_config + get_all_text 两次）。
        一次桥调用实测 30~120 ms，串起来就是用户嫌的那一秒。
        返回里带 `success`，其余字段与那五个单独调用一模一样，老调用方一律不受影响。
        """
        saved = self.save_config(config)
        out = {"success": bool(saved)}
        if not saved:
            return out
        merged = self.load_config()
        out["config"] = merged
        out["texts"] = self.get_all_text()
        out["language_info"] = self.get_language_info()
        out["app_info"] = self.get_app_info()
        return out

    def get_app_info(self):
        """设置窗口「关于」页需要的信息。"""
        return {
            "name": APP_NAME,
            "version": CURRENT_VERSION,
            # 向导按它判断本机这个版本走过没有（version 给界面显示用，两个字段各留一头）
            "current_version": CURRENT_VERSION,
            # 这一回安装的批次标记：同一个版本反复重装时，向导靠它区分「装完第一次启动」
            "install_stamp": _install_stamp(),
            "author": APP_AUTHOR,
            "update_page": UPDATE_PAGE_URL,
            "license_page": LICENSE_PAGE_URL,
            "readme_page": README_PAGE_URL,
            "config_file": self.CONFIG_FILE,
            # 日志目录是固定的；三类日志都在它下面，「打开日志目录」一次看全
            "log_dir": LOG_DIR,
            "log_file": LOG_FILE,
            "install_log_file": INSTALL_LOG_FILE,
            "error_log_file": ERROR_LOG_FILE,
            # 设置窗口那行「运行日志」要顺带说清现在占多大，用户才知道该不该清理
            "log_usage": log_usage(),
            # 安装/首次运行与报错日志不参与清理，单独报占用
            "protected_log_usage": protected_log_usage(),
        }

    def job_status(self):
        """界面上「第 n/N 页 + 取消」那颗东西要的状态；纯字典，不弹任何东西。"""
        return jobctl.status()

    def job_cancel(self):
        """用户点「取消」：只置一个标记，循环里下一次查到才真停（强杀会把成果一起丢掉）。"""
        return jobctl.cancel()

    def get_all_text(self):
        """按配置里的语言返回界面文案（缺键回退简体中文）。"""
        return texts((self.load_config() or {}).get('language'))

    def get_language_info(self):
        """语言清单 + 当前生效的那一档 + 这台电脑的系统语言，给向导和设置窗口用。

        分开报 current 和 system 是因为「跟随系统」这一项要在界面上显示成
        「跟随系统（简体中文）」，用户才知道不选具体语言会得到什么。
        """
        stored = (self.load_config() or {}).get('language') or AUTO
        return {'available': available(), 'stored': stored,
                'current': resolve_language(stored), 'system': detect_system_language()}

    # ---------- 日志 ----------
    def open_log_folder(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            logging.info(f"打开日志目录: {LOG_DIR}")
            if sys.platform == 'win32':
                os.startfile(LOG_DIR)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', LOG_DIR])
            else:
                subprocess.Popen(['xdg-open', LOG_DIR])
            return {"success": True}
        except Exception as e:
            logging.exception("打开日志目录失败")
            return {"success": False, "message": str(e)}

    def get_log_usage(self):
        """使用日志现在占多大：{'files': 个数, 'bytes': 总字节}。"""
        return log_usage()

    def clear_log(self):
        """
        清空使用日志（当前日志 + 轮转副本），清完继续写，不需要重启程序。
        安装/首次运行日志与报错日志不在这里的清理范围内，会原样保留。

        界面在用户确认之后才调这里，所以它不做二次确认，只把结果如实报出去：
        删了几个、释放多少、有没有删不掉的（被另一个实例占着那种）。
        """
        try:
            result = clear_log()
        except Exception as error:
            logging.exception("清理日志失败")
            return {"success": False, "message": str(error)}
        return result

    # ---------- 文件对话框 ----------
    def open_file_dialog(self, filetypes=("PDF files", "*.pdf")):
        try:
            result = self._pick_files(filetypes, multiple=False)
            if not result:
                return ""
            # 某些 pywebview 版本在单选时直接回裸字符串，不归一的话前端拿到的是
            # 一个字符一个元素的「列表」
            if isinstance(result, str):
                return result
            return str(result[0])
        except Exception:
            logging.exception("打开文件对话框失败")
            return ""

    def open_files_dialog(self, filetypes=("Image files", "*.png;*.jpg;*.jpeg;*.bmp")):
        """
        多选文件对话框，返回路径列表；用户取消或出错都返回空列表。

        图片转 PDF（F11）一次要挑十几张照片，用单选框点十几次没人受得了。
        pywebview 在 allow_multiple=True 时本来就返回 tuple，这里只是把
        「取消 -> None」统一成空列表，前端不必再区分 None 和 []。
        """
        try:
            result = self._pick_files(filetypes, multiple=True)
            if not result:
                return []
            if isinstance(result, str):
                return [result]
            return [str(p) for p in result]
        except Exception:
            logging.exception("多选文件对话框失败")
            return []

    def _pick_files(self, filetypes, multiple):
        window = _target_window()
        if window is None:
            return None
        return window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=multiple,
            file_types=_build_file_types(filetypes)
        )

    def save_file_dialog(self, default_name="output.pdf", filetypes=("PDF files", "*.pdf")):
        """
        pywebview 不同版本 / 不同平台对 SAVE_DIALOG 的返回值可能是：
          - str（正常情况）
          - tuple / list（某些版本）
          - None（用户取消）
        统一强制返回 str，调用方只需判断空字符串。
        """
        try:
            window = _target_window()
            if window is None:
                return ""
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=_build_file_types(filetypes)
            )
            logging.info(f"save_file_dialog 原始返回: {result!r} (type={type(result).__name__})")
            if not result:
                return ""
            if isinstance(result, (list, tuple)):
                if len(result) == 0:
                    return ""
                return str(result[0])
            return str(result)
        except Exception:
            logging.exception("保存文件对话框失败")
            return ""

    def select_directory(self):
        try:
            window = _target_window()
            if window is None:
                return ""
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
            if not result:
                return ""
            if isinstance(result, (list, tuple)):
                return str(result[0]) if result else ""
            return str(result)
        except Exception:
            logging.exception("选择目录失败")
            return ""

    # ---------- 外部链接 ----------
    def open_url(self, url):
        try:
            if not url or not str(url).lower().startswith(('http://', 'https://')):
                return False
            webbrowser.open(str(url))
            return True
        except Exception:
            logging.exception("打开外部链接失败")
            return False

    # ---------- 程序退出 ----------
    def quit_app(self, reason=""):
        """
        退出整个程序（首次向导里用户不同意使用条款时用）。

        destroy() 会让 webview.start() 返回、进程收尾，而 js_api 的返回值要等
        窗口还活着才能送回 JS，所以把销毁推到一小段延时之后，先让 Promise 落地。
        隐藏不必等：用户点「不同意」的那一帧就该什么都看不见了，而销毁要排队等
        WebView2 拆除，实测能让最后一颗窗口多挂在屏幕上近一秒。
        """
        logging.info("请求退出程序：%s", reason or "未说明")
        winclose.hide_all(list(webview.windows))

        def _close():
            winclose.destroy_later(list(webview.windows), why='退出程序')

        threading.Timer(0.3, _close).start()
        return {"success": True}
