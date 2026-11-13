"""由 core/constants.py 生成打包与安装脚本需要的版本文件。

版本号只有一个来源（core/constants.py 的 CURRENT_VERSION），
PyInstaller 的版本资源和 Inno Setup 的版本定义都从这里派生，
避免出现"改了代码忘了改安装脚本"的情况。

生成：
    installer/version_info.txt   PyInstaller 的 Windows 版本资源
    installer/version.iss        Inno Setup 的版本定义

version_info.txt 里的 FileVersion 必须是纯数字，界面标题那份由 APP_TITLE
带进 FileDescription（"PDF Toolkit 7.0.1"）。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.constants import (  # noqa: E402
    APP_AUTHOR,
    APP_COPYRIGHT,
    APP_NAME,
    APP_TITLE,
    CURRENT_VERSION,
)
from core.paths import APP_ID  # noqa: E402  包标识属于路径层，和配置目录名同源

VERSION_INFO_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', '{author}'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{app_id}'),
         StringStruct('LegalCopyright', '{copyright}'),
         StringStruct('OriginalFilename', '{app_id}.exe'),
         StringStruct('ProductName', '{name}'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def _version_tuple(version):
    """把 "1.2.3" 变成 (1, 2, 3, 0)，固定四位。"""
    parts = []
    for chunk in str(version).split('.'):
        digits = ''.join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def main():
    version = CURRENT_VERSION

    version_info = VERSION_INFO_TEMPLATE.format(
        version_tuple=_version_tuple(version),
        version=version,
        author=APP_AUTHOR,
        copyright=APP_COPYRIGHT,
        description=APP_TITLE,
        app_id=APP_ID,
        name=APP_NAME,
    )
    info_path = os.path.join(HERE, 'version_info.txt')
    with open(info_path, 'w', encoding='utf-8') as handle:
        handle.write(version_info)

    iss_path = os.path.join(HERE, 'version.iss')
    with open(iss_path, 'w', encoding='utf-8') as handle:
        handle.write(f'#define MyAppVersion "{version}"\n')
        handle.write(f'#define MyAppName "{APP_NAME}"\n')
        handle.write(f'#define MyAppPublisher "{APP_AUTHOR}"\n')
        handle.write('#define MyAppExeName "PDFToolkit.exe"\n')

    print(f'[version] {APP_NAME} v{CURRENT_VERSION}')
    print(f'[version] -> {info_path}')
    print(f'[version] -> {iss_path}')


if __name__ == '__main__':
    main()
