"""Transcribe an original English WEM, translate it locally, then synthesize one target WEM."""
from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
from pathlib import Path


# The GUI captures this process output as UTF-8.  Windows otherwise selects a
# legacy console code page, which can fail after a successful translation.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass


NLLB_TARGET_LANGUAGES = {
    'de': 'deu_Latn', 'en': 'eng_Latn', 'es': 'spa_Latn', 'fr': 'fra_Latn',
    'it': 'ita_Latn', 'ja': 'jpn_Jpan', 'pl': 'pol_Latn',
    'ptbr': 'por_Latn', 'zhhans': 'zho_Hans',
}

TRANSLATEGEMMA_TARGET_LANGUAGES = {
    'de': ('German', 'de-DE'), 'en': ('English', 'en-US'),
    'es': ('Spanish', 'es-ES'), 'fr': ('French', 'fr-FR'),
    'it': ('Italian', 'it-IT'), 'ja': ('Japanese', 'ja-JP'),
    'pl': ('Polish', 'pl-PL'), 'ptbr': ('Portuguese (Brazil)', 'pt-BR'),
    'zhhans': ('Chinese (Simplified)', 'zh-CN'),
}


def translate_english_with_nllb(text: str, model_path: Path, target_language: str) -> str:
    """Translate English ASR text locally, releasing CUDA before TTS is loaded."""
    target_code = NLLB_TARGET_LANGUAGES.get(target_language.lower())
    if not target_code:
        raise ValueError(f'NLLB does not have a configured target code for {target_language!r}')
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    print('TRANSLATE loading local NLLB-200 distilled 600M', flush=True)
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), src_lang='eng_Latn', local_files_only=True)
    model = AutoModelForSeq2SeqLM.from_pretrained(
        str(model_path), torch_dtype=torch.float16, local_files_only=True,
    ).to('cuda').eval()
    try:
        encoded = tokenizer(text, return_tensors='pt', truncation=True, max_length=512).to('cuda')
        generated = model.generate(
            **encoded, forced_bos_token_id=tokenizer.convert_tokens_to_ids(target_code),
            max_new_tokens=200, num_beams=4, do_sample=False,
        )
        return tokenizer.batch_decode(generated, skip_special_tokens=True)[0].strip()
    finally:
        del model
        gc.collect()
        torch.cuda.empty_cache()


