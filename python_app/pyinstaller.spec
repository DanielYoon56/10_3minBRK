# -*- mode: python ; coding: utf-8 -*-
# Korea3MinBreakoutTrader PyInstaller Build Spec

block_cipher = None

# python_app 폴더에서 `pyinstaller pyinstaller.spec` 실행
# 결과: dist/Korea3MinBreakoutTrader.exe  — config.json / DB / 상태파일은 EXE 옆에 생성됨
a = Analysis(
    ['main.py'],
    pathex=['..'],            # python_app 패키지 임포트용 (상위 폴더)
    binaries=[],
    datas=[
        ('config.example.json', '.'),
    ],
    hiddenimports=[
        'PySide6',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'requests',
        'sqlite3',
        'tzdata',
        'zoneinfo',
        'python_app.app_config',
        'python_app.broker.kiwoom_rest',
        'python_app.engine.live_engine',
        'python_app.strategy.breakout',
        'python_app.strategy.exit_rules',
        'python_app.db.storage',
        'python_app.notify.telegram',
        'python_app.broker.kiwoom_condition',
        'python_app.screener.accumulation',
        'websocket',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Korea3MinBreakoutTrader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI 창만 표시 (콘솔 숨김)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
