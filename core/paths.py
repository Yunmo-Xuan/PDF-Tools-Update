"""集中管理程序用到的所有目录与文件路径。

按安装版布局约定：
  * 只读程序资源（assets / resources）随安装包分发，位于程序目录下；
  * 用户配置放 Roaming AppData，卸载时可选择保留，多机漫游不丢设置；
  * 日志放 Local AppData，不进漫游配置，也不会像系统临时目录那样被清理掉。

源码直接运行时，配置和日志同样落到上述用户目录，
这样开发时不会在工作副本里留下一堆运行期文件。
"""

import logging
import os
import sys

APP_ID = 'PDFToolkit'


def get_base_path():
    """程序资源所在目录（PyInstaller 解包目录 / Nuitka 程序目录 / 源码项目根）。"""
    if getattr(sys, 'frozen', False):
        # PyInstaller：--onedir 和 --onefile 都会把 --add-data 的资源放到 _MEIPASS
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.argv[0])))
    if '__compiled__' in globals():  # Nuitka
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_program_dir():
    """
    exe 所在目录：安装版 = 安装目录，便携版 = dist\\PDFToolkit，源码模式 = 项目根。

    和 get_base_path() 不是一回事：打包后后者指向 _internal（资源目录），
    而安装器留下的批次标记落在 exe 旁边。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROGRAM_DIR = get_program_dir()
# 安装器每次装完写下的批次标记，见 installer/pdf-toolkit.iss 的 CurStepChanged
INSTALL_STAMP_FILE = os.path.join(PROGRAM_DIR, 'install.stamp')


def get_asset_path(*parts):
    """assets 目录下的资源路径，例如 get_asset_path('css', 'style.css')。"""
    return os.path.join(get_base_path(), 'assets', *parts)


def _roaming_dir():
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, APP_ID)


def _local_dir():
    if sys.platform == 'win32':
        base = (os.environ.get('LOCALAPPDATA')
                or os.environ.get('APPDATA')
                or os.path.expanduser('~'))
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Logs')
    else:
        base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(base, APP_ID)


CONFIG_DIR = _roaming_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

# 日志目录是固定的：设置页要能指向它，「清理日志」也只在里面找自己的文件。
# 三类日志同目录分文件：usage 收全部，install/error 各留一份副本，
# 后两份不参与「清理日志」，避免一次清理把安装现场和报错现场一起删掉。
LOG_DIR = os.path.join(_local_dir(), 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'pdf_toolkit.log')
INSTALL_LOG_FILE = os.path.join(LOG_DIR, 'pdf_toolkit-install.log')
ERROR_LOG_FILE = os.path.join(LOG_DIR, 'pdf_toolkit-errors.log')

# PaddleOCR 的运行时缓存（PaddleX 的日志/临时文件）。模型随包分发，不从这里下载；
# 放在 Local AppData 是为了不往用户主目录里撒文件。
OCR_CACHE_DIR = os.path.join(_local_dir(), 'ocr_cache')

# 本机字体清单的磁盘缓存（见 core/fonts.py）：冷启动全量解析字体名表要好几秒，
# 落一份按「文件大小+mtime 指纹」失效的缓存，后续启动只对变过的文件重算。
# 和 ocr_cache 同一套口径：Local AppData，不进 Roaming（缓存错了要能自愈，
# 漫游到另一台机器上指纹全变，反而全是无谓的重算）。
# 目录由 core.fonts 在第一次真要写的时候才创建，不在这儿 mkdir。
FONT_CACHE_DIR = os.path.join(_local_dir(), 'font_cache')

# 页面管理的草稿（未保存的编辑意图）。放 Local 而不是 Roaming：
# 它记的是「这台机器上这个会话改到哪了」，跟着用户漫游到另一台机器
# 反而危险——那边的源文件路径与 mtime 多半对不上，套上去就是错改文档。
# 目录由 core.drafts 在第一次真要写的时候才创建，不在这儿 mkdir。
DRAFT_DIR = os.path.join(_local_dir(), 'drafts')

# 可选组件目录：目前只是给「需要额外下载的语言包」这类东西预留一个固定落点
# （见开发方案 §4.16）。7.0.1 不往里写任何东西，也不创建它。
COMPONENT_DIR = os.path.join(_local_dir(), 'components')

# 「按文件打印」只打其中几页时，要先攒一份只含那几页的临时 PDF（见 api/printing.py）。
# 不放系统 %TEMP%：一是和草稿同一套口径，验收挪沙盒目录就能整体挪走；
# 二是这份副本要等对面程序读走，不能指望临时目录清理器替你收尾。
# 目录由 printing.py 在真要写的时候才创建，不在这儿 mkdir。
PRINT_TMP_DIR = os.path.join(_local_dir(), 'printfiles')


# 7.0.3 之前这个程序叫 PDFToolbox（安装目录还是中文的「PDF 工具箱」）。改名之后配置、
# 日志、草稿、OCR 缓存的落点全跟着变，老用户一升级就等于被清空了所有设置——
# 所以第一次启动时把旧目录整个搬成新名字。只在「新名字还不存在」时搬，
# 搬不动也不影响程序起来（顶多回到出厂设置），绝不覆盖新目录里已有的东西。
LEGACY_APP_IDS = ('PDFToolbox',)
LEGACY_LOG_BASES = ('pdf_toolbox',)


def migrate_legacy_dirs():
    """把改名前的 %APPDATA%\\PDFToolbox 与 %LOCALAPPDATA%\\PDFToolbox 搬成新名字。"""
    import shutil

    moved = []
    for parent in {os.path.dirname(CONFIG_DIR), os.path.dirname(_local_dir())}:
        for old_id in LEGACY_APP_IDS:
            src = os.path.join(parent, old_id)
            dst = os.path.join(parent, APP_ID)
            if not os.path.isdir(src) or os.path.exists(dst):
                continue
            try:
                os.replace(src, dst)
            except OSError:
                try:
                    shutil.move(src, dst)
                except OSError:
                    logging.getLogger(__name__).warning('旧目录搬不过来: %s', src)
                    continue
            moved.append(dst)
    if LOG_DIR and os.path.isdir(LOG_DIR):
        for name in sorted(os.listdir(LOG_DIR)):
            base = os.path.basename(name)
            if not base.startswith(LEGACY_LOG_BASES):
                continue
            target = os.path.join(LOG_DIR, base.replace('pdf_toolbox', 'pdf_toolkit', 1))
            if os.path.exists(target):
                continue
            try:
                os.replace(os.path.join(LOG_DIR, base), target)
            except OSError:
                logging.getLogger(__name__).warning('旧日志改不了名: %s', base)
    return moved


def ensure_dirs():
    """确保配置与日志目录存在，返回 [(目录, 是否可用), ...]。"""
    migrate_legacy_dirs()
    result = []
    for directory in (CONFIG_DIR, LOG_DIR):
        try:
            os.makedirs(directory, exist_ok=True)
            result.append((directory, True))
        except Exception:
            logging.getLogger(__name__).warning(f"无法创建目录: {directory}")
            result.append((directory, False))
    return result
