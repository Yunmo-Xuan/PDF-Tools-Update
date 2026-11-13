"""拍照文档变清晰：把手机拍的那张纸处理成扫描仪出来的样子（对标扫描王/ CamScanner）。

三档就是扫描 App 面板上的三个按钮：
  magic  彩色增强  保住颜色（红章、荧光笔、彩色图表还在），去阴影 + 白平衡 + 提对比；
  gray   灰度      去阴影后拉成满量程灰度，字黑纸白但还有层次；
  binary 黑白      自适应阈值成纯黑白，最像激光打印稿，压得也最小。

三个开关是可选的三段几何处理，都在 enhance_image 的关键字参数上（option_labels 给界面）：
  perspective 摆正斜拍  纸拍成梯形时按四个角做一次单应，把纸拉回矩形再往下走；
                        认不出可信四角就原样交回去 —— 猜出来的梯形拉完是黑页或糊页，
                        比留一圈桌面边难看得多。
  deskew      自动纠偏  量字行的众数角，最多补两趟转平。
  crop        裁边      找出纸的外接框，把框外的桌面切掉（透视拉过之后一般无事可做）。

真正起作用的一步是「估背景再相除」：照片不干净不是因为分辨率，是光照不均——
四角暗、手影、纸的起伏。把这些当成一幅低频的乘法光照场 bg(x,y)，
逐像素算 out = 原图 / bg * 255，纸面就被拉回接近纯白，阴影和暗角整片消失，
而笔画比同一位置的白纸暗一个固定倍数，除完还是暗的，字因此保住。
全局阈值做不到的正是这一点：暗角里的纸和亮处的纸亮度差三四倍，
一条阈值线只能让其中一半变白（所以别把它退化成一个 threshold）。

numpy / cv2 都按需 import 在函数里：这一整块是可选能力，不该为它付 1–2 秒的启动开销
（同 core/ocr.py 里引擎延后建、Paddle 只在真要识别时才 import 是一个道理）。

内存：这机器只有 4 GB，一页 4000×3000 的 RGB 就是 36 MB，背景估计、相除、锐化
每一步还要再开同尺寸的副本，一页能吃到 200 MB 以上；所以所有重型算子之前先把
长边压到 work_side（默认 1600 ≈ 10×13 页 @115dpi，出 A4 扫描件够清晰），
并且调用方保证一次只有一页在手里（见 api/enhance.py 的逐页循环）。

进出约定：数组一律 BGR、uint8（PyMuPDF 出的 PNG 解码完就是它）；
给界面看的入口都在 api/enhance.py，本模块里除 enhance_image / 编解码之外的
公开函数都返回纯类型。读不出来的输入直接抛 ValueError（消息是给人看的中文），
由 api 层收成 {'success': False, 'message': ...}。
"""

import logging

# 顺序就是界面上三个按钮的顺序；加一档只动这里。
MODES = {
    'magic': '彩色增强',
    'gray': '灰度',
    'binary': '黑白',
}
DEFAULT_MODE = 'magic'
MAGIC, GRAY, BINARY = 'magic', 'gray', 'binary'

# 重型算子之前统一压到的长边。上限 2400 不是画质考虑，是内存考虑：
# 2400×1800 的 float 中间量一份就是 50 MB。
WORK_SIDE = 1600
MIN_WORK_SIDE = 480
MAX_WORK_SIDE = 2400
# 背景场在下面这个小图上加起来算，再放回原尺寸：闭运算的核必须大过整段正文，
# 全尺寸下这核得 100 像素见方，开一次能把界面卡住。
# 220 试过：太小，纸外那圈桌面被借来的亮背景糊成灰框，裁边也就不认得纸在哪了；
# 400 已经够把「核 > 正文」和「带 > 纸边」这两头都占住，再大只是慢。
BG_SIDE = 400
# 纠偏只在 ±MAX_DESKEW 内转：超过这个数就不是「拍歪了」，是 Hough 被桌面花纹骗了，
# 硬转会把整页抹掉（同 api/ocr.py::_preprocess 里 0.3 < |角| < 15 那条守卫的口径）。
MIN_DESKEW = 0.3
MAX_DESKEW = 15.0
# 白平衡只认这个亮度以上的像素为「纸」（除完背景之后，纸基本在 220 以上）。
WB_MIN_LUMA = 170
# 纠偏最多几趟：一趟转完还剩 0.5°～1°（Hough 的读数受插值和残余阴影影响），
# 第二趟补掉，实测两趟之后读数稳定在 0.1° 以内，第三趟没有意义。
DESKEW_PASSES = 2

JPEG_QUALITY = 88
LOG = logging.getLogger(__name__)

