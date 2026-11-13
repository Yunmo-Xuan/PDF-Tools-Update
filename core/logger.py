"""日志初始化与清理。

日志固定写到 Local AppData 下的 logs 目录（见 core.paths），
不再放系统临时目录——那里会被系统清理，出问题时日志往往已经没了。

同一个目录里分三份：
  * pdf_toolkit.log         使用日志：所有级别、所有模块，和以前一样；
  * pdf_toolkit-install.log 安装/首次运行：安装批次、装了什么、首次启动做了哪些事；
  * pdf_toolkit-errors.log  报错：ERROR 及以上，含未捕获异常。

使用日志本身就包含另外两类的内容；单独再落两份，是为了「清理日志」只清使用日志时，
安装现场和报错现场不会被一起删掉。清理只认 pdf_toolkit.log 及其轮转副本，
另外两份日志（以及安装批次标记）不在清理范围内。
"""

import logging
import logging.handlers
import os
import sys
import threading

from core.paths import ERROR_LOG_FILE, INSTALL_LOG_FILE, LOG_DIR, LOG_FILE

_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

# 清理和写入不能同时发生：js_api 的每一次调用都在新线程上跑，
# 连点两下「清理日志」会交错执行到把 handler 摘掉、却还没挂回去的中间状态。
_clear_lock = threading.Lock()

# 轮转上限：使用日志最常用，留得多一点；安装日志一次安装只写几行，小份就够。
_USAGE_BACKUP_COUNT = 3
_INSTALL_BACKUP_COUNT = 2
_ERROR_BACKUP_COUNT = 3

_INSTALL_LOGGER = 'install'


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _handler_path(handler):
    base = getattr(handler, 'baseFilename', '')
    return _norm(base) if base else ''


def _is_handler_for(handler, path):
    """
    这个 handler 写的是不是指定那份日志。

    不能直接拿 baseFilename 和路径比字符串：handler 存的是 abspath 之后的结果，
    而路径由环境变量拼出来，斜杠方向可能混着（LOCALAPPDATA 给的是 \\，
    临时目录之类的外部值可能带 /）。比字符串一比就错，错了的后果很难看：
    清理时认不出自己的 handler，既没把它摘下来（文件被自己占着删不掉），
    又新挂一个上去，日志从此写双份。
    """
    current = _handler_path(handler)
    return bool(current) and current == _norm(path)


def _has_handler(logger, path):
    return any(
        isinstance(h, logging.handlers.RotatingFileHandler) and _is_handler_for(h, path)
        for h in logger.handlers
    )


def _add_file_handler(logger, path, level, max_bytes, backup_count, formatter):
    if _has_handler(logger, path):
        return
    try:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except Exception:
        # 日志不可写不应该影响程序启动
        pass


