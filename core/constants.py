"""全局常量。

版本号只在这里维护一处，界面标题、启动画面、打包脚本、安装脚本都从这里取值。
只出 64 位包，不再区分位数。

目录与文件位置在 core.paths 里，本模块不代为转出——两边各管各的，
避免「同一个常量有两个入口」。
"""

CURRENT_VERSION = "7.0.3"

APP_NAME = "PDF Toolkit"
APP_AUTHOR = "云陌"

APP_TITLE = f"{APP_NAME} {CURRENT_VERSION}"
APP_COPYRIGHT = f"© {APP_AUTHOR}"

# 远端文件所在的仓库与「镜像池」。
# 三个用途（版本 JSON / 条款 / 更新日志）共用同一组镜像，只是文件名不同：
# 以前各写一份，结果 README 的地址里漏掉了 fastly，而恰恰是 fastly 那份是新的
# ——镜像的缓存会滞后，少一个源就等于少一次机会。
# 注意这里必须是「原始文件」地址，GitHub 的 /blob/ 页面返回的是 HTML，无法解析。
_GH_REPO = "Yunmo-Xuan/PDF-Tools-Update"
_JSD_PATH = f"{_GH_REPO}@main"
_RAW_PATH = f"{_GH_REPO}/main"
# 顺序仍然有意义，但只用于「内容一样新时先取谁」：联网取法见 core/mirror.py，
# 它一次问完所有源，再按内容挑最新的那份，而不是第一个答回来的那份。
# 本机实测国内网络对 cdn.jsdelivr.net 与 raw.githubusercontent.com 常是「连接被重置」，
# 所以把可达的镜像排在前面，GitHub 原始地址保留兜底（换了网络、镜像失效时还能用）。
_MIRRORS = (
    f"https://cdn.jsdmirror.com/gh/{_JSD_PATH}/",
    f"https://fastly.jsdelivr.net/gh/{_JSD_PATH}/",
    f"https://cdn.jsdelivr.net/gh/{_JSD_PATH}/",
    f"https://raw.githubusercontent.com/{_RAW_PATH}/",
)
UPDATE_URLS = tuple(f'{base}version.json' for base in _MIRRORS)
UPDATE_PAGE_URL = f"https://github.com/{_GH_REPO}"

# 许可协议：页面地址给用户点开看最新版，上面的镜像池供程序在线拉取正文。
LICENSE_PAGE_URL = f"https://github.com/{_GH_REPO}/blob/main/LICENSE.md"
LICENSE_URLS = tuple(f'{base}LICENSE.md' for base in _MIRRORS)

# 更新日志：仓库 README 里的「📝 更新日志」一节，发现新版本时展示给用户看。
README_PAGE_URL = f"https://github.com/{_GH_REPO}/blob/main/README.md"
README_URLS = tuple(f'{base}README.md' for base in _MIRRORS)

DEFAULT_CONFIG = {
    "recent_files": [],
    "dark_mode": False,
    "theme_color": "blue",
    "check_update_on_startup": True,
    # 走完（或跳过）向导时记下当时的版本号。
    # 向导按版本判定而不是只看「有没有完成过首次设置」：只看后者的话，
    # 老用户配置里永远是 true，装了新版本就再也见不到向导。
    "first_run_completed": False,
    "wizard_version": "",
    # 再记一次「哪一回安装」走过的向导（安装器写的 install.stamp）。
    # 只比版本号的话，同一个版本反复重装（开发期天天这样）就永远只弹第一次。
    "wizard_stamp": "",
    # 首页「常用功能」置顶的导航项，按用户点置顶的先后顺序存。
    # 只存 id 不存名字：文案改了、加了新页都不用迁移配置；读的时候对不上的 id 直接忽略。
    "pinned_pages": [],
    # 页面管理缩略图的出图精度档位（64 / 96 / 128）。
    # 默认仍是 6.0.4 的 64：档位越高单页字节数按面积涨，别替用户默认选贵的那档。
    "thumb_dpi": 64,
    # 页面管理草稿的保留策略：
    #   keep_recent  = 自动落盘，最多留 10 份（超出按最后保存时间淘汰）
    #   keep_current = 只留当前打开的这份文件
    #   off          = 完全不写草稿
    "draft_policy": "keep_recent",
    # 功能页里那些「这一步怎么用」的小字说明（.form-note）。
    # 默认开：新功能是这一版加进来的，没人带一遍就看不懂；用熟了可以在设置或向导里关掉。
    "show_feature_notes": True,
    # 界面语言。默认是「auto」而不是某个具体语言：第一次启动按这台电脑的系统语言显示，
    # 用户手动选过之后才存成具体代码（zh-CN / en / ja …），不再跟着系统变。
    "language": "auto",
    # 左侧导航栏收成图标条（只留图标，操作区宽出 152 px）。
    # 默认不收：生手要靠文字认功能，图标条是给用熟了的人省地方的。
    "sidebar_collapsed": False,
    # 收成图标条之后，鼠标碰到侧边就临时把名字亮出来（浮在内容上，不动操作区宽度），移开自动收回。
    # 只在「已收起」那一侧生效，所以默认开着也不会挡到没收起的人；点把手仍是「一直开着」。
    "sidebar_hover_expand": True,
    # 主界面交给用户之后，在后台把 OCR 引擎先建好。
    # 引擎一次要 8~13 秒（Paddle 导入 + 模型进内存），以前只有打开「提取 → OCR」才预热，
    # 于是每回重开程序都得重新等一遍。这一条默认开；机器吃紧或不想多占内存可以关。
    "ocr_preload": True,
    # Windows 资源管理器右键菜单（「打开方式」条目 + .pdf 右键项，见 core/shell.py）。
    # 默认关：这一项动的是用户自己的资源管理器，程序不能不打招呼就住进去；
    # 由他在设置（或向导）里打开开关才写注册表，且只写当前用户（HKCU）自己那一片。
    "shell_context_menu": False,

    # 翻译（⑦）：这一轮只做能力与配置，界面入口还没给；provider 默认走随包词表，
    # 在线那档要用户自己填服务地址——没填就明确报错，绝不编一句译文冒充翻好了。
    'translate_provider': 'offline',
    'translate_url': '',
    'translate_api_key': '',
    'translate_timeout': 12.0,
    'translate_batch_max': 100,
    'translate_lang_map': {},
}
