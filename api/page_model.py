"""「PDF 编辑」数据层的桥接方法：页面模型（PDF → JSON）与文字改动写回。

真正的解析与写回在 `core/page_model.py` / `core/page_edit.py`，这里只做三件事：
把路径参数收干净、给没点名输出位置的请求按项目既有习惯起个新文件名、
把 core 层的错误码翻成界面文案（`_text('键')`，本模块一个字都不写死）。

方法名说明（避免和现有一处撞车）：
  `api/pages.py` 已经有一个 `apply_page_edits(pdf_path, output_path, slots)`，
  那是「页面管理」一次性写出旋转/删除/调序的落盘入口，语义完全不同。
  所以本模块的写回叫 **`apply_text_edits`**，不叫 apply_page_edits——
  两个 mixin 同时挂在 Api 上时，同名方法会按 MRO 互相盖掉，界面点到哪个都不一定。

注册：由 api/__init__.py 那边把 `PageModelMixin` 加进 `Api` 的继承列表（本文件不动它）。
"""

import logging
import os

from core import page_edit as _edit
from core import page_model as _model
from core.i18n import t as _text

from ._io import unique_output_path

# core 层的错误码 → 界面文案。core 模块不 import i18n（错误码是纯数据），
# 翻译只在这一层做，将来别的入口（命令行/批处理）才能自己决定怎么说。
def _message(code, detail, pdf_path=''):
    if code == 'not_found':
        return _text('api_file_not_found') % (detail or pdf_path or '')
    if code == 'page_out_of_range':
        info = detail if isinstance(detail, dict) else {}
        return _text('ocr_page_out_of_range', index=info.get('index', ''),
                     total=info.get('total', ''))
    if code == 'needs_password':
        return _text('api_encrypted_need_password')
    if code == 'need_save_path':
        return _text('api_need_save_path')
    if code == 'must_end_pdf':
        return _text('api_pages_must_end_pdf')
    if code == 'bad_password':
        return _text('pm_bad_password')
    if code == 'empty_document':
        return _text('pm_empty_document')
    if code == 'read_only_permissions':
        return _text('pm_read_only')
    if code == 'output_is_source':
        return _text('pm_output_is_source')
    if code == 'bad_edits':
        return _text('pm_edits_bad')
    if code == 'edits_empty':
        return _text('pm_edits_empty')
    if code == 'nothing_to_apply':
        return _text('pm_nothing_applied')
    if code == 'span_not_found':
        info = detail if isinstance(detail, dict) else {}
        rows = list(info.get('not_found') or [])
        return _text('pm_span_not_found', count=len(rows),
                     ids='、'.join(str(x) for x in rows[:5]) or '-')
    if code == 'font_unsupported':
        info = detail if isinstance(detail, dict) else {}
        rows = ['%s（%s）' % (item.get('id'), item.get('chars'))
                for item in (info.get('unsupported') or [])[:3]]
        return _text('pm_font_unsupported', items='、'.join(rows) or '-')
    if code == 'verify_mismatch':
        if isinstance(detail, str) and detail:
            return _text('pm_verify_mismatch', detail=detail)
        return _text('pm_verify_failed')
    if code == 'unreadable':
        return _text('pm_unreadable', detail=detail or '')
    if code == 'parse_failed':
        return _text('pm_parse_failed', detail=detail or '')
    if code == 'write_failed':
        return _text('pm_write_failed', detail=detail or '')
    return _text('pm_unexpected', code=code or '')


def _fail(result, pdf_path=''):
    """core 层的 `{'success': False, 'error': 码}` → 界面契约 `{'success': False, 'message': 话}`。"""
    out = {'success': False, 'message': _message(result.get('error'), result.get('detail'), pdf_path)}
    # 错误码原样带着：界面要按它区分「要密码」「没文字层」「读回核对不合格」，
    # 只给一句话前端就只能猜。
    out['error'] = result.get('error')
    if 'file' in result:
        out['file'] = result['file']
    if 'edits' in result:
        out['edits'] = result['edits']
        out['verified'] = result.get('verified', False)
        out['problems'] = result.get('problems', [])
    if 'pages' in result:
        out['pages'] = result['pages']
    if result.get('not_found'):
        out['not_found'] = result['not_found']
    return out


