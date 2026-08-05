"""Regenerate one review-table item inside the selected model's own runtime."""
from __future__ import annotations

import argparse
import gc
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--reader', type=Path, required=True)
    parser.add_argument('--decoder', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target-text', required=True)
    parser.add_argument('--language', required=True)
    parser.add_argument('--output-wem', type=Path, required=True)
    parser.add_argument('--wwise-console', type=Path, required=True)
    parser.add_argument('--wwise-project', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--phonetic-dictionary-root', type=Path, required=True)
    parser.add_argument('--voxcpm-steps', type=int, default=6)
    parser.add_argument('--number', type=int, default=0)
    args = parser.parse_args()

    from full_voxcpm2_batch import (
        extract_and_decode_english_wav, generate_with_engine, load_engine,
        normalize_and_encode_generated_wem, stable_seed, configure_phonetic_dictionary_root,
    )
    configure_phonetic_dictionary_root(args.phonetic_dictionary_root)
    relative = Path(*args.source.replace('\\', '/').split('/'))
    temp_wem = args.work_dir / relative
    reference_wav = temp_wem.with_suffix('.wav')
    target_wav = args.work_dir / 'target' / relative.with_suffix('.wav')
    args.work_dir.mkdir(parents=True, exist_ok=True)
    print('MANUAL REGEN extract English WEM', flush=True)
    extract_and_decode_english_wav(args.reader, args.decoder, args.archive, args.source, temp_wem, reference_wav, args.number, args.number, False)
    print(f'MANUAL REGEN loading {args.engine}', flush=True)
    model = load_engine(args.engine, args.model)
    try:
        print('MANUAL REGEN synthesizing target-language WAV', flush=True)
        audio, rate = generate_with_engine(args.engine, model, args.target_text, reference_wav, args.work_dir, args.language, args.voxcpm_steps, stable_seed(args.source))
        print('MANUAL REGEN normalizing and encoding WEM', flush=True)
        normalize_and_encode_generated_wem(audio, rate, reference_wav, target_wav, args.output_wem, args.wwise_console, args.wwise_project, args.decoder, args.number, args.number)
    finally:
        del model
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    if not args.output_wem.is_file() or args.output_wem.stat().st_size == 0:
        raise RuntimeError('Wwise did not produce the target WEM')
    print(f'MANUAL REGEN complete output={args.output_wem}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
