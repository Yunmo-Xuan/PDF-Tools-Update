"""长任务的「逐页进度 + 可取消」。

为什么要单独一层：这套程序里凡是慢的活（整本 OCR、可搜索 PDF、写入书签、扫描质检、
拍照增强）都是「一页一页循环」，用户点下去就只能干等，中途停不掉——一旦挑错文件
或者页数比预想的多，只能等它跑完或者强杀程序。这一层给循环一个「每页之间问一句」的
地方，给界面一个「现在到第几页 / 要不要停」的入口。

一次只跑一个大任务是这套程序的实情：所有耗时操作都是用户一格一格点的，
OCR 那边还有 core.ocr 的 _run_lock 串行化，所以一个槽位就够，不做队列。

取消的语义是「把已经做完的交出来」而不是「什么都没发生」：循环里 should_stop()
为真就 break，调用方带着 cancelled / pages_done 返回，界面据此说明停在哪一页、
已经识别了多少页，而不是把成果丢掉。
"""

import threading

_LOCK = threading.Lock()
_CANCEL = threading.Event()
_JOB = {'name': '', 'total': 0, 'done': 0, 'kind': 'page'}

# 进度条上「第 3 / 10 页」的单位。只有这两种，加一种就要给十本字典各加一句文案，
# 所以真来了新单位先看能不能归到这两类里。
KINDS = ('page', 'file')


def start(name, total, kind='page'):
    """登记一个任务；同时清掉上一次的取消标记，别让上一次按下的「取消」影响这一次。

    kind 只决定界面怎么念这个数（「页」还是「张」），不参与任何计算；认不出来按页算，
    省得界面因为一个脏参数什么都不显示。
    """
    with _LOCK:
        _CANCEL.clear()
        _JOB['name'] = str(name or '')
        _JOB['total'] = max(0, int(total or 0))
        _JOB['done'] = 0
        _JOB['kind'] = kind if kind in KINDS else 'page'
    return _JOB['total']


def should_stop():
    """循环里每页查一次；没任务在跑时返回 False（探针和后端自测都能直接调到底）。"""
    return _CANCEL.is_set()


def advance(count=1):
    with _LOCK:
        _JOB['done'] = max(0, _JOB['done'] + int(count or 0))


def set_progress(done):
    """按绝对值设进度（调用方自己算好第几页时用这个）。"""
    with _LOCK:
        _JOB['done'] = max(0, int(done or 0))


def cancel():
    """界面点「取消」：只置标记，真正停下来要等循环里下一次查到它。"""
    with _LOCK:
        active = bool(_JOB['total']) and _JOB['done'] < _JOB['total']
    _CANCEL.set()
    return active


def finish():
    """任务收尾：把槽位清空，界面上的进度条与取消按钮跟着消失。"""
    with _LOCK:
        _CANCEL.clear()
        _JOB.update({'name': '', 'total': 0, 'done': 0, 'kind': 'page'})


def status():
    """给界面的那一份状态；纯类型，能直接 JSON 回去。"""
    with _LOCK:
        total = _JOB['total']
        done = _JOB['done']
        return {
            'active': total > 0,
            'name': _JOB['name'],
            'total': total,
            'done': done,
            'kind': _JOB['kind'],
            'percent': int(round(done * 100.0 / total)) if total else 0,
            'cancelling': bool(_CANCEL.is_set() and total and done < total),
        }
