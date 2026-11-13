r"""「PDF 文本翻译」的桥接方法：拿译文、按 span 写回、导对照表。

能力全在 `core/translate.py`（provider 接口 + offline/http 两档 + 批量 + 缓存），
PDF 的读与写**一个字都不在这里重复实现**：解析用 `core/page_model.py`，
写回用 `core/page_edit.py`。所以这一层只做四件事：

1. 收路径参数、没点名保存位置时按项目习惯在旁边起个新名字（绝不覆盖打开的那份）；
2. 逐页取 span 原文 → 批量送 provider → 把 `{span_id: 译文}` 交给 `page_edit` 写回；
3. 把 core 层的错误码翻成人话（`_text('键')`，本模块不写死一句话术）；
4. 长任务登记 `core/jobctl.py` 的槽位：逐页进度 + 每页之间查取消标记。

## 三条口径（跟既有代码一致，改之前先读）

* **不假装成功**。没配服务地址 / 连不上 / 超时 / 服务回空译文 → `success: False` 带
  `error` 码和一句人话，绝不回一份「译文是空字符串」或「原文当译文」的成品。
  唯一的例外是 `offline` 兜底档：它翻不到的词**原样保留**，但一定带
  `coverage`（命中率）和 `note: 'fallback_glossary'`，成功与否照实说。
* **取消不是失败也不是成功**：已经翻完的页照常写出、照常读回核对，
  返回值里 `cancelled: True` + `pages_done`/`translated` 说明停在哪、交回了多少。
* **目标语言只认软件支持的那十种**（`core/i18n.SUPPORTED`，和界面语言一档不差）。
  认不出来的代码直接报错，不硬翻——硬翻出来的东西没人认得出是什么。

## 界面入口（现在故意不做）

按要求这版**不挂导航栏/首页卡片**：本 mixin 的方法都通了，
`api/__init__.py` 那边把 `TranslateMixin` 加进 `Api` 的继承列表就能调；
前端要显示进度条还得把慢接口登记进 `assets/js/common.js` 的 `LONG_JOBS`
（`translate_pdf_to_pdf`、`export_translation_pair`），兜底表 `api/_decorators.py`
同样要补这几笔。这些都写在该模块的验收报告里，等主入口方案定了再做。
"""

import logging
import os

from core import jobctl
from core import page_edit as _edit
from core import page_model as _model
from core import translate as _tr
from core.i18n import SUPPORTED as TARGET_LANGS
from core.i18n import language_name as _lang_name
from core.i18n import t as _text

from ._io import atomic_output, unique_output_path
from ._range import select_pages
# 页面模型/写回那两套 core→错误码→文案 的表已经在 api/page_model.py 里，
# 这里直接复用而不是再抄一份：两边对同一个 not_found / needs_password 的说法必须一致。
# 项目里这种「跨模块取私有函数」有先例（api/ocr.py 用 api.redact._classify）。
from .page_model import _message as _pm_message

# provider 名字 → 文案键。自定义档（第三方引擎）没有专属键就统一说「其他」。
_PROVIDER_KEYS = {_tr.PROVIDER_OFFLINE: 'tr_provider_offline',
                  _tr.PROVIDER_HTTP: 'tr_provider_http'}

# 能写出对照表的两种格式：txt 谁都读得了，xlsx 是项目里已经带着的依赖（openpyxl）。
# Word 那条路（python-docx）也在包里，但「原文/译文对照」本质是一张两列表格，
# 用 docx 反而要多写表格框线那一套，故选 xlsx + txt。
_EXPORT_EXTS = ('.txt', '.xlsx')


