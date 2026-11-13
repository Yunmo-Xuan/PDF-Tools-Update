"""清理隐藏信息（F20）：元数据、XMP 包、嵌入式附件、JavaScript 动作，外加一次重度重存。

单独立模块而不是塞进 metadata.py / compress.py：这几类信息在文件里的位置与删除手段完全不同
（/Info 字典、目录 /Metadata 流、/EmbeddedFiles 名树、散在目录与页面对象上的动作引用），
混在一处迟早出「压缩顺手把书签吃了」这种事故。方案 §4.7 要求逐类开关 + 只清引用不改对象本体，
就是为了把「到底动了什么」写清楚。
"""

import logging
import os
import time

from ._io import close_pdf, save_pdf_doc, unique_output_path
from core.i18n import t as _text

# 实测 pm_3000.pdf：1,040,071 -> 415,815 字节（-60.0%，9,004 个对象）。这个比例只对「文字页」
# 一类样本成立，扫描件不保证，所以界面和返回值只报实测字节、不许诺「小六成」。
# no_new_id 是给「同一份文件清两遍」用的：不加时两次只差 /ID 里那个随机串（字节数相同），
# 加上之后第二遍与第一遍逐字节一致，用户反复点清理不会把文件越改越花。
SAVE_KWARGS = {"garbage": 4, "clean": True, "deflate": True, "use_objstms": 1, "no_new_id": 1}

# JavaScript 动作常常直接内联写成 <</S/JavaScript/...>>（实测 MuPDF 生成的就是这样），
# 所以判据是「值里有没有 /JavaScript」，而不是「它是否指向一个 /S/JavaScript 对象」。
_JS_KEYS = ("OpenAction", "AA", "JS", "A")


_OFF = ("", "0", "false", "no", "off")


def _flag(opt, key, default):
    """复选框传来字符串时 bool("false") 也为真、传来 null 又等于没这项——两个坑都得防。"""
    value = opt.get(key, default)
    if value is None:
        return bool(default)
    return value.strip().lower() not in _OFF if isinstance(value, str) else bool(value)


def _as_opt(opt):
    """认字典，也认前端直接 stringify 过的 JSON；认不出来返回 None，由调用方说人话。"""
    if opt is None:
        return {}
    if isinstance(opt, str):
        import json
        try:
            opt = json.loads(opt)
        except ValueError:
            return None
    return opt if isinstance(opt, dict) else None


def _num(value):
    """把 "7 0 R" 这类引用值取成对象号。"""
    try:
        return int(str(value).split()[0])
    except (IndexError, ValueError):
        return 0


def _display_name(name):
    """
    附件名给界面看得懂的样子：实测库把 UTF-8 名字按 latin-1 解出来（文件里 /UF 是正确编码，
    读出来「工资表.xlsx」却是乱码），字节能还原成 UTF-8 就还原，还原不了就原样返回。
    只影响显示：删除仍用库给的原名，因为 embfile_del 认的是它自己那份解码结果。
    """
    try:
        return name.encode("latin-1").decode("utf-8") or name
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _js_refs(doc):
    """
    找出指向 JavaScript 动作的引用，返回 [(xref, 键路径), ...]。

    只清引用：置 null 后动作对象成了孤儿，交给保存时的 garbage=4 回收，内容流和页面字典
    其余部分一个字节都不重写。实测 pm_3000.pdf（9,004 个对象）全扫一遍约 0.18s，
    所以这条分支只在用户真勾了「清脚本」时才跑。认不出来的引用一律不动——宁可漏清不可误删。
    """
    refs = []
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=True) or ""
        except Exception:
            obj = ""  # 单个对象读不通不代表整份文件坏，跳过它继续扫
        if "/JavaScript" not in obj:
            continue
        for key in _JS_KEYS:
            kind, value = doc.xref_get_key(xref, key)
            if kind == "null":
                continue
            # 动作既可能是内联字典也可能是指向动作对象的引用，两种都得认出 /JavaScript；
            # /Names 底下的 JavaScript 子树单独处理，因为实测它和附件的 EmbeddedFiles
            # 挂在同一个字典里，清掉整个 /Names 会把用户的附件一起带走
            probe = doc.xref_object(_num(value), compressed=True) if kind == "xref" else value
            if "/JavaScript" in str(probe or ""):
                refs.append((xref, key))
        kind, value = doc.xref_get_key(xref, "Names")
        holder, path = (xref, "Names/JavaScript") if kind == "dict" else (_num(value), "JavaScript")
        if kind != "null" and holder and doc.xref_get_key(holder, path)[0] != "null":
            refs.append((holder, path))
    return refs