# ------------------------------------------------------------ 透视矫正的门限
# 这一步做错的代价（整页拉花、切掉骑缝章）比不做的代价（留一圈桌面边）大得多，
# 所以每一条都是「认不准就不矫」的方向，而且全部回原图、不回半张黑页。
MIN_QUAD_AREA = 0.30
# 上限卡在 98.5%：几乎占满画面的那个四边形其实是照片自己的边界（拍照时纸被裁到框外，
# 四条边都贴在画幅上），照着它拉等于把一圈桌面透视进画面正中。
MAX_QUAD_AREA = 0.985
# 四条边都要够长（相对画面短边）。细长一条的是纸缝、桌沿、被压住的折痕，
# 这种形状上四个角的位置几乎不受轮廓约束，单应解出来抖得厉害。
MIN_QUAD_SIDE_RATIO = 0.18
# 内角必须落在 42°–138°：纸被拍成梯形，最斜的那个角也就 60° 上下（手机不可能贴着桌面拍），
# 再斜就是自交或退化成一条带子（approxPolyDP 在噪声轮廓上抖出来的假角）。
MIN_QUAD_ANGLE = 42.0
# 单应矩阵（按画面长边归一化后）的条件数上限。它是「这四点到底有多透视」的量：
# 正对拍 ≈ 1，常规斜拍 2–6，接近消视线（有一角快翻到背面去）会冲到几十以上。
MAX_QUAD_COND = 12.0
# 四角的 w 分量（齐次除法的那个除数）最大/最小之比。线性函数在凸四边形上的极值只在角上取，
# 所以这个比值就是「消视线离纸有多近」的量：比值大说明有一角要穿过无穷远，拉出来必糊。
MAX_QUAD_W_RATIO = 6.0
# approxPolyDP 的几档容差（占轮廓周长的百分比）。一条容差不够用：
# 纸边干净时 1% 就压成四点，边上有反光、角被手指压住时要 4% 才收成四点。
QUAD_EPSILON_RATIOS = (0.010, 0.020, 0.030, 0.045)
# 找边缘时连通断口用的膨胀量（和 _page_box 里的 grow 同一个口径）。轮廓是量在
# 「膨胀过的边」上的，而且 Canny 之前先高斯模糊过一轮，纸/桌面的过渡带被铺开成
# ±2 px —— 四角只收 2 px 还整条落在过渡带里，拉完成品边上留一圈灰黑缝，
# 黑白档把它阈值成一绕页的黑边（就是 _page_box 注释里躲的那个「画框」）。
# 实测收 4 px：画面最外一行/列的平均从 163 升到 246，缝没了，代价是纸边少掉 4 px
# （量出来的四角离真四角从 1.4 px 变成 5 px，相对 1520 的画面是 0.3%）。
QUAD_GROW = 4


def mode_labels():
    """给界面下拉框：[{value, label}, ...]，顺序即展示顺序，纯类型能直接过 js 桥。"""
    return [{'value': key, 'label': MODES[key]} for key in MODES]


def option_labels():
    """三个勾选项的清单（同 mode_labels 的用途）：界面对上这一张表就是完整的一组开关。

    顺序即界面上从上到下的顺序；value 一律用 enhance_image 的关键字名，
    api 层可以直接 **kwargs 传下去，不需要在两边各维护一份映射。
    """
    return [{'value': 'perspective', 'label': '摆正斜拍页面（把梯形拉回矩形）'},
            {'value': 'deskew', 'label': '自动纠偏（摆正拍歪的页面）'},
            {'value': 'crop', 'label': '裁掉页面以外的背景'}]


def mode_label(mode):
    """一档的中文名；认不出的值按默认档回，界面不该因为一个脏参数报错。"""
    return MODES.get(str(mode or DEFAULT_MODE).strip().lower(), MODES[DEFAULT_MODE])


def normalize_mode(mode):
    key = str(mode or DEFAULT_MODE).strip().lower()
    if key not in MODES:
        raise ValueError('不认识的处理档: %s（只有 %s）' % (mode, ' / '.join(MODES)))
    return key


def _as_bgr(image):
    """把进来的数组统一成连续的三通道 uint8；通道数、类型不对都在这一处挡掉。"""
    import cv2
    import numpy as np

    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif arr.shape[2] == 4:
        # 带透明的页面（截图存的 PNG）：alpha 没有信息，铺白丢掉，
        # 留着除背景时会得到一片可疑的 0。
        arr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    elif arr.shape[2] == 1:
        arr = cv2.cvtColor(arr[:, :, 0], cv2.COLOR_GRAY2BGR)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError('要处理的图像不是一张 BGR 图')
    if min(arr.shape[:2]) < 24:
        raise ValueError('图像太小（短边不足 24 像素），没法可靠地去阴影')
    return np.ascontiguousarray(arr)


def fit_long_side(image, side=WORK_SIDE):
    """长边压到 side（INTER_AREA）；已经够小就原样回，绝不放大。"""
    import cv2

    side = _clamp_side(side)
    height, width = image.shape[:2]
    long_side = max(height, width)
    if long_side <= side:
        return image
    scale = float(side) / long_side
    # INTER_AREA 是唯一能一边缩一边做低通的插值；用 INTER_LINEAR 缩会把字边缘
    # 变成马赛克，后面二值化的时候毛刺全冒出来了。
    return cv2.resize(image, (max(1, int(round(width * scale))),
                              max(1, int(round(height * scale)))),
                      interpolation=cv2.INTER_AREA)


