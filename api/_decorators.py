import functools
import logging
from core.i18n import t as _text


_FALLBACKS = {
    'open_file_dialog': '',
    'save_file_dialog': '',
    'select_directory': '',
    'render_page_range': [],
    'render_page': '',
    'get_pdf_page_count': -1,
    # 查看页的页尺寸清单：炸了要给同样的字典形状（前端拿 sizes.length 排版面）
    'get_page_sizes': {"success": False, "total": 0, "limit": 0, "sizes": [],
                       'message': lambda: _text('api_page_size_read_failed')},
    # 查看页的可选文字层：炸了必须给同一套外壳，且 success=False——
    # 前端按 pages 铺层，缺了 unit/version 就是 undefined 参与乘法，整层字会飘到页外
    'get_text_layer_range': {"success": False, "unit": "pt", "coord_space": "display",
                             "version": 1, "total": 0, "start": 0, "count": 0, "pages": [],
                             'message': lambda: _text('api_viewer_layer_failed')},
    'get_text_layer': {"success": False, "unit": "pt", "coord_space": "display",
                       "version": 1, "total": 0, "start": 0, "count": 0, "pages": [],
                       'message': lambda: _text('api_viewer_layer_failed')},
    'get_all_text': {},
    'get_version': '',
    'get_log_path': '',
    'load_config': {},
    'save_config': False,
    # 「保存并应用」那一条快路：炸了要还给界面同一个字典形状（前端读 bundle.success）
    # 骑缝印：炸了同样不许还 success:True，否则界面弹「已盖章」而文件上一片都没多
    'apply_cross_page_seal': {'success': False, 'file': '', 'pages': 0,
                              'message': lambda: _text('api_qifeng_failed')},
    # LaTeX 这三笔：炸了同样不许还 success:True，否则界面弹「排好了」而文件一片空白
    'latex_to_pdf': {'success': False, 'file': '', 'pages': 0,
                     'message': lambda: _text('api_latex_convert_failed')},
    'get_latex_capabilities': {'success': False, 'summary': {},
                               'message': lambda: _text('api_latex_caps_failed')},
    'check_tex_file': {'success': False, 'blocks': 0, 'unknown': [], 'ignored': [],
                       'errors': [], 'message': lambda: _text('api_latex_convert_failed')},
    # 文档信息页「生成工具链」那一栏：炸了要给同一套键（界面逐格读 is_tex / fonts /
    # font_note_texts…），漏一格就是 JS 那边一句 undefined.length；message 走字典，
    # 这一栏读不出来时也要说得出「读不出来」。
    'detect_tex_provenance': {'success': False, 'reason': 'error',
                              'message': lambda: _text('api_tex_read_failed'),
                              'detail': '', 'creator': '', 'producer': '',
                              'engine': 'none', 'engine_label_key': 'tex_engine_none',
                              'engine_label': lambda: _text('tex_engine_none'),
                              'version': None, 'via_dvi': False, 'dviprocessor': None,
                              'is_tex': False, 'page_count': 0, 'font_count': 0,
                              'fonts': [], 'font_notes': [], 'font_note_texts': [],
                              'warnings': [], 'warning_texts': []},
    # 翻译那一档接口：炸了要还给界面「没成功」的形状，兜底里绝不能出现 success:True，
    # 否则界面弹「翻译完成」而实际一个字都没翻。
    'get_translate_providers': {'success': False, 'providers': [], 'targets': [],
                                'default': '', 'settings': {}},
    'translate_text': {'success': False, 'results': [], 'count': 0,
                       'message': lambda: _text('tr_provider_failed')},
    'translate_pdf_page': {'success': False, 'page': 0, 'pairs': [], 'edits': {},
                           'message': lambda: _text('tr_provider_failed')},
    'translate_pdf_to_pdf': {'success': False, 'file': '', 'translated': 0,
                             'message': lambda: _text('tr_provider_failed')},
    'export_translation_pair': {'success': False, 'file': '', 'rows': 0,
                                'message': lambda: _text('tr_provider_failed')},
    'save_config_and_bundle': {"success": False},
    'get_recent_files': [],
    'open_url': False,
    # 下面 8 个是 7.0.1 新增、返回数组或对象的方法：漏登记时装饰器只会还回
    # {"success": false, "message": ...}，前端紧接着的一句 .map 就把整块面板炸掉，
    # 而日志里只留一条 exception。兜底值一律照各方法自己失败分支的形状给。
    'open_files_dialog': [],
    'get_page_size': {"success": False, 'message': lambda: _text('api_page_size_read_failed')},
    'extract_images': {"success": False, "dir": "", "count": 0, "files": []},
    'detect_sensitive_info': {"success": False, "hits": [], "done": True, "next_index": 0},
    'get_bookmarks': {"success": False, "bookmarks": [], "page_count": 0},
    'split_by_bookmarks': {"success": False, "files": []},
    'load_page_draft': {"success": False, "found": False, "restorable": False},
    'list_page_drafts': [],
    # 打印同理：列表炸了要给空数组（下拉框照常渲染、只是没得选），
    # print_pdf 绝不能兜成 success，否则前端会提示「已送到打印机」而其实一张都没出
    'get_printers': {"success": False, "printers": [], "default": "", "builtin": False,
                     "message": lambda: _text('api_printers_list_failed')},
    'print_pdf': {"success": False, "method": "none", "message": lambda: _text('api_print_failed')},
    # 打印前的路数预提示：炸了要说「预提示读不到」，而不是让界面那句一直空着，
    # 用户会以为这版没做这个功能
    'print_plan': {"success": False, "method": "none", "pages": [], "copies": 1,
                   "message": lambda: _text('dyn_1016')},
    # 排版四格的单页效果预览：出不了图就给空 image，界面照常显示那句失败原因
    'preview_page_effect': {"success": False, "image": "", "page": 0, "page_count": 0,
                            "message": lambda: _text('dyn_1472')},
    # OCR：模型缺失或 Paddle 起不来时，调用方要看得到「不可用」而不是一个空文本
    'ocr_status': {"success": False, "available": False, "qualities": {},
                   "versions": {}, "engine_loaded": False},
    'ocr_warmup': {"success": False, "message": lambda: _text('api_ocr_warmup_failed')},
    'ocr_image': {"success": False, "text": "", "lines": [], 'message': lambda: _text('api_ocr_failed')},
    'ocr_pdf_page': {"success": False, "text": "", "lines": [],
                     'message': lambda: _text('api_ocr_failed')},
    'ocr_pdf_text': {"success": False, "text": "", "pages": [], "chars": 0,
                     'message': lambda: _text('api_ocr_failed')},
    'ocr_export_text': {"success": False, "file": "", "message": lambda: _text('dyn_1159')},
    'ocr_searchable_pdf': {"success": False, "file": "", "message": lambda: _text('dyn_1164')},
    'ocr_sensitive_info': {"success": False, "hits": [], "count": 0,
                           "message": lambda: _text('api_ocr_sensitive_failed')},
    'ocr_scan_report': {"success": False, "pages": [], "flagged": 0,
                        "message": lambda: _text('dyn_1180')},
    'ocr_auto_bookmarks': {"success": False, "bookmarks": [], "count": 0,
                           "message": lambda: _text('dyn_1193')},
    # Word / Excel 两个导出是 7.0.3 加的，形状和 txt 一路：炸了要给「没出文件」，
    # 前端那句 res.file 拼进状态行才不会显示成 undefined
    'ocr_export_docx': {"success": False, "file": "", "pages": 0, "chars": 0,
                        "message": lambda: _text('dyn_1462')},
    'ocr_export_xlsx': {"success": False, "file": "", "pages": 0, "chars": 0,
                        "message": lambda: _text('dyn_1465')},
    # 拍照增强四条入口：预览出不了图就是两张空 data URL，界面那句失败原因照旧显示
    'enhance_status': {"success": False, "available": False, "version": "", "modes": [],
                       "options": [], "work_side": 0, "message": lambda: _text('api_enhance_unavailable')},
    'enhance_pdf': {"success": False, "file": "", "pages": 0, "message": lambda: _text('dyn_1479')},
    'enhance_images': {"success": False, "files": [], "count": 0, "failed": 0,
                       "message": lambda: _text('dyn_1485')},
    'enhance_preview': {"success": False, "before": "", "after": "", "page_count": 0,
                        "message": lambda: _text('dyn_1472')},
    # 本机字体清单（⑨）：炸了要给空清单而不是裸字典，界面下拉框照旧渲染、只是没得选
    'list_fonts': {"success": False, "families": [], "count": 0,
                   "message": lambda: _text('api_font_list_failed')},
    # 进度条那两读：job_status 兜成「没在跑」而不是 success，界面就会把条收掉而不是挂个假进度
    'job_status': {"active": False, "name": "", "total": 0, "done": 0, "kind": "page",
                   "percent": 0, "cancelling": False},
    'job_cancel': False,
}


def _fallback_for(name):
    """按「现在这一时刻」把兜底值算出来：message 那格存的是 lambda 而不是成品字符串。

    为什么要现取：这张表在模块 import 时就建好了，那时候界面语言还没从配置里读出来；
    当场翻成字符串等于「英文界面下一次崩溃，提示还是中文」——
    而崩溃提示恰恰是最需要看得懂的那一句。
    """
    box = _FALLBACKS.get(name)
    if isinstance(box, dict):
        return {key: (value() if callable(value) else value) for key, value in box.items()}
    return box


def _make_safe(func, name):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logging.exception(f"Api.{name} 调用失败")
            if name in _FALLBACKS:
                return _fallback_for(name)
            return {"success": False, "message": f"{type(e).__name__}: {e}"}
    return wrapper


def _wrap_api_methods(cls):
    """遍历 MRO，把所有公开方法包一层异常兜底。"""
    seen = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, attr in list(vars(klass).items()):
            if name in seen:
                continue
            if name.startswith('_'):
                continue
            if not callable(attr):
                continue
            if isinstance(attr, (staticmethod, classmethod)):
                continue
            seen.add(name)
            setattr(cls, name, _make_safe(attr, name))
    return cls