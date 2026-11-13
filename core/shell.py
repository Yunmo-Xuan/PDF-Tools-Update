r"""Windows 资源管理器右键菜单接入：注册 / 注销 / 查询，全部走 HKCU。

三条硬边界，下面每个函数都守着：

1. **只写 HKEY_CURRENT_USER。** 本程序是按当前用户安装的（安装器
   ``PrivilegesRequired=lowest``，装机路径 ``%LOCALAPPDATA%\Programs\PDF Toolkit\``），
   没有管理员令牌；HKLM 不但写不动，也不该写。本模块的任何调用都不碰 HKLM。
2. **只写自己那一片。** 写出去的键全部以程序自己命名：
   ``Software\Classes\Applications\PDFToolkit.exe`` 与
   ``Software\Classes\SystemFileAssociations\.pdf\shell\PDFToolkit.Open`` 两棵树。
   注销也只删这两棵树——绝不动 ``.pdf`` 的默认关联（那是用户自己在「打开方式」
   里选定的，本模块只读都不需要读），也绝不删 ``.pdf\shell`` 或
   ``SystemFileAssociations\.pdf\shell`` 这两层本身（它们的同级子键里可能住着
   别的程序的动词），``Software\Classes`` 下别的一切同样原样不碰。
3. **任何一步失败都如实回话。** 「没有管理员权限（PermissionError）」「键已存在」
   「键被别的程序改过」「注册表写入抛异常」四种现场各自给出明确的 reason/message，
   并且回读一遍真实落点，绝不静默失败、绝不假报成功。

为什么菜单挂在 ``SystemFileAssociations\.pdf\shell`` 而不是自有 ProgID：
写在自有 ProgID（如 ``PDF Toolkit.pdf``）下的菜单项，只有当 ``.pdf`` 的**默认关联**
指到这个 ProgID 时才会出现——而改默认关联正是上面第 2 条划死的红线。
``SystemFileAssociations\<扩展名>\shell`` 按扩展名生效，与「当前默认程序是谁」无关，
也是 Windows 允许按用户写的规范路径。「打开方式」列表里出现本程序，走的是
``Software\Classes\Applications\<exe 名>``（同样按用户、同样规范）。

命令行怎么把选中的文件真的传给程序：

* 「打开方式」与右键菜单条目都用同一个形状： ``"<exe>" "%1"``。
  微软对静态动词的文档口径里，%1 展开为**文件的完整路径**（learn.microsoft.com
  “Creating Shortcut Menu Handlers”的示例全是 ``program.exe "%1"``）；
* 右键菜单条目另配 ``MultiSelectModel=Player``（微软文档：“Player = 支持任意数量
  条目的动词”）：多选几个 PDF 时资源管理器对每个选中的文件各启动一次命令，
  每次命令行里的 %1 就是那个文件的完整路径——选中的文件确实送到了程序手上；
* 占位符包在双引号里：路径带空格、带中文，展开后仍是一个完整的参数，
  不会被拆成两截（引号结构与回读校验见 build_command / parse_command / status）；
* **不用**老写法 ``"%V%\1"``：在现代 Explorer 的文件类型动词里 %1 已经是全路径，
  再拼一个 %V% 会得出「目录+目录+文件名」这种双前缀路径，磁盘上根本没有那个
  文件——条目就变成「看着在、点了没真东西」，正是本模块禁止的形状。

程序侧接收命令行文件参数的落点在主入口 ``main.py``；本模块按上面这套规范把
「exe 真路径 + 加引号的 %1」写进注册表，主程序按同一口径接 argv 即可接通。

非 Windows 或解释器没有 ``winreg`` 时，三个入口函数一律返回
``reason='not_windows'`` 的明确结果，不抛异常（调用方是桥接层，崩不得）。
"""

import logging
import os
import re
import sys

from core.i18n import t as _text

try:  # winreg 是 Windows 专属标准库；非 Windows 上导入失败也不算错
    import winreg
except ImportError:  # pragma: no cover - 非 Windows
    winreg = None

# winreg 只在 Windows 上有；两个条件都判，防「有 winreg 但非 win32」的怪环境。
IS_WINDOWS = sys.platform == 'win32'

APP_EXE_NAME = 'PDFToolkit.exe'