def _message(code, detail, pdf_path=''):
    """翻译这一层自己的错误码 → 人话；认不出的交给页面模型那张表（not_found 等）。"""
    info = detail if isinstance(detail, dict) else {}
    if code == 'no_texts':
        return _text('tr_no_texts')
    if code == 'no_url':
        return _text('tr_no_url')
    if code == 'bad_url':
        return _text('tr_bad_url', url=info.get('url') or '')
    if code == 'timeout':
        return _text('tr_timeout', attempts=info.get('attempts', ''),
                     timeout=info.get('timeout', ''))
    if code == 'unreachable':
        return _text('tr_unreachable', reason=info.get('reason') or '')
    if code == 'server_error':
        return _text('tr_server_error', reason=info.get('reason') or '')
    if code == 'bad_status':
        return _text('tr_bad_status', status=info.get('status', ''))
    if code == 'bad_payload':
        return _text('tr_bad_payload', reason=info.get('reason') or '')
    if code == 'provider_failed':
        return _text('tr_provider_failed')
    if code == 'unknown_provider':
        return _text('tr_unknown_provider', provider=info.get('provider') or '')
    if code == 'unsupported_target':
        return _text('tr_unsupported_target', target=info.get('target') or '',
                     supported='、'.join(_lang_name(c) for c in TARGET_LANGS))
    if code == 'cancelled':
        return _text('tr_cancelled', translated=info.get('translated', ''))
    if code == 'no_source_text':
        return _text('tr_no_source_text')
    if code == 'no_text_in_document':
        return _text('tr_no_text_in_document')
    if code == 'bad_output_ext':
        return _text('tr_bad_output_ext', path=info.get('path') or (detail or ''))
    if code == 'xlsx_unavailable':
        return _text('tr_xlsx_unavailable')
    return _pm_message(code, detail, pdf_path)


def _fail(result, pdf_path=''):
    """core 层的 `{'success': False, 'error': 码}` → 界面契约。多余字段按白名单带过去。"""
    out = {'success': False,
           'message': _message(result.get('error'), result.get('detail'), pdf_path),
           'error': result.get('error')}
    for key in ('provider', 'target', 'source', 'requests', 'cached', 'translated',
                'attempts', 'done', 'detail', 'file', 'edits', 'verified', 'problems', 'pages',
                'page_count', 'not_found', 'risk_count', 'elapsed_ms', 'coverage',
                'cancelled', 'pages_done', 'pages_skipped', 'has_text_layer', 'note'):
        if key in result:
            out[key] = result[key]
    return out


def _beside(pdf_path, name, ext):
    """源文件同目录拼派生名字（和 api/page_model.py 同一口径，但后缀是这里自己的键）。"""
    return os.path.join(os.path.dirname(os.path.abspath(str(pdf_path))),
                        os.path.splitext(os.path.basename(str(pdf_path)))[0] + name + ext)


def _texts_of(model):
    """这一页要翻译的 span：`[{'id', 'text'}]`，纯空白的不送（省一次请求）。"""
    rows = []
    for _block, _line, span in _model.iter_spans(model):
        text = str(span.get('text') or '')
        if text.strip():
            rows.append({'id': span['id'], 'text': text})
    return rows


def _layered_pages(scan):
    """`editable_pages` 的结果 → `{页码: 有没有文字层}`。

    用字典而不是按下标取：万一某一页没报回来（截断/异常数据），按下标会 IndexError，
    按字典取就是「这页不知道有没有字 → 当作要跑」，交给后面的解析去如实报错。
    """
    out = {}
    for row in scan.get('pages') or []:
        out[int(row.get('page') or 0)] = bool(row.get('has_text_layer'))
    return out


def _aggregate(rows):
    """把几页的覆盖率合成一个总数（按句数加权，不按页数——长句多的页才吃覆盖率）。

    任何一批没报覆盖率（http 档、服务方不给）就是 `None`：**不猜**一个数给界面。
    """
    total = 0.0
    units = 0
    for count, coverage in rows:
        if coverage is None:
            return None
        total += float(coverage) * count
        units += count
    return round(total / units, 4) if units else None


def _stats(payload):
    """从 core 的返回里挑出「该报给界面的那几个数」，各处成功分支共用一个形状。"""
    return {'provider': payload.get('provider'), 'coverage': payload.get('coverage'),
            'unmatched': payload.get('unmatched', 0), 'cached': payload.get('cached', 0),
            'requests': payload.get('requests', 0)}