def _clamp_side(side):
    try:
        side = int(side)
    except (TypeError, ValueError):
        return WORK_SIDE
    return max(MIN_WORK_SIDE, min(MAX_WORK_SIDE, side))


def _odd(value, floor):
    """取一个不小于 floor 的奇数核尺寸（morphology/adaptiveThreshold 都要奇数）。"""
    size = int(max(floor, value))
    return size if size % 2 else size + 1


def _estimate_bg(gray):
    """估光照场 bg(x,y)：和纸面同色、但没有字的低频版本。

    在 BG_SIDE 的小图上做闭运算，再中值、平滑，最后放回原尺寸。方向别记反：
    **闭运算**（先膨胀后腐蚀）吃的是「比核小的暗东西」= 笔画，开运算吃的是亮东西，
    用开运算等于把白点当阴影、把整段正文留在背景里，除完正文位置直接变纯白——字就没了。
    """
    import cv2
    import numpy as np

    height, width = gray.shape[:2]
    scale = float(BG_SIDE) / max(height, width)
    small = gray if scale >= 1.0 else cv2.resize(
        gray, (max(8, int(round(width * scale))), max(8, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA)
    short = min(small.shape[:2])
    # 核要盖住整行正文（正文在小图上只有几个像素高），但盖不住整页，
    # 否则连纸的弯折、手影这种「真的该保留的明暗」也被抹平了。
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (_odd(short / 8, 7), _odd(short / 12, 5)))
    closed = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
    med = cv2.medianBlur(small, _odd(short / 16, 5))
    # 两份都是「亮底」估计，取更亮的那个：背景偏亮只是字更黑，
    # 背景偏暗（除数小）会把整片正文提成白的，字直接消失。
    bg = cv2.max(closed, med)
    # 上限 = 小半径的局部最大值。闭运算的核很大，会把远处的亮纸「借」到窄窄的桌面边、
    # 纸缝上——除完那条边变成一圈灰黑框，成品像蒙了个镜框（实测第一版就是这样，
    # 也因此裁边找不到纸的边界）。局部最大只允许背景用自己附近几格的亮度，
    # 窄带中心就回到自己该有的值；大片正文处两者本来几乎相等，等于不干预。
    # 半径按「比最粗的笔画大一点、比纸外那条带子窄得多」取：短边 1/80 ≈ 全尺寸 15 px，
    # 再大就开始把 20 px 粗的黑框、印章实底当成背景借走（那些地方除完会变白，字就淡了）。
    cap = cv2.dilate(small, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (_odd(short / 80, 3), _odd(short / 80, 3))))
    bg = cv2.min(bg, cap)
    bg = cv2.medianBlur(bg, 5)
    # 光照场必须是平滑的：留着高频就会在字边上出现一圈白 halo。
    # 这一刀在小图上抹，sigma≈short/14（放回原尺寸等于 8 倍大）——
    # 放回 1600 尺寸再做同等级的高斯要 70 ms/页，整本扫描件白等几秒。
    bg = cv2.GaussianBlur(bg, (0, 0), max(1.5, short / 14.0))
    if bg.shape[:2] != (height, width):
        # INTER_CUBIC 本身就把小图铺成连续曲面，不用再补一次全尺寸模糊
        bg = cv2.resize(bg, (width, height), interpolation=cv2.INTER_CUBIC)
    # 下限 24：桌面/阴影里本来就近乎 0，不夹一下除法会炸成一片白斑。
    return np.maximum(bg, 24)


def _divide(image, bg):
    """image / bg * 255（饱和到 255）：去阴影的全部秘密就在这一步。"""
    import cv2

    return cv2.divide(image, bg, scale=255)


def _divide_bgr(image, bg):
    """彩色逐通道相除；共用同一幅背景，因为光照本来就是灰度量。

    逐通道各估一幅背景（共 3 份）不是不行，是没必要：色偏交给后面的灰世界白平衡管，
    省下两份全尺寸图和一次三倍的核开销。
    """
    import cv2

    channels = cv2.split(image)
    out = [cv2.divide(ch, bg, scale=255) for ch in channels]
    return cv2.merge(out)


def _levels(gray, low_pct, high_pct, out_low, out_high):
    """按分位数拉一条灰阶曲线，返回 (LUT, 拉完的灰度)。

    取分位数而不是 min/max：照片里总有几个死点和反光的高光点，
    拿极值当端点等于把 99% 的像素挤到中间，纸不白、字也不黑。
    """
    import cv2
    import numpy as np

    lo, hi = np.percentile(gray, [low_pct, high_pct])
    if hi - lo < 40:
        # 空白页 / 本来就干净的扫描件：hi≈lo≈255，硬拉会把整页拉花，
        # 这时按「白底黑字」的常识给一个固定端点。
        lo, hi = 90.0, 245.0
    lut = np.interp(np.arange(256, dtype=np.float32),
                    np.array([0, lo, hi, 255], dtype=np.float32),
                    np.array([0, out_low, out_high, 255], dtype=np.float32))
    lut = np.clip(lut, 0, 255).astype(np.uint8)
    return lut, cv2.LUT(gray, lut)


