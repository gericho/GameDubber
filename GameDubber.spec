# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['game_dubber_gui.py'],
    pathex=[],
    binaries=[('archive_reader\\target\\release\\archive_reader.exe', '.'), ('tools\\vgmstream\\vgmstream-cli.exe', '.'), ('tools\\vgmstream\\*.dll', '.')],
    datas=[('asr_validation.py', '.'), ('full_voxcpm2_batch.py', '.'), ('manual_review_regenerate.py', '.'), ('manual_review_transcribe_asr.py', '.'), ('manual_review_transcribe_translate_generate.py', '.'), ('voxcpm_fallback_batch.py', '.'), ('phonetic_dictionaries.py', '.'), ('tools\\acknowledge.wav', 'tools')],
    hiddenimports=['phonetic_dictionaries'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GameDubber',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