class TranslateMixin:
    """PDF 文本翻译（对照 / 写回 / 导出）。"""

    # ---------- 配置与能力探测 ----------
    def _translate_settings(self):
        """配置字典（`translate_*` 那几个键由 core 层自己挑）。

        没挂 `Api`（例如自测里直接实例化本 mixin）就拿不到 `load_config`，
        这时回空字典：core 层会自己去读配置文件，两边走的是同一个入口。
        """
        reader = getattr(self, 'load_config', None)
        if not callable(reader):
            return {}
        try:
            conf = reader()
        except Exception:
            logging.exception('translate: 读配置失败，按默认值走')
            return {}
        return conf if isinstance(conf, dict) else {}

    def get_translate_providers(self):
        """现在有哪几档翻译引擎、哪一档真能用、缺哪个配置键。

        返回 `{'success', 'providers': [{'name','label','configured','default','missing',
        'url','has_key','glossary'}], 'default', 'targets': [{'code','name'}], 'settings'}`。
        **密钥只回 `has_key` 布尔**，绝不把密钥本身送回界面（前端日志里留一份密钥就等于没锁）。
        """
        conf = self._translate_settings()
        rows = _tr.providers(conf)
        merged = _tr.merged_settings(conf)
        for row in rows:
            row['label'] = _text(_PROVIDER_KEYS.get(row.get('name'), 'tr_provider_other'))
            row['has_key'] = bool(row.pop('needs_key', False))
        return {'success': True, 'providers': rows,
                'default': merged.get('translate_provider'),
                'targets': [{'code': code, 'name': _lang_name(code)} for code in TARGET_LANGS],
                'settings': {'translate_provider': merged.get('translate_provider'),
                             'translate_url': merged.get('translate_url') or '',
                             'timeout': merged.get('translate_timeout'),
                             'batch_max': merged.get('translate_batch_max'),
                             'config_file': _tr.CONFIG_FILE}}

    # ---------- 纯文本 ----------
    def translate_text(self, texts, target, provider='', source='auto'):
        """翻一组文本（不动 PDF），返回逐句原文/译文。

        `texts` 可以是字符串、字符串数组，或前端传来的 JSON 数组文本。
        成功：`{'success', 'results': [{'src','dst'}], 'count', 'coverage', 'unmatched',
        'provider', 'cached', 'requests', 'note'}`；
        失败：`success: False` + `error` 码 + `message`（没配服务地址、超时、语言不支持都在这一类）。
        同一句原文只问一次（进程内 + 磁盘缓存），`cached` 报省掉了几句。
        """
        rows = _as_texts(texts)
        if not rows:
            return {'success': False, 'error': 'no_texts', 'message': _message('no_texts', {})}
        result = _tr.translate_texts(rows, target, source=source, provider=provider,
                                     settings=self._translate_settings(),
                                     should_stop=jobctl.should_stop)
        if not result.get('success'):
            return _fail(result)
        out = dict(result)
        if out.get('note') == 'fallback_glossary':
            # 兜底档必须自带说明：不然界面上「看起来有译文」和「真翻了」分不出来
            out['hint'] = _text('tr_offline_note')
        return out

    # ---------- 单页（只拿对照，不动 PDF） ----------
    def translate_pdf_page(self, pdf_path, page_index=0, target='', password='',
                           provider='', source='auto'):
        """翻一页，返回逐 span 的原文/译文对照；**不写 PDF**（写回是 `apply_text_edits` 的活）。

        成功带 `pairs: [{'id','src','dst','changed'}]` 和 `edits: {span_id: 译文}`——
        `edits` 直接可以交给页面模型那边的 `apply_text_edits`，两条线就是在这儿接上的。
        纯图片页/空页：`success: False` + `error: 'no_source_text'` + `has_text_layer`，
        让界面能顺势问一句「要先做 OCR 识别吗」，而不是报一个看不出所以然的失败。
        """
        model = _model.page_model(pdf_path, page_index, password=password or '')
        if not model.get('success'):
            return _fail(model, pdf_path)
        spans = _texts_of(model)
        if not spans:
            return {'success': False, 'error': 'no_source_text',
                    'message': _message('no_source_text', {}),
                    'page': model.get('page'), 'pairs': [],
                    'has_text_layer': bool(model.get('has_text_layer'))}
        result = _tr.translate_texts([row['text'] for row in spans], target,
                                     source=source, provider=provider,
                                     settings=self._translate_settings(),
                                     should_stop=jobctl.should_stop)
        if not result.get('success'):
            out = _fail(result, pdf_path)
            out['page'] = model.get('page')
            out['pairs'] = []
            return out
        pairs = []
        edits = {}
        for row, item in zip(spans, result.get('results') or []):
            dst = item.get('dst', row['text'])
            pairs.append({'id': row['id'], 'src': row['text'], 'dst': dst,
                          'changed': dst != row['text']})
            if dst != row['text']:
                edits[row['id']] = dst
        out = {'success': True, 'page': model.get('page'), 'page_index': page_index,
               'pairs': pairs, 'edits': edits, 'translated': len(edits),
               'count': len(pairs), 'coverage': result.get('coverage'),
               'provider': result.get('provider'), 'target': result.get('target'),
               'cached': result.get('cached', 0), 'requests': result.get('requests', 0),
               'unmatched': result.get('unmatched', 0), 'note': result.get('note', ''),
               'elapsed_ms': result.get('elapsed_ms', 0)}
        if out['note'] == 'fallback_glossary':
            out['hint'] = _text('tr_offline_note')
        return out

    # ---------- 整本 → 新 PDF ----------
    def translate_pdf_to_pdf(self, pdf_path, target, output_path='', password='',
                             provider='', source='auto', page_range=''):
        """整本（或指定页）逐页翻译后按 span id 写回**新** PDF。

        进度与取消：`jobctl` 一页一格，每页之间查取消标记。中途取消**不丢掉已翻的页**——
        已完成的页照常写回、照常读回核对，返回值里 `cancelled: True` + `pages_done`
        说清「只做了这些」，绝不报成整本翻完。
        没有文字层的页跳过并计数（`pages_skipped`）；整本都没文字层就是失败，不写空壳文件。
        `output_path` 留空 → 源文件旁边起一个 `<原名>_译文.pdf`，撞名加序号。
        """
        path = str(pdf_path or '')
        if not path or not os.path.isfile(path):
            return {'success': False, 'error': 'not_found',
                    'message': _message('not_found', path, path)}
        scan = _edit.editable_pages(path, password=password or '')
        if not scan.get('success'):
            return _fail(scan, path)
        pages, error = select_pages(page_range, int(scan.get('page_count') or 0))
        if error:
            return {'success': False, 'error': 'page_range', 'message': error}
        # 只有真有字的页值得跑：可搜索 PDF 那一边也是这个口径（有文字层就当数字页）
        layered = _layered_pages(scan)
        todo = [n for n in pages if layered.get(n, True)]
        skipped = len(pages) - len(todo)
        if not todo:
            return {'success': False, 'error': 'no_text_in_document',
                    'message': _message('no_text_in_document', {}),
                    'page_count': len(pages), 'pages_skipped': skipped}
        if not output_path or not str(output_path).strip():
            output_path = unique_output_path(_beside(path, _text('tr_output_suffix'), '.pdf'))
        engine = provider or _tr.merged_settings(
            self._translate_settings()).get('translate_provider')

        edits = {}
        done = []
        cover = []
        counters = {'cached': 0, 'requests': 0, 'unmatched': 0, 'sentences': 0}
        cancelled = False
        failed = None
        try:
            jobctl.start('translate_pdf', len(todo))
            for number in todo:
                if jobctl.should_stop():
                    cancelled = True
                    break
                jobctl.set_progress(len(done))
                model = _model.page_model(path, number - 1, password=password or '')
                if not model.get('success'):
                    failed = _fail(model, path)
                    break
                spans = _texts_of(model)
                if spans:
                    result = _tr.translate_texts([row['text'] for row in spans], target,
                                                 source=source, provider=provider,
                                                 settings=self._translate_settings())
                    if not result.get('success'):
                        # 取消和真失败分得开：前者交回已有成果，后者一个字都不写
                        if result.get('error') == 'cancelled':
                            cancelled = True
                        else:
                            failed = _fail(result, path)
                        break
                    for row, item in zip(spans, result.get('results') or []):
                        dst = item.get('dst', row['text'])
                        if dst != row['text']:
                            edits[row['id']] = dst
                    counters['cached'] += result.get('cached', 0)
                    counters['requests'] += result.get('requests', 0)
                    counters['unmatched'] += result.get('unmatched', 0)
                    counters['sentences'] += len(spans)
                    cover.append((len(spans), result.get('coverage')))
                done.append(number)
                jobctl.advance()
            jobctl.set_progress(len(done))
        finally:
            jobctl.finish()

        if failed:
            failed['pages_done'] = len(done)
            failed['pages_skipped'] = skipped
            if cancelled:
                failed['cancelled'] = True
            return failed
        if cancelled and not done:
            return {'success': False, 'error': 'cancelled', 'cancelled': True,
                    'message': _text('tr_cancelled_none'),
                    'pages_done': 0, 'pages_skipped': skipped,
                    'coverage': _aggregate(cover)}
        if not edits:
            # 全都翻得和原文一样（兜底档整本没命中一个词）：写一份「原样副本」是骗人的
            return {'success': False, 'error': 'no_source_text',
                    'message': _message('no_source_text', {}),
                    'pages_done': len(done), 'pages_skipped': skipped,
                    'coverage': _aggregate(cover), 'provider': engine}
        applied = _edit.apply_text_edits(path, edits, output_path, password=password or '')
        if not applied.get('success'):
            out = _fail(applied, path)
            out['pages_done'] = len(done)
            out['pages_skipped'] = skipped
            out['cancelled'] = cancelled
            out['coverage'] = _aggregate(cover)
            return out
        out = dict(applied)
        out.update({'cancelled': cancelled, 'pages_translated': len(done),
                    'pages_done': len(done), 'pages_skipped': skipped,
                    'sentences': counters['sentences'],
                    'translated': len(applied.get('edits') or []),
                    'coverage': _aggregate(cover),
                    'cached': counters['cached'], 'requests': counters['requests'],
                    'unmatched': counters['unmatched'],
                    'provider': engine})
        if cancelled:
            out['message'] = _text('tr_cancelled_pages', pages=len(done),
                                   count=out['translated'])
        logging.info('translate: %s -> %s（%d 页 / %d 处改动 / 取消=%s / 核对=%s）',
                     path, out.get('file'), len(done), out['translated'],
                     cancelled, out.get('verified'))
        return out

    # ---------- 对照表导出（不动 PDF） ----------
    def export_translation_pair(self, pdf_path, target, output_path='', password='',
                                provider='', source='auto', page_range=''):
        """导出「原文 / 译文」对照表，**不改 PDF**。

        格式看后缀：`.txt` 逐页排开（谁都读得了），`.xlsx` 四列（页码 / span id / 原文 / 译文，
        用项目里已经随包的 openpyxl，不新增依赖）。传别的后缀直接报错，不闷声写个 txt 改名。
        同样是逐页进度 + 可取消：取消后**已经翻好的那些页照样写出去**，
        并带 `cancelled: True` + `pages_done` 说清只收了前几页。
        """
        path = str(pdf_path or '')
        if not path or not os.path.isfile(path):
            return {'success': False, 'error': 'not_found',
                    'message': _message('not_found', path, path)}
        out_path = str(output_path or '').strip()
        if not out_path:
            return {'success': False, 'error': 'need_save_path',
                    'message': _text('api_need_save_path')}
        ext = os.path.splitext(out_path)[1].lower()
        if ext not in _EXPORT_EXTS:
            return {'success': False, 'error': 'bad_output_ext',
                    'message': _message('bad_output_ext', {'path': out_path}),
                    'detail': {'ext': ext, 'allowed': list(_EXPORT_EXTS)}}

        scan = _edit.editable_pages(path, password=password or '')
        if not scan.get('success'):
            return _fail(scan, path)
        pages, error = select_pages(page_range, int(scan.get('page_count') or 0))
        if error:
            return {'success': False, 'error': 'page_range', 'message': error}
        layered = _layered_pages(scan)
        todo = [n for n in pages if layered.get(n, True)]
        skipped = len(pages) - len(todo)

        rows = []          # (页码, span id, 原文, 译文)
        cover = []
        done = []
        cancelled = False
        failed = None
        engine = provider or _tr.merged_settings(
            self._translate_settings()).get('translate_provider')
        counters = {'cached': 0, 'requests': 0}
        try:
            jobctl.start('translate_export', len(todo))
            for number in todo:
                if jobctl.should_stop():
                    cancelled = True
                    break
                jobctl.set_progress(len(done))
                model = _model.page_model(path, number - 1, password=password or '')
                if not model.get('success'):
                    failed = _fail(model, path)
                    break
                spans = _texts_of(model)
                if spans:
                    result = _tr.translate_texts([row['text'] for row in spans], target,
                                                 source=source, provider=provider,
                                                 settings=self._translate_settings())
                    if not result.get('success'):
                        if result.get('error') == 'cancelled':
                            cancelled = True
                        else:
                            failed = _fail(result, path)
                        break
                    for row, item in zip(spans, result.get('results') or []):
                        rows.append((number, row['id'], row['text'],
                                     item.get('dst', row['text'])))
                    counters['cached'] += result.get('cached', 0)
                    counters['requests'] += result.get('requests', 0)
                    cover.append((len(spans), result.get('coverage')))
                done.append(number)
                jobctl.advance()
            jobctl.set_progress(len(done))
        finally:
            jobctl.finish()

        if failed:
            failed['pages_done'] = len(done)
            return failed
        if cancelled and not rows:
            return {'success': False, 'error': 'cancelled', 'cancelled': True,
                    'message': _text('tr_cancelled_none'), 'pages_done': 0}
        if not rows:
            return {'success': False, 'error': 'no_text_in_document',
                    'message': _message('no_text_in_document', {}),
                    'page_count': len(pages), 'pages_skipped': skipped}

        head = {'source': _text('tr_label_source'), 'target': _text('tr_label_target'),
                'page': _text('tr_label_page'), 'span': _text('tr_label_span'),
                'target_name': _lang_name(target)}
        try:
            with atomic_output(out_path) as handle:
                if ext == '.xlsx':
                    written = _write_xlsx(handle, rows, head)
                    if written is None:
                        return {'success': False, 'error': 'xlsx_unavailable',
                                'message': _message('xlsx_unavailable', {})}
                else:
                    handle.write(_render_txt(rows, head).encode('utf-8'))
        except Exception as error:
            logging.exception('translate: 写对照表失败 %s', out_path)
            return {'success': False, 'error': 'write_failed',
                    'message': '%s: %s' % (type(error).__name__, error)}
        out = {'success': True, 'file': out_path, 'format': ext.lstrip('.'),
               'pages': len(done), 'pages_done': len(done), 'pages_skipped': skipped,
               'rows': len(rows), 'cancelled': cancelled,
               'coverage': _aggregate(cover), 'provider': engine,
               'target': target, 'cached': counters['cached'],
               'requests': counters['requests']}
        if cancelled:
            out['message'] = _text('tr_export_cancelled', pages=len(done))
        if engine == _tr.PROVIDER_OFFLINE:
            out['note'] = 'fallback_glossary'
            out['hint'] = _text('tr_offline_note')
        logging.info('translate: 对照表 %s 第 %s 页 -> %s（%d 条 / 取消=%s）',
                     path, ','.join(str(n) for n in done[:8]), out_path, len(rows), cancelled)
        return out