def _gray_world_gains(image, gray):
    """灰世界白平衡：把纸面三个通道的均值拉平，回三元组（纯类型，能单独验）。

    必须只在白底上算：暗角、拍进来的桌面都偏暖，拿整幅的均值当「灰」
    等于骗处理器说这场景是灰的，它反手给纸加一层冷色——白平衡的前提是场景里真有白，
    而只有亮区能保证那是纸。
    """
    import cv2
    import numpy as np

    mask = cv2.inRange(gray, WB_MIN_LUMA, 255)
    if int(cv2.countNonZero(mask)) < 64:
        # 几乎没有白底（一整页黑图、只拍到纸缝）：没有参考面就别动颜色
        return (1.0, 1.0, 1.0)
    means = np.array([float(cv2.mean(image, mask=mask)[i]) for i in range(3)])
    if np.any(means <= 1.0):
        return (1.0, 1.0, 1.0)
    # 夹到 ±33%：增益放到 1.5 会把红章染绿，宁可留一点暖偏。
    gains = np.clip(means.mean() / means, 0.75, 1.33)
    return (float(gains[0]), float(gains[1]), float(gains[2]))


def _channel_table(lut, gains):
    """把「乘增益」和「灰阶曲线」合成一张 256×3 的表：白平衡和调对比一次 LUT 走完。

    不合成表就得先 float32 乘一遍再查表：1600×1130 的 float32 是 21 MB，
    还要两趟全像素遍历（实测 63 ms/页，整本扫描件白等几秒）；
    一张 3 KB 的表 4 ms 搞定，这条路上从此不出现浮点大图。
    """
    import numpy as np

    index = np.arange(256, dtype=np.float32)
    # 形状给 (1, 256, 3)：cv2.LUT 要的是「256 个像素、通道数和源图一致」那张表，
    # 它按 total()==256 检查，写成 (256, 3) 会被当成 768 个像素直接抛错。
    table = np.zeros((1, 256, 3), dtype=np.uint8)
    for channel, gain in enumerate(gains):
        table[0, :, channel] = lut[np.clip(np.round(index * gain), 0, 255).astype(np.int32)]
    return table


def _unsharp(image, amount=0.85, sigma=1.5):
    """USM 锐化：原图 + amount*(原图 - 高斯模糊)。手机照片是软焦，去阴影又靠除法
    把噪声抬起来一档，不锐化的成品看着像蒙了层纱。"""
    import cv2

    blur = cv2.GaussianBlur(image, (0, 0), sigma)
    return cv2.addWeighted(image, 1.0 + amount, blur, -amount, 0)


def _dominant_angle(gray):
    """文字行的众数角（度，y 轴朝下取反正切）；认不出可靠方向就回 0.0。"""
    import cv2
    import numpy as np

    height, width = gray.shape[:2]
    edges = cv2.Canny(cv2.medianBlur(gray, 3), 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60,
                            minLineLength=max(24, width // 6), maxLineGap=15)
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in lines[:, 0]:
        if x2 == x1:
            continue
        angle = float(np.degrees(np.arctan2(float(y2 - y1), float(x2 - x1))))
        # 折到 ±90 内：竖线（纸边、表框）的角和水平线差 90，折回来一起当证据用
        if angle > 45:
            angle -= 90
        if angle < -45:
            angle += 90
        if abs(angle) <= 20:
            angles.append(angle)
    if len(angles) < 5:
        # 少于 5 条线就是没统计意义（空白页、或者桌面花纹的两三条线），
        # 回 0 等于不转——猜错方向的代价比不纠偏大得多。
        return 0.0
    return float(np.median(angles))


def _rotate(image, angle):
    """按 angle 转一页（正角＝画面逆时针），转出来的角补白。

    补白而不是补原图的边（api/ocr.py::_preprocess 用的 BORDER_REPLICATE）：
    这一步图像已经去过阴影，边缘拖出一条灰带会污染下一轮的白平衡，也会让裁边框歪。
    """
    import cv2

    if abs(angle) < 1e-3:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    border = 255 if image.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=border)


def _page_box(gray):
    """框出「纸」在哪里（外接矩形），裁边就照这一框裁。

    为什么必须在**没去过阴影**的那张灰度图上找：除法干的就是「把纸和桌面的亮度差抹平」
    这件事，抹完两者一样白，谁也不认得出谁；这一步靠的是边缘，不是亮度。
    为什么不用 OTSU 找亮区：暗角里的纸角比桌面中心还暗，按亮区框会把纸自己的四角切掉，
    切掉的那块往往正盖着骑缝章。
    认的是「图上最长最直、又围得出面积的一圈轮廓」= 纸的轮廓；
    贴到照片外沿的（整幅就是纸的扫描件）和太小的一块（桌面上的杯子、花纹）都不要，
    于是这种情况等于不裁——宁可不裁，也不切正文。
    """
    import cv2
    import numpy as np

    height, width = gray.shape[:2]
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 30, 90)
    # 连一下断口：纸边上有反光、被压角挡住，Canny 出来的是一串弧不是闭合环，
    # 不连通就围不出面积，轮廓也就选不出来。
    grow = 2
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=grow)
    found = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[0] if len(found) == 2 else found[1]
    best, best_area = None, 0.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area <= best_area:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        # 贴到照片外沿的一律不要：拍照时纸总被整幅切边，那条「边」其实是照片自己的边界
        # 或者桌面和画面外黑暗区的分界，量出来比纸还大，照着裁等于什么都没裁
        # （第一版挑中的就是它，1130×1560 里只裁掉 3.7%）。
        if x <= 2 or y <= 2 or x + w >= width - 2 or y + h >= height - 2:
            continue
        if w > width * 0.98 or h > height * 0.98:
            continue          # 那是照片自己的边，不是纸的边
        if w < width * 0.34 or h < height * 0.34:
            continue          # 半页都不到的东西不是纸
        best, best_area = (int(x), int(y), int(x + w), int(y + h)), area
    if best is None:
        return None
    # 上面连通用的膨胀把轮廓向外撑了 grow 个像素，这里收回去，
    # 否则「纸边」会被认到桌面里 2–3 px，一圈桌面灰边留在成品上。
    x0, y0, x1, y1 = (best[0] + grow, best[1] + grow, best[2] - grow, best[3] - grow)
    # 往外让一点：轮廓是量在纸边「内侧」的，贴着裁会把那 1–2 px 的纸边留成黑线。
    # 只让 2 px，不再多：往外每多让一像素，就多带进一像素桌面——黑白档会把那一圈
    # 桌面阈值成一条实黑边，成品像加了画框（第一版让 6 px，左边和下边就出现了黑带）。
    pad = 2
    return (max(0, x0 - pad), max(0, y0 - pad),
            min(width, x1 + pad), min(height, y1 + pad))


