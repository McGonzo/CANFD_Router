# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import json

from PyInstaller.utils.hooks import collect_submodules


project_root = Path.cwd()
portable_config = project_root / 'build' / 'portable-configs' / 'default_config.json'
portable_config.parent.mkdir(parents=True, exist_ok=True)
portable_config.write_text(json.dumps({
    'hardware': [],
    'routes': [],
    'algorithms': [],
    'runtime': {
        'queue_size': 5000,
        'rx_timeout_s': 0.02,
        'dispatch_timeout_s': 0.05,
        'monitor_interval_s': 2.0,
    },
}, indent=2), encoding='utf-8')

datas = [
    (str(portable_config), 'configs'),
    (str(project_root / 'configs' / 'example_config.json'), 'configs'),
    (str(project_root / 'zlgcan_x64'), 'zlgcan_x64'),
    (str(project_root / 'zlgcan License.txt'), '.'),
    (str(project_root / 'README.md'), '.'),
]

hiddenimports = []
hiddenimports += collect_submodules('cantools')
hiddenimports += collect_submodules('can')
hiddenimports += collect_submodules('nixnet')


a = Analysis(
    ['main.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tests'],
    noarchive=False,
    optimize=0,
)
# Qt uses the Windows ICU API. An unrelated unversioned ICU DLL found on
# the build machine's PATH (for example Poppler's) has different exports.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() != 'icuuc.dll']
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CANFD_Router',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CANFD_Router_Portable',
)