# 注册表键位（相对 HKCU）。写入的键都在这两棵「自己命名」的树下面。
_CLASSES = r'Software\Classes'
APP_ROOT = _CLASSES + r'\Applications\PDFToolkit.exe'
_OPEN_KEY = APP_ROOT + r'\shell\open'
_OPEN_COMMAND = _OPEN_KEY + r'\command'
_TYPE_KEY = APP_ROOT + r'\SupportedTypes\application\pdf'
VERB_ROOT = _CLASSES + r'\SystemFileAssociations\.pdf\shell\PDFToolkit.Open'
_VERB_COMMAND = VERB_ROOT + r'\command'

# 注销只删这两棵树（深度无关，两棵互不包含）。除此之外一个键都不删。
OWNED_ROOTS = (VERB_ROOT, APP_ROOT)

# 「打开方式」与右键菜单条目都用 "%1"（完整路径，见模块 docstring 的取舍）；
# 多选靠 MultiSelectModel=Player：每个选中文件各起一次，每次一个完整路径。
PLACEHOLDER_OPENWITH = '%1'
PLACEHOLDER_MENU = '%1'

# HKLM 哨兵：任何调用试图走它都算缺陷，直接点名的 ValueError（比 OSError 更难被误吞）。
_NO_HKLM = '本模块只允许写 HKCU（按用户安装，没有管理员权限，也不该写 HKLM）: %s'


def _require_hkcu(hive):
    """非 Windows 上 winreg 是 None；在 Windows 上只准 HKCU 一个 hive 过闸。"""
    if hive is not winreg.HKEY_CURRENT_USER:
        raise ValueError(_NO_HKLM % hive)


def build_command(exe, placeholder):
    """拼注册表里存的那一行命令：两个部分各自加引号。

    exe 或占位符里带双引号的话 Windows 命令行没法表达（没有转义机制），
    直接拒绝（返回空串），绝不写一条「引号会咬人」的命令进注册表。
    """
    if not exe or '"' in exe or '"' in placeholder:
        return ''
    return '"%s" "%s"' % (exe, placeholder)


def parse_command(cmd):
    """把 build_command 的产物拆回 (exe, 占位符)；对不上返回 (None, None)。

    status() 拿它回读校验：注册表里那行命令必须能原样拆出这两段，
    拆不出或 exe 对不上，就是「键被别的程序改过」。
    """
    match = re.match(r'^"([^"]*)"\s+"([^"]*)"$', cmd or '')
    if not match:
        return None, None
    return match.group(1), match.group(2)


def resolve_exe_path(explicit=None):
    """该往注册表里写哪个程序路径。

    优先级：点名给的（安装器/验收/将来别的入口）> 打包后的 sys.executable
    （装机成品就是 PDFToolkit.exe 本身）> 默认的按用户安装路径（源码模式下，
    右键菜单是给「装进这台电脑的程序」用的，本机装着它就该指它）> 源码运行的解释器。
    """
    if explicit:
        return str(explicit).strip()
    if getattr(sys, 'frozen', False):
        return sys.executable
    local = os.environ.get('LOCALAPPDATA') or ''
    candidate = os.path.join(local, 'Programs', 'PDF Toolkit', APP_EXE_NAME)
    if os.path.isfile(candidate):
        return candidate
    return sys.executable


def _labels():
    """注册表里给人看的两段名字，都从十本字典取（缺键先显示键名，补字典后自动变对）。"""
    app = _text('app_title')
    entry = _text('shell_open_entry', name=app)
    return app, entry


def _expected_entries(exe):
    """register 要写的全部键与值，status 也拿同一份去回读比对——口径只有一处。"""
    app, entry = _labels()
    open_command = build_command(exe, PLACEHOLDER_OPENWITH)
    menu_command = build_command(exe, PLACEHOLDER_MENU)
    return [
        {'id': 'openwith_entry', 'key': _OPEN_KEY,
         'values': {'': entry, 'FriendlyAppName': app}},
        {'id': 'openwith_command', 'key': _OPEN_COMMAND,
         'values': {'': open_command}},
        {'id': 'openwith_type', 'key': _TYPE_KEY, 'values': {'': '.pdf'}},
        {'id': 'menu_entry', 'key': VERB_ROOT,
         'values': {'': entry, 'Icon': '"%s",0' % exe,
                    'MultiSelectModel': 'Player'}},
        {'id': 'menu_command', 'key': _VERB_COMMAND, 'values': {'': menu_command}},
    ]