def _distance(a, b):
    """两点之间的直线距离（float）。算四边形的边长用，不带符号。"""
    import numpy as np

    return float(np.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1])))


def _corner_order(points):
    """把四个点排成「左上 → 右上 → 右下 → 左下」；围不出一个凸四边形的回 None。

    先取凸包而不是直接信顺序：approxPolyDP 给的点是沿着轮廓走的，起点在哪、
    顺不顺时针都不一定，而单应要的是「哪个角对哪个角」——错一个位置，
    拉出来就是整页翻面或者转了 90° 的糊图。
    凸包只剩 3 个点说明有一角落在另外三个角围成的三角形里（那不是纸的边，
    是轮廓上被啃出的一块），这种四点撑不出纸的透视，回 None。
    """
    import cv2
    import numpy as np

    pts = np.asarray(points, np.float32).reshape(-1, 2)
    if pts.shape[0] != 4:
        return None
    hull = cv2.convexHull(pts.reshape(1, -1, 2)).reshape(-1, 2)
    if len(hull) != 4:
        return None
    # 画面 y 轴朝下，所以 x+y 最小的是左上、最大的是右下；
    # y−x 最小的是右上、最大的是左下。凸四边形的四个角各占一个极值，
    # 要是两个极值落在同一个点上（四点几乎成一条线），下面 set 检查就回 None。
    total = hull[:, 0] + hull[:, 1]
    skew = hull[:, 1] - hull[:, 0]
    order = [int(np.argmin(total)), int(np.argmin(skew)),
             int(np.argmax(total)), int(np.argmax(skew))]
    if len(set(order)) != 4:
        return None
    return hull[order].astype(np.float32)


def _quad_angles(quad):
    """四个内角（度），顺序同 _corner_order。用来认「这还是不是个近似矩形」。"""
    import numpy as np

    out = []
    for index in range(4):
        here = quad[index]
        left = quad[(index - 1) % 4] - here
        right = quad[(index + 1) % 4] - here
        cosine = float(np.dot(left, right) /
                       (float(np.linalg.norm(left)) * float(np.linalg.norm(right)) + 1e-9))
        out.append(float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))))
    return out


def _approx_quad(contour):
    """把一条轮廓压成四个角（左上起顺时针）；几档容差都压不出四点的回 None。"""
    import cv2

    perim = float(cv2.arcLength(contour, True))
    if perim <= 0:
        return None
    for ratio in QUAD_EPSILON_RATIOS:
        approx = cv2.approxPolyDP(contour, ratio * perim, True).reshape(-1, 2)
        if len(approx) == 4:
            return _corner_order(approx)
    return None


