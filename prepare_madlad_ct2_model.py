"""Explicit one-time MADLAD conversion launcher; never runs automatically."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description='Convert local MADLAD weights to CTranslate2 int8-float16.')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (args.source / 'model.safetensors').is_file():
        raise FileNotFoundError(args.source / 'model.safetensors')
    if args.output.exists():
        raise FileExistsError(f'Refusing to overwrite conversion output: {args.output}')
    command = [
        sys.executable, '-m', 'ctranslate2.converters.transformers',
        '--model', str(args.source), '--output_dir', str(args.output),
        '--quantization', 'int8_float16', '--copy_files',
        'spiece.model', 'tokenizer_config.json', 'generation_config.json',
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
