"""本机字体清单入口（⑨）：给界面「写进 PDF 的可编辑文字换字体」下拉框供数据。

清单本体在 core/fonts.py（三处来源去重、name 表解析、磁盘缓存都在那儿）；
这里只按后端返回契约包一层 dict。第一次调用会触发一次冷扫描（本机实测秒级），
之后走内存 memo + 指纹增量，界面上「刷新字体列表」用 refresh=True。
"""

import logging

from core.i18n import t as _text


class FontsMixin:
    def list_fonts(self, refresh=False):
        """本机可用字体族清单：{success, families, count}。

        families 每项含 family（界面显示名，中文优先）/ english_name / full_name /
        path / face_index / has_cjk / embedded_ok / styles[]——细节见 core/fonts.py。
        读清单炸了（注册表读不了、字体目录被安全软件锁住之类）不报错给前端一个
        异常字符串，而是 success=False + 一句能看懂的话：字体列表拿不到时界面
        照常跑，只是这一轮换不了字体。
        """
        try:
            from core import fonts as local_fonts
            families = local_fonts.list_families(bool(refresh))
            return {"success": True, "families": families, "count": len(families)}
        except Exception:
            logging.exception("list_fonts 失败")
            return {"success": False, "families": [], "count": 0,
                    "message": _text('api_font_list_failed')}