def _inset_quad(quad, grow):
    """把四边形的每条边向内挪 grow 个像素，相邻边的交点就是新角；退化成平行的回 None。

    为什么不能整体按质心缩一点：纸的边几乎都平行于画面边，缩放会把「每条边内缩 grow」
    变成「每条边内缩 grow × 边长/半径」，长边缩得多、短边缩得少，四角就量歪了。
    这里逐条边按法线平移再求交，角点跟着边走，四角的形状（也就是那个梯形本身）不变，
    只是整圈往纸里收 grow 个像素。
    """
    import numpy as np

    centroid = np.asarray(quad, np.float32).mean(axis=0)
    lines = []
    for index in range(4):
        start = np.asarray(quad[index], np.float32)
        end = np.asarray(quad[(index + 1) % 4], np.float32)
        direction = end - start
        normal = np.array([-direction[1], direction[0]], np.float32)
        length = float(np.hypot(normal[0], normal[1]))
        if length < 1e-6:
            return None
        normal = normal / length
        if float(np.dot(normal, centroid - start)) < 0:
            normal = -normal          # 法线统一指向图形内侧，这样才能「向内挪」
        lines.append((start + normal * float(grow), direction))
    out = []
    for index in range(4):
        point_a, dir_a = lines[index - 1]
        point_b, dir_b = lines[index]
        cross = float(dir_a[0] * dir_b[1] - dir_a[1] * dir_b[0])
        if abs(cross) < 1e-6:
            return None               # 相邻两边几乎平行：这个角点定不出来，不敢猜
        delta = point_b - point_a
        step = float((delta[0] * dir_b[1] - delta[1] * dir_b[0]) / cross)
        out.append(point_a + dir_a * step)
    return np.array(out, np.float32)


def _page_quad(gray):
    """框出「纸」的四角（不是外接矩形），回 4×2 float32 或 None。

    为什么和 _page_box 一样必须在**没相除**的灰度上找：除法抹平的正是纸和桌面的亮度差，
    抹完之后梯形和背景一样白，四条边在哪个方向弯的都看不出来了。
    挑的是「面积最大、又贴得住画面内沿的凸四边形」；每一条守卫都在 _page_quad 顶部
    那几个常量上写死了理由，共同的落点是回 None —— 回 None 等于这一步不动，
    而不是回一个猜出来的梯形（猜错的梯形拉完是黑页或者糊页，比留一圈桌面难看得多）。
    """
    import cv2
    import numpy as np

    height, width = gray.shape[:2]
    frame = float(height * width)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 30, 90)
    # 纸边上有反光、压角、手指挡住的一段，Canny 出来的是一串弧而不是闭合环；
    # 连一下口才围得出面积（核和 _page_box 用同一个，两条路的判据不该各调各的）。
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    found = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[0] if len(found) == 2 else found[1]
    best, best_area = None, 0.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        # 面积区间两头都卡：小于半页的多半是桌上的杯子、花纹、另一张纸的一角；
        # 大到快满幅的那个是照片自己的边界（下面贴边检查也会拦住它，这里是双保险）。
        if area <= best_area:
            continue
        if area < MIN_QUAD_AREA * frame or area > MAX_QUAD_AREA * frame:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if x <= 2 or y <= 2 or x + w >= width - 2 or y + h >= height - 2:
            continue          # 贴到画面外沿的四边形是画面自己，不是纸
        quad = _approx_quad(contour)
        if quad is None:
            continue
        sides = [_distance(quad[i], quad[(i + 1) % 4]) for i in range(4)]
        if min(sides) < MIN_QUAD_SIDE_RATIO * min(height, width):
            continue          # 有一条边特别短 = 纸角被啃掉一块，或者根本不是纸
        angles = _quad_angles(quad)
        if min(angles) < MIN_QUAD_ANGLE or max(angles) > 180.0 - MIN_QUAD_ANGLE:
            continue          # 角斜到这个份上，拉出来的是平行四边形，不是正的纸
        tightened = _inset_quad(quad, QUAD_GROW)
        if tightened is None:
            continue
        best, best_area = tightened, area
    if best is None:
        LOG.debug('透视矫正：没找到可信的纸四角（画面 %d×%d，外围轮廓 %d 条）',
                  width, height, len(contours))
    else:
        LOG.debug('透视矫正：四角 %s', np.round(best, 1).tolist())
    return best


