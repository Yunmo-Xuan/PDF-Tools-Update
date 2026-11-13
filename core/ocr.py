"""本地 OCR：PaddleOCR + OpenCV，依赖和模型都随安装包分发。

模型分三档：
  fast     PP-OCRv6_tiny    最快，适合交互式预览、整页粗读；
  balanced PP-OCRv6_small   速度和准确率折中；
  accurate PP-OCRv6_medium  识别效果更好，CPU 上明显更慢。

识别语言：PP-OCRv6 这一套模型本身就覆盖 简中 / 繁中 / 英 / 日 和整个拉丁语系
（法德意西葡…，见 paddleocr 的 _PPOCRV6_LANGS），所以软件十种界面语言里有八种不用换模型。
只有 韩语 和 俄语 不在 v6 的字典里，要另配 PP-OCRv5 的那两份识别模型
（korean / cyrillic_PP-OCRv5_mobile_rec，随包一起打进来，不联网下载）。
检测模型是语言无关的，换语言时沿用当前档位的检测，不额外多塞一份。

Paddle 的推理器读不了带中文的绝对路径（安装目录叫「PDF Toolkit」），
所以构造引擎时临时切到程序目录、把 resources/ocr_models 下的相对路径交给它，
加载完再切回来。图片/PDF 路径不受这个限制：进模型前先用 cv2.imdecode 读成数组。
"""

import logging
import numbers
import os
import threading
import time
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from core.i18n import t as _text
from core.paths import OCR_CACHE_DIR, get_base_path

# 模型全部随包：不查模型源、不从网上下载。必须在导入 paddleocr 之前设好。
os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')
os.environ.setdefault('PADDLE_PDX_CACHE_HOME', OCR_CACHE_DIR)

_LOG = logging.getLogger(__name__)

_MODEL_REL = os.path.join('resources', 'ocr_models')
_QUALITY_MODELS = {
    'fast': ('PP-OCRv6_tiny_det', 'PP-OCRv6_tiny_rec'),
    'balanced': ('PP-OCRv6_small_det', 'PP-OCRv6_small_rec'),
    'accurate': ('PP-OCRv6_medium_det', 'PP-OCRv6_medium_rec'),
}
DEFAULT_QUALITY = 'fast'
QUALITIES = tuple(_QUALITY_MODELS)
_REQUIRED_FILES = ('inference.json', 'inference.pdiparams')
# 大页面先把长边压到这个尺寸再检测；实测 A4@150dpi 从 15s+ 降到 3s 左右。
# 2026-10-29 想过按档位放开（1280 / 1600），量完撤回了：拿同一张密排表格页
# （tests/out/ocr_limit_probe.py）测，fast 2.5s→3.1s、accurate 28s→110s，
# 而认回来的字数只差 0~4、八个表头格子三档全 7/7 —— 放开上限不涨识别率，只涨等待。
_DET_LIMIT_SIDE = 960

