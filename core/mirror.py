"""同一份文件在多个镜像上的取法：一次问完，再按内容挑最新的。

镜像之间不只是「谁快」的问题。jsDelivr 一系的镜像会缓存 `gh/<仓库>@main` 的内容，
GitHub 上改了正文，某个镜像可能还在发几天前的快照，而且它响应很快。
「按顺序试、第一个成功的就用」于是等于让最旧的快照赢：

  * 更新源读到过期那份，写着 v6.0.3 —— 本机 6.0.4 就认定「已是最新」，不提示更新；
  * README 读到过期那份，日志顶端还是 v6.0.3 —— 6.0.4 那一版的记录根本不在里面，
    用户看到的正是「GitHub 已经改了，程序却读不到更新日志」。

所以这里并行地问所有源，把每一份回答都交回调用方，由调用方按内容比较再挑：
JSON 比 version 字段，markdown 比更新日志顶端那条的版本号。
第一个回答之后只再等 `grace` 这么久就收工，免得一个卡死的镜像把界面按在原地；
总预算 `deadline` 是硬上限，超时就用已经拿到的那些。
"""

import logging
import threading
import time
import urllib.request

from core.constants import CURRENT_VERSION

# 第一个回答到达后，最多再等这么久收其余的镜像（它们大概率正在回，只是慢）
DEFAULT_GRACE_SECONDS = 1.2
# 单个请求的读超时与整轮总预算；沿用原来「9s 联网预算」的口径
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_DEADLINE_SECONDS = 9.0


def _fetch_one(url, label, timeout, box, index):
    """取一个源：成功就把正文写进 box[index]，失败只记日志（其余源照常跑）。"""
    try:
        request = urllib.request.Request(
            url, headers={'User-Agent': f'PDFToolkit/{CURRENT_VERSION}'}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode('utf-8', errors='replace').strip()
    except Exception as error:
        logging.warning("获取%s失败 %s: %s", label, url, error)
        return
    if not text:
        logging.warning("获取%s返回空正文 %s", label, url)
        return
    box[index] = text


def fetch_all(urls, label, timeout=DEFAULT_TIMEOUT_SECONDS,
              deadline=DEFAULT_DEADLINE_SECONDS, grace=DEFAULT_GRACE_SECONDS):
    """
    并行取回每个 url 的正文，返回 [(url, text), ...]，顺序沿用 urls。

    一个都没取到时返回空列表——调用方据此区分「联网失败」与「取到了但内容更旧」。
    预算用完时不再等剩下的镜像：那几个线程是 daemon，会自己因为超时而结束，
    不会拖住退出（打包后的进程必须能立刻退出，见 main.py）。
    """
    urls = list(urls or ())
    if not urls:
        return []

    box = {}
    per_request = min(timeout, deadline)
    threads = []
    for index, url in enumerate(urls):
        thread = threading.Thread(
            target=_fetch_one, args=(url, label, per_request, box, index),
            name=f'{label}-fetch-{index}', daemon=True)
        thread.start()
        threads.append(thread)

    started_at = time.monotonic()
    stop_at = started_at + deadline
    first_at = None
    while time.monotonic() < stop_at:
        if box and first_at is None:
            first_at = time.monotonic()
        if not any(thread.is_alive() for thread in threads):
            break
        if first_at is not None and time.monotonic() - first_at >= grace:
            break
        time.sleep(0.02)

    copies = [(url, box[index]) for index, url in enumerate(urls) if index in box]
    if not copies:
        logging.warning("获取%s：全部 %d 个源都没有结果", label, len(urls))
    elif len(copies) < len(urls):
        logging.info("获取%s：%d/%d 个源有结果", label, len(copies), len(urls))
    return copies


def parse_version(value):
    """
    把 "1.2.3" / "v1.2.3" / "1.2.3-beta" 解析成可比较的整数元组；解析不出来返回 None。

    多个源的回答要靠它比新旧，所以这里只认前导数字段，认不出来就当没有，
    宁可不采用那份回答，也不能把格式不明的东西当成「更新」。
    """
    text = str(value or '').strip().lstrip('vV')
    if not text:
        return None
    parts = text.split('.')
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        cleaned = []
        for part in parts:
            digits = ''.join(ch for ch in part if ch.isdigit())
            if not digits:
                break
            cleaned.append(int(digits))
        return tuple(cleaned) if cleaned else None