def _as_texts(value):
    """把桥层收到的 `texts` 收成字符串列表。

    pywebview 一般给真数组，但前端也常把数组 `JSON.stringify` 之后再递过来
    （表单里存一份就是这么干的），所以两种都认；单个字符串就当作一句。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if x is not None]
    text = str(value)
    stripped = text.strip()
    if stripped.startswith('[') and stripped.endswith(']'):
        import json
        try:
            data = json.loads(stripped)
        except Exception:
            return [text]
        if isinstance(data, list):
            return [str(x) for x in data if x is not None]
    return [text] if text else []


def _render_txt(rows, head):
    """TXT 对照：每页一个分隔行（沿用既有的 `export_page_marker`），下面逐条原文/译文。"""
    parts = []
    current = None
    for number, span_id, src, dst in rows:
        if number != current:
            current = number
            parts.append('%s\n' % _text('export_page_marker', n=number))
        parts.append('%s: %s\n%s: %s\n\n' % (head['source'], src, head['target'], dst))
    return ''.join(parts)


def _write_xlsx(handle, rows, head):
    """xlsx 对照：四列表格。openpyxl 是随包依赖；真拿不到就回 None 让调用方如实报错。"""
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
    except Exception as error:
        logging.info('translate: 没有 openpyxl，导不了 xlsx（%s）', error)
        return None
    book = Workbook()
    sheet = book.active
    sheet.title = head['target_name']
    sheet.append([head['page'], head['span'], head['source'], head['target']])
    for row in rows:
        sheet.append(list(row))
    for column, width in enumerate((8, 22, 60, 60), start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.freeze_panes = 'A2'
    book.save(handle)
    return True
