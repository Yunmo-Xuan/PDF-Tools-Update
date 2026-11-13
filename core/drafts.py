"""页面管理草稿（未保存的编辑意图）的读写。

存的是「这份文件我打算怎么改」——页序、增量旋转、撤销栈、选中格，
绝不存缩略图：1200 页会话的图有几百 MB，落盘等于把内存问题搬到磁盘上。
恢复后按现有出图管线重新画一遍，代价是几秒，换来的是草稿永远和源文件对得上。

为什么不复用 api/_io.py 的 atomic_output：那是能力层的工具，而本模块在基座层，
基座反过来 import 能力层会绕成环。这里只有一份六行的同名写法，
两份的语义（先写 .tmp 再一次改名）完全一致。
"""

import hashlib
import json
import logging
import os
import re
import time

from core.paths import DRAFT_DIR

# 一份源文件对应一份草稿，键就是下面这八个字符，所以文件名必须是纯十六进制。
# 校验它而不是直接拼接：前端传回来的键会被当成文件名用，不校验就等于目录穿越。
_KEY_RE = re.compile(r'^[0-9a-f]{8,40}$')

MAX_DRAFTS = 10
MAX_BYTES = 8 * 1024 * 1024

POLICY_RECENT = 'keep_recent'
POLICY_CURRENT = 'keep_current'
POLICY_OFF = 'off'
POLICIES = (POLICY_RECENT, POLICY_CURRENT, POLICY_OFF)


def normalize_source(pdf_path):
    """把源文件路径归一化成用于哈希的形式：大小写与分隔符不同不该算两份草稿。"""
    try:
        return os.path.normcase(os.path.abspath(str(pdf_path))).replace('\\', '/')
    except Exception:
        return str(pdf_path)


def draft_key(pdf_path, total_pages, source_mtime):
    """
    草稿的定位键 = 归一化路径 + 页数 + mtime 的哈希。

    带上页数与 mtime 是有意的：用户在两次打开之间换了文件（同名不同内容）
    时键就变了，旧草稿不会被错套到新内容上。恢复时还要再核一遍这三项，
    哈希只能保证「不会自动认错」，不能保证「找回来的时候还对得上」。
    """
    stamp = '' if source_mtime is None else '%.6f' % float(source_mtime)
    raw = f'{normalize_source(pdf_path)}|{int(total_pages)}|{stamp}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]


def valid_key(key):
    return bool(_KEY_RE.match(str(key or '')))


def _path_of(key):
    if not valid_key(key):
        raise ValueError(f'草稿键不合法: {key!r}')
    return os.path.join(DRAFT_DIR, f'{key}.json')