def _warp_quad(image, quad):
    """按四角把纸拉成矩形（BGR uint8）；撑不出合理单应的回 None。

    出图尺寸跟着四角自己的边长走（对边取大的那条），不给固定画布：
    写死 A4 竖幅的话，横着拍的那一页会被拉成竖的、字还歪——尺寸是「量」出来的这件事
    本身就是这一步的一部分。注意量到的是投影进画面的长度，比纸真实尺寸短，
    所以拉完只会偏小不会偏大，也不会超过进来那张图的长边（下面还是截了一刀）。
    """
    import cv2
    import numpy as np

    tl, tr, br, bl = quad
    out_w = int(round(max(_distance(tl, tr), _distance(bl, br))))
    out_h = int(round(max(_distance(tl, bl), _distance(tr, br))))
    side = float(max(image.shape[:2]))
    if out_w < 24 or out_h < 24:
        return None
    # 透视很强的时候「对边最大长度」会超过进来那幅的长边（斜着伸出去），
    # 这里按进来时的长边等比截一刀：这一步不该反过来把内存档破掉（同 fit_long_side）。
    if max(out_w, out_h) > side:
        shrink = side / float(max(out_w, out_h))
        out_w = int(round(out_w * shrink))
        out_h = int(round(out_h * shrink))
        if min(out_w, out_h) < 24:
            return None
    dst = np.array([[0.0, 0.0], [out_w - 1.0, 0.0],
                    [out_w - 1.0, out_h - 1.0], [0.0, out_h - 1.0]], np.float32)
    matrix = cv2.getPerspectiveTransform(quad, dst)
    # 条件数量的是「这四点有多透视」，用它挡住接近退化的一组解。归一化之后再算：
    # 同一个梯形在 480 和 2400 的画面下，像素坐标里的矩阵只差一个相似变换，
    # 条件数会跟着画面大小漂，拿它当门限就跨不了分辨率；除以长边后（S·M·S⁻¹）才只跟形状有关。
    norm = np.diag([1.0 / side, 1.0 / side, 1.0])
    cond = float(np.linalg.cond(norm @ matrix @ np.linalg.inv(norm)))
    if cond > MAX_QUAD_COND:
        LOG.info('透视矫正：单应条件数 %.1f 超过 %.1f，这四点撑不出合理的透视，不矫',
                 cond, MAX_QUAD_COND)
        return None
    # 齐次除法那个 w（第三行）在四角上的最大/最小比：w 是线性函数，在凸四边形上的极值
    # 只在角上取，所以这个比值就是「消视线离纸有多近」的量。比值一大，说明有一角
    # 快翻到无穷远处去了 —— 硬拉会得到一条对折的糊带，宁可不动。
    weights = np.abs(matrix[2] @ np.vstack([quad[:, 0], quad[:, 1],
                                            np.ones(4, np.float32)]))
    if float(weights.max()) > MAX_QUAD_W_RATIO * float(weights.min()):
        LOG.info('透视矫正：四角的透视权重差 %.1f 倍（> %.1f），有一角接近消视线，不矫',
                 float(weights.max()) / max(float(weights.min()), 1e-9), MAX_QUAD_W_RATIO)
        return None
    # 补白用白不用复制边：这一步之后还要估背景，边缘拖出一条桌面灰带会被当成纸，
    # 后面裁边、白平衡都跟着错（同 _rotate 里那句）。
    return cv2.warpPerspective(image, matrix, (out_w, out_h), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def _perspective(image, gray):
    """透视矫正的编排：认四角 → 拉直。任一环不成立就原样回，绝不回半张图。

    回来的两个数组一定是同一几何（灰度是从拉完的彩色图上重新取的），
    否则下一步 _estimate_bg 的背景场和纸面对不上，除完会出现一侧亮一侧暗。
    """
    import cv2

    quad = _page_quad(gray)
    if quad is None:
        return image, gray
    warped = _warp_quad(image, quad)
    if warped is None:
        return image, gray
    LOG.debug('透视矫正：%s → %s', str(image.shape[:2]), str(warped.shape[:2]))
    return warped, cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)


def estimate_skew(image):
    """量一下这页歪了几度（纯类型，界面可拿来显示「纠偏 -1.3°」）。"""
    gray = to_gray(image)
    return _dominant_angle(_divide(gray, _estimate_bg(gray)))


def to_gray(image):
    """公开的小工具：任意 BGR/灰度数组 → 连续的灰度 uint8。"""
    import cv2

    return cv2.cvtColor(_as_bgr(image), cv2.COLOR_BGR2GRAY)


def _deskew(norm, raw, work):
    """最多 DESKEW_PASSES 趟纠偏；返回转好后的 (去阴影灰度, 原灰度或 None, 彩色副本或 None)。

    为什么要转两趟：第一趟读出来的是「这页现在歪多少」，转完插值和残余的阴影起伏
    会让读数偏掉半度到一度（本机合成图实测 -1.8° → 转完还剩 +0.9°）；
    再量一次补掉就落到 0.1° 以内。一趟就把阈值放宽去「猜」真值，猜歪的代价更大。
    三层必须用同一个角度转：裁边认的是原灰度上的纸边，它要是没跟着转，
    框出来的坐标就套不到成品图上（切歪一条边）。
    """
    for _ in range(DESKEW_PASSES):
        angle = _dominant_angle(norm)
        if not (MIN_DESKEW < abs(angle) < MAX_DESKEW):
            break
        norm = _rotate(norm, angle)
        if raw is not None:
            raw = _rotate(raw, angle)
        if work is not None:
            work = _rotate(work, angle)
    return norm, raw, work


