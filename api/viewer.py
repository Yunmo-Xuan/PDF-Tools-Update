import base64
import collections
import html
import json
import logging
import os
import threading

import webview

from core import text_layer
from core import winicon
from core import winclose
from core.i18n import t as _text

# 单次请求最多渲染多少页。前端的分批大小（PAGE_BATCH）与此一致；
# 超过就截断，避免有人直接传个大 count 把内存和 js 桥一起打爆。
MAX_PAGES_PER_REQUEST = 60
# 查看页能翻到多少页。这不是「一次加载多少页」——出图是前端按视口窗口取的，
# 这个数只管「页码清单给到第几页」，所以它撑得住五千页，靠的是清单小、图不多。
MAX_VIEWER_PAGES = 5000
# 含图页面（扫描件、照片页）改用 JPEG 的画质。
THUMB_JPEG_QUALITY = 75
# 放大预览的单页出图精度：读正文够用，也不会让一页的 base64 大到拖垮 js 桥
ZOOM_DPI = 150
ZOOM_WINDOW_SIZE = (1000, 780)
ZOOM_WINDOW_MIN_SIZE = (520, 400)
# 一次高清浏览最多攥多少格页序：三千页的册子整条序列才 60 KB，内存不是问题，
# 这里防的是前端算错传进来一条几十万项的列表，把 js 桥和会话一起压死。
ZOOM_MAX_SEQUENCE = 10000
# 文字层一次给多少页：与前端 PAGE_BATCH 同口径。可见窗口就两三屏，
# 再多就是给看不见的页白铺一层 DOM（每页几十到几百个 span）。
TEXT_LAYER_BATCH = 24
# 文字层缓存的页数为上界：一页实测 3~8 KB，600 页约 3 MB，够来回滚十几屏不重算；
# 再大就把 WebView2 的堆挤给这层缓存了，不值。
TEXT_LAYER_CACHE_PAGES = 600

_zoom_lock = threading.Lock()
_zoom_window = None
# 高清浏览会话（F24）：{path, seq: [(orig, rot), ...]}。
# 只记「这份文件打算长什么样」的页序和每格暂存的旋转角，不记图片也不记翻到第几格——
# 翻页要翻的是用户眼前的顺序而不是原始页码，不然调过序之后翻出来的页是错的；
# 位置交给窗口自己攥着，后端保持无状态，来回请求谁先谁后都不会把页码串位。
_zoom_session = None

# 文字层的 LRU：键是 (路径, mtime_ns, 字节数, 页序)。
# 把 mtime 与大小放进键，改过保存过的文件自然就不是同一份缓存——
# 编辑完再翻回去时读到旧一层字，比慢一点糟得多，所以这里不省这一次 stat。
_layer_lock = threading.Lock()
_layer_cache = collections.OrderedDict()


def _layer_cache_get(key):
    with _layer_lock:
        hit = _layer_cache.get(key)
        if hit is not None:
            _layer_cache.move_to_end(key)
        return hit


def _layer_cache_put(key, value):
    with _layer_lock:
        _layer_cache[key] = value
        _layer_cache.move_to_end(key)
        while len(_layer_cache) > TEXT_LAYER_CACHE_PAGES:
            _layer_cache.popitem(last=False)