def _atomic_write(path, text):
    """先写同目录的 .tmp 再一次改名替换：磁盘上任何时刻都只有完整的旧版或新版。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _iter_drafts():
    """读出目录里所有能解析的草稿，返回 [(key, path, data), ...]，坏文件跳过并记日志。"""
    result = []
    try:
        names = os.listdir(DRAFT_DIR)
    except OSError:
        return result
    for name in sorted(names):
        if not name.endswith('.json'):
            continue
        key = name[:-5]
        if not valid_key(key):
            continue
        path = os.path.join(DRAFT_DIR, name)
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception:
            logging.exception('草稿文件读不出来，忽略: %s', path)
            continue
        if isinstance(data, dict):
            result.append((key, path, data))
    return result


def list_drafts():
    """列出所有草稿的摘要（不含 slots/history），按最后保存时间倒序。"""
    summary = []
    for key, path, data in _iter_drafts():
        summary.append({
            'key': key,
            'source': data.get('source') or '',
            'total_pages': data.get('total_pages') or 0,
            'source_mtime': data.get('source_mtime'),
            'saved_at': data.get('saved_at') or 0,
            'slot_count': len(data.get('slots') or []),
            'dirty': bool(data.get('dirty', True)),
            'size': os.path.getsize(path) if os.path.exists(path) else 0,
        })
    summary.sort(key=lambda item: item['saved_at'] or 0, reverse=True)
    return summary


def load(key):
    """按键读回整份草稿，没有或键不合法返回 {}。"""
    if not valid_key(key):
        return {}
    try:
        with open(_path_of(key), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception('草稿读取失败: %s', key)
        return {}


def save(payload, policy=POLICY_RECENT):
    """
    写一份草稿，返回 {'success': bool, 'key': ..., 'reason': ...}。

    payload 由前端组：slots / history / selected / dirty 加源文件信息。
    这里只补键、版本、时间戳，并保证不落图片字段——多存一份就多一份脏数据。
    """
    if policy == POLICY_OFF:
        return {'success': False, 'reason': 'draft_off'}
    if not isinstance(payload, dict):
        return {'success': False, 'reason': 'bad_payload'}

    source = str(payload.get('source') or '')
    try:
        total_pages = int(payload.get('total_pages') or 0)
    except (TypeError, ValueError):
        return {'success': False, 'reason': 'bad_page_count'}
    if not source or total_pages <= 0:
        return {'success': False, 'reason': 'no_source'}

    try:
        mtime = float(payload.get('source_mtime') or 0)
    except (TypeError, ValueError):
        mtime = 0.0

    key = draft_key(source, total_pages, mtime)
    record = {
        'key': key,
        'version': str(payload.get('version') or ''),
        'source': source,
        'total_pages': total_pages,
        'source_mtime': mtime,
        'slots': payload.get('slots') or [],
        'history': payload.get('history') or [],
        'selected': payload.get('selected') or [],
        'dirty': bool(payload.get('dirty', True)),
        'saved_at': int(time.time()),
    }

    text = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
    if len(text.encode('utf-8')) > MAX_BYTES:
        # 正常量级的 3000 页草稿实测不到 100 KB；真超了说明前端把不该存的东西塞进来了
        logging.error('草稿超过 %d 字节上限，拒绝写入: %s (%d 字节)', MAX_BYTES, key, len(text))
        return {'success': False, 'reason': 'too_large', 'key': key}

    try:
        os.makedirs(DRAFT_DIR, exist_ok=True)
        if policy == POLICY_CURRENT:
            for other_key, other_path, _ in _iter_drafts():
                if other_key != key:
                    try:
                        os.remove(other_path)
                    except OSError:
                        logging.exception('清理旧草稿失败: %s', other_path)
        _atomic_write(_path_of(key), text)
        _evict(keep=key)
    except Exception as error:
        logging.exception('草稿写入失败: %s', key)
        return {'success': False, 'reason': 'write_failed', 'key': key, 'message': str(error)}

    logging.info('草稿已保存: %s（%d 格 / 源文档 %d 页）', key, len(record['slots']), total_pages)
    return {'success': True, 'key': key, 'saved_at': record['saved_at']}


def clear(key):
    """删除一份草稿（保存成功后必须调，此时页码已按新文件重编号，旧草稿是脏数据）。"""
    if not valid_key(key):
        return {'success': False, 'reason': 'bad_key'}
    try:
        os.remove(_path_of(key))
    except FileNotFoundError:
        return {'success': True, 'key': key, 'removed': False}
    except Exception as error:
        logging.exception('草稿删除失败: %s', key)
        return {'success': False, 'reason': 'remove_failed', 'message': str(error)}
    logging.info('草稿已删除: %s', key)
    return {'success': True, 'key': key, 'removed': True}


def _evict(keep=None):
    """最多留 MAX_DRAFTS 份，超出按 saved_at 淘汰最旧的；keep 那份是刚写的，绝不动。"""
    drafts = [item for item in _iter_drafts() if item[0] != keep]
    if len(drafts) < MAX_DRAFTS:
        return
    drafts.sort(key=lambda item: (item[2].get('saved_at') or 0))
    for key, path, _ in drafts[:len(drafts) + (0 if keep is None else 1) - MAX_DRAFTS]:
        try:
            os.remove(path)
            logging.info('草稿超出 %d 份上限，淘汰最旧的: %s', MAX_DRAFTS, key)
        except OSError:
            logging.exception('淘汰草稿失败: %s', path)


def check_restorable(draft, current_pages=None):
    """
    判断这份草稿能不能安全套回当前文件，返回 (可以?, 原因, 提示给用户的话)。

    宁可什么都不恢复，也不许把改动套到另一个文件上：源文件不见了、页数变了、
    mtime 变了，都说明用户打开的可能已经不是当初那份。
    current_pages 由调用方读出来传进来（本模块在基座层，不该为了数页数去 import PyMuPDF）。
    """
    if not isinstance(draft, dict) or not draft.get('source'):
        return False, 'no_draft', '没有找到可恢复的草稿'
    source = str(draft['source'])
    if not os.path.isfile(source):
        return False, 'missing_source', f'原文件已移动或不存在：{os.path.basename(source)}'
    try:
        mtime = os.path.getmtime(source)
    except OSError:
        return False, 'unreadable', '原文件当前读不到，已放弃恢复'
    try:
        recorded = float(draft.get('source_mtime') or 0)
    except (TypeError, ValueError):
        recorded = 0.0
    if recorded and abs(mtime - recorded) > 1.0:
        return False, 'changed', '原文件在这之后被改动过，草稿里的改动无法安全套用'
    try:
        recorded_pages = int(draft.get('total_pages') or 0)
    except (TypeError, ValueError):
        recorded_pages = 0
    if current_pages is not None and recorded_pages and int(current_pages) != recorded_pages:
        return False, 'pages_changed', (
            f'原文件现在是 {int(current_pages)} 页，草稿记的是 {recorded_pages} 页，不能直接套用')
    return True, 'ok', ''
