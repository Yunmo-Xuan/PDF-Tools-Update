"""页面管理落盘与草稿。

界面把旋转、删除、提取、调序都暂存在内存里，攒够了由用户点一次「保存」，
再由这里一次性写出去——所以本模块写成品 PDF 的入口只有 apply_page_edits 一个。

早先每个操作各自立刻生成 xxx_rotated.pdf / xxx_deleted.pdf：想「转两页再删一页」
就得存两次文件，而且一次点错就把改动落在了盘上，退不回来。

草稿（save/load/list/clear_page_draft）写的是另一个东西：只记「打算怎么改」的
JSON，落在用户数据目录里，永远不是成品文件，两者不许互相代劳。
"""

import logging
import os

from core.i18n import t as _text

from ._io import atomic_output
from core.constants import CURRENT_VERSION


def _rotation_of(value):
    """
    把暂存的旋转角度收成 0/90/180/270；不是 90 的倍数就返回 None 表示不认。

    PDF 的 /Rotate 本身只允许 90 的倍数，非 90 倍数的请求多半是前端算错了，
    静默取整会让用户在成品里看到和自己点过的不一样的方向。
    """
    try:
        angle = int(value)
    except (TypeError, ValueError):
        return None
    if angle % 90 != 0:
        return None
    return angle % 360


def _read_slots(slots, total):
    """
    校验前端暂存的页面槽位，返回 (页码列表, 旋转列表, 错误信息)。

    slots 是成品页面的逐页描述：[{"page": 1-based 原页码, "rotation": 增量角度}]，
    顺序即成品顺序，重复即复制页，缺席即删除。任一项不合法就整批拒绝——
    只处理其中合法的那部分并回报成功，等于静默丢页。
    """
    if not isinstance(slots, (list, tuple)):
        return None, None, "页面清单格式不正确"
    if not slots:
        return None, None, "没有任何页面要保存（至少要保留一页）"

    pages = []
    rotations = []
    for pos, slot in enumerate(slots, start=1):
        raw_page = slot.get("page") if isinstance(slot, dict) else None
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            return None, None, f"第 {pos} 个页面的页码不合法（文档共 {total} 页），已取消保存"
        if not 1 <= page <= total:
            return None, None, f"第 {pos} 个页面的页码 {page} 超出范围（文档共 {total} 页），已取消保存"
        angle = _rotation_of(slot.get("rotation", 0) if isinstance(slot, dict) else 0)
        if angle is None:
            return None, None, f"第 {pos} 个页面的旋转角度不合法（必须是 90 的倍数），已取消保存"
        pages.append(page)
        rotations.append(angle)
    return pages, rotations, None


