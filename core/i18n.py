"""界面文案：十种语言一张表，缺键回退到简体中文。

三条口径写在这儿，改语言系统前先读：

1. **默认值是 `auto`，不是某种语言。** 软件第一次启动要按这台电脑的系统语言显示
   （`GetUserDefaultUILanguage`，量的是 Windows 显示语言而不是区域格式），
   所以配置里存的是「auto」这个哨兵，真正的语言在用的时候才解析出来。
   用户手动选过之后存的就是具体代码，不再跟着系统变。
2. **每种语言只写自己那一档，取的时候按 zh-CN 兜底合并**（`texts()`）。
   新增键只加在 `lang_zh_cn.py`，别的语言漏了不会让界面变空白，
   但 `env_check` 的键位对齐检查会红——漏翻译当场看得见，不会一路带到出包。
3. **语言名用该语言自己的写法**（English / 日本語 / Deutsch …），只有「跟随系统」
   这一项需要翻译：它是描述性的，不是语言名。
"""
import ctypes
import importlib

from core.lang_zh_cn import TEXTS as ZH_CN

AUTO = 'auto'
DEFAULT_LANG = 'zh-CN'

# 顺序就是下拉框里的顺序：中文在前（这个软件的用户主要是中文用户），
# 其余按 ISO 代码排，方便和 Inno Setup 那边的语言列表对。
LANGUAGES = [
    ('zh-CN', '简体中文'),
    ('zh-TW', '繁體中文'),
    ('en', 'English'),
    ('ja', '日本語'),
    ('ko', '한국어'),
    ('fr', 'Français'),
    ('ru', 'Русский'),
    ('de', 'Deutsch'),
    ('es', 'Español'),
    ('it', 'Italiano'),
]

# 十份文案表按需要才 import：一次启动只会用到其中一到两份，
# 顶层全量 import 等于让启动画面替用户读九份用不到的字典。
_MODULE = {
    'zh-CN': 'core.lang_zh_cn',
    'zh-TW': 'core.lang_zh_tw',
    'en': 'core.lang_en',
    'ja': 'core.lang_ja',
    'ko': 'core.lang_ko',
    'fr': 'core.lang_fr',
    'ru': 'core.lang_ru',
    'de': 'core.lang_de',
    'es': 'core.lang_es',
    'it': 'core.lang_it',
}
_CACHE = {'zh-CN': ZH_CN}
_MERGED = {}
# 后端侧（不带界面语言参数的调用）用它取一句话：api 层读/存配置时刷新
_PREFERRED = [None]
SUPPORTED = [code for code, _name in LANGUAGES]


def lang_texts(code):
    """某一种语言自己写的那一份（不含回退基线）。"""
    hit = _CACHE.get(code)
    if hit is not None:
        return hit
    hit = importlib.import_module(_MODULE.get(code, _MODULE[DEFAULT_LANG])).TEXTS
    _CACHE[code] = hit
    return hit

# Windows 显示语言的 LANGID → 我们的代码。
# 只认这十种语言对应的 ID；命中不了（葡萄牙语、波兰语、匈牙利语…）就退回英语，
# 因为英语是这套界面里覆盖面最广的那一档。
_FULL_LANGIDS = {
    0x0804: 'zh-CN',   # 中文(简体，中国)
    0x1004: 'zh-CN',   # 中文(简体，新加坡)
    0x0404: 'zh-TW',   # 中文(繁体，台湾)
    0x0C04: 'zh-TW',   # 中文(繁体，香港)
    0x1404: 'zh-TW',   # 中文(繁体，澳门)
    0x0411: 'ja',      # 日语
    0x0412: 'ko',      # 韩语
    0x040C: 'fr',      # 法语(法国)
    0x0C0C: 'fr',      # 法语(加拿大)
    0x0419: 'ru',      # 俄语
    0x0407: 'de',      # 德语(德国)
    0x0C07: 'de',      # 德语(奥地利)
    0x1007: 'de',      # 德语(瑞士)
    0x0C0A: 'es',      # 西班牙语(国际)
    0x040A: 'es',      # 西班牙语(西班牙传统)
    0x0410: 'it',      # 意大利语
    0x0409: 'en',      # 英语(美国)
    0x0809: 'en',      # 英语(英国)
    0x040E: 'en',      # 匈牙利语 → 退回英语
}


def language_name(code):
    """下拉框里显示的名字（该语言自己的写法）。"""
    for c, name in LANGUAGES:
        if c == code:
            return name
    return code


def detect_system_language():
    """这台电脑用什么语言。

    问的是 **UI 语言**（`GetUserDefaultUILanguage`）而不是区域格式：
    把区域设成美国但显示语言是中文的人，要的是中文界面。
    拿不到（非 Windows、API 失败）就退回 zh-CN —— 这个软件出厂就是简体中文。
    """
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:
        return DEFAULT_LANG
    if not langid:
        return DEFAULT_LANG
    hit = _FULL_LANGIDS.get(langid)
    if hit:
        return hit
    # 具体区域没列进表里（比如 es-MX、fr-BE），按主语言号再认一次
    primary = langid & 0x3F
    by_primary = {0x04: 'zh-CN', 0x02: 'zh-TW', 0x11: 'ja', 0x12: 'ko',
                  0x0C: 'fr', 0x19: 'ru', 0x07: 'de', 0x0A: 'es',
                  0x10: 'it', 0x09: 'en'}
    return by_primary.get(primary, 'en')


def resolve_language(value):
    """配置里的值 → 真正要用的语言代码。

    `auto` / 空 / 没这个键 → 跟随系统；认不出的值也回系统语言
    （配置文件可能被手改，改坏了不该让界面变成空白）。
    """
    if not value or value == AUTO:
        return detect_system_language()
    if value in _MODULE:
        return value
    return detect_system_language()


def texts(value=None):
    """取某一档文案：先铺简体中文基线，再用目标语言覆盖。

    返回的字典一定包含基线的全部键，所以前端不会因为某个语言漏了一条就显示空白。
    """
    lang = resolve_language(value)
    hit = _MERGED.get(lang)
    if hit is None:
        merged = dict(ZH_CN)
        merged.update(lang_texts(lang))
        merged['app_locale'] = lang
        hit = _MERGED[lang] = merged
    return hit


def set_language(value):
    """让后端侧的 t() 知道现在该用哪门语言（配置读出来/存进去时各刷一次）。"""
    _PREFERRED[0] = value


def t(key, **fmt):
    """后端侧取一句用户可见的话，缺键就报键名本身（不会像缺字符串那样整个崩）。"""
    value = texts(_PREFERRED[0]).get(key, key)
    return value.format(**fmt) if fmt else value


def available():
    """给界面用：可选的语言列表（含「跟随系统」）与本机识别到的那一档。"""
    return [{'code': AUTO, 'name': ZH_CN['lang_auto']}] + [
        {'code': code, 'name': name} for code, name in LANGUAGES]