def _brief(error):
    """异常摘要：带异常类型（现场能分清是不是权限），压进一行，只给用户看得懂的那截。"""
    return '%s: %s' % (type(error).__name__, str(error).strip().splitlines()[0][:120])


def _reason_message(reason, detail=''):
    """四种失败现场各归一条字典键；detail 只进日志，不混进给用户的话。"""
    if reason == 'access_denied':
        return _text('api_shell_access_denied')
    if reason == 'not_windows':
        return _text('api_shell_not_windows')
    if reason == 'no_exe':
        return _text('api_shell_no_exe')
    if reason == 'bad_exe':
        return _text('api_shell_bad_exe')
    message = _text('api_shell_error')
    return '%s：%s' % (message, detail) if detail else message


def _read_entry(expect):
    """回读一个键：missing（键或值不在）/ ok（与要写的一字不差）/
    foreign（在，但内容不是我们的——被别的程序改过）/ error（读不动，如实报）。"""
    states = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, expect['key'], 0,
                            winreg.KEY_READ) as key:
            for name, want in expect['values'].items():
                try:
                    got = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    states.append('missing')
                    continue
                states.append('ok' if str(got) == str(want) else 'foreign')
    except FileNotFoundError:
        return 'missing', None
    except OSError as error:
        if isinstance(error, PermissionError):
            return 'error', 'access_denied'
        return 'error', _brief(error)
    except Exception as error:  # 假 winreg / 意外对象也不许掀桌
        return 'error', _brief(error)
    if 'foreign' in states:
        return 'foreign', None
    if 'missing' in states:
        return 'missing', None
    return 'ok', None


def status(exe_path=None):
    """右键菜单现状：是否已注册、注册在哪几个键、每个键是什么状态。

    界面（设置里那行说明）与验收都读这里。registered 只在「五个键全部 ok」
    时为真——键在但内容被别的程序改过，算未注册（foreign 点名列出）。
    """
    out = {'registered': False, 'enabled': False, 'reason': '', 'message': '',
           'exe': '', 'command': '', 'keys': [], 'entries': []}
    if winreg is None or not IS_WINDOWS:
        out['reason'] = 'not_windows'
        out['message'] = _reason_message('not_windows')
        return out
    exe = resolve_exe_path(exe_path)
    out['exe'] = exe
    if not exe:
        out['reason'] = 'no_exe'
        out['message'] = _reason_message('no_exe')
        return out
    out['command'] = build_command(exe, PLACEHOLDER_MENU)
    for expect in _expected_entries(exe):
        state, error = _read_entry(expect)
        out['entries'].append({'id': expect['id'],
                               'key': 'HKCU\\' + expect['key'],
                               'state': state, 'detail': error or ''})
        if state != 'error':
            out['keys'].append('HKCU\\' + expect['key'])
    registered = all(e['state'] == 'ok' for e in out['entries'])
    out['registered'] = registered
    out['enabled'] = registered
    if any(e['state'] == 'error' for e in out['entries']):
        out['reason'] = 'error'
        out['message'] = _reason_message('error')
    return out


def _write_entry(expect):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, expect['key']) as key:
        for name, data in expect['values'].items():
            # SetValueEx 的 value_name 给空串写的就是「默认值」；
            # 具名值（FriendlyAppName / Icon / MultiSelectModel）走同一个入口。
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)


def _delete_owned_roots():
    """best-effort 删掉自己那两棵树；不存在的算成功。返回 (removed, absent, failed)。"""
    removed, absent, failed = [], [], []
    for root in OWNED_ROOTS:
        try:
            _require_hkcu(winreg.HKEY_CURRENT_USER)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, root)
            removed.append(root)
        except FileNotFoundError:
            absent.append(root)
        except PermissionError:
            failed.append((root, 'access_denied'))
        except Exception as error:
            failed.append((root, _brief(error)))
    return removed, absent, failed


