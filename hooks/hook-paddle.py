# Collect paddle without importing paddle.jit.sot (PyInstaller's isolated
# submodule import crashes there on this machine). Modules are enumerated from
# the package directory; binaries and data come from PyInstaller helpers.
import os
import importlib.util

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

spec = importlib.util.find_spec('paddle')
root = os.path.dirname(spec.origin)

datas = collect_data_files('paddle')
binaries = collect_dynamic_libs('paddle')
hiddenimports = []

for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != '__pycache__']
    rel = os.path.relpath(dirpath, root)
    parts = [] if rel == '.' else rel.split(os.sep)
    if any(p in ('tests', 'test') for p in parts) or '/'.join(parts).startswith('jit/sot'):
        dirnames[:] = []
        continue
    if '__init__.py' in filenames:
        hiddenimports.append('paddle' + ('.' + '.'.join(parts) if parts else ''))
    for name in filenames:
        if name.endswith('.py') and name != '__init__.py':
            hiddenimports.append('paddle.' + '.'.join(parts + [name[:-3]]))
        elif name.endswith(('.pyd', '.so', '.dll')):
            base = name.split('.')[0]
            if base and not base.startswith('_'):
                hiddenimports.append('paddle.' + '.'.join(parts + [base]))
