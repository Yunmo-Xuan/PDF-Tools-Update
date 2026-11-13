import logging
import os
from core.i18n import t as _text


def _key(path):
    """
    最近文件的比较键。

    Windows 路径大小写不敏感、分隔符也可能写法不同，用原始字符串比较会把
    同一个文件当成两个，10 条上限被重复条目占掉，删除也删不掉。
    """
    return os.path.normcase(os.path.abspath(path))


class RecentMixin:
    MAX_RECENT = 10

    def add_recent_file(self, filepath):
        try:
            if not filepath or not os.path.isfile(filepath):
                return {"success": False, "message": _text('api_file_not_exist')}
            filepath = os.path.abspath(filepath)

            config = self.load_config()
            target = _key(filepath)
            recent = [p for p in config.get("recent_files", [])
                      if not (isinstance(p, str) and _key(p) == target)]
            recent.insert(0, filepath)
            recent = recent[:self.MAX_RECENT]
            config["recent_files"] = recent
            # 写盘失败必须如实上报，否则界面会显示"已记录"但下次什么都没有
            if not self.save_config(config):
                return {"success": False, 'message': _text('api_config_write_failed')}
            return {"success": True, "recent_files": recent}
        except Exception as e:
            logging.exception("add_recent_file 失败")
            return {"success": False, "message": str(e)}

    def get_recent_files(self):
        """
        只返回磁盘上仍然存在的文件。

        旧实现直接回吐配置里的路径，文件被移动或删除后，首页会出现
        点了没反应的死条目；这里顺手把它们从配置里清掉。
        """
        try:
            config = self.load_config()
            recent = config.get("recent_files", []) or []
            alive = [p for p in recent if isinstance(p, str) and os.path.isfile(p)]
            if len(alive) != len(recent):
                config["recent_files"] = alive
                self.save_config(config)
            return alive
        except Exception:
            logging.exception("get_recent_files 失败")
            return []

    def remove_recent_file(self, filepath):
        try:
            config = self.load_config()
            recent = list(config.get("recent_files", []) or [])
            target = _key(filepath)
            remaining = [p for p in recent if not (isinstance(p, str) and _key(p) == target)]
            if len(remaining) == len(recent):
                # 一条都没匹配上却返回成功，界面就会显示"已删除"但条目还在
                logging.info(f"最近文件未找到可删除项: {filepath}")
            config["recent_files"] = remaining
            if not self.save_config(config):
                return {"success": False, 'message': _text('api_config_write_failed')}
            return {"success": True, "recent_files": remaining}
        except Exception as e:
            logging.exception("remove_recent_file 失败")
            return {"success": False, "message": str(e)}

    def clear_recent_files(self):
        try:
            config = self.load_config()
            config["recent_files"] = []
            if not self.save_config(config):
                return {"success": False, 'message': _text('api_config_write_failed')}
            return {"success": True}
        except Exception as e:
            logging.exception("clear_recent_files 失败")
            return {"success": False, "message": str(e)}
