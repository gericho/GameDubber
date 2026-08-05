"""Versioned local build for GameDubber.

Each invocation snapshots the editable GUI sources, increments the ALPHA
version embedded in the title, writes a fresh build timestamp, compiles and
updates the root EXE.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GUI = ROOT / 'game_dubber_gui.py'
BACKUP_FILES = (
    'game_dubber_gui.py', 'xtranslator_mapper.py', 'full_voxcpm2_batch.py',
    'manual_review_regenerate.py', 'phonetic_dictionaries.py',
    'GameDubber.spec', 'build_game_dubber.py', 'GameDubber.exe',
)


def update_title_metadata() -> tuple[str, str]:
    text = GUI.read_text(encoding='utf-8')
    match = re.search(r'APP_VERSION = "ALPHA 0\.1\.(\d+)"', text)
    if match is None:
        raise RuntimeError('APP_VERSION does not have the expected ALPHA 0.1.N format')
    version = f'ALPHA 0.1.{int(match.group(1)) + 1}'
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    text = text[:match.start()] + f'APP_VERSION = "{version}"' + text[match.end():]
    text, replaced = re.subn(r'BUILD_TIMESTAMP = "[^"]*"', f'BUILD_TIMESTAMP = "{timestamp}"', text, count=1)
    if replaced != 1:
        raise RuntimeError('BUILD_TIMESTAMP was not found')
    GUI.write_text(text, encoding='utf-8', newline='\n')
    return version, timestamp


def snapshot_sources() -> Path:
    destination = ROOT / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
    destination.mkdir(parents=True, exist_ok=False)
    for name in BACKUP_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    return destination


def main() -> int:
    backup = snapshot_sources()
    version, timestamp = update_title_metadata()
    subprocess.run([sys.executable, '-m', 'py_compile', 'game_dubber_gui.py', 'xtranslator_mapper.py', 'manual_review_regenerate.py'], cwd=ROOT, check=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', 'GameDubber.spec'], cwd=ROOT, check=True)
    built = ROOT / 'dist' / 'GameDubber.exe'
    if not built.is_file():
        raise RuntimeError('PyInstaller did not produce dist\\GameDubber.exe')
    shutil.copy2(built, ROOT / 'GameDubber.exe')
    print(f'Built {version} at {timestamp}; backup: {backup}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
