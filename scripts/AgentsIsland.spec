# -*- mode: python ; coding: utf-8 -*-
# Agents Island — PyInstaller 单文件打包（Windows 侧执行）
#   python -m PyInstaller scripts/AgentsIsland.spec --noconfirm
# 产物 dist/AgentsIsland.exe：
#   双击 = 岛壳，自动 spawn 自身 --bridge 子进程（Windows 本机桥，纯聚合远程）
#   配置 = %USERPROFILE%\.agents-island\settings.json（见 dist 模板）
from pathlib import Path

ROOT = Path(SPECPATH).parent          # apps/agents-island

a = Analysis(
    [str(ROOT / 'win' / 'island.py')],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / 'win' / 'island_config.json'), '.'),
        (str(ROOT / 'win' / 'island.ico'), '.'),
        (str(ROOT / 'win' / 'tray_idle.ico'), '.'),
        (str(ROOT / 'win' / 'tray_frames'), 'tray_frames'),
        (str(ROOT / 'win' / 'tray_sleep'), 'tray_sleep'),
        (str(ROOT / 'win' / 'tray_super'), 'tray_super'),
        (str(ROOT / 'bridge'), 'bridge'),
        (str(ROOT / 'web'), 'web'),
        (str(ROOT / 'hooks'), 'hooks'),
    ],
    hiddenimports=[
        'webview.platforms.winforms',
        'clr_loader', 'pythonnet',
        'sqlite3', 'sqlite3.dbapi2',   # agy_monitor 读 AGY 会话 SQLite
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'PIL', 'playwright', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AgentsIsland',
    icon=str(ROOT / 'win' / 'island.ico'),
    console=False,
    upx=False,
    bootloader_ignore_signals=False,
    strip=False,
)
