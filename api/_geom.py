"""幅面名的pt尺寸表与换算。

「A4 / A3 / Letter」这几个名字在图片转 PDF（F11）、页面缩放（F17）里都要用，
表只留一份：两处各写一遍迟早会出现「同一个 A4，转出来 595 而缩放用 595.28」
这种边距对不上的毛病。前端只传幅面名，具体的 pt 一律在这里算（方案 §4.3）。
"""

# 纵向（竖放）尺寸，单位 pt；1 pt = 1/72 英寸。
PAGE_SIZES = {
    "a3": (841.89, 1190.55),
    "a4": (595.28, 841.89),
    "a5": (419.53, 595.28),
    "letter": (612.0, 792.0),
}

# 「不套标准纸，按内容自身尺寸成页」的几种叫法。
_FIT_NAMES = ("fit", "auto", "orig", "原图", "原尺寸", "自适应", "按图片", "不变")

# 中文幅面叫法：用户手打或从别处抄来的名字不该因为写法差异就认不出。
_ALIASES = {
    "a3纸": "a3",
    "a4纸": "a4",
    "b5": "a5",
    "信纸": "letter",
    "美式信纸": "letter",
    "usa": "letter",
}


def _normalize(name):
    key = str(name or "").strip().lower()
    for junk in (" ", "\t", "　", "－", "—", "×", "x", "(", ")", "（", "）"):
        key = key.replace(junk, "")
    return key


def is_fit(name):
    """判断幅面名是不是「按原尺寸」这一类特殊档。"""
    return _normalize(name) in _FIT_NAMES


def lookup_size(name, landscape=False):
    """
    认幅面名，返回 (宽, 高)（单位 pt）；认不出来返回 None。

    函数不叫 page_size 是因为桥方法的入参就叫 page_size（F11/F17 的口径），
    同名会在方法里把函数遮成一个字符串——这种错只有跑到那儿才炸。
    名字里带「横」的按横向处理，前端不必自己交换宽高、也不必记 A4 横向是几乘几。
    空名字当 A4 用：界面留空就是「我不挑」，不是输入错误。
    """
    key = _normalize(name)
    if "横" in key:
        landscape = True
        key = key.replace("横向", "").replace("横", "")
    if key in ("", "默认"):
        key = "a4"
    key = _ALIASES.get(key, key)
    size = PAGE_SIZES.get(key)
    if not size:
        return None
    return (size[1], size[0]) if landscape else size
