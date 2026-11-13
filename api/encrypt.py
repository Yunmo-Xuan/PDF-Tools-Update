"""加解密（F06）与权限限制（F21）。

三件事用的是两套引擎、也是两种语义，放一个模块是因为它们动的是同一个东西：
文件的加密字典（/Encrypt）。加密决定「打不打得开」，权限决定「打开之后能干什么」，
而 PDF 规范里权限位只在文档带加密字典时才有效——所以「只禁复制、不设口令」这件事
看着简单，落到两台引擎上是三个不同的坑，set_pdf_permissions 的 docstring 逐条记着。
"""

import logging
import os
import re

from ._io import atomic_output, close_pdf, save_pdf_doc, unique_output_path
from core.i18n import t as _text


def _copy_metadata(reader, writer):
    """
    把原文档的元数据带进新文档。

    加密/解密都是重建一个 PDF，不带元数据的话标题、作者、主题、关键词会在这一步
    无声消失 —— 用户只是加了个密，文档信息却全没了。
    """
    try:
        info = getattr(reader, 'metadata', None)
        if info:
            writer.add_metadata(dict(info))
    except Exception:
        # 元数据读不出来（文档结构异常）不该让整个操作失败
        logging.exception("复制元数据失败，继续输出")


# 权限位：规范名 -> (/P 里的位值, 界面中文名)。两台引擎的位值完全一致，
# 只有 pypdf 的成员名不一样（见 set_pdf_permissions 里那段陷阱注释）。
_PERMS = (
    ("print", 4, "打印"),
    ("modify", 8, "修改页面"),
    ("copy", 16, "复制内容"),
    ("annotate", 32, "批注"),
    ("forms", 256, "填写表单"),
    ("accessibility", 512, "无障碍朗读"),
    ("assemble", 1024, "页面拼装"),
    ("print_hq", 2048, "高分辨率打印"),
)
_PERM_BITS = {name: bit for name, bit, _ in _PERMS}
_PERM_HELP = "、".join("%s(%s)" % (name, cn) for name, _, cn in _PERMS)
_PERM_ALL = 4 | 8 | 16 | 32 | 256 | 512 | 1024 | 2048

# 中文说法 -> 规范名。界面上点的是中文，测试和手工调用也可能直接给裸位整数。
_PERM_CN = {
    "打印": "print", "修改": "modify", "编辑": "modify", "复制": "copy",
    "提取": "copy", "摘录": "copy", "批注": "annotate", "注释": "annotate",
    "填写表单": "forms", "填表": "forms", "表单": "forms",
    "无障碍朗读": "accessibility", "无障碍": "accessibility", "朗读": "accessibility",
    "页面拼装": "assemble", "拼装": "assemble", "组装": "assemble",
    "高分辨率打印": "print_hq", "高清打印": "print_hq", "高质量打印": "print_hq",
}

# permissions-only（没有打开口令）时补的 owner 口令。见 set_pdf_permissions 的说明。
_PERMS_ONLY_OWNER_PW = "dsh-perms-owner"


def _resolve_deny(deny):
    """
    把人话解析成 /P 权限位。

    deny 允许混着写：列表、逗号/顿号分隔的字符串（中英文逗号都行）、裸位整数。
    返回 (规范名列表, 合并后的位值)。认不出来的名字直接抛错，并把可选值列回去——
    静默忽略拼错的项，用户会以为「禁止复制」生效了，实际文件照样能复制。
    """
    if isinstance(deny, (str, int)):
        deny = [deny]
    names, bits = [], 0
    for item in deny or []:
        # 一整个字符串塞进来也要拆开（中英文逗号、顿号、分号都当分隔符）
        for raw in (p.strip() for p in re.split(r"[,，、;；]+", str(item).strip()) if p.strip()):
            if raw.isdigit():
                value = int(raw)
                hit = [n for n, b in _PERM_BITS.items() if value & b]
                if not hit:
                    raise ValueError("不认识的权限位 %s，可选：%s" % (raw, _PERM_HELP))
            else:
                name = _PERM_CN.get(raw.lower(), raw.lower())
                if name not in _PERM_BITS:
                    raise ValueError("不认识的权限名 %r，可选：%s" % (raw, _PERM_HELP))
                hit = [name]
            for name in hit:
                if name not in names:
                    names.append(name)
                bits |= _PERM_BITS[name]
    if not bits:
        raise ValueError("没有要限制的操作：deny 是空的，写出来的只会是一个「加了密却什么都不限」的文件")
    return names, bits