def failure_result(action='', reason='error'):
    """给桥接层（api/shell.py）兜底用的同形状失败结果——取话也走这里，
    免得 api 层自己 import t()，让「话在字典里」这条闸有两个口径。"""
    return {'success': False, 'enabled': False, 'registered': False,
            'action': action, 'reason': reason, 'keys': [], 'entries': [],
            'message': _reason_message(reason)}


def register(exe_path=None):
    """启用：把「打开方式」条目与 .pdf 右键菜单项写进 HKCU 自己那一片。

    * 键已存在：照常覆写自己的值（重复注册幂等，不产生重复条目）；
    * 没有权限（PermissionError）：reason='access_denied'，并回滚本次新建的键；
    * 其它异常：reason='error'，带异常摘要，同样回滚；
    * 成功与否不只看「写没抛异常」——写完回读 status()，五个键全对得上才报成功。
    """
    out = {'success': False, 'enabled': False, 'action': 'register',
           'reason': '', 'message': '', 'exe': '', 'keys': [], 'entries': []}
    if winreg is None or not IS_WINDOWS:
        out['reason'] = 'not_windows'
        out['message'] = _reason_message('not_windows')
        return out
    exe = resolve_exe_path(exe_path)
    out['exe'] = exe
    if not exe:
        out['reason'] = 'no_exe'
        out['message'] = _reason_message('no_exe')
        return out
    if '"' in exe:
        out['reason'] = 'bad_exe'
        out['message'] = _reason_message('bad_exe')
        return out
    try:
        for expect in _expected_entries(exe):
            _write_entry(expect)
    except PermissionError:
        _delete_owned_roots()  # 半截状态比失败更糟：能撤的全撤掉
        out['reason'] = 'access_denied'
        out['message'] = _reason_message('access_denied')
        out['enabled'] = status(exe)['enabled']
        return out
    except Exception as error:
        logging.exception('写入右键菜单注册表失败')
        _delete_owned_roots()
        out['reason'] = 'error'
        out['message'] = _reason_message('error', _brief(error))
        out['enabled'] = status(exe)['enabled']
        return out
    checked = status(exe)
    out['entries'] = checked['entries']
    out['keys'] = checked['keys']
    out['enabled'] = checked['enabled']
    out['success'] = checked['registered']
    out['message'] = (_text('api_shell_registered') if out['success']
                      else _text('api_shell_error'))
    if not out['success']:
        out['reason'] = 'verify_failed'
    return out


def _owned_present():
    """那五个键在不在（不比内容）。注销失败时靠它说真话：
    「没删掉」就是还在开着——内容对不对得上是 status() 的事，存在性归存在性。"""
    for path in (_OPEN_KEY, _OPEN_COMMAND, _TYPE_KEY, VERB_ROOT, _VERB_COMMAND):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ):
                pass
        except Exception:
            return False
    return True


def unregister():
    """停用：只删自己那两棵树（见 OWNED_ROOTS），其余一个键都不碰。

    * 键本来就不存在：算成功（absent 点名列出），不谎报「删了」；
    * 删除被拒（PermissionError）：failed 点名 + reason，success=False——
      并且回读真实状态：没删掉的话 enabled 仍是 True，绝不报「已关闭」。
    """
    out = {'success': False, 'enabled': False, 'action': 'unregister',
           'reason': '', 'message': '', 'removed': [], 'absent': [], 'failed': []}
    if winreg is None or not IS_WINDOWS:
        out['reason'] = 'not_windows'
        out['message'] = _reason_message('not_windows')
        return out
    removed, absent, failed = _delete_owned_roots()
    out['removed'], out['absent'], out['failed'] = removed, absent, failed
    try:
        still_there = _owned_present()
    except Exception:
        still_there = True
    out['enabled'] = still_there
    if failed:
        reason = ('access_denied' if any(f[1] == 'access_denied' for f in failed)
                  else 'error')
        out['reason'] = reason
        detail = '; '.join('%s -> %s' % f for f in failed)
        out['message'] = _reason_message(reason, detail)
    else:
        # 删干净了才算关闭；键还在的话（不该发生）如实报 verify_failed
        out['success'] = not still_there
        out['message'] = (_text('api_shell_unregistered') if out['success']
                          else _text('api_shell_error'))
        if not out['success']:
            out['reason'] = 'verify_failed'
    return out