# 界面语言代码 → 识别模型；None 表示用该档位自带的 v6 模型（含拉丁语系）。
# 这一份表就是「软件支持的语言」在 OCR 侧的落点，加语言只动这里。
LANG_MODELS = {
    'auto': None,
    'zh-CN': None,
    'zh-TW': None,
    'en': None,
    'ja': None,
    'fr': None,
    'de': None,
    'it': None,
    'es': None,
    'ko': 'korean_PP-OCRv5_mobile_rec',
    'ru': 'cyrillic_PP-OCRv5_mobile_rec',
}
DEFAULT_LANG = 'auto'
# 界面上给的选择顺序：跟随界面语言 + 十种语言本名（本名不翻译，这是语言选项的惯例）
LANG_CHOICES = ('auto', 'zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'ru', 'fr', 'de', 'it', 'es')
LANG_LABELS = {
    'auto': '自动（中/繁/英/日/法德意西）',
    'zh-CN': '简体中文',
    'zh-TW': '繁體中文',
    'en': 'English',
    'ja': '日本語',
    'ko': '한국어',
    'ru': 'Русский',
    'fr': 'Français',
    'de': 'Deutsch',
    'it': 'Italiano',
    'es': 'Español',
}

_engines = {}
_engine_lock = threading.Lock()
_run_lock = threading.Lock()


def model_root():
    return os.path.join(get_base_path(), 'resources', 'ocr_models')


def _model_dir(model_name):
    return os.path.join(model_root(), model_name + '_infer')


def normalize_lang(lang):
    """把界面语言代码折到 OCR 认得的那一档；认不出的一律当 auto。"""
    value = str(lang or DEFAULT_LANG)
    return value if value in LANG_MODELS else DEFAULT_LANG


def rec_model_name(quality, lang=None):
    """这一档 + 这一语言实际要用的识别模型名。

    LANG_MODELS 里值为 None 的那些（简中/繁中/英/日/拉丁语系）不用换模型——
    PP-OCRv6 那一份字典本来就含它们；只有韩语和俄语才真的换一份识别模型。
    """
    if quality not in _QUALITY_MODELS:
        raise ValueError(f'未知的 OCR 档位: {quality}')
    return LANG_MODELS[normalize_lang(lang)] or _QUALITY_MODELS[quality][1]


def _missing_files(quality, lang=None):
    if quality not in _QUALITY_MODELS:
        raise ValueError(f'未知的 OCR 档位: {quality}')
    names = [_QUALITY_MODELS[quality][0], rec_model_name(quality, lang)]
    missing = []
    for model_name in names:
        directory = _model_dir(model_name)
        for filename in _REQUIRED_FILES:
            path = os.path.join(directory, filename)
            if not os.path.isfile(path) or os.path.getsize(path) < 1024:
                missing.append(os.path.relpath(path, get_base_path()))
    return missing



def _version(package):
    try:
        return _pkg_version(package)
    except PackageNotFoundError:
        return ''


def _cpu_threads():
    # 4 GB 机器上别让 Paddle 开满所有核；4 线程实测够用，也留出 WebView2 的余量
    return max(1, min(4, os.cpu_count() or 4))


def _create_engine(quality, lang=None):
    """构造 PaddleOCR。调用方必须已经持有 _engine_lock。"""
    os.makedirs(OCR_CACHE_DIR, exist_ok=True)
    from paddleocr import PaddleOCR

    det_name = _QUALITY_MODELS[quality][0]
    rec_name = rec_model_name(quality, lang)
    old_cwd = os.getcwd()
    try:
        os.chdir(get_base_path())
        engine = PaddleOCR(
            text_detection_model_name=det_name,
            text_detection_model_dir=os.path.join(_MODEL_REL, det_name + '_infer'),
            text_recognition_model_name=rec_name,
            text_recognition_model_dir=os.path.join(_MODEL_REL, rec_name + '_infer'),
            # PDF 页面是正向的：不做文档方向判断、不做曲面矫正、不做行方向判断，
            # 这样只需要检测+识别两个模型，省体积也省时间。
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device='cpu',
            # Paddle 3.3.1 的 oneDNN 路径在 PP-OCRv6 上会抛
            # ConvertPirAttribute2RuntimeAttribute not support，只能走 CPU 原生实现。
            enable_mkldnn=False,
            cpu_threads=_cpu_threads(),
            text_det_limit_side_len=_DET_LIMIT_SIDE,
            text_det_limit_type='max',
        )
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            _LOG.warning('恢复工作目录失败: %s', old_cwd)
    return engine


def _ensure_engine(quality=DEFAULT_QUALITY, lang=None):
    if quality not in _QUALITY_MODELS:
        raise ValueError(f'未知的 OCR 档位: {quality}')
    # 缓存键认「实际加载了哪一份识别模型」，不认语言代码：简中/繁中/英/日/拉丁语系
    # 用的是同一份 v6 模型，按语言分键会把同一套模型装进内存好几遍。
    rec_name = rec_model_name(quality, lang)
    key = (quality, rec_name)
    with _engine_lock:
        engine = _engines.get(key)
        if engine is not None:
            return engine
        missing = _missing_files(quality, lang)
        if missing:
            raise RuntimeError('OCR 模型缺失：' + '、'.join(missing))
        started = time.monotonic()
        engine = _create_engine(quality, lang)
        _engines[key] = engine
        _LOG.info('OCR 引擎就绪：%s / %s（%.2fs）', quality, rec_name,
                  time.monotonic() - started)
        return engine


def _plain(value):
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Real):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    try:
        return [_plain(item) for item in value]
    except TypeError:
        return str(value)


def _result_data(result):
    data = getattr(result, 'json', None)
    if callable(data):
        data = data()
    if isinstance(data, dict):
        inner = data.get('res')
        return inner if isinstance(inner, dict) else data
    return {}


def _collect_lines(results):
    lines = []
    for result in results or []:
        data = _result_data(result)
        texts = data.get('rec_texts') or []
        scores = data.get('rec_scores') or []
        boxes = (data.get('rec_polys') or data.get('dt_polys')
                 or data.get('rec_boxes') or [])
        for index, text in enumerate(texts):
            text = str(text).strip()
            if not text:
                continue
            lines.append({
                'text': text,
                'score': float(scores[index]) if index < len(scores) else None,
                'box': _plain(boxes[index]) if index < len(boxes) else None,
            })
    return lines


def _run_image(image, quality, extra=None, lang=None):
    engine = _ensure_engine(quality, lang)
    with _run_lock:
        started = time.monotonic()
        results = list(engine.predict(image))
        elapsed_ms = int((time.monotonic() - started) * 1000)
    lines = _collect_lines(results)
    payload = {
        'success': True,
        'quality': quality,
        'lang': normalize_lang(lang),
        'text': '\n'.join(line['text'] for line in lines),
        'lines': lines,
        'elapsed_ms': elapsed_ms,
    }
    if extra:
        payload.update(extra)
    return payload


