"""Manual WEM regeneration helper, optionally retained as a model server."""
from __future__ import annotations

import argparse
import gc
import json
import shutil
import sys
import traceback
from pathlib import Path


def _release_model(model) -> None:
    if model is not None and hasattr(model, 'close'):
        model.close()
    del model
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _generate(args, model, job: dict) -> None:
    from full_voxcpm2_batch import (
        extract_and_decode_english_wav, generate_with_engine,
        normalize_and_encode_generated_wem, stable_seed, synthesis_text,
    )
    source = str(job['source'])
    archive = Path(job['archive'])
    work_dir = Path(job['work_dir'])
    output_wem = Path(job['output_wem'])
    number = int(job.get('number', 0))
    relative = Path(*source.replace('\\', '/').split('/'))
    temp_wem = work_dir / relative
    reference_wav = temp_wem.with_suffix('.wav')
    target_wav = work_dir / 'target' / relative.with_suffix('.wav')
    work_dir.mkdir(parents=True, exist_ok=True)
    print('MANUAL REGEN extract English WEM', flush=True)
    extract_and_decode_english_wav(args.reader, args.decoder, archive, source, temp_wem, reference_wav, number, number, False)
    target_text = synthesis_text(
        str(job['target_text']).strip(), str(job['language']), args.engine,
    )
    if not target_text.strip():
        raise ValueError('the selected row has no target-language subtitle')
    print('MANUAL REGEN synthesizing target-language WAV', flush=True)
    audio, rate = generate_with_engine(
        args.engine, model, target_text, reference_wav, work_dir,
        str(job['language']), int(job.get('voxcpm_steps', 6)), stable_seed(source),
    )
    print('MANUAL REGEN normalizing and encoding WEM', flush=True)
    normalize_and_encode_generated_wem(
        audio, rate, reference_wav, target_wav, output_wem, args.wwise_console,
        args.wwise_project, args.decoder, number, number,
    )
    if not output_wem.is_file() or output_wem.stat().st_size == 0:
        raise RuntimeError('Wwise did not produce the target WEM')
    print(f'MANUAL REGEN complete output={output_wem}', flush=True)


def _job_from_arguments(args) -> dict:
    required = ('archive', 'source', 'target_text', 'language', 'output_wem', 'work_dir')
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        raise ValueError('missing required manual regeneration arguments: ' + ', '.join(missing))
    return {
        'archive': str(args.archive), 'source': args.source, 'target_text': args.target_text,
        'language': args.language, 'output_wem': str(args.output_wem),
        'work_dir': str(args.work_dir), 'voxcpm_steps': args.voxcpm_steps,
        'number': args.number,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--reader', type=Path, required=True)
    parser.add_argument('--decoder', type=Path, required=True)
    parser.add_argument('--wwise-console', type=Path, required=True)
    parser.add_argument('--wwise-project', type=Path, required=True)
    parser.add_argument('--phonetic-dictionary-root', type=Path, required=True)
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--source')
    parser.add_argument('--target-text')
    parser.add_argument('--language')
    parser.add_argument('--output-wem', type=Path)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--voxcpm-steps', type=int, default=6)
    parser.add_argument('--number', type=int, default=0)
    args = parser.parse_args()

    from full_voxcpm2_batch import configure_phonetic_dictionary_root, load_engine
    configure_phonetic_dictionary_root(args.phonetic_dictionary_root)
    print(f'MANUAL REGEN loading {args.engine}', flush=True)
    model = load_engine(args.engine, args.model)
    if not args.server:
        try:
            _generate(args, model, _job_from_arguments(args))
            return 0
        finally:
            _release_model(model)

    print('MANUAL REGEN SERVER READY', flush=True)
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request.get('command') == 'shutdown':
                    print('MANUAL REGEN SERVER STOPPING', flush=True)
                    break
                _generate(args, model, request)
                print('MANUAL_REGEN_RESULT ' + json.dumps({'ok': True}, separators=(',', ':')), flush=True)
            except Exception as error:
                traceback.print_exc()
                print('MANUAL_REGEN_RESULT ' + json.dumps({'ok': False, 'error': str(error)}, separators=(',', ':')), flush=True)
    finally:
        _release_model(model)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