def _layer_stamp(pdf_path):
    """缓存键里的文件指纹；量不到就退回 (0, 0)，让键只按路径与页序走。"""
    try:
        st = os.stat(pdf_path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _layer_envelope(pages, start, count, total):
    """给前端的固定外壳：坐标系、版本、区间都在这儿，前端不必猜这一份是谁家的。"""
    return {
        "success": True,
        "unit": text_layer.UNIT,
        "coord_space": text_layer.COORD_SPACE,
        "version": text_layer.LAYER_VERSION,
        "total": total,
        "start": start,
        "count": count,
        "pages": pages,
    }


def _clamp_dpi(dpi, default=100):
    """限制渲染精度，避免误传超大 dpi 直接把内存吃光。"""
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        return default
    return max(30, min(dpi, 300))


def _encode_page(page, dpi):
    """
    出一页的 data URL：纯文字页用 PNG，带图片的页面用 JPEG。

    这不是锦上添花，是体积差一个数量级的取舍（本机 1000 页样本实测）：
    文字版书籍 PNG 6.6 KB/页，同一页转 JPEG 要 42.7 KB；
    扫描版正相反，PNG 要 804 KB/页（一批 24 页就是 19 MB、4.2 秒），
    JPEG 只要 66 KB/页。以前一律 PNG，扫描书翻起来就是「渲染很慢」的那个手感。
    判定只看页面上挂没挂图，成本 0.2 ms/页。
    """
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    try:
        has_image = bool(page.get_images(full=False))
    except Exception:
        logging.debug("探测页面图片失败，按文字页处理: page=%s", getattr(page, "number", "?"),
                      exc_info=True)
        has_image = False
    if has_image:
        raw = pix.tobytes("jpg", jpg_quality=THUMB_JPEG_QUALITY)
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii")



def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _zoom_rotation(value):
    """放大预览只认 90 的倍数；返回 None 表示这个角度不该出图。"""
    try:
        angle = int(value)
    except (TypeError, ValueError):
        return None
    return angle if angle % 90 == 0 else None


def _zoom_sequence(sequence, total_pages):
    """
    把界面传来的页序 [[orig, rot], ...] 整理成 [(orig, rot), ...]；不能用来翻页时返回 None。

    界面传的是页面管理暂存的页序：那是用户眼前的顺序，删过页、调过序之后
    和文件里的原始页码已经不是一回事，翻页必须按它来。
    越界的 orig 直接丢掉——暂存的序列是前端算的，后端不能信到拿就用；
    角度不合法则整条序列作废（返回 None），不出「没转的那些页」，
    否则用户会以为暂存的旋转没生效。
    """
    if not sequence:
        return None
    items = []
    try:
        for item in sequence:
            orig = int(item[0])
            rot = _zoom_rotation(item[1] if len(item) > 1 else 0)
            if rot is None:
                return None
            if 0 <= orig < total_pages:
                items.append((orig, rot))
    except (TypeError, ValueError, IndexError):
        return None
    if not items:
        return None
    if len(items) > ZOOM_MAX_SEQUENCE:
        logging.warning(f"页序过长，截断到 {ZOOM_MAX_SEQUENCE} 格: 原长 {len(items)}")
        items = items[:ZOOM_MAX_SEQUENCE]
    return items


def _zoom_fallback_sequence(total_pages):
    """没有页序时的默认序列：整本书按原始顺序，一格都不转。"""
    return [(i, 0) for i in range(min(total_pages, ZOOM_MAX_SEQUENCE))]


def _zoom_start_pos(sequence, page_index):
    """起点格：在页序里找第一个指向这一格的位子，找不到返回 -1（页码不在这份文件里）。"""
    for pos, (orig, _rot) in enumerate(sequence):
        if orig == page_index:
            return pos
    return -1


def _zoom_state(file_name, pos, seq_len, orig, src_total):
    """首屏塞给窗口的会话状态；翻页后由窗口自己更新。"""
    return {
        'pos': pos,
        'total': seq_len,
        'orig': orig,
        'src_total': src_total,
        'file': file_name,
        'busy': False,
    }


# 放大预览窗口：底图是一张 PNG/JPEG，缩放和「左转右转」全在前端做，不再回后端要图。
# 翻页才回后端取下一页的高清图（一次一格，取整本会把内存打爆）。
# 用 token 替换而不是 f-string/格式化，是为了躲开 CSS 里成堆的花括号。
_ZOOM_TEMPLATE = '''<!DOCTYPE html>
<html lang="__LOCALE__">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; }
    body { display: flex; flex-direction: column; background: #23252c; color: #e8eaf0;
           font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
    .bar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; flex: none;
           background: #2c2f38; border-bottom: 1px solid #3b3f4a; }
    .bar .name { flex: 1; min-width: 0; font-size: 13px; color: #b9bfcc;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    button { background: #3b4050; color: #e8eaf0; border: none; border-radius: 6px;
             padding: 5px 10px; font-size: 13px; cursor: pointer; }
    button:hover { background: #4a5063; }
    button:disabled { opacity: .35; cursor: default; }
    .pager { min-width: 72px; }
    .zoom { min-width: 56px; }
    .pager, .zoom { text-align: center; font-size: 13px; font-variant-numeric: tabular-nums; }
    .sep { width: 1px; height: 20px; background: #3b3f4a; flex: none; }
    .stage { flex: 1; overflow: auto; padding: 18px; }
    /* 左转/右转只是「看」的动作，所以用 CSS transform 转图，不回后端重出图。
       外框按转之后的外接矩形占位，图在框里居中自转，滚动条才知道要多长。 */
    #frame { position: relative; margin: 0 auto; }
    #page { position: absolute; left: 50%; top: 50%; background: #fff;
            box-shadow: 0 6px 18px rgba(0, 0, 0, .45); transform: translate(-50%, -50%); }
</style>
</head>
<body>
    <div class="bar">
        <button id="prev" onclick="go(-1)">__PREV__</button>
        <span class="pager" id="pager">1 / 1</span>
        <button id="next" onclick="go(1)">__NEXT__</button>
        <span class="sep"></span>
        <button onclick="bump(1 / 1.25)">－</button>
        <span class="zoom" id="zoom">100%</span>
        <button onclick="bump(1.25)">＋</button>
        <button onclick="fit()">__FIT__</button>
        <button onclick="setScale(1)">__ACTUAL__</button>
        <span class="sep"></span>
        <button onclick="spin(-90)" title="__ROT_L__">↺</button>
        <button onclick="spin(90)" title="__ROT_R__">↻</button>
        <span class="name" id="name">__NAME__</span>
        <button onclick="closeIt()">__CLOSE__</button>
    </div>
    <div class="stage" id="stage"><div id="frame"><img id="page" src="__IMAGE__" alt="__ALT__"></div></div>
<script>
    var stage = document.getElementById('stage');
    var frame = document.getElementById('frame');
    var img = document.getElementById('page');
    var state = __STATE__;
    // 这一页的文字全部由后端按界面语言填进来（十本字典里 zoom_* 那十几条）。
    var T = __TEXTS__;
    // 不用 replace 的第二个参数直接传值：文件名里带 $& 会被 replace 当成特殊记号展开。
    // {0} {1} … 依次取后面的实参。用 split/join 而不是 replace：
    // replace 的第二个参数里出现 $& 会被当成特殊记号，而文件名里完全可能有 $。
    function fmt(tpl) {
        var out = String(tpl);
        for (var i = 0; i + 1 < arguments.length; i++) {
            out = out.split('{' + i + '}').join(String(arguments[i + 1]));
        }
        return out;
    }
    var scale = 1;
    var viewRot = 0;
    // 首屏按「适应窗口」打开，之后用户手动调过大小就保持他的倍数
    var autoFit = true;
    // 图还在路上时按下的那一步先记下来，画完这一格就补上（见 go()）
    var pendingStep = 0;

    function api() { return (window.pywebview || {}).api || null; }

    // 横过来那一格占的是竖过来的尺寸：缩放基准和占位尺寸都得跟着视角换边
    function boxW() { return viewRot % 180 ? img.naturalHeight : img.naturalWidth; }
    function boxH() { return viewRot % 180 ? img.naturalWidth : img.naturalHeight; }

    function fitScale() {
        if (!img.naturalWidth) return 1;
        var avail = stage.clientWidth - 36;
        return Math.max(0.05, Math.min(4, avail / boxW()));
    }

    function layout() {
        var w = Math.round(img.naturalWidth * scale);
        var h = Math.round(img.naturalHeight * scale);
        img.style.width = w + 'px';
        img.style.height = h + 'px';
        img.style.transform = 'translate(-50%, -50%) rotate(' + viewRot + 'deg)';
        // 竖着看时横边是 h、竖边是 w，外框不跟着换就会把图裁掉一半
        frame.style.width = Math.round(viewRot % 180 ? h : w) + 'px';
        frame.style.height = Math.round(viewRot % 180 ? w : h) + 'px';
        document.getElementById('zoom').innerText = Math.round(scale * 100) + '%';
    }

    function setScale(value) { autoFit = false; scale = Math.max(0.05, Math.min(8, value)); layout(); }
    function bump(factor) { setScale(scale * factor); }
    function fit() { autoFit = true; scale = fitScale(); layout(); }
    function spin(delta) { viewRot = (viewRot + delta + 360) % 360; layout(); }

    function paint() {
        document.getElementById('pager').innerText = (state.pos + 1) + ' / ' + state.total;
        // 文件名走 textContent，路径里的尖括号之类不会被当标签解析
        document.getElementById('name').textContent =
            fmt(T.line, state.file, state.orig + 1, state.src_total);
        document.getElementById('prev').disabled = state.pos <= 0;
        document.getElementById('next').disabled = state.pos >= state.total - 1;
    }

    function setBusy(value) {
        state.busy = value;
        document.getElementById('prev').disabled = value || state.pos <= 0;
        document.getElementById('next').disabled = value || state.pos >= state.total - 1;
    }

    // 翻页回后端要图：一次只要一格的 150 dpi，比把整本提前出图省两个数量级
    // 连按 → 的时候，后一次往往落在「上一张还在路上」那一瞬。以前直接 return，
    // 用户看到的就是「按了没反应」——机器一忙能连着吞掉好几下（实测吞过 Z8/Z9 那两步）。
    // 现在把这一步记下来，这一格画完立刻补上；连按几次就往前/往后几步，不会攒出跳页。
    function go(step) {
        if (state.busy) { pendingStep += step; return; }
        // 连按到尽头那几下的意图是「翻到最后那一格」，不是「什么都不做」：
        // 把目标夹在页序里，到了头就停在那一格，剩下的那几步作废。
        var target = Math.min(state.total - 1, Math.max(0, state.pos + step));
        step = target - state.pos;
        if (!step) { pendingStep = 0; return; }
        var bridge = api();
        // 桥不是一开始就全的：pywebview 先挂出 window.pywebview.api 这个壳，方法要等
        // ready 之后才填进去。窗刚开出来的那一瞬按 → 会走到这里，`bridge` 有了但
        // `get_zoom_page` 还不是函数，直接调就是 TypeError（复验时炸在这行）。
        // 和「图还在路上」同一处理：记一步，桥齐了再补。
        if (!bridge || typeof bridge.get_zoom_page !== 'function') {
            pendingStep += step;
            return;
        }
        setBusy(true);
        bridge.get_zoom_page(target).then(function (res) {
            if (res && res.success) {
                state.pos = res.pos;
                state.orig = res.orig;
                state.total = res.total;
                state.src_total = res.src_total;
                // 翻到的是另一张纸，不该继承上一页的歪法
                viewRot = 0;
                // 换 src 之后由下面那个统一的 load 回调负责排版并解除忙碌
                img.src = res.image;
                paint();
            } else {
                // 取图失败就把记下的那一步丢掉：这一格没换成，紧接着再要一次只会重复同一个错，
                // 用户看到的是那句失败提示，不是页面凭空跳走。
                pendingStep = 0;
                setBusy(false);
                document.getElementById('name').textContent =
                    fmt(T.failed, (res && res.message) || T.unknown);
            }
        }).catch(function () {
            pendingStep = 0;
            setBusy(false);
        });
    }

    // 补上「图在路上时按下的那一步」：忙碌解除才有意义。取图失败的那条路不补——
    // 那一格没换成，紧接着再要一次只会重复同一个错，那一步在失败分支里已经清掉了。
    function flushPending() {
        var step = pendingStep;
        pendingStep = 0;
        if (step) go(step);
    }

    function onImageLoad() {
        // 页面尺寸可能和上一页不同，适应窗口状态下要重算，否则长横幅页会溢出
        if (autoFit) fit(); else layout();
        stage.scrollTop = 0;
        stage.scrollLeft = 0;
        setBusy(false);
        flushPending();
    }

    // Ctrl + 滚轮缩放，普通滚轮照常翻页；preventDefault 免得浏览器整页放大
    stage.addEventListener('wheel', function (event) {
        if (!event.ctrlKey) return;
        event.preventDefault();
        bump(event.deltaY < 0 ? 1.15 : 1 / 1.15);
    }, { passive: false });

    // ← → 翻页、Esc 关窗：连续看几十页时比去够鼠标快得多
    document.addEventListener('keydown', function (event) {
        if (event.key === 'ArrowLeft') { event.preventDefault(); go(-1); }
        else if (event.key === 'ArrowRight') { event.preventDefault(); go(1); }
        else if (event.key === 'Escape') closeIt();
    });

    img.addEventListener('load', onImageLoad);
    // 桥齐了就把「窗刚开出来那一瞬按下的那一步」补上，不然那一步永远留在账上没人做
    document.addEventListener('pywebviewready', function () { flushPending(); });
    window.addEventListener('resize', function () { if (autoFit) fit(); });
    function closeIt() {
        if (window.pywebview && window.pywebview.api) window.pywebview.api.close_zoom_window();
    }
    paint();
    if (img.naturalWidth) onImageLoad();
</script>
</body>
</html>'''


def _zoom_title(state):
    """窗口标题：「放大预览 · 第 N / M 页」这一句跟着界面语言走（十本字典的 zoom_title）。"""
    return _text('zoom_title').format(state['orig'] + 1, state['src_total'])


def _zoom_html(state, image_url):
    """
    把首屏的图和会话状态塞进模板。

    标题走 _zoom_title；界面里那一行的文字和前端 paint() 排出来的用同一个格式，
    免得脚本跑起来前后跳字。文件名要转义，路径里的 & 之类不能当标签吐出来。
    这一页是后端拼出来的 HTML，拿不到界面的语言包，所以按钮、悬浮提示、
    还有前端要现拼的那几句全在这儿按当前语言填好（他 2026-10-31 圈的「语言没统一」
    就包括这一窗：英文界面下点开放大预览，工具栏整条是中文）。
    """
    name = _text('zoom_name_line').format(html.escape(state['file']),
                                          state['orig'] + 1, state['src_total'])
    front_texts = json.dumps({'line': _text('zoom_name_line'),
                              'failed': _text('zoom_load_failed'),
                              'unknown': _text('zoom_unknown_error')},
                             ensure_ascii=False)
    return (_ZOOM_TEMPLATE
            .replace('__LOCALE__', _text('app_locale'))
            .replace('__TITLE__', _zoom_title(state))
            .replace('__PREV__', _text('zoom_prev'))
            .replace('__NEXT__', _text('zoom_next'))
            .replace('__FIT__', _text('zoom_fit'))
            .replace('__ACTUAL__', _text('zoom_actual'))
            .replace('__CLOSE__', _text('zoom_close'))
            .replace('__ROT_L__', _text('zoom_rotate_left_tip'))
            .replace('__ROT_R__', _text('zoom_rotate_right_tip'))
            .replace('__TEXTS__', front_texts)
            .replace('__NAME__', name)
            .replace('__ALT__', _text('zoom_alt').format(state['orig'] + 1))
            .replace('__STATE__', json.dumps(state, ensure_ascii=False))
            .replace('__IMAGE__', image_url))


def _on_zoom_closed(*args):
    global _zoom_window, _zoom_session
    with _zoom_lock:
        _zoom_window = None
        # 会话跟着窗口走：窗口没了还攥着页序，下次翻页就会对着一个已经不存在的界面出图
        _zoom_session = None


class ViewerMixin:
    def render_page_range(self, pdf_path, start_index, count=24, dpi=100):
        """
        渲染 [start_index, start_index + count) 这一段页面，返回 data URL 列表。

        页面清单必须由 get_pdf_page_count 的真实总数构建，再由这里按需补齐：
        一次性渲染会在大文档上被截断，而截断结果一旦被当成页面清单，
        保存时就会写出一个缺页的文件。分批同时让单次内存可控。

        每一项都是能直接塞给 img.src 的 data URL（文字页 PNG、含图页 JPEG），
        前端不必猜编码格式。长度可能短于 count（已到文档末尾）；
        失败返回空列表，具体原因看日志。
        """
        import pymupdf as fitz
        try:
            dpi = _clamp_dpi(dpi, 100)
            start = _to_int(start_index, -1)
            count = _to_int(count, 0)
            if start < 0 or count <= 0:
                return []
            count = min(count, MAX_PAGES_PER_REQUEST)

            doc = fitz.open(pdf_path)
            try:
                total = doc.page_count
                if start >= total:
                    return []
                stop = min(start + count, total)
                logging.info(f"分批渲染: {pdf_path}, 区间=[{start},{stop}), dpi={dpi}, 总页数={total}")
                return [_encode_page(doc[i], dpi) for i in range(start, stop)]
            finally:
                doc.close()
        except Exception:
            logging.exception(f"render_page_range 失败: {pdf_path} start={start_index} count={count}")
            return []

    def render_page(self, pdf_path, page_index, dpi=150):
        """渲染单页为 data URL；失败或页码越界时返回空字符串。"""
        import pymupdf as fitz
        try:
            dpi = _clamp_dpi(dpi, 150)
            try:
                page_index = int(page_index)
            except (TypeError, ValueError):
                return ""

            doc = fitz.open(pdf_path)
            try:
                if page_index < 0 or page_index >= doc.page_count:
                    return ""
                return _encode_page(doc[page_index], dpi)
            finally:
                doc.close()
        except Exception:
            logging.exception(f"render_page 失败: {pdf_path} page={page_index}")
            return ""

    def get_pdf_page_count(self, pdf_path):
        """返回页数；读取失败或需要密码时返回 -1。"""
        import pymupdf as fitz
        try:
            doc = fitz.open(pdf_path)
            try:
                # 加密文档 fitz 能打开，但 page_count 会是 0：不区分的话界面
                # 显示成一个「空白的 PDF」，用户看不出是加密还是坏了。
                if doc.needs_pass:
                    logging.info(f"PDF 需要密码，页数不可读: {pdf_path}")
                    return -1
                return doc.page_count
            finally:
                doc.close()
        except Exception:
            logging.exception(f"get_pdf_page_count 失败: {pdf_path}")
            return -1

    def get_page_sizes(self, pdf_path, max_pages=MAX_VIEWER_PAGES):
        """
        一次性读回每页的宽高（pt），给查看页排版面用；只量尺寸、不出图。

        为什么要单独给这个：查看页要能翻到五千页，就不能「滚到哪才认识哪一页」——
        滚动条长度和「跳到第 N 页」都得在出图之前算得出来，而尺寸是便宜的
        （5000 页读 rect 约几十毫秒，返回的数不到 100 KB），出图才是贵的。
        页旋转已经算在 rect 里（page.rect 是旋转后的外接框），前端不必再管。

        返回 {"success", "total", "limit", "sizes": [[宽, 高], ...]}；
        sizes 只给到 limit（=min(max_pages, MAX_VIEWER_PAGES)）为止，
        total 仍是真实页数，前端据此决定要不要写「后面还有多少页没给」。
        """
        import pymupdf as fitz
        try:
            limit = _to_int(max_pages, MAX_VIEWER_PAGES)
            limit = max(1, min(limit, MAX_VIEWER_PAGES))
            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    return {"success": False, "total": 0, "limit": limit, "sizes": [],
                            "message": _text('api_viewer_locked')}
                total = doc.page_count
                sizes = []
                for index in range(min(total, limit)):
                    rect = doc[index].rect
                    sizes.append([round(rect.width, 1), round(rect.height, 1)])
                return {"success": True, "total": total, "limit": limit, "sizes": sizes}
            finally:
                doc.close()
        except Exception as error:
            logging.exception(f"get_page_sizes 失败: {pdf_path}")
            return {"success": False, "total": 0, "limit": _to_int(max_pages, MAX_VIEWER_PAGES),
                    "sizes": [], "message": "%s: %s" % (type(error).__name__, error)}

    def _layers(self, pdf_path, start_index, count):
        """取 [start, start+n) 这几页的文字层；一次开档，命中的页走缓存。

        为什么按批：查看页一次要看两三屏，逐页回后端要就是两三趟开档 + 桥往返，
        按批只要一趟。为什么缓存：来回滚同一屏时 rawdict 那一下虽只要几毫秒，
        但每次重算会让滚动条上多出一串可感知的停顿，而这一层的内容根本不会变。
        """
        import pymupdf as fitz
        start = _to_int(start_index, -1)
        want = _to_int(count, TEXT_LAYER_BATCH)
        envelope = {"success": False, "unit": text_layer.UNIT,
                    "coord_space": text_layer.COORD_SPACE,
                    "version": text_layer.LAYER_VERSION,
                    "total": 0, "start": max(0, start), "count": 0, "pages": []}
        if start < 0:
            envelope["message"] = _text('api_viewer_page_invalid')
            return envelope
        want = max(1, min(want, TEXT_LAYER_BATCH))
        try:
            doc = fitz.open(pdf_path)
        except Exception as error:
            logging.exception(f"取文字层失败（打不开文件）: {pdf_path}")
            envelope["message"] = _text('api_viewer_layer_failed')
            envelope["detail"] = "%s: %s" % (type(error).__name__, error)
            return envelope
        try:
            if doc.needs_pass:
                envelope["message"] = _text('api_viewer_locked')
                return envelope
            total = doc.page_count
            if start >= total:
                envelope["message"] = _text('api_viewer_page_range') % total
                return envelope
            stop = min(start + want, total)
            stamp = _layer_stamp(pdf_path)
            pages = []
            for index in range(start, stop):
                key = (pdf_path, stamp[0], stamp[1], index)
                hit = _layer_cache_get(key)
                if hit is not None:
                    pages.append(hit)
                    continue
                try:
                    layer = text_layer.build_layer(doc[index], page_number=index + 1)
                except Exception:
                    # 单页坏了不拖整批：那一页没层就是选不了字，比整屏都铺不出来好
                    logging.exception(f"取文字层失败（跳过这一页）: page={index}")
                    continue
                _layer_cache_put(key, layer)
                pages.append(layer)
            return _layer_envelope(pages, start, stop - start, total)
        finally:
            doc.close()

    def get_text_layer_range(self, pdf_path, start_index, count=TEXT_LAYER_BATCH):
        """
        查看页的可选文字层：一批页面的文字框，坐标是渲染 pt（左上原点、y 向下、/Rotate 已转正）。

        返回 {"success", "unit", "coord_space", "version", "total", "start", "count",
              "pages": [{...见 core/text_layer.build_layer...}]}。
        前端只要拿「这一页占的 px 宽 ÷ 这一页的 pt 宽」一个系数就能把框铺到图上，
        缩放与换页都不必回后端重取——这是它和 render_page_range 的分工：
        图按精度重出，框只按系数重排。
        """
        return self._layers(pdf_path, start_index, count)

    def get_text_layer(self, pdf_path, page_index):
        """单页文字层（放大预览那一窗用）；外壳与批量那一个完全同形，前端只读 pages[0]。"""
        return self._layers(pdf_path, page_index, 1)

    def open_page_zoom(self, pdf_path, page_index, rotation=0, sequence=None):
        """
        开一个独立窗口高清浏览某一页，并把这份文件的页序交给窗口连着翻（F24）。

        page_index 始终是「原始页码」：右键菜单和已有的窗口标题断言都按它说话。
        sequence 是界面暂存的页序 [[orig, rot], ...]，传了就在这一串格子里翻页，
        因为用户要翻的是他眼前那一摞的顺序；没传就退化成整本书按原始顺序翻，
        并把 rotation 记在起点那一格上，保持 6.0.x 单页放大的行为不变。
        rotation 是这一格的增量角度：改动还没落盘，出图前先在内存里把这页转到位，
        否则预览看到的和待保存的不是同一回事。角度不合法就拒绝出图，
        不返回「没转的那一页」——用户会以为暂存的旋转没生效。
        """
        global _zoom_window, _zoom_session
        import pymupdf as fitz
        try:
            index = int(page_index)
        except (TypeError, ValueError):
            return {"success": False, "message": _text('api_viewer_page_invalid')}
        rot = _zoom_rotation(rotation)
        if rot is None:
            return {"success": False, "message": _text('api_viewer_rot_invalid')}

        try:
            doc = fitz.open(pdf_path)
            try:
                if doc.needs_pass:
                    return {"success": False, "message": _text('api_viewer_pw_needed')}
                total = doc.page_count
                if index < 0 or index >= total:
                    return {"success": False,
                            "message": _text('api_viewer_page_range') % total}
                seq = _zoom_sequence(sequence, total)
                if seq is None:
                    # 没给序列（或序列全被判废）就按原始页码翻整本，起点单独转
                    seq = _zoom_fallback_sequence(total)
                    seq[index] = (index, rot)
                pos = _zoom_start_pos(seq, index)
                if pos < 0:
                    return {"success": False,
                            "message": _text('api_viewer_not_in_seq')}
                orig, slot_rot = seq[pos]
                page = doc[orig]
                if slot_rot % 360:
                    page.set_rotation((page.rotation + slot_rot) % 360)
                image_url = _encode_page(page, ZOOM_DPI)
            finally:
                doc.close()
            if not image_url:
                return {"success": False, 'message': _text('api_page_render_failed')}
        except Exception as error:
            logging.exception(f"open_page_zoom 失败: {pdf_path} page={page_index}")
            return {"success": False, "message": str(error)}

        state = _zoom_state(os.path.basename(pdf_path), pos, len(seq), orig, total)
        with _zoom_lock:
            _zoom_session = {"path": pdf_path, "seq": seq}
        content = _zoom_html(state, image_url)
        title = _zoom_title(state)
        try:
            with _zoom_lock:
                window = _zoom_window
                reused = window is not None
                if reused:
                    # 复用同一个窗口换页，免得用户连点十次攒出十个窗口
                    window.load_html(content)
                    window.set_title(title)
                else:
                    _zoom_window = webview.create_window(
                        title,
                        html=content,
                        js_api=self,
                        width=ZOOM_WINDOW_SIZE[0],
                        height=ZOOM_WINDOW_SIZE[1],
                        min_size=ZOOM_WINDOW_MIN_SIZE,
                        resizable=True,
                        background_color='#23252c',
                    )
                    _zoom_window.events.closed += _on_zoom_closed
                    winicon.attach(_zoom_window, 'zoom')
                    window = _zoom_window
        except Exception as error:
            logging.exception("创建放大预览窗口失败")
            with _zoom_lock:
                _zoom_window = None
                _zoom_session = None
            return {"success": False, "message": str(error)}

        try:
            window.show()
            window.restore()
        except Exception:
            logging.exception("放大预览窗口显示失败")

        logging.info(f"放大预览: {pdf_path} 第 {orig + 1}/{total} 页，可翻 {len(seq)} 格"
                     f"（{'复用窗口' if reused else '新建窗口'}）")
        return {"success": True, "reused": reused, "total": len(seq), "pos": pos}

    def get_zoom_page(self, seq_index):
        """
        高清浏览窗口翻页用：按会话里的页序出第 seq_index 格（F24）。

        会话只在取出来那一下加锁，渲染放锁外——一页 150 dpi 要算几百毫秒，
        抱着锁算会把「关闭」按钮卡成点了没反应。位置由窗口自己攥着，
        后端无状态，两次翻页请求谁先回来都不影响出图内容。
        """
        import pymupdf as fitz
        try:
            pos = int(seq_index)
        except (TypeError, ValueError):
            return {"success": False, "message": _text('api_viewer_seq_invalid')}
        with _zoom_lock:
            session = dict(_zoom_session) if _zoom_session else None
        if not session:
            return {"success": False, "message": _text('api_viewer_session_gone')}
        seq = session["seq"]
        if pos < 0 or pos >= len(seq):
            return {"success": False, "message": _text('api_viewer_at_end')}
        orig, slot_rot = seq[pos]
        try:
            doc = fitz.open(session["path"])
            try:
                total = doc.page_count
                if orig >= total:
                    # 文件在预览期间被换过或改短了：宁可报错，也不翻出一页错的内容
                    return {"success": False,
                            "message": _text('api_viewer_pages_changed') % total}
                page = doc[orig]
                if slot_rot % 360:
                    page.set_rotation((page.rotation + slot_rot) % 360)
                image_url = _encode_page(page, ZOOM_DPI)
            finally:
                doc.close()
        except Exception as error:
            logging.exception(f"get_zoom_page 失败: {session['path']} pos={pos}")
            return {"success": False, "message": str(error)}
        if not image_url:
            return {"success": False, 'message': _text('api_page_render_failed')}
        return {
            "success": True,
            "pos": pos,
            "orig": orig,
            "page": orig + 1,
            "total": len(seq),
            "src_total": total,
            "file": os.path.basename(session["path"]),
            "image": image_url,
        }

    def close_zoom_window(self):
        """关掉放大预览窗口（窗口里的「关闭」按钮调用）。"""
        global _zoom_window, _zoom_session
        with _zoom_lock:
            window = _zoom_window
            _zoom_window = None
            _zoom_session = None
        if window is None:
            return {"success": True}
        try:
            # destroy 可能同步触发 closed 回调，必须在锁外调用，否则自锁
            winclose.hide(window)   # 同上：先撤下屏幕，别让「已关」的窗口继续挂着
            window.destroy()
        except Exception:
            logging.exception("关闭放大预览窗口失败")
            return {"success": False, "message": _text('api_viewer_close_failed')}
        return {"success": True}
