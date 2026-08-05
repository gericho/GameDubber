import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def norm(value: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]+", " ", value.lower()).split())


def overlap(left: str, right: str) -> float:
    a, b = norm(left), norm(right)
    return round(len(a & b) / max(1, len(a | b)), 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--jobs', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    jobs = [json.loads(line) for line in Path(args.jobs).read_text(encoding='utf-8').splitlines() if line.strip()]
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    print('Loading Whisper large-v3-turbo on CUDA for validation samples...', flush=True)
    model = WhisperModel(args.model, device='cuda', compute_type='int8_float16')
    completed = 0
    with output.open('w', encoding='utf-8', newline='\n') as handle:
        for number, job in enumerate(jobs, 1):
            try:
                segments, info = model.transcribe(job['workspace_wav_path'], language='en', task='transcribe', beam_size=5, vad_filter=False)
                text = ' '.join(segment.text.strip() for segment in segments).strip()
                score = overlap(text, job.get('english_subtitle', ''))
                record = {**job, 'asr_status':'complete', 'asr_backend':'faster-whisper', 'asr_model':'large-v3-turbo', 'asr_device':'cuda', 'asr_compute_type':'int8_float16', 'asr_language':info.language, 'asr_language_probability':round(info.language_probability,4), 'asr_text':text, 'english_overlap':score, 'asr_at':datetime.now(timezone.utc).isoformat()}
                completed += 1
                print(f'ASR validation sample | overlap {score:.3f} | {text[:110]}', flush=True)
            except Exception as error:
                record = {**job, 'asr_status':'failed', 'asr_error':str(error), 'asr_at':datetime.now(timezone.utc).isoformat()}
                print(f'ASR validation sample failed: {error}', flush=True)
            handle.write(json.dumps(record, ensure_ascii=False)+'\n'); handle.flush()
    print(f'ASR validation complete | Report: {output}', flush=True)
    return 0 if completed == len(jobs) else 1

if __name__ == '__main__':
    raise SystemExit(main())