def _init_logging():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_FORMAT)

    # 使用日志：收全部，清理时的唯一目标。
    _add_file_handler(logger, LOG_FILE, logging.DEBUG,
                      2 * 1024 * 1024, _USAGE_BACKUP_COUNT, formatter)
    # 报错日志：只收 ERROR 及以上，单独留存。
    _add_file_handler(logger, ERROR_LOG_FILE, logging.ERROR,
                      2 * 1024 * 1024, _ERROR_BACKUP_COUNT, formatter)

    # 安装/首次运行日志：走独立 logger，既进自己的文件，也向 root 传播进使用日志。
    install_logger = logging.getLogger(_INSTALL_LOGGER)
    install_logger.setLevel(logging.INFO)
    install_logger.propagate = True
    _add_file_handler(install_logger, INSTALL_LOG_FILE, logging.INFO,
                      512 * 1024, _INSTALL_BACKUP_COUNT, formatter)

    # 打包后没有控制台，只有源码运行时才加流输出。
    # 判据是「有没有一个纯流 handler」而不是「有没有流 handler」：
    # RotatingFileHandler 也是 StreamHandler 的子类，写宽一点就会漏判；
    # 这个函数在清理日志后还要再跑一次，漏判等于每清理一次就多一份控制台输出。
    if not getattr(sys, 'frozen', False) and not any(
            type(h) is logging.StreamHandler for h in logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return LOG_DIR, LOG_FILE


# 导入本模块就完成初始化：建目录、挂 handler。
# LOG_DIR / LOG_FILE 绑的就是 core.paths 里那一份路径，初始化只是让目录真存在，
# 不必再把这个元组接回来重新绑一次（值完全相同，重绑反而像有两个来源）。
_init_logging()


def log_install(message, *args, **kwargs):
    """写一条安装/首次运行日志；同一份记录也会进使用日志。"""
    logging.getLogger(_INSTALL_LOGGER).info(message, *args, **kwargs)


def _log_file_names(path):
    """指定日志对应的文件名（当前日志 + 它的轮转副本）。"""
    base = os.path.basename(path)
    try:
        return [n for n in os.listdir(LOG_DIR) if n == base or n.startswith(base + '.')]
    except OSError:
        return []


def _file_stats(path):
    total = 0
    count = 0
    for name in _log_file_names(path):
        try:
            total += os.path.getsize(os.path.join(LOG_DIR, name))
            count += 1
        except OSError:
            continue
    return {"files": count, "bytes": total}


def log_usage():
    """使用日志现在占多大：{'files': 个数, 'bytes': 总字节}，目录都没建时也返回 0。"""
    return _file_stats(LOG_FILE)


def protected_log_usage():
    """安装/首次运行日志与报错日志的占用；这两份不参与清理。"""
    install = _file_stats(INSTALL_LOG_FILE)
    error = _file_stats(ERROR_LOG_FILE)
    return {
        "install": install,
        "error": error,
        "files": install["files"] + error["files"],
        "bytes": install["bytes"] + error["bytes"],
    }


def _detach_file_handlers(path):
    """把指向指定日志的轮转 handler 摘下来（不摘就删不掉：文件句柄还在自己手上）。"""
    detached = []
    for logger in (logging.getLogger(), logging.getLogger(_INSTALL_LOGGER)):
        for handler in list(logger.handlers):
            if (isinstance(handler, logging.handlers.RotatingFileHandler)
                    and _is_handler_for(handler, path)):
                logger.removeHandler(handler)
                handler.close()
                detached.append(handler)
    return detached


def clear_log():
    """
    清空使用日志：删掉当前日志和它的轮转副本，然后重新挂一个 handler 继续写。

    只删 pdf_toolkit.log 这一族，安装/首次运行日志与报错日志原样保留——
    「一删全没」正是分三份日志要防的事。
    删不掉的文件（被别的实例占着）不硬来，原样留着并在结果里报出来，
    删完无论如何都要把 handler 挂回去 —— 只剩「日志没了」而写入方断了，
    等于把这次清理变成一个新的故障。
    """
    with _clear_lock:
        before = log_usage()
        _detach_file_handlers(LOG_FILE)

        removed, failed = [], []
        for name in _log_file_names(LOG_FILE):
            path = os.path.join(LOG_DIR, name)
            try:
                size = os.path.getsize(path)
                os.remove(path)
                removed.append({"name": name, "bytes": size})
            except OSError as error:
                failed.append({"name": name, "message": str(error)})

        # 不管删没删干净，都要有一份能写的日志
        _init_logging()

        freed = sum(item["bytes"] for item in removed)
        logging.info(f"清理日志：删除 {len(removed)} 个文件，释放 {freed} 字节"
                     + (f"，另有 {len(failed)} 个删不掉" if failed else '')
                     + '（安装/首次运行与报错日志保留）')
        return {
            "success": not failed,
            "removed": len(removed),
            "freed_bytes": freed,
            "failed": failed,
            "before_bytes": before["bytes"],
            "after_bytes": log_usage()["bytes"],
            # 给界面/测试一个明确的「另外两份还在」的凭证
            "protected": protected_log_usage(),
        }