def enhance_image(bgr_array, mode=DEFAULT_MODE, deskew=False, crop=False,
                  work_side=WORK_SIDE, perspective=False):
    """一张「拍下来的纸」进去，一张「扫出来的纸」出来（BGR uint8）。

    步骤顺序不能调：先透视（认梯形得赶在相除之前，理由见 _page_quad），再去阴影
    （后面每一步的判断都建立在「白底真是白的」上），再纠偏（Hough 找的是干净字行），
    再裁边（裁完像素最少），最后才分档强化。
    perspective 拉直之后，纸就已经铺满整个画面了，这时 _page_box 认不出贴边的纸
    （它专挑不贴框的轮廓），crop 这一档自然等于不裁 —— 这是对的，不是漏了一步。

    出图尺寸可能比进来小（长边压到 work_side、裁了边、转了角），这是刻意的；
    PDF 那边按原页幅插回，页的观感尺寸不变。透视那一步可能把图**改大改小**：
    出的是量出来的纸面尺寸（横版纸不会被拧成竖版）。
    """
    import cv2

    key = normalize_mode(mode)
    image = fit_long_side(_as_bgr(bgr_array), work_side)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if perspective:
        # 旧的那两份在这儿就脱手：拉完是一幅新图，留着 4 GB 的机器上白占一份
        image, gray = _perspective(image, gray)
    bg = _estimate_bg(gray)
    # 彩色档整张彩色图相除，另两档后面只看灰度，别多养一份全尺寸副本
    work = _divide_bgr(image, bg) if key == MAGIC else None
    norm = _divide(gray, bg)
    del image, bg
    # 裁边要回到「没相除」的那张灰度上认纸边，所以只有真要用时才留这一份（1.8 MB 量级）
    raw = gray if crop else None

    if deskew:
        norm, raw, work = _deskew(norm, raw, work)

    if crop:
        box = _page_box(raw)
        if box:
            x0, y0, x1, y1 = box
            norm = norm[y0:y1, x0:x1]
            if work is not None:
                work = work[y0:y1, x0:x1]

    if key == MAGIC:
        lut, stretched = _levels(norm, 1.0, 99.0, 24, 255)
        # 白平衡和拉对比合成一张表，一次遍历走完（见 _channel_table）
        result = cv2.LUT(work, _channel_table(lut, _gray_world_gains(work, stretched)))
        # 锐化只在 Lab 的明度上做：RGB 三通道各锐一遍会让红章边缘发紫（彩色振铃）。
        lab = cv2.cvtColor(result, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = _unsharp(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    if key == GRAY:
        lut, stretched = _levels(norm, 0.5, 99.5, 0, 255)
        return cv2.cvtColor(cv2.LUT(stretched, lut), cv2.COLOR_GRAY2BGR)

    # 黑白档：图已经除过背景，所以自适应阈值只负责「补剩下的局部起伏」——
    # 纸的褶皱、除完残留的一点梯度，这些在固定阈值下会粘成一片黑。
    short = min(norm.shape[:2])
    block = _odd(short / 12, 15)
    binary = cv2.adaptiveThreshold(norm, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, block, 10)
    # 3×3 中值专门扫除法放大出来的椒盐点：一个像素的白洞能把笔画打断，
    # 但再大的核就会开始啃字了。
    binary = cv2.medianBlur(binary, 3)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def read_image(path):
    """按路径读一张图成 BGR。

    不走 cv2.imread：Windows 上它只认 ANSI 代码页，装在「D:\\文档\\」这类中文路径
    下的照片会静默返回 None（安装目录本身带中文这件事在 core/ocr.py 里也踩过）。
    np.fromfile + imdecode 走的是字节，什么都认。
    """
    import cv2
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        raise ValueError('图片是空的或读不出来: %s' % path)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('图片解码失败（可能不是受支持的格式）: %s' % path)
    return image


def decode_png(png_bytes):
    """把内存里的 PNG/JPEG 字节解开成 BGR（PyMuPDF 出的页就是这条路）。"""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('页面图像解码失败')
    return image


def encode_png(image):
    """BGR → PNG 字节；失败回空串，由调用方决定怎么报（不往 api 层抛）。"""
    import cv2

    ok, buf = cv2.imencode('.png', image)
    return buf.tobytes() if ok else b''


def encode_jpeg(image, quality=JPEG_QUALITY):
    """BGR → JPEG 字节（成品 PDF 用这个：去阴影后的纸面大片纯白，JPEG 比 PNG 小一个量级）。"""
    import cv2

    ok, buf = cv2.imencode('.jpg', image,
                           [int(cv2.IMWRITE_JPEG_QUALITY), int(max(40, min(98, quality)))])
    return buf.tobytes() if ok else b''


def encode_for(path, image, quality=JPEG_QUALITY):
    """按目标扩展名编码：png 无损、其余一律 JPEG（bmp/tiff/webp 的位置统一换成 jpg）。"""
    name = str(path or '').lower()
    if name.endswith('.png'):
        data = encode_png(image)
        if data:
            return data, '.png'
    return encode_jpeg(image, quality), '.jpg'


def status():
    """这摊 OpenCV 代码在不在、是哪份；界面上的灰态和日志都看这一句。

    两个 opencv 包都装在（见 requirements.txt），import cv2 只会拿到先注册的那份，
    这里把实际用的那份版本号回出来，出问题时不用猜。
    """
    info = {'available': False, 'version': '', 'modes': mode_labels(),
            'options': option_labels(), 'work_side': WORK_SIDE, 'message': ''}
    try:
        import cv2
    except Exception as error:            # 打包漏收 cv2 时界面要说「这个功能用不了」而不是崩
        LOG.exception('增强功能导入 OpenCV 失败')
        info['message'] = 'OpenCV 不可用: %s' % error
        return info
    import numpy

    info['available'] = True
    info['version'] = str(cv2.__version__)
    info['numpy_version'] = str(numpy.__version__)
    return info