def _readback_fitz(path, user_pw, owner_pw):
    """
    读回权限位（PyMuPDF）。

    doc.permissions 给的是带符号整数：未加密的文档报 -4（=0xFFFFFFFC，全允许），
    本机实测「禁打印+禁复制」的那份报 -24。所以比较前一律 & 0xFFFFFFFF，
    而且只能按位与，不许和 pypdf 的数字比相等。
    """
    import pymupdf as fitz
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            doc.authenticate(user_pw or owner_pw or "")
        return doc.permissions
    finally:
        close_pdf(doc)


def _readback_pypdf(path, user_pw, owner_pw):
    """
    读回权限位（pypdf）。

    6.20.1 的 PdfReader 根本没有 .permissions 属性，也没有 .allows()（实测
    AttributeError），只能用 int(reader.user_access_permissions) 再按位与。
    """
    from pypdf import PdfReader
    reader = PdfReader(path)
    reader.decrypt(user_pw or owner_pw or "")
    return int(reader.user_access_permissions)


class EncryptMixin:
    def encrypt_pdf(self, pdf_path, output_path, user_pw, owner_pw=""):
        from pypdf import PdfReader, PdfWriter
        try:
            if not user_pw:
                return {"success": False, "message": _text('api_need_open_password')}

            reader = PdfReader(pdf_path)
            if reader.is_encrypted:
                return {"success": False, 'message': _text('api_pdf_encrypted')}

            writer = PdfWriter()
            # append 而不是逐页 add_page：逐页复制会丢掉书签目录。
            writer.append(reader)

            _copy_metadata(reader, writer)

            # 权限密码留空时让它等于打开密码：PDF 规范下两者相同表示"无限制"，
            # 传空字符串部分阅读器会直接拒绝打开。
            writer.encrypt(user_pw, owner_pw or user_pw)

            with atomic_output(output_path) as f:
                writer.write(f)
            logging.info(f"加密完成: {pdf_path} -> {output_path}")
            return {"success": True, "file": output_path}
        except Exception as e:
            logging.exception("encrypt_pdf 失败")
            return {"success": False, "message": str(e)}

    def decrypt_pdf(self, pdf_path, output_path, password=""):
        from pypdf import PdfReader, PdfWriter
        try:
            reader = PdfReader(pdf_path)
            if reader.is_encrypted:
                if not password:
                    return {"success": False, "message": _text('api_encrypted_need_password')}
                if not reader.decrypt(password):
                    return {"success": False, "message": _text('api_password_wrong')}

            writer = PdfWriter()
            writer.append(reader)

            _copy_metadata(reader, writer)

            with atomic_output(output_path) as f:
                writer.write(f)
            logging.info(f"解密完成: {pdf_path} -> {output_path}")
            return {"success": True, "file": output_path}
        except Exception as e:
            logging.exception("decrypt_pdf 失败")
            return {"success": False, "message": str(e)}

    def set_pdf_permissions(self, pdf_path, output_path="", user_pw="", owner_pw="",
                            deny=None, engine="fitz"):
        """
        只改权限位（F21）：能做「能随便看、但不许打印/复制」这种文件。

        为什么不复用 encrypt_pdf：那边必须先有打开密码，而用户要的常常是
        permissions-only（user_pw 留空）。这种文件两台引擎的要求不一样，都是本机实测：
          - PyMuPDF：user_pw 和 owner_pw 都给空串会「保存成功但一位都没禁」，
            读回 -4（=0xFFFFFFFC 全允许）；owner_pw 给个非空值限制才真的落盘（读回 -24）。
          - pypdf 6.20.1：两个口令都给空串也能落限制（读回 4294967272）。
        所以 owner_pw 空时退到 user_pw，两个都空退到 _PERMS_ONLY_OWNER_PW。
        不用随机串当 owner 口令：随机串等于给用户上了一把谁也打不开的锁，
        以后要解除限制只能靠它，而这个口令 UI 从头到尾没问过用户。

        engine="fitz"（默认）只加权限不动结构，顺带 garbage=4+clean 清冗余：
        本机实测 book_1000.pdf 450804 -> 377866 字节（-16.2%，0.5s），
        1200 页的 pm_over.pdf 413689 -> 394866（-4.5%，0.5s）。
        engine="pypdf" 留给「同时要重排页面」的调用：它重建文档结构，实测同一份
        book_1000.pdf 450804 -> 496110（+10.1%，0.4s），基本只增不减。

        返回里的 permissions_readback 是引擎原样报的整数（fitz 带符号、pypdf 无符号），
        判定一律按位与：允许 <=> (p & bit) != 0。两台引擎同一意图读回的原始数字可能不同，
        谁都不许把它改成 == 比较。
        """
        import pymupdf as fitz
        try:
            if engine not in ("fitz", "pypdf"):
                return {"success": False, "message": _text('api_engine_unrecognized') % engine}
            if not pdf_path or not os.path.isfile(pdf_path):
                return {"success": False, 'message': _text('api_pdf_missing')}
            names, deny_bits = _resolve_deny(deny)
            if not output_path:
                output_path = unique_output_path(os.path.splitext(pdf_path)[0] + "_权限.pdf")

            # permissions 传的是「允许」掩码，不是「禁止」掩码：
            # 实测直接把 deny 的位传进去，读回来正好是反的（只允许了这两项）。
            owner = owner_pw or user_pw or _PERMS_ONLY_OWNER_PW
            allow = _PERM_ALL ^ deny_bits
            before = os.path.getsize(pdf_path)

            if engine == "fitz":
                doc = fitz.open(pdf_path)
                if doc.needs_pass:
                    close_pdf(doc)
                    return {"success": False, 'message': _text('api_pdf_encrypted')}
                # save_pdf_doc 负责关文档；输出路径等于源路径时它先写临时文件再替换
                save_pdf_doc(doc, output_path, encryption=fitz.PDF_ENCRYPT_AES_256,
                             user_pw=user_pw, owner_pw=owner, permissions=allow,
                             garbage=4, clean=True, deflate=True)
                readback = _readback_fitz(output_path, user_pw, owner)
            else:
                from pypdf import PdfReader, PdfWriter
                # 陷阱：UserAccessPermissions 只能从 pypdf.constants 导，
                # 6.20.1 里 `from pypdf import UserAccessPermissions` 直接 ImportError。
                from pypdf.constants import UserAccessPermissions

                reader = PdfReader(pdf_path)
                if reader.is_encrypted:
                    return {"success": False, 'message': _text('api_pdf_encrypted')}
                writer = PdfWriter()
                # append 而不是逐页 add_page：逐页复制会丢掉书签目录
                writer.append(reader)
                _copy_metadata(reader, writer)
                # 成员名和 PyMuPDF 不一样，只有 PRINT(4)/MODIFY(8) 对得上，其余是
                # EXTRACT(16)、ADD_OR_MODIFY(32)、FILL_FORM_FIELDS(256)、
                # EXTRACT_TEXT_AND_GRAPHICS(512)、ASSEMBLE_DOC(1024)、
                # PRINT_TO_REPRESENTATION(2048)；写 U.COPY / U.ANNOTATE / U.FORM
                # 会 AttributeError。all() 还会连带置上 26 个保留位，所以读回的
                # 原始整数和位值不是一个量级，只能按位与判定。
                writer.encrypt(user_password=user_pw, owner_password=owner,
                               algorithm="AES-256",
                               permissions_flag=UserAccessPermissions.all() ^ deny_bits)
                with atomic_output(output_path) as f:
                    writer.write(f)
                readback = _readback_pypdf(output_path, user_pw, owner)

            unsigned = readback & 0xFFFFFFFF
            still_open = [n for n in names if unsigned & _PERM_BITS[n]]
            if still_open:
                # 这就是上面那个「空 owner 口令静默不限制」的兜底：写进去了但没生效，
                # 不能说成功，否则用户拿着一个自认为受限的文件去发人。
                return {"success": False, "file": output_path,
                        "permissions_readback": readback,
                        "message": _text('api_perm_check_failed')
                                   % ("、".join(still_open), readback)}

            after = os.path.getsize(output_path)
            logging.info(f"权限设置完成: {pdf_path} -> {output_path} "
                         f"(engine={engine} 禁止={'/'.join(names)} 读回={readback})")
            result = {"success": True, "file": output_path, "engine": engine,
                      "denied": names, "deny_bits": deny_bits,
                      "permissions_readback": readback,
                      "password_required": bool(user_pw)}
            if engine == "fitz":
                result["before"] = before
                result["after"] = after
            return result
        except Exception as e:
            logging.exception("set_pdf_permissions 失败")
            return {"success": False, "message": str(e)}