class PagesMixin:
    """页面管理的落盘与草稿。

    apply_page_edits 是唯一写成品 PDF 的入口；下面四个方法只碰
    %LOCALAPPDATA%\\PDFToolkit\\drafts\\ 下的草稿 JSON，两者不许互相代劳。
    """

    def apply_page_edits(self, pdf_path, output_path, slots):
        """
        按暂存的槽位写出新文件，一次成型。

        旋转写成「原页面的 /Rotate + 本次增量」：扫描页、横版页本身可能就带着角度，
        直接赋值等于把人家原有的方向改掉。
        """
        from pypdf import PdfReader, PdfWriter
        try:
            if not output_path or not str(output_path).strip():
                return {"success": False, "message": _text('api_pages_no_save_path')}
            output_path = str(output_path)
            if not output_path.lower().endswith(".pdf"):
                return {"success": False, "message": _text('api_pages_must_end_pdf')}

            try:
                reader = PdfReader(pdf_path)
            except Exception as error:
                logging.exception("apply_page_edits 读取源文件失败")
                return {"success": False, "message": _text('api_pages_read_failed') % error}
            if reader.is_encrypted:
                return {"success": False, "message": _text('api_pages_encrypted')}
            total = len(reader.pages)

            pages, rotations, bad = _read_slots(slots, total)
            if bad:
                return {"success": False, "message": bad}

            writer = PdfWriter()
            for page, angle in zip(pages, rotations):
                writer.add_page(reader.pages[page - 1])
                if angle:
                    # add_page 是逐个拷贝，最后一页一定是刚刚加进来的这一页
                    writer.pages[-1].rotate(angle)

            with atomic_output(output_path) as f:
                writer.write(f)

            logging.info(
                f"页面改动已保存: {pdf_path} -> {output_path} "
                f"({len(pages)} 页，源文档 {total} 页，"
                f"旋转 {sum(1 for a in rotations if a)} 页)"
            )
            return {
                "success": True,
                "file": output_path,
                "pages": len(pages),
                "total": total,
            }
        except Exception as e:
            logging.exception("apply_page_edits 失败")
            return {"success": False, "message": str(e)}

    # ---------- 草稿（F08）：改动还没保存就崩了 / 关了，下次能接上 ----------
    def _draft_policy(self):
        """读配置里的草稿策略；认不出来一律按最保守的「正常保留」处理。"""
        try:
            from core.drafts import POLICY_CURRENT, POLICY_OFF, POLICY_RECENT
            policy = str(self.load_config().get("draft_policy") or "")
            if policy in (POLICY_OFF, POLICY_CURRENT, POLICY_RECENT):
                return policy
        except Exception:
            logging.exception("读取草稿策略失败，按默认策略处理")
        return "keep_recent"

    def save_page_draft(self, pdf_path, payload):
        """
        把当前暂存的编辑意图落盘。前端在改动后防抖 800ms、以及关窗前各调一次。

        payload 里的 source / total_pages / source_mtime 由前端带上，
        但键要用「后端此刻看到的这份文件」来算：不然前端拿旧状态传回来的
        页数会算出另一个键，同一份文件就攒出两堆互不相干的草稿。
        """
        from core import drafts
        try:
            if not isinstance(payload, dict):
                return {"success": False, "message": _text('api_pages_draft_bad')}
            total = self.get_pdf_page_count(pdf_path)
            if total <= 0:
                return {"success": False, "message": _text('api_pages_draft_nopages')}
            try:
                mtime = os.path.getmtime(pdf_path)
            except OSError:
                mtime = 0.0
            record = dict(payload)
            record["source"] = os.path.abspath(str(pdf_path))
            record["total_pages"] = total
            record["source_mtime"] = mtime
            record["version"] = CURRENT_VERSION
            result = drafts.save(record, policy=self._draft_policy())
            if not result.get("success"):
                logging.info("草稿未保存: %s -> %s", pdf_path, result.get("reason"))
            return result
        except Exception as e:
            logging.exception("save_page_draft 失败")
            return {"success": False, "message": str(e)}

    def load_page_draft(self, key):
        """
        按草稿键读回内容，并当场判断它还能不能安全套到源文件上。

        恢复校验放在后端而不是前端：只有这里能真的打开源文件数一遍页数，
        让前端自己比就是把「改错了文件」的风险交给一段 JS 去兜。
        """
        from core import drafts
        try:
            draft = drafts.load(key)
            if not draft:
                return {"success": False, "found": False, "restorable": False,
                        "reason": "no_draft", "message": _text('api_pages_draft_missing')}
            source = str(draft.get("source") or "")
            current_pages = self.get_pdf_page_count(source) if source else -1
            ok, reason, message = drafts.check_restorable(draft, current_pages)
            return {
                "success": True,
                "found": True,
                "restorable": ok,
                "reason": reason,
                "message": message,
                "draft": draft,
                "key": draft.get("key") or key,
                "current_pages": current_pages,
                "file_name": os.path.basename(source),
            }
        except Exception as e:
            logging.exception("load_page_draft 失败")
            return {"success": False, "found": False, "restorable": False,
                    "reason": "error", "message": str(e)}

    def list_page_drafts(self):
        """列出所有草稿摘要，供「恢复上次未保存的改动」那里挑。"""
        from core import drafts
        try:
            return drafts.list_drafts()
        except Exception:
            logging.exception("list_page_drafts 失败")
            return []

    def clear_page_draft(self, key):
        """丢掉一份草稿：保存成功后、或用户在恢复提示里点「丢弃」时调。"""
        from core import drafts
        try:
            return drafts.clear(key)
        except Exception as e:
            logging.exception("clear_page_draft 失败")
            return {"success": False, "message": str(e)}