class CleanMixin:
    def clean_hidden_info(self, pdf_path, output_path="", opt=None):
        """
        清理隐藏信息（F20），另存新文件，绝不原地改用户原来那份。

        opt 的键（除重度重存外全部默认关，由界面逐项勾选）：
          clear_metadata(默认 True) / clear_xmp / clear_attachments / clear_scripts
        成品一定重新打开核对：勾选的类别真没了、页数与首页文字没变，对不上就报失败，
        不交「接口说成功、文件已经变形」的产物（§4.7 那条「可能影响阅读器书签」的风险）。
        """
        import pymupdf as fitz
        started = time.perf_counter()
        try:
            if not pdf_path or not os.path.isfile(pdf_path):
                return {"success": False, 'message': _text('api_pdf_missing')}
            opt = _as_opt(opt)
            if opt is None:
                return {"success": False, "message": _text('api_clean_opt_bad')}
            do_meta = _flag(opt, "clear_metadata", True)
            do_xmp = _flag(opt, "clear_xmp", False)
            do_att = _flag(opt, "clear_attachments", False)
            do_js = _flag(opt, "clear_scripts", False)
            before_bytes = os.path.getsize(pdf_path)
            dropped, xmp_bytes, refs = [], 0, []

            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    close_pdf(doc)
                    return {"success": False, "message": _text('api_clean_encrypted')}
                pages = doc.page_count
                if not pages:
                    # 实测 0 页文档走到 save 会抛 "cannot save with zero pages"，别让用户看英文
                    close_pdf(doc)
                    return {"success": False, "message": _text('api_clean_no_pages')}
                # 首页文字留个底：save 带 clean=True 会重写内容流的语法，清理信息不该动页面文字
                first_text = doc[0].get_text()
                removed = {"metadata": 0, "xmp": 0, "attachments": 0, "scripts": 0}
                if do_meta:
                    dropped = sorted(k for k, v in (doc.metadata or {}).items() if v and k != "format")
                    doc.set_metadata({})  # 实测清完 /Info 只剩 format（"PDF 1.7" 这类派生值）
                    removed["metadata"] = len(dropped)
                if do_xmp:
                    xmp_bytes = len(doc.get_xml_metadata() or "")
                    if xmp_bytes:
                        doc.xref_set_key(doc.pdf_catalog(), "Metadata", "null")
                    removed["xmp"] = 1 if xmp_bytes else 0
                raw_att = doc.embfile_names()
                att_names = [_display_name(n) for n in raw_att]  # 界面看这个清单决定要不要勾「删附件」
                if do_att:
                    for name in raw_att:
                        doc.embfile_del(name)  # 只删条目；字节要靠 garbage=4 才真从文件里消失
                    removed["attachments"] = len(raw_att)
                if do_js:
                    refs = _js_refs(doc)
                    for xref, key in refs:
                        doc.xref_set_key(xref, key, "null")
                    removed["scripts"] = len(refs)
            except Exception:
                close_pdf(doc)
                raise

            if not output_path:  # 没给保存位置就在源文件旁边起个不撞名的新文件
                base, ext = os.path.splitext(pdf_path)
                output_path = unique_output_path(base + "_已清理" + (ext or ".pdf"))
            save_pdf_doc(doc, output_path, **SAVE_KWARGS)  # 它负责关 doc，要用的 doc 数值得先拿完
            after_bytes = os.path.getsize(output_path)

            check = fitz.open(output_path)
            try:
                left_meta = len([k for k, v in (check.metadata or {}).items() if v and k != "format"])
                # 用户没勾「清脚本」就不扫：_js_refs 要遍历全部 xref，实测 pm_3000（9,004 个
                # 对象）单是这一趟就 0.18s，而它查的结果无论如何都不会被用来判定失败
                js_left = len(_js_refs(check)) if do_js else 0
                left = (("元数据", do_meta, left_meta),
                        ("XMP 字节", do_xmp, len(check.get_xml_metadata() or "")),
                        ("附件", do_att, len(check.embfile_names())),
                        ("JavaScript 引用", do_js, js_left))
                same_pages = check.page_count == pages
                same_text = check[0].get_text() == first_text
                toc_count = len(check.get_toc())
            finally:
                close_pdf(check)
            failed = ["%s 仍剩 %d" % (name, count) for name, wanted, count in left
                      if wanted and count]
            if not same_pages:
                failed.append("页数变了")
            if not same_text:
                failed.append("首页文字变了")

            logging.info(f"清理隐藏信息完成: {pdf_path} 共 {pages} 页 "
                         f"{before_bytes} -> {after_bytes} 字节 "
                         f"/ {time.perf_counter() - started:.2f}s -> {output_path} 各类清除量 {removed}")
            result = {"success": not failed, "file": output_path,
                      "before": before_bytes, "after": after_bytes, "pages": pages,
                      "removed": removed, "dropped_metadata": dropped, "xmp_bytes": xmp_bytes,
                      "attachments": att_names, "script_refs": len(refs), "toc": toc_count}
            if failed:
                result["message"] = _text('api_clean_verify_failed') + "、".join(failed)
            elif after_bytes >= before_bytes:
                # 本来就没多少信息可清的文件重存后字节数不变，这是正常情况，不能让它看着像失败
                result["note"] = ("这份文件本来就没多少可清理的信息，重存后体积没再变小"
                                  "（%d -> %d 字节），内容没有丢" % (before_bytes, after_bytes))
            return result
        except Exception as e:
            logging.exception("clean_hidden_info 失败")
            return {"success": False, "message": str(e)}