def _read_image(path):
    import cv2
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        raise RuntimeError('图片文件为空或读不出来')
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError('图片解码失败')
    return image


def _decode_png(png_bytes):
    import cv2
    import numpy as np

    return cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)


def warmup(quality=DEFAULT_QUALITY, lang=None):
    """提前把引擎建好；返回结果和 status(with_engine=True) 一致。"""
    try:
        _ensure_engine(quality, lang)
        result = status()
        result['engine_loaded'] = True
        result['engine_qualities'] = sorted(_engines)
        result['warmed_quality'] = quality
        result['warmed_lang'] = normalize_lang(lang)
        return result
    except Exception as error:
        _LOG.exception('OCR 预热失败')
        return {'success': False, 'message': f'{type(error).__name__}: {error}'}


def loaded_engines():
    """当前进程已经建好的引擎键（档位, 语言），给状态页和验收断言看。"""
    return sorted(_engines)


def ocr_image(image_path, quality=DEFAULT_QUALITY, lang=None):
    path = os.path.abspath(str(image_path or ''))
    if not os.path.isfile(path):
        return {'success': False, 'message': _text('ocr_image_missing', path=path)}
    try:
        return _run_image(_read_image(path), quality, {'image_path': path}, lang)
    except Exception as error:
        _LOG.exception('OCR 图片识别失败: %s', path)
        return {'success': False, 'message': f'{type(error).__name__}: {error}'}


def ocr_pdf_page(pdf_path, page_index=0, dpi=150, quality=DEFAULT_QUALITY, lang=None):
    path = os.path.abspath(str(pdf_path or ''))
    if not os.path.isfile(path):
        return {'success': False, 'message': _text('ocr_pdf_missing', path=path)}
    try:
        import pymupdf
    except ImportError as error:
        return {'success': False, 'message': _text('ocr_no_engine', error=error)}
    try:
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                return {'success': False, 'message': _text('ocr_pdf_locked')}
            total = len(doc)
            try:
                index = int(page_index)
            except (TypeError, ValueError):
                index = 0
            if index < 0 or index >= total:
                return {'success': False, 'message': _text('ocr_page_out_of_range', index=index, total=total)}
            page = doc.load_page(index)
            dpi = max(72, min(400, int(dpi or 150)))
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            image = _decode_png(pixmap.tobytes('png'))
            if image is None:
                return {'success': False, 'message': _text('ocr_page_render_failed')}
            return _run_image(image, quality, {
                'pdf_path': path,
                'page_index': index,
                'page_count': total,
                'dpi': dpi,
                'width': pixmap.width,
                'height': pixmap.height,
            }, lang)
    except Exception as error:
        _LOG.exception('OCR PDF 页面识别失败: %s', path)
        return {'success': False, 'message': f'{type(error).__name__}: {error}'}


def status(with_engine=False):
    """OCR 是否可用、三档模型在不在、依赖版本、引擎有没有建好。"""
    info = {
        'success': True,
        'available': True,
        'default_quality': DEFAULT_QUALITY,
        'qualities': {},
        'default_lang': DEFAULT_LANG,
        'langs': {},
        'lang_choices': list(LANG_CHOICES),
        'model_root': model_root(),
        'cache_dir': OCR_CACHE_DIR,
        'versions': {},
        'engine_loaded': bool(_engines),
        'engine_qualities': sorted(_engines),
    }
    for package in ('opencv-contrib-python', 'opencv-python-headless', 'numpy',
                    'paddlepaddle', 'paddleocr', 'paddlex'):
        value = _version(package)
        if value:
            info['versions'][package] = value
    for quality, (det_name, rec_name) in _QUALITY_MODELS.items():
        missing = _missing_files(quality)
        info['qualities'][quality] = {
            'det_model': det_name,
            'rec_model': rec_name,
            'model_dir': _model_dir(det_name),
            'det_limit_side': _DET_LIMIT_SIDE,
            'missing': missing,
        }
        if missing:
            info['available'] = False
    for lang in LANG_CHOICES:
        rec_name = rec_model_name(DEFAULT_QUALITY, lang)
        missing = _missing_files(DEFAULT_QUALITY, lang)
        info['langs'][lang] = {
            'label': LANG_LABELS.get(lang, lang),
            'rec_model': rec_name,
            'shares_v6_models': rec_name == _QUALITY_MODELS[DEFAULT_QUALITY][1],
            'missing': missing,
        }
        if missing:
            info['available'] = False
    if with_engine:
        try:
            _ensure_engine(DEFAULT_QUALITY)
            info['engine_loaded'] = True
            info['engine_qualities'] = sorted(_engines)
        except Exception as error:
            info['success'] = False
            info['error'] = f'{type(error).__name__}: {error}'
    return info
