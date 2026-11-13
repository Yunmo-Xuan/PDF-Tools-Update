"""版本更新检查。

两个入口，语义不同，不要混用：
  * check_update_silent —— 启动时自动调用。只有「确实发现更新」才返回内容，
    已是最新、本地版本更新、断网、超时、解析失败一律静默返回不可用；
  * check_update_manual —— 用户主动点击。先确认能连上更新源，再比较版本号，
    明确给出「有新版本 / 已是最新 / 正在使用测试版本」三种结论之一。
"""

import json
import logging

from core.constants import CURRENT_VERSION, UPDATE_URLS
from core.i18n import t as _text
from core.mirror import fetch_all, parse_version


class UpdaterMixin:
    def _fetch_remote_version(self, timeout=6, deadline=9.0, grace=1.2):
        """
        并行问完所有更新源，返回 (version, download_url, source_url)——取版本号最大的那份回答。

        不按「谁先答就用谁」：镜像的缓存会滞后，先答回来的很可能就是几天前的快照，
        那样本机 6.0.4 会读到「最新还是 6.0.3」，既不提示更新、也读不到自己这一版的日志。
        每个源的回答都记进行日志，出问题时能一眼看出是哪份快照滞后。
        全部失败时抛异常（调用方按「连不上更新源」处理）。
        """
        copies = fetch_all(UPDATE_URLS, '更新源', timeout=timeout,
                           deadline=deadline, grace=grace)
        if not copies:
            raise RuntimeError('没有可用的更新源')

        best = None
        for url, text in copies:
            try:
                payload = json.loads(text)
            except Exception as error:
                logging.warning("更新源返回的不是 JSON %s: %s", url, error)
                continue
            version = str(payload.get('version', '')).lstrip('vV')
            parsed = parse_version(version)
            if parsed is None:
                logging.warning("更新源未返回可用的 version 字段 %s", url)
                continue
            logging.info("更新源回答 %s -> v%s", url, version)
            if best is None or parsed > best[0]:
                best = (parsed, version, payload.get('download_url', ''), url)

        if best is None:
            raise RuntimeError('更新源返回的内容无法解析')
        logging.info("更新源采用 %s -> v%s（%d 个源回答中的最新版本）",
                     best[3], best[1], len(copies))
        return best[1], best[2], best[3]

    def check_update_silent(self):
        """
        启动时自动检查。只有远端版本更高时才提示，
        其余情况（含未联网）全部静默，不打扰用户。
        """
        try:
            latest, url, _ = self._fetch_remote_version()
        except Exception:
            logging.info("启动检查更新：无法连接更新源，静默跳过")
            return {"update_available": False}

        remote = parse_version(latest)
        local = parse_version(CURRENT_VERSION)
        if remote and local and remote > local:
            logging.info(f"发现新版本 v{latest}（当前 v{CURRENT_VERSION}）")
            return {"update_available": True, "version": latest, "url": url}

        return {"update_available": False}

    def check_update_manual(self):
        """
        用户主动检查：先确认网络，再按版本号给出明确结论。
        status 取值：newer / latest / ahead / offline / error
        """
        try:
            latest, url, _ = self._fetch_remote_version()
        except Exception:
            logging.exception("手动检查更新失败")
            return {
                "status": "offline",
                "message": _text('api_update_offline'),
            }

        remote = parse_version(latest)
        local = parse_version(CURRENT_VERSION)
        if not remote or not local:
            return {"status": "error", "message": _text('api_update_version_bad')}

        if remote > local:
            return {
                "status": "newer",
                "version": latest,
                "url": url,
                "message": _text('api_update_available') % (latest, CURRENT_VERSION),
            }
        if remote == local:
            return {
                "status": "latest",
                "version": latest,
                "message": _text('api_update_latest') % CURRENT_VERSION,
            }
        return {
            "status": "ahead",
            "version": latest,
            "message": _text('api_update_beta') % (CURRENT_VERSION, latest),
        }

    # 兼容旧调用名
    def check_for_update(self):
        return self.check_update_silent()