def _beside(pdf_path, name, ext):
    """源文件同目录拼一个派生名字：<stem>_<语言那一份「编辑」>.pdf（api/bookmark.py 同口径）。

    后缀也按界面语言来：英文界面下写出 `report_edited.pdf`，不是 `report_编辑.pdf`——
    文件名是要进用户资源管理器的，属于界面的一部分。
    """
    return os.path.join(os.path.dirname(os.path.abspath(str(pdf_path))),
                        os.path.splitext(os.path.basename(str(pdf_path)))[0] + name + ext)


class PageModelMixin:
    """页面模型（PDF→JSON）与文字编辑写回。"""

    def get_page_model(self, pdf_path, page_index=0, password=''):
        """把一页 PDF 解析成结构化 JSON（文字块/行/span + 图片 + 矢量框线）。

        坐标是**页面渲染坐标**：左上原点、y 向下、单位 pt、/Rotate 已经生效，
        和查看页 `render_page` 出的那张图同一套，前端可以直接把框叠在图上。
        每个 span 的 id 形如 `p<页号>_b<块序>_l<行序>_s<span 序>`，同一份文件重复解析不变，
        改文字就按这个 id 递回来（`apply_text_edits`）。

        扫描件（没有文字层）不报错：`success: True` + `has_text_layer: False`，
        `blocks` 里只有图片/矢量；空页同理（`blocks: []`）。
        加密没给密码、文件坏了、页码越界才 `success: False` + `message`。
        """
        result = _model.page_model(pdf_path, page_index, password=password or '')
        if not result.get('success'):
            return _fail(result, pdf_path)
        return result

    def get_editable_pages(self, pdf_path, password=''):
        """逐页报「这页有没有文字层 / 多少字 / 转了几度」，给界面决定哪些页能编辑。

        比一次给整本的页面模型便宜得多（整本模型是几十上百次整页解析）。
        返回 `{'success', 'page_count', 'pages': [{'page','has_text_layer','text_chars','rotation'}]}`。
        """
        result = _edit.editable_pages(pdf_path, password=password or '')
        if not result.get('success'):
            return _fail(result, pdf_path)
        return result

    def apply_text_edits(self, pdf_path, edits, output_path='', password=''):
        """按 `{span_id: 新文本}` 把改动写回，**写到新文件**，写完重新打开核对。

        `edits` 也接受 `[{'id':…, 'text':…}]`（前端从模型直接映射的数组，顺序有意义）；
        新文本给空串是删掉这一句。中文/日文/韩文写得进去（内置 china-s/japan/korea）。
        只要有一个 id 找不到、或有内置字体盖不住的字符，整批一个字都不写并如实报错
        （照页面管理那边的口径：只处理其中合法的那部分还回报成功，等于静默丢改动）。

        没点名保存位置时就按项目习惯在旁边起一个 `<原名>_编辑.pdf`，撞名加序号，
        绝不覆盖被打开的那份。返回里每条改动都带 `risk`（底下压着图片/框线、
        换了内置字体、新句子超出剩余位置等）与 `verified`；核对不合格就是
        `success: False` + `verified: False`，不许假装成功。
        """
        path = str(pdf_path or '')
        if not output_path or not str(output_path).strip():
            if not path or not os.path.isfile(path):
                return {'success': False, 'message': _message('not_found', path, path),
                        'error': 'not_found'}
            # 起名字走「写出新文件」的既有习惯：同目录、加后缀、撞名换名
            output_path = unique_output_path(_beside(path, _text('pm_output_suffix'), '.pdf'))
        result = _edit.apply_text_edits(path, edits, output_path, password=password or '')
        if not result.get('success'):
            return _fail(result, path)
        logging.info('页面文字编辑已写回: %s -> %s（%d 处 / 核对=%s / risk=%d）',
                     path, result.get('file'), len(result.get('edits') or []),
                     result.get('verified'), result.get('risk_count', 0))
        return result
