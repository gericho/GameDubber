"""Run one English Whisper transcription in the dedicated ASR runtime."""
from __future__ import annotations

import argparse
import json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--wav', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--language', default='en')
    args = parser.parse_args()

    from full_voxcpm2_batch import transcribe_checkpoint_wav

    transcript = transcribe_checkpoint_wav(args.wav, args.model, args.language)
    if not transcript:
        raise RuntimeError('Whisper returned an empty transcript')
    print('ASR_TRANSCRIPT_JSON=' + json.dumps(transcript, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
