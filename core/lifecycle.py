"""安装批次与首次运行记录。

安装器每次装完会在 exe 旁边写一个 install.stamp（安装时间），程序第一次在新批次上
启动时，把「装的是什么、什么时候装的、装到哪儿、首次启动都做了哪些事」记进
install 日志。同一批次只记一次：标记落在日志目录下的 .install_seen，
它不在「清理日志」的清理范围里。

源码/便携版没有 install.stamp，按「版本号」算一批：版本一变记一次。
"""

import os
import platform
import sys
import time

from core.constants import APP_NAME, CURRENT_VERSION
from core.logger import log_install
from core.paths import CONFIG_FILE, INSTALL_STAMP_FILE, LOG_DIR, PROGRAM_DIR

_MARKER_FILE = os.path.join(LOG_DIR, '.install_seen')
_state = {'new_install': False, 'install_stamp': ''}


def _read_text(path, limit=200):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read().strip()[:limit]
    except OSError:
        return ''


def _write_marker(value):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_MARKER_FILE, 'w', encoding='utf-8') as handle:
            handle.write(value)
    except OSError:
        # 标记写不进去只会导致下次再记一遍安装信息，不该影响启动
        pass


def _file_info(path):
    try:
        info = os.stat(path)
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info.st_mtime))
        return f'{path}（{info.st_size} 字节，修改于 {stamp}）'
    except OSError:
        return f'{path}（不存在）'


def _stamp_mtime():
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S',
                             time.localtime(os.stat(INSTALL_STAMP_FILE).st_mtime))
    except OSError:
        return ''


def begin_session():
    """在日志初始化之后、任何窗口创建之前调用一次。

    返回这次启动是否属于「装完第一次启动」（源码/便携版 = 新版本第一次启动）。
    """
    stamp = _read_text(INSTALL_STAMP_FILE)
    installed = bool(stamp)
    marker = '|'.join(('installed' if installed else 'source',
                       CURRENT_VERSION, stamp))
    new_install = _read_text(_MARKER_FILE, limit=400) != marker
    _state.update(new_install=new_install, install_stamp=stamp)

    if not new_install:
        return False

    log_install('=' * 60)
    log_install('安装/首次运行记录：%s v%s', APP_NAME, CURRENT_VERSION)
    log_install('运行模式: %s', '安装版' if installed else '源码/便携')
    log_install('安装批次: %s', stamp or '无 install.stamp（源码/便携）')
    if installed:
        log_install('安装时间: %s', stamp)
        stamp_mtime = _stamp_mtime()
        if stamp_mtime:
            log_install('install.stamp 修改时间: %s', stamp_mtime)
    log_install('程序目录: %s', PROGRAM_DIR)
    target = sys.executable if getattr(sys, 'frozen', False) else os.path.join(PROGRAM_DIR, 'main.py')
    log_install('程序文件: %s', _file_info(target))
    log_install('配置文件: %s（%s）', CONFIG_FILE,
                '已存在' if os.path.exists(CONFIG_FILE) else '尚未创建')
    log_install('日志目录: %s', LOG_DIR)
    log_install('操作系统: %s', platform.platform())
    log_install('Python: %s', sys.version.split()[0])
    _write_marker(marker)
    return True


def first_run_active():
    return bool(_state['new_install'])


def log_first_run(message, *args, **kwargs):
    """只有「装完第一次启动」这一轮才写；其余启动不往安装日志里灌日常记录。"""
    if _state['new_install']:
        log_install(message, *args, **kwargs)