def translate_english_with_translategemma(
    text: str, model_path: Path, llama_cli: Path, target_language: str,
) -> str:
    """Translate on CUDA with the local Q4 TranslateGemma GGUF.

    ``-ngl all`` deliberately rejects CPU layer offload.  The process exits
    before the selected TTS model is loaded, releasing its GPU allocation.
    """
    target = TRANSLATEGEMMA_TARGET_LANGUAGES.get(target_language.lower())
    if not target:
        raise ValueError(f'TranslateGemma has no configured target for {target_language!r}')
    if not model_path.is_file() or not llama_cli.is_file():
        raise FileNotFoundError('TranslateGemma Q4 model or CUDA llama.cpp runtime is missing')
    target_name, target_code = target
    prompt = (
        f'You are a professional English (en) to {target_name} ({target_code}) translator. '
        f'Your goal is to accurately convey the meaning and nuances of the original English text '
        f'while adhering to {target_name} grammar, vocabulary, and cultural sensitivities. '
        f'Produce only the {target_name} translation, without any additional explanations or commentary. '
        f'Please translate the following English text into {target_name}:\n\n\n{text}'
    )
    print('TRANSLATE loading local TranslateGemma 4B Q4 on CUDA', flush=True)
    command = [
        str(llama_cli), '-m', str(model_path), '-ngl', 'all', '-p', prompt,
        '-n', '160', '--temp', '0', '--no-warmup', '--no-jinja',
        '--no-conversation', '--single-turn', '--no-display-prompt',
        '--simple-io', '--log-disable',
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f'TranslateGemma CUDA failed: {detail[-1000:]}')
    # llama-cli writes its loading banner and timing statistics to stdout too.
    # The response begins immediately after the final occurrence of the exact
    # English input, and finishes before the first timing/status line.
    raw_output = completed.stdout.replace('\r\n', '\n')
    marker = raw_output.rfind(text)
    if marker < 0:
        raise RuntimeError('TranslateGemma output did not contain the source transcript marker')
    response_tail = raw_output[marker + len(text):]
    response_lines: list[str] = []
    for line in response_tail.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') or stripped == 'Exiting...':
            break
        if stripped:
            response_lines.append(stripped)
    translated = ' '.join(response_lines).strip()
    if not translated:
        raise RuntimeError('TranslateGemma returned an empty target-language translation')
    return translated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--reader', type=Path, required=True)
    parser.add_argument('--decoder', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--language', required=True)
    parser.add_argument('--output-wem', type=Path, required=True)
    parser.add_argument('--wwise-console', type=Path, required=True)
    parser.add_argument('--wwise-project', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--phonetic-dictionary-root', type=Path, required=True)
    parser.add_argument('--whisper-model', type=Path, required=True)
    parser.add_argument('--asr-runtime', type=Path, required=True)
    parser.add_argument('--asr-script', type=Path, required=True)
    parser.add_argument('--translation-backend', choices=('nllb', 'translategemma'), default='nllb')
    parser.add_argument('--nllb-model', type=Path)
    parser.add_argument('--translategemma-model', type=Path)
    parser.add_argument('--llama-cli', type=Path)
    parser.add_argument('--voxcpm-steps', type=int, default=6)
    parser.add_argument('--number', type=int, default=0)
    args = parser.parse_args()

    from full_voxcpm2_batch import (
        configure_phonetic_dictionary_root, extract_and_decode_english_wav,
        generate_with_engine, load_engine, normalize_and_encode_generated_wem,
        stable_seed, synthesis_text,
    )
    configure_phonetic_dictionary_root(args.phonetic_dictionary_root)
    relative = Path(*args.source.replace('\\', '/').split('/'))
    source_wem = args.work_dir / relative
    reference_wav = source_wem.with_suffix('.wav')
    target_wav = args.work_dir / 'target' / relative.with_suffix('.wav')
    args.work_dir.mkdir(parents=True, exist_ok=True)

    print('TRANSCRIBE extracting original English WEM', flush=True)
    extract_and_decode_english_wav(args.reader, args.decoder, args.archive, args.source,
                                   source_wem, reference_wav, args.number, args.number, False)
    # The source is always the extracted English WEM.  Do not infer language
    # from the selected target or from its existing subtitle.
    print('TRANSCRIBE Whisper input language=en', flush=True)
    if not args.asr_runtime.is_file() or not args.asr_script.is_file():
        raise RuntimeError('Dedicated ASR runtime or helper script is missing')
    asr_process = subprocess.run(
        [str(args.asr_runtime), str(args.asr_script), '--wav', str(reference_wav),
         '--model', str(args.whisper_model), '--language', 'en'],
        check=False, capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    if asr_process.returncode:
        detail = (asr_process.stderr or asr_process.stdout).strip()
        raise RuntimeError(f'English Whisper transcription failed: {detail[-1000:]}')
    transcript = ''
    for line in asr_process.stdout.splitlines():
        if line.startswith('ASR_TRANSCRIPT_JSON='):
            transcript = json.loads(line.partition('=')[2])
            break
    if not transcript:
        raise RuntimeError('Whisper returned an empty English transcript')
    print(f'TRANSCRIBE English: {transcript}', flush=True)
    if args.translation_backend == 'translategemma':
        translated = translate_english_with_translategemma(
            transcript, args.translategemma_model, args.llama_cli, args.language,
        )
        translator_name = 'TranslateGemma'
    else:
        if args.nllb_model is None:
            raise RuntimeError('NLLB model path is missing')
        translated = translate_english_with_nllb(transcript, args.nllb_model, args.language)
        translator_name = 'NLLB'
    if not translated:
        raise RuntimeError(f'{translator_name} returned an empty target-language translation')
    print(f'TRANSLATE target ({args.language}): {translated}', flush=True)

    synthesis = synthesis_text(translated, args.language, args.engine)
    if synthesis != translated:
        print(f'GENERATE phonetic synthesis text: {synthesis}', flush=True)
    print(f'GENERATE loading {args.engine}', flush=True)
    model = load_engine(args.engine, args.model)
    try:
        audio, rate = generate_with_engine(
            args.engine, model, synthesis, reference_wav, args.work_dir,
            args.language, args.voxcpm_steps, stable_seed(args.source),
        )
        print('GENERATE normalizing and encoding target WEM', flush=True)
        normalize_and_encode_generated_wem(
            audio, rate, reference_wav, target_wav, args.output_wem,
            args.wwise_console, args.wwise_project, args.decoder,
            args.number, args.number,
        )
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
    print(f'TRANSCRIBE TRANSLATE GENERATE complete output={args.output_wem}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
