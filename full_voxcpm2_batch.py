"""Fresh, workspace-only full VoxCPM2 voice-over generation batch."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import gc
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import socket
import subprocess
import sys
import traceback
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import wave
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly
from phonetic_dictionaries import load_dictionary


# Single validated XTTS v2 production profile.  The GUI and offline tools
# import this value so a test can never silently use another set of settings.
XTTS_V2_STANDARD_PARAMETERS = {
    "temperature": 0.70,
    "top_p": 0.88,
    "top_k": 55,
    "repetition_penalty": 9.0,
    "do_sample": True,
    "num_beams": 1,
}


_PHONETIC_DICTIONARY_ROOT = Path(__file__).resolve().parent / "work" / "phonetic_dictionaries"


class PauseRequested(Exception):
    """Cooperative stop used between individual ASR/transcoding items."""


def configure_phonetic_dictionary_root(root: Path) -> None:
    """Set the workspace dictionary directory used for per-line synthesis."""
    global _PHONETIC_DICTIONARY_ROOT
    _PHONETIC_DICTIONARY_ROOT = Path(root)


def stable_seed(line_id: str) -> int:
    """Return a reproducible, distinct 31-bit seed for a generated WEM path."""
    digest = hashlib.sha256(line_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "little") & 0x7FFFFFFF


def set_generation_seed(seed: int) -> None:
    """Seed all local RNGs immediately before a single CUDA generation call."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


_ITALIAN_UNITS = (
    'zero', 'uno', 'due', 'tre', 'quattro', 'cinque', 'sei', 'sette', 'otto', 'nove',
    'dieci', 'undici', 'dodici', 'tredici', 'quattordici', 'quindici', 'sedici',
    'diciassette', 'diciotto', 'diciannove',
)
_ITALIAN_TENS = (
    '', '', 'venti', 'trenta', 'quaranta', 'cinquanta', 'sessanta', 'settanta',
    'ottanta', 'novanta',
)


def italian_integer_words(value: int) -> str:
    """Return clear Italian cardinal words for the VoxCPM2 input frontend."""
    if value < 0:
        return 'meno ' + italian_integer_words(-value)
    if value < 20:
        return _ITALIAN_UNITS[value]
    if value < 100:
        tens, unit = divmod(value, 10)
        stem = _ITALIAN_TENS[tens]
        # Italian drops the final vowel before uno and otto.
        if unit in (1, 8):
            stem = stem[:-1]
        return stem if not unit else stem + italian_integer_words(unit)
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        prefix = 'cento' if hundreds == 1 else italian_integer_words(hundreds) + 'cento'
        return prefix if not rest else prefix + ' ' + italian_integer_words(rest)
    if value < 1_000_000:
        thousands, rest = divmod(value, 1000)
        prefix = 'mille' if thousands == 1 else italian_integer_words(thousands) + 'mila'
        return prefix if not rest else prefix + ' ' + italian_integer_words(rest)
    if value < 1_000_000_000:
        millions, rest = divmod(value, 1_000_000)
        prefix = 'un milione' if millions == 1 else italian_integer_words(millions) + ' milioni'
        return prefix if not rest else prefix + ' ' + italian_integer_words(rest)
    billions, rest = divmod(value, 1_000_000_000)
    prefix = 'un miliardo' if billions == 1 else italian_integer_words(billions) + ' miliardi'
    return prefix if not rest else prefix + ' ' + italian_integer_words(rest)


def expand_italian_numbers(text: str) -> str:
    """Expand numbers and percentages only in the private Vox synthesis text."""
    def decimal(match: re.Match[str]) -> str:
        whole, fraction = match.group(1), match.group(2)
        return italian_integer_words(int(whole)) + ' virgola ' + ' '.join(italian_integer_words(int(digit)) for digit in fraction)

    def percentage(match: re.Match[str]) -> str:
        value = match.group(1)
        expanded = re.sub(r'(?<!\w)(\d+)[,.](\d+)(?!\w)', decimal, value)
        if expanded == value:
            expanded = italian_integer_words(int(value))
        return expanded + ' per cento'

    text = re.sub(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*%', percentage, text)
    text = re.sub(r'(?<!\w)(\d+)[,.](\d+)(?!\w)', decimal, text)
    return re.sub(r'(?<!\w)\d+(?!\w)', lambda match: italian_integer_words(int(match.group(0))), text)


def synthesis_text(canonical_text: str, language_code: str, generation_engine: str = "") -> str:
    """Return backend-specific generation text; official subtitles never change."""
    text = canonical_text
    if not generation_engine:
        return text
    # Dictionaries contain only synthesis cues. Canonical subtitles in the
    # database and every user-visible dialogue line remain untouched.
    dictionary = load_dictionary(_PHONETIC_DICTIONARY_ROOT, generation_engine)
    dictionary_language = str(dictionary.get("language", "")).strip().lower()
    # A dictionary is opt-in for its declared target language.  This keeps
    # language-specific phonetic cues from leaking into another selected
    # target language while allowing the user to create dictionaries for any
    # supported language.
    if dictionary_language and dictionary_language != language_code.lower():
        return text
    for rule in dictionary.get("replacements", []):
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        pattern, replacement = rule.get("pattern"), rule.get("replacement")
        if isinstance(pattern, str) and isinstance(replacement, str):
            text = re.sub(pattern, replacement, text)
    if dictionary.get("options", {}).get("expand_numbers", generation_engine == 'voxcpm2' and language_code.lower() == 'it'):
        if language_code.lower() == 'it':
            text = expand_italian_numbers(text)
    if dictionary.get("options", {}).get("remove_terminal_period") and text.endswith("."):
        text = text[:-1].rstrip()
    return text


_TARGET_LANGUAGE_SETTINGS = {
    'de': {'qwen': 'German', 'chatterbox': 'de', 'xtts': 'de', 'zonos': 'de_de'},
    'en': {'qwen': 'English', 'chatterbox': 'en', 'xtts': 'en', 'zonos': 'en_us'},
    'es': {'qwen': 'Spanish', 'chatterbox': 'es', 'xtts': 'es', 'zonos': 'es_es'},
    'fr': {'qwen': 'French', 'chatterbox': 'fr', 'xtts': 'fr', 'zonos': 'fr_fr'},
    'it': {'qwen': 'Italian', 'chatterbox': 'it', 'xtts': 'it', 'zonos': 'it_it'},
    'ja': {'qwen': 'Japanese', 'chatterbox': 'ja', 'xtts': 'ja', 'zonos': 'ja_jp'},
    'pl': {'qwen': None, 'chatterbox': 'pl', 'xtts': 'pl', 'zonos': 'pl_pl'},
    'ptbr': {'qwen': 'Portuguese', 'chatterbox': 'pt', 'xtts': 'pt', 'zonos': 'pt_br'},
    'zhhans': {'qwen': 'Chinese', 'chatterbox': 'zh', 'xtts': 'zh-cn', 'zonos': 'zh_cn'},
}

# Faster-Whisper expects an ISO language identifier rather than the game
# archive locale.  Keep this mapping separate from each TTS backend's own
# locale mapping above: production validation must always follow the language
# chosen in the GUI.
_ASR_LANGUAGE_CODES = {
    'de': 'de', 'en': 'en', 'es': 'es', 'fr': 'fr', 'it': 'it', 'ja': 'ja',
    'pl': 'pl', 'ptbr': 'pt', 'zhhans': 'zh',
}


def asr_language_code(target_language: str) -> str:
    try:
        return _ASR_LANGUAGE_CODES[target_language.lower()]
    except KeyError as error:
        raise ValueError(f'Unsupported ASR target language code: {target_language}') from error


_APOSTROPHE_VARIANTS = "'’‘`´ʼ"
_NUMBER_WORDS: dict[str, dict[str, str]] = {
    # Language data, never an Italian-only rule.  This covers the common
    # small cardinal forms encountered in subtitles; unmatched text is left
    # untouched and continues through the conservative universal comparison.
    'de': {'null': '0', 'eins': '1', 'ein': '1', 'zwei': '2', 'drei': '3', 'vier': '4', 'funf': '5', 'sechs': '6', 'sieben': '7', 'acht': '8', 'neun': '9', 'zehn': '10', 'zwanzig': '20', 'hundert': '100', 'prozent': 'percent'},
    'en': {'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10', 'twenty': '20', 'hundred': '100', 'percent': 'percent'},
    'es': {'cero': '0', 'uno': '1', 'dos': '2', 'tres': '3', 'cuatro': '4', 'cinco': '5', 'seis': '6', 'siete': '7', 'ocho': '8', 'nueve': '9', 'diez': '10', 'veinte': '20', 'cien': '100', 'ciento': '100', 'por ciento': 'percent'},
    'fr': {'zero': '0', 'un': '1', 'une': '1', 'deux': '2', 'trois': '3', 'quatre': '4', 'cinq': '5', 'six': '6', 'sept': '7', 'huit': '8', 'neuf': '9', 'dix': '10', 'vingt': '20', 'cent': '100', 'pour cent': 'percent'},
    'it': {'zero': '0', 'uno': '1', 'un': '1', 'una': '1', 'due': '2', 'tre': '3', 'quattro': '4', 'cinque': '5', 'sei': '6', 'sette': '7', 'otto': '8', 'nove': '9', 'dieci': '10', 'venti': '20', 'cento': '100', 'per cento': 'percent'},
    'pl': {'zero': '0', 'jeden': '1', 'jedna': '1', 'dwa': '2', 'trzy': '3', 'cztery': '4', 'piec': '5', 'szesc': '6', 'siedem': '7', 'osiem': '8', 'dziewiec': '9', 'dziesiec': '10', 'dwadziescia': '20', 'sto': '100', 'procent': 'percent'},
    'ptbr': {'zero': '0', 'um': '1', 'uma': '1', 'dois': '2', 'tres': '3', 'quatro': '4', 'cinco': '5', 'seis': '6', 'sete': '7', 'oito': '8', 'nove': '9', 'dez': '10', 'vinte': '20', 'cem': '100', 'por cento': 'percent'},
}


def normalise_asr_text(text: str, target_language: str = '') -> str:
    """Return a language-aware but non-translating ASR comparison form."""
    value = unicodedata.normalize('NFKD', str(text).lower())
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = value.translate(str.maketrans({char: ' ' for char in _APOSTROPHE_VARIANTS}))
    value = value.replace('%', ' percent ')
    replacements = _NUMBER_WORDS.get(target_language.lower(), {})
    for phrase, number in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        value = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", f" {number} ", value)
    return value


def subtitle_words(text: str, target_language: str = '') -> list[str]:
    """Tokenise universal typography plus optional target-language numbers."""
    return re.findall(r"\w+", normalise_asr_text(text, target_language), flags=re.UNICODE)


def is_nonverbal_subtitle(text: str) -> bool:
    """Return true for an intentionally empty subtitle or a *sound cue*.

    Starfield localisations use fully asterisk-delimited strings for cues such
    as ``*sniffle*`` / ``*tira su col naso*``.  They are deferred to the
    exception phase: a cue can mask real English speech, so English ASR must
    decide between translation and retaining the original WEM.
    """
    value = str(text or '').strip()
    return not value or bool(re.fullmatch(r"\*\s*[^*].*?\s*\*", value, flags=re.DOTALL))


def word_error_rate(expected: list[str], actual: list[str]) -> float:
    """Small dependency-free word-level Levenshtein distance."""
    if not expected:
        return 0.0 if not actual else 1.0
    previous = list(range(len(actual) + 1))
    for index, token in enumerate(expected, 1):
        current = [index]
        for actual_index, actual_token in enumerate(actual, 1):
            current.append(min(
                previous[actual_index] + 1,
                current[actual_index - 1] + 1,
                previous[actual_index - 1] + (token != actual_token),
            ))
        previous = current
    return previous[-1] / len(expected)


def evaluate_asr_match(expected_text: str, transcript: str, target_language: str = '') -> dict[str, object]:
    """Return a conservative, language-neutral acceptance decision.

    This is deliberately aimed at catching missing or radically wrong lines,
    not at rejecting a natural Whisper punctuation/accent variation.
    """
    expected = subtitle_words(expected_text, target_language)
    recognised = subtitle_words(transcript, target_language)
    distance = word_error_rate(expected, recognised)
    shared = sum(min(expected.count(token), recognised.count(token)) for token in set(expected))
    coverage = shared / len(expected) if expected else 1.0
    expected_compact = ''.join(expected)
    recognised_compact = ''.join(recognised)
    character_similarity = SequenceMatcher(None, expected_compact, recognised_compact).ratio() if expected_compact or recognised_compact else 1.0
    # A checkpoint is intended to catch a skipped or substantially wrong
    # sentence.  Whisper may harmlessly join a compound word or vary one
    # inflection, so accept up to a small, clearly bounded variation.
    strict = bool(recognised) and distance <= 0.35 and coverage >= 0.70
    # This fallback is deliberately narrow: it only accepts a nearly
    # identical utterance with a minor elision/spacing variation.  It cannot
    # accept a different sentence merely because it shares a few words.
    relaxed = bool(recognised) and character_similarity >= 0.93 and coverage >= 0.60
    satisfactory = strict or relaxed
    return {
        'satisfactory': satisfactory,
        'match_mode': 'strict' if strict else ('normalized' if relaxed else 'rejected'),
        'expected_word_count': len(expected),
        'recognised_word_count': len(recognised),
        'word_error_rate': round(distance, 4),
        'expected_word_coverage': round(coverage, 4),
        'character_similarity': round(character_similarity, 4),
        'comparison_language': target_language.lower(),
    }


def transcribe_checkpoint_wav(wav_path: Path, model_path: Path, target_language: str) -> str:
    """Load ASR only for one checkpoint, then return all CUDA memory to TTS."""
    from faster_whisper import WhisperModel
    model = None
    try:
        model = WhisperModel(str(model_path), device='cuda', compute_type='int8_float16')
        segments, _info = model.transcribe(
            str(wav_path), language=asr_language_code(target_language), task='transcribe',
            beam_size=5, vad_filter=False,
        )
        return ' '.join(segment.text.strip() for segment in segments).strip()
    finally:
        if model is not None:
            del model
        gc.collect()
        torch.cuda.empty_cache()


def generation_language_setting(engine: str, target_language: str) -> str | None:
    """Return the engine-specific language value for an installed game code."""
    settings = _TARGET_LANGUAGE_SETTINGS.get(target_language.lower())
    if settings is None:
        raise ValueError(f'Unsupported target language code: {target_language}')
    key = {'qwen_0_6b': 'qwen', 'qwen_1_7b': 'qwen', 'chatterbox_v3': 'chatterbox',
           'xtts_v2': 'xtts', 'zonos2_q4': 'zonos'}.get(engine)
    if key is None:
        return None
    language_value = settings[key]
    if language_value is None:
        raise ValueError(f'{engine} does not support target language {target_language}')
    return str(language_value)


def dbfs(value: float) -> float:
    return round(20.0 * math.log10(max(value, 1e-9)), 2)


def audio_metrics(audio: np.ndarray) -> dict[str, float]:
    mono = np.asarray(audio, dtype=np.float32)
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64))) if mono.size else 0.0
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    return {"rms": rms, "peak": peak, "rms_dbfs": dbfs(rms), "peak_dbfs": dbfs(peak)}


def match_reference_format(generated: np.ndarray, generated_rate: int, reference_path: Path) -> tuple[np.ndarray, int, dict[str, float]]:
    """CPU-only resample and gain-match to the decoded English reference."""
    reference, reference_rate = sf.read(reference_path, dtype="float32", always_2d=True)
    target_channels = reference.shape[1]
    output = np.asarray(generated, dtype=np.float32)
    if output.ndim == 1:
        output = output[:, None]
    if output.shape[1] == 1 and target_channels > 1:
        output = np.repeat(output, target_channels, axis=1)
    elif output.shape[1] != target_channels:
        output = output[:, :target_channels]
    # A failed XTTS decode can very rarely return an implausibly long stream.
    # Resampling that stream appears frozen from the GUI even though it can no
    # longer be a usable game line.  Keep a generous duration ceiling based
    # on the original English line before the CPU resampler is invoked.
    expected_generated_frames = max(1, round(len(reference) * int(generated_rate) / int(reference_rate)))
    max_generated_frames = max(int(generated_rate) * 20, expected_generated_frames * 4)
    if len(output) > max_generated_frames:
        print(f"WARNING normalization input trimmed generated_frames={len(output)} limit={max_generated_frames}", flush=True)
        output = output[:max_generated_frames]
    if not np.isfinite(output).all():
        raise RuntimeError('Generated audio contains non-finite samples before normalization')
    if generated_rate != reference_rate:
        factor = math.gcd(int(generated_rate), int(reference_rate))
        output = resample_poly(output, reference_rate // factor, generated_rate // factor, axis=0).astype(np.float32)

    source = audio_metrics(reference)
    before = audio_metrics(output)
    gain = 1.0
    if before["rms"] > 1e-9 and source["rms"] > 1e-9:
        gain = source["rms"] / before["rms"]
    if before["peak"] > 1e-9 and source["peak"] > 1e-9:
        gain = min(gain, source["peak"] / before["peak"])
    output = np.clip(output * gain, -1.0, 1.0).astype(np.float32)
    after = audio_metrics(output)
    metrics = {
        "reference_sample_rate": int(reference_rate),
        "reference_channels": int(target_channels),
        "reference_rms_dbfs": source["rms_dbfs"],
        "reference_peak_dbfs": source["peak_dbfs"],
        "output_rms_dbfs": after["rms_dbfs"],
        "output_peak_dbfs": after["peak_dbfs"],
        "normalization_gain_db": round(20.0 * math.log10(max(gain, 1e-9)), 2),
    }
    return output, int(reference_rate), metrics


def exact_unique_count(voice_map: Path) -> int:
    seen: set[str] = set()
    with voice_map.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            path = str(row.get("source_audio_path", "")).replace("\\", "/").lower()
            if row.get("mapping_status") == "xtranslator_exact" and path:
                seen.add(path)
    return len(seen)


def iter_exact_unique(voice_map: Path):
    seen: set[str] = set()
    with voice_map.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            path = str(row.get("source_audio_path", "")).replace("\\", "/")
            key = path.lower()
            if row.get("mapping_status") != "xtranslator_exact" or not path or key in seen:
                continue
            seen.add(key)
            yield row, path


def write_json_line(handle, row: dict) -> None:
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    handle.flush()


def setup_database(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS production_voice_outputs (
        run_id TEXT NOT NULL, source_audio_path TEXT NOT NULL, target_language TEXT NOT NULL,
        dialogue_id TEXT, voice_id TEXT, canonical_subtitle TEXT NOT NULL, synthesis_text TEXT NOT NULL,
        output_wav_path TEXT NOT NULL, status TEXT NOT NULL, reference_sample_rate INTEGER,
        reference_channels INTEGER, reference_rms_dbfs REAL, reference_peak_dbfs REAL,
        output_rms_dbfs REAL, output_peak_dbfs REAL, normalization_gain_db REAL,
        output_duration_ms INTEGER, error TEXT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
        generation_engine TEXT, voxcpm_steps INTEGER, generation_seed INTEGER, output_opus_path TEXT,
        opus_bitrate_kbps INTEGER, opus_duration_ms INTEGER,
        output_wem_path TEXT, wwise_conversion TEXT, wem_bitrate_kbps INTEGER,
        wem_duration_ms INTEGER,
        PRIMARY KEY(run_id, source_audio_path, target_language))""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(production_voice_outputs)")}
    if "generation_engine" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN generation_engine TEXT")
    if "voxcpm_steps" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN voxcpm_steps INTEGER")
    if "generation_seed" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN generation_seed INTEGER")
    if "output_opus_path" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN output_opus_path TEXT")
    if "opus_bitrate_kbps" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN opus_bitrate_kbps INTEGER")
    if "opus_duration_ms" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN opus_duration_ms INTEGER")
    if "output_wem_path" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN output_wem_path TEXT")
    if "wwise_conversion" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN wwise_conversion TEXT")
    if "wem_bitrate_kbps" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN wem_bitrate_kbps INTEGER")
    if "wem_duration_ms" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN wem_duration_ms INTEGER")
    connection.commit()


def record_database(connection: sqlite3.Connection, run_id: str, row: dict) -> None:
    connection.execute("""INSERT OR REPLACE INTO production_voice_outputs
        (run_id, source_audio_path, target_language, dialogue_id, voice_id,
         canonical_subtitle, synthesis_text, output_wav_path, status,
         reference_sample_rate, reference_channels, reference_rms_dbfs,
         reference_peak_dbfs, output_rms_dbfs, output_peak_dbfs,
         normalization_gain_db, output_duration_ms, error, started_at,
         finished_at, generation_engine, voxcpm_steps, generation_seed, output_opus_path,
         opus_bitrate_kbps, opus_duration_ms, output_wem_path, wwise_conversion,
         wem_bitrate_kbps, wem_duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        run_id, row.get("source_audio_path"), row.get("target_language"), row.get("dialogue_id"),
        row.get("voice_id"), row.get("official_subtitle", ""), row.get("synthesis_text", ""),
        row.get("output_wav_path", ""), row.get("status"), row.get("reference_sample_rate"),
        row.get("reference_channels"), row.get("reference_rms_dbfs"), row.get("reference_peak_dbfs"),
        row.get("output_rms_dbfs"), row.get("output_peak_dbfs"), row.get("normalization_gain_db"),
        row.get("duration_ms"), row.get("error"), row.get("started_at"), row.get("finished_at"),
        row.get("generation_engine"), row.get("voxcpm_steps"), row.get("generation_seed"), row.get("output_opus_path"),
        row.get("opus_bitrate_kbps"), row.get("opus_duration_ms"),
        row.get("output_wem_path"), row.get("wwise_conversion"),
        row.get("wem_bitrate_kbps"), row.get("wem_duration_ms"),
    ))
    connection.commit()


class Zonos2Engine:
    """One local ZONOS2 Vulkan server kept alive for the entire batch."""
    def __init__(self, model_path: Path) -> None:
        model_path = model_path.resolve()
        workspace = model_path.parent.parent
        # The released Windows binary uses AVX-512 instructions and crashes on
        # this AVX2-only Ryzen CPU. Use the local Vulkan/AVX2 build instead.
        self.runtime = workspace / 'runtimes' / 'zonos2-build-vulkan-avx2'
        self.ffmpeg = workspace / 'runtimes' / 'ffmpeg' / 'ffmpeg.exe'
        server = self.runtime / 'zonos2-server.exe'
        backbone = model_path / 'zonos2-q4_k.gguf'
        dac = model_path / 'dac.gguf'
        speaker = model_path / 'spk-encoder.gguf'
        missing = [str(path) for path in (server, self.ffmpeg, backbone, dac, speaker) if not path.is_file()]
        if missing:
            raise FileNotFoundError('ZONOS2 component missing: ' + '; '.join(missing))
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(('127.0.0.1', 0))
        self.port = int(probe.getsockname()[1])
        probe.close()
        environment = os.environ.copy()
        environment['ZONOS2_FFMPEG'] = str(self.ffmpeg)
        environment['PATH'] = str(self.ffmpeg.parent) + os.pathsep + str(self.runtime) + os.pathsep + environment.get('PATH', '')
        self.server_log_path = workspace / 'logs' / 'zonos2-server-startup.log'
        self.server_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.server_log = self.server_log_path.open('w', encoding='utf-8', buffering=1)
        self.process = subprocess.Popen([str(server), str(backbone), '--dac', str(dac), '--spk', str(speaker), '--gpu', '--dac-cpu', '--dac-threads', '1', '--host', '127.0.0.1', '--port', str(self.port)], cwd=str(self.runtime), stdout=self.server_log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=environment)
        deadline = time.monotonic() + 120
        health = f'http://127.0.0.1:{self.port}/health'
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.server_log.close()
                detail = self.server_log_path.read_text(encoding='utf-8', errors='replace')[-4000:].strip()
                raise RuntimeError(f"ZONOS2 server stopped during startup (exit {self.process.returncode}): {detail or 'no diagnostic output'}")
            try:
                with urllib.request.urlopen(health, timeout=2) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.5)
        self.close()
        raise TimeoutError('ZONOS2 server did not become ready within 120 seconds')

    def generate(self, text: str, reference: Path, target_language: str) -> tuple[np.ndarray, int]:
        payload = json.dumps({
            'text': text,
            'language': generation_language_setting('zonos2_q4', target_language),
            'stream': False,
            'format': 'wav',
            'seed': 1,
            'speaker_audio_base64': base64.b64encode(reference.read_bytes()).decode('ascii'),
            'clean_speaker_background': True,
        }).encode('utf-8')
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}/tts/generate', data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                wav_bytes = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'ZONOS2 generation failed: HTTP {error.code} {error.read().decode("utf-8", "replace")}') from error
        generated = reference.parent / '_zonos2_generated.wav'
        generated.write_bytes(wav_bytes)
        try:
            values, sample_rate = sf.read(generated, dtype='float32')
            return np.asarray(values), int(sample_rate)
        finally:
            generated.unlink(missing_ok=True)

    def close(self) -> None:
        if getattr(self, 'process', None) is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if getattr(self, 'server_log', None) is not None and not self.server_log.closed:
            self.server_log.close()

def load_engine(engine: str, model_path: Path):
    if engine == 'voxcpm2':
        from voxcpm import VoxCPM
        return VoxCPM.from_pretrained(str(model_path), device='cuda', optimize=False, load_denoiser=False)
    if engine in {'qwen_0_6b', 'qwen_1_7b'}:
        from qwen_tts import Qwen3TTSModel
        return Qwen3TTSModel.from_pretrained(str(model_path), device_map='cuda:0', dtype=torch.bfloat16, attn_implementation='sdpa')
    if engine == 'cosyvoice3':
        # CosyVoice is kept in its own isolated runtime.  Its source package
        # lives beside the model under work so no game or system installation
        # path is required by production runs.
        source_root = model_path.parents[1] / 'runtimes' / 'cosyvoice3-source'
        for path in (source_root, source_root / 'third_party' / 'Matcha-TTS'):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        from cosyvoice.cli.cosyvoice import AutoModel
        return AutoModel(model_dir=str(model_path), fp16=True)
    if engine == 'chatterbox_v3':
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        # Use the checked project-local snapshot, never Hugging Face cache or
        # a network lookup.  The snapshot intentionally contains its v3 T3
        # weights plus common voice encoder and S3 generator assets.
        return ChatterboxMultilingualTTS.from_local(str(model_path), device='cuda', t3_model='v3')
    if engine == 'xtts_v2':
        from TTS.api import TTS
        # Always load the project-local XTTS copy.  A model-name lookup would
        # silently fall back to Coqui's user cache outside this workspace.
        return TTS(model_path=str(model_path), config_path=str(model_path / 'config.json')).to('cuda')
    if engine == 'zonos2_q4':
        return Zonos2Engine(model_path)
    raise ValueError(f'Unsupported generation engine: {engine}')


def _xtts_load_reference_wav(audio_path: str, sampling_rate: int) -> torch.Tensor:
    """Load the already-decoded local WAV without Torchaudio/TorchCodec."""
    values, source_rate = sf.read(audio_path, dtype='float32', always_2d=True)
    mono = np.mean(values, axis=1, dtype=np.float32)
    if int(source_rate) != int(sampling_rate):
        divisor = math.gcd(int(source_rate), int(sampling_rate))
        mono = resample_poly(mono, int(sampling_rate) // divisor, int(source_rate) // divisor).astype(np.float32, copy=False)
    return torch.from_numpy(np.clip(mono, -1.0, 1.0)).unsqueeze(0)

def generate_with_engine(engine: str, model, text: str, reference: Path, temp_root: Path,
                         target_language: str, voxcpm_steps: int = 6,
                         generation_seed: int | None = None) -> tuple[np.ndarray, int]:
    if generation_seed is not None:
        # Not every backend exposes a per-call seed.  Seeding immediately
        # before its call keeps retry attempts reproducible regardless.
        set_generation_seed(generation_seed)
    if engine == 'voxcpm2':
        arguments = {
            "text": text,
            "reference_wav_path": str(reference),
            "cfg_value": 2.0,
            "inference_timesteps": voxcpm_steps,
            "normalize": True,
            "denoise": False,
            "retry_badcase": False,
        }
        values = model.generate(**arguments)
        return np.asarray(values), int(model.tts_model.sample_rate)
    if engine in {'qwen_0_6b', 'qwen_1_7b'}:
        wavs, sample_rate = model.generate_voice_clone(text=text, language=generation_language_setting(engine, target_language), ref_audio=str(reference), x_vector_only_mode=True)
        return np.asarray(wavs[0]), int(sample_rate)
    if engine == 'cosyvoice3':
        # Do not provide the English transcript: this is cross-lingual voice
        # cloning, so the English WAV contributes speaker identity while the
        # target-language frontend controls pronunciation.
        prompt_text = 'You are a helpful assistant.<|endofprompt|>' + text
        pieces = list(model.inference_cross_lingual(prompt_text, str(reference), stream=False))
        if not pieces:
            raise RuntimeError('CosyVoice 3 returned no audio')
        values = torch.cat([piece['tts_speech'] for piece in pieces], dim=1)
        return values.squeeze().detach().float().cpu().numpy(), int(model.sample_rate)
    if engine == 'chatterbox_v3':
        values = model.generate(text, language_id=generation_language_setting(engine, target_language), audio_prompt_path=str(reference), cfg_weight=0.0, exaggeration=0.6)
        return values.squeeze().detach().float().cpu().numpy(), int(model.sr)
    if engine == 'zonos2_q4':
        return model.generate(text, reference, target_language)
    if engine == 'xtts_v2':
        # XTTS calls torchaudio.load for the reference; recent Torchaudio uses
        # TorchCodec, which is unavailable in this isolated Windows runtime.
        # The input here is always our decoded local WAV, so SoundFile is enough.
        import TTS.tts.models.xtts as xtts_module
        xtts_module.load_audio = _xtts_load_reference_wav
        generated_path = temp_root / 'xtts_generated.wav'
        model.tts_to_file(
            text=text, speaker_wav=[str(reference)],
            language=generation_language_setting(engine, target_language),
            file_path=str(generated_path), split_sentences=False,
            **XTTS_V2_STANDARD_PARAMETERS,
        )
        values, sample_rate = sf.read(generated_path, dtype='float32')
        generated_path.unlink(missing_ok=True)
        return np.asarray(values), int(sample_rate)
    raise ValueError(f'Unsupported generation engine: {engine}')


def encode_and_verify_wwise_wem(wwise_console: Path, wwise_project: Path, decoder: Path, source_wav: Path, target_wem: Path, expected_duration_ms: int) -> tuple[int, int]:
    """Encode one PCM WAV with Starfield's Wwise project and verify the WEM."""
    target_wem.parent.mkdir(parents=True, exist_ok=True)
    wsources = source_wav.with_suffix(".wsources")
    wsources.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ExternalSourcesList SchemaVersion="1">\n'
        f'  <Source Path="{xml_escape(source_wav.resolve().as_posix())}" Conversion="Vorbis Quality Medium" />\n'
        '</ExternalSourcesList>\n', encoding="utf-8")
    try:
        encode = subprocess.run([
            str(wwise_console), "convert-external-source", str(wwise_project), "--no-wwise-dat",
            "--platform", "Windows", "--source-file", str(wsources), "--output", "Windows",
            str(target_wem.parent), "--quiet",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        wsources.unlink(missing_ok=True)
    if encode.returncode or not target_wem.is_file() or target_wem.stat().st_size == 0:
        raise RuntimeError(encode.stderr.strip() or encode.stdout.strip() or "Wwise produced no WEM output")
    verify = subprocess.run([str(decoder), "-m", str(target_wem)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    details = verify.stdout
    if verify.returncode or "encoding: Custom Vorbis" not in details or "Audiokinetic Wwise RIFF header" not in details:
        raise RuntimeError(verify.stderr.strip() or details.strip() or "Generated WEM is not Wwise Custom Vorbis")
    rate_match = re.search(r"sample rate: (\d+) Hz", details)
    channels_match = re.search(r"channels: (\d+)", details)
    samples_match = re.search(r"stream total samples: (\d+)", details)
    bitrate_match = re.search(r"bitrate: (\d+) kbps", details)
    if not all((rate_match, channels_match, samples_match, bitrate_match)):
        raise RuntimeError("Generated WEM metadata is incomplete")
    sample_rate = int(rate_match.group(1))
    channels = int(channels_match.group(1))
    duration_ms = round(int(samples_match.group(1)) * 1000 / sample_rate)
    with wave.open(str(source_wav), "rb") as reference:
        if sample_rate != reference.getframerate() or channels != reference.getnchannels():
            raise RuntimeError(f"WEM format mismatch: expected {reference.getframerate()} Hz/{reference.getnchannels()} ch, got {sample_rate} Hz/{channels} ch")
    if abs(duration_ms - expected_duration_ms) > 250:
        raise RuntimeError(f"WEM duration mismatch: expected {expected_duration_ms} ms, got {duration_ms} ms")
    return duration_ms, int(bitrate_match.group(1))


def extract_and_decode_english_wav(reader: Path, decoder: Path, archive: Path, internal_path: str,
                                   temp_wem: Path, temp_wav: Path, number: int, total: int,
                                   prefetched: bool) -> None:
    """Prepare one reference WAV using CPU tools only.

    This function deliberately knows nothing about the CUDA model.  When it is
    submitted to the one-worker executor, the next English reference is ready
    while the current line is being synthesized on the GPU.  A single worker
    avoids concurrent BA2 reads and keeps disk use bounded to one next item.
    """
    marker = "PREFETCH" if prefetched else "ITEM"
    temp_wem.parent.mkdir(parents=True, exist_ok=True)
    # archive_reader deliberately refuses to overwrite.  A paused retry may
    # leave these workspace-only files behind, so clear that exact pair before
    # extracting it again.  No game asset is ever modified.
    temp_wem.unlink(missing_ok=True)
    temp_wav.unlink(missing_ok=True)
    print(f"{marker} {number}/{total} stage=extract", flush=True)
    extract = subprocess.run(
        [str(reader), "extract", str(archive), internal_path, str(temp_wem)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if extract.returncode:
        raise RuntimeError(extract.stderr.strip() or extract.stdout.strip() or "WEM extraction failed")
    print(f"{marker} {number}/{total} stage=decode_cpu", flush=True)
    decode = subprocess.run(
        [str(decoder), "-o", str(temp_wav), str(temp_wem)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if decode.returncode or not temp_wav.is_file():
        raise RuntimeError(decode.stderr.strip() or decode.stdout.strip() or "WEM decode failed")


def retain_original_wem(reader: Path, decoder: Path, archive: Path, internal_path: str,
                        output_wem: Path) -> None:
    """Extract one original non-verbal WEM into the final mod tree unchanged."""
    output_wem.parent.mkdir(parents=True, exist_ok=True)
    extract = subprocess.run(
        [str(reader), "extract", str(archive), internal_path, str(output_wem)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if extract.returncode or not output_wem.is_file() or not output_wem.stat().st_size:
        raise RuntimeError(extract.stderr.strip() or extract.stdout.strip() or "Original WEM extraction failed")
    verify = subprocess.run([str(decoder), "-m", str(output_wem)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if verify.returncode or "Audiokinetic Wwise RIFF header" not in verify.stdout:
        raise RuntimeError(verify.stderr.strip() or verify.stdout.strip() or "Retained original WEM verification failed")


def normalize_and_encode_generated_wem(audio: np.ndarray, generated_rate: int, reference_wav: Path,
                                       target_wav: Path, output_wem: Path, wwise_console: Path,
                                       wwise_project: Path, decoder: Path, number: int, total: int,
                                       preview_root: Path | None = None, preview_sequence: int | None = None) -> dict:
    """CPU/Wwise output stage, intentionally independent from CUDA synthesis."""
    print(f"OUTPUT {number}/{total} stage=normalize_cpu", flush=True)
    processed, sample_rate, metrics = match_reference_format(audio, generated_rate, reference_wav)
    # Retry/resume paths are created lazily, unlike the normal output queue.
    # Ensure the local target-WAV folder exists before SoundFile opens it.
    target_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(target_wav, processed, sample_rate, subtype="PCM_16")
    duration_ms = round(len(processed) * 1000 / sample_rate)
    print(f"OUTPUT {number}/{total} stage=encode_wwise_wem", flush=True)
    wem_duration_ms, wem_bitrate_kbps = encode_and_verify_wwise_wem(
        wwise_console, wwise_project, decoder, target_wav, output_wem, duration_ms,
    )
    preview_wav_path = None
    if preview_root is not None and preview_sequence is not None:
        queued_preview = enqueue_background_wav_preview(target_wav, preview_root, preview_sequence, "target")
        if queued_preview is not None:
            preview_wav_path = str(queued_preview)
    return {
        **metrics,
        "wem_duration_ms": wem_duration_ms,
        "wem_bitrate_kbps": wem_bitrate_kbps,
        "sample_rate": sample_rate,
        "duration_ms": duration_ms,
        "target_preview_wav_path": preview_wav_path,
    }


def start_background_wav_player(preview_root: Path) -> None:
    """Start one invisible, sequential WAV player without blocking generation."""
    preview_root.mkdir(parents=True, exist_ok=True)
    # Preview copies are disposable.  A resumed run must never replay an old
    # queue or inherit a stale acknowledgement from a previous player.
    for stale in (*preview_root.glob('*.wav'), *preview_root.glob('*.wav.done'),
                  preview_root / '_player.pid', preview_root / '_stop'):
        stale.unlink(missing_ok=True)
    disabled_path = preview_root / "_preview_disabled"
    disabled_path.unlink(missing_ok=True)
    player_script = (
        "import os, sys, time, winsound\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "disabled = root / '_preview_disabled'\n"
        "stop = root / '_stop'\n"
        "pid_file = root / '_player.pid'\n"
        "pid_file.write_text(str(os.getpid()), encoding='ascii')\n"
        "try:\n"
        "    while True:\n"
        "        if disabled.is_file(): break\n"
        "        items = sorted(root.glob('*.wav'))\n"
        "        if items:\n"
        "            item = items[0]\n"
        "            if '_english_' in item.name:\n"
        "                item.unlink(missing_ok=True); continue\n"
        "            try: winsound.PlaySound(str(item), winsound.SND_FILENAME)\n"
        "            except (OSError, RuntimeError): pass\n"
        "            finally:\n"
        "                item.unlink(missing_ok=True)\n"
        "                if not disabled.is_file(): winsound.MessageBeep()\n"
        "                Path(str(item) + '.done').touch()\n"
        "            continue\n"
        "        if stop.is_file(): break\n"
        "        time.sleep(0.05)\n"
        "finally:\n"
        "    pid_file.unlink(missing_ok=True)\n"
        "    stop.unlink(missing_ok=True)\n"
    )
    subprocess.Popen(
        [sys.executable, "-u", "-c", player_script, str(preview_root)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def enqueue_background_wav_preview(source_wav: Path, preview_root: Path, sequence: int, role: str) -> Path | None:
    """Copy atomically into the background player's FIFO-like folder."""
    if (preview_root / "_preview_disabled").is_file():
        return None
    preview_root.mkdir(parents=True, exist_ok=True)
    destination = preview_root / f"{sequence:010d}_{role}_{uuid.uuid4().hex}.wav"
    partial = destination.with_suffix('.wav.part')
    shutil.copyfile(source_wav, partial)
    partial.replace(destination)
    return destination


def wait_for_background_wav_preview(preview_wav: Path, preview_root: Path) -> bool:
    """Wait for the player acknowledgement, unless Preview WAVs was disabled."""
    completed = Path(str(preview_wav) + '.done')
    pid_file = preview_root / '_player.pid'
    player_start_deadline = time.monotonic() + 5.0
    deadline = time.monotonic() + 120.0
    while not completed.is_file():
        if (preview_root / "_preview_disabled").is_file():
            return False
        # Preview must never turn into a failed voice-generation item.  If the
        # separate player could not start, leave the WEM intact and continue.
        if time.monotonic() >= player_start_deadline and not pid_file.is_file():
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    completed.unlink(missing_ok=True)
    return True


def stop_background_wav_player(preview_root: Path) -> None:
    if preview_root.is_dir():
        (preview_root / '_stop').touch()

def main() -> int:
    # The GUI reads this process through a pipe.  Force UTF-8 instead of the
    # current Windows code page so target-language subtitles retain accents in both
    # the on-screen terminal and the chronological text log.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice-map", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--reader", type=Path, required=True)
    parser.add_argument("--decoder", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--engine", choices=("voxcpm2", "qwen_0_6b", "qwen_1_7b", "cosyvoice3", "chatterbox_v3", "xtts_v2"), required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--voxcpm-steps", type=int, choices=range(1, 21), default=6, help="VoxCPM2 diffusion steps; ignored by other engines.")
    parser.add_argument("--wwise-console", type=Path, required=True, help="Wwise 2021.1.10 console used for Starfield-compatible WEM encoding.")
    parser.add_argument("--wwise-project", type=Path, required=True, help="Workspace copy of the Starfield Wwise project.")
    parser.add_argument("--preview-wav-playback", action="store_true", help="Play target-language temporary WAVs sequentially in an invisible separate process.")
    parser.add_argument("--preview-wav-initially-disabled", action="store_true", help="Keep the preview capability idle until the GUI checkbox is enabled.")
    parser.add_argument("--phonetic-dictionary-root", type=Path, help="Directory containing user-editable model phonetic dictionaries.")
    parser.add_argument("--asr-checkpoint-interval", type=int, default=500, help="Validate one eligible generated line every N completed WEMs; 0 disables it.")
    parser.add_argument("--asr-max-attempts", type=int, default=5, help="Maximum total synthesis attempts for a failed ASR checkpoint line.")
    parser.add_argument("--resume", action="store_true", help="Continue an interrupted run and skip verified final WEM outputs.")
    args = parser.parse_args()
    if args.asr_checkpoint_interval < 0:
        raise ValueError("--asr-checkpoint-interval must be zero or positive")
    if args.asr_max_attempts < 1:
        raise ValueError("--asr-max-attempts must be at least one")
    configure_phonetic_dictionary_root(args.phonetic_dictionary_root or args.model.parents[1] / "phonetic_dictionaries")
    generation_language_setting(args.engine, args.language)

    for required in (args.voice_map, args.reader, args.decoder, args.model, args.wwise_console, args.wwise_project):
        if not required.exists():
            raise FileNotFoundError(required)
    if args.run_dir.exists() and not args.resume:
        raise RuntimeError(f"Fresh run directory already exists: {args.run_dir}")
    if args.resume and not args.run_dir.is_dir():
        raise RuntimeError(f"Interrupted run directory was not found: {args.run_dir}")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    # Models are siblings under work\models (the previous parents[1] form
    # incorrectly looked directly under work when XTTS was selected).
    asr_model_path = args.model.parent / "whisper-large-v3-turbo"
    if args.asr_checkpoint_interval and not asr_model_path.is_dir():
        raise FileNotFoundError(f"ASR checkpoint model is missing: {asr_model_path}")

    total = exact_unique_count(args.voice_map)
    if not total:
        raise RuntimeError("No xTranslator exact voice mappings were found")
    if not args.resume:
        args.run_dir.mkdir(parents=True, exist_ok=False)
    run_id = args.run_dir.name
    wem_root = args.run_dir / "wem"
    temp_root = args.run_dir / "_temporary_english"
    target_temp_root = args.run_dir / "_temporary_target"
    preview_root = args.run_dir / "_preview_audio"
    preview_ready_path = preview_root / "_preview_capability_ready"
    pause_request_path = args.run_dir / "_pause_requested"
    selection_path = args.run_dir / "selection.jsonl"
    results_path = args.run_dir / "results.jsonl"
    summary_path = args.run_dir / "summary.json"
    asr_checks_path = args.run_dir / "asr_checkpoint_checks.jsonl"
    asr_errors_path = args.run_dir / "asr_validation_errors.jsonl"
    # This small append-only journal is distinct from the successful-check
    # journal.  It lets a paused run finish the current retry queue before it
    # is allowed to inspect the next 500 generated WEMs.
    asr_retry_path = args.run_dir / "asr_pending_retries.jsonl"
    if args.resume and not selection_path.is_file():
        raise RuntimeError(f"Interrupted run has no selection manifest: {selection_path}")

    if not args.resume:
        print(f"START total={total} language={args.language} engine={args.engine} voxcpm_steps={args.voxcpm_steps} seed_strategy=sha256_source_audio_path_31bit retry_badcase=False preview_wav_playback={args.preview_wav_playback} asr_checkpoint_interval={args.asr_checkpoint_interval} asr_max_attempts={args.asr_max_attempts}", flush=True)
        with selection_path.open("w", encoding="utf-8", newline="\n") as selection:
            for number, (row, internal_path) in enumerate(iter_exact_unique(args.voice_map), 1):
                canonical = str(row.get("official_subtitle", "")).strip()
                write_json_line(selection, {
                    "number": number, "original_archive_path": row.get("original_archive_path"),
                    "source_audio_path": internal_path, "speaker_id": row.get("speaker_id"),
                    "dialogue_id": row.get("dialogue_id"), "voice_id": row.get("voice_id"),
                    "target_language": args.language, "english_subtitle": row.get("english_subtitle"),
                    "official_subtitle": canonical, "synthesis_text": synthesis_text(canonical, args.language, args.engine),
                })
    else:
        # A resume always starts a new cooperative pause window.
        pause_request_path.unlink(missing_ok=True)

    completed_sources: set[str] = set()
    completed_result_rows: list[dict] = []
    if args.resume and results_path.is_file():
        with results_path.open("r", encoding="utf-8") as previous_results:
            for line in previous_results:
                try:
                    previous = json.loads(line)
                    # A production item is complete only after the final
                    # Starfield-compatible WEM has been written.  Temporary
                    # WAV/Opus artefacts must never make resume skip a line.
                    output = Path(str(previous.get("output_wem_path") or ""))
                    if previous.get("status") in {"wem_generated", "original_wem_retained"} and output.is_file() and output.stat().st_size > 0:
                        completed_sources.add(str(previous.get("source_audio_path", "")))
                        if previous.get("status") == "wem_generated":
                            completed_result_rows.append(previous)
                except (ValueError, TypeError):
                    continue
        print(f"RESUME completed={len(completed_sources)} total={total} language={args.language} engine={args.engine}", flush=True)
    completed_max_number = max((int(row.get("number", 0)) for row in completed_result_rows), default=0)
    resume_partial_rows: list[dict] = []
    if args.resume and args.asr_checkpoint_interval and completed_max_number % args.asr_checkpoint_interval:
        active_block_start = ((completed_max_number - 1) // args.asr_checkpoint_interval) * args.asr_checkpoint_interval + 1
        # results.jsonl is append-only: a WEM may have several historical
        # entries after a resume.  A checkpoint must contain each source WEM
        # exactly once, using its latest completed result.
        latest_partial_rows: dict[str, dict] = {}
        for row in completed_result_rows:
            if active_block_start <= int(row.get("number", 0)) <= completed_max_number:
                latest_partial_rows[str(row.get("source_audio_path", ""))] = row
        resume_partial_rows = list(latest_partial_rows.values())
    connection = sqlite3.connect(args.database)
    setup_database(connection)
    torch.cuda.empty_cache()
    # A resume continues synthesis immediately unless it has a retry queue
    # for a checkpoint that was already closed before shutdown.  In
    # particular, a partial 500-line group never loads Whisper on launch.
    defer_generation_model = False
    model = None
    if defer_generation_model:
        print("MODEL loading Whisper first for resume ASR validation", flush=True)
    else:
        print(f"MODEL loading {args.engine} on CUDA", flush=True)
        try:
            model = load_engine(args.engine, args.model)
        except Exception as error:
            print(f"FATAL model load {type(error).__name__}: {error}", flush=True)
            traceback.print_exc()
            return 1
    generated = len(completed_sources)
    failed = 0
    retained_original = 0
    asr_checked = 0
    asr_retry_recovered = 0
    asr_unresolved = 0
    completed_asr_checkpoints: set[int] = set()
    asr_checked_sources: set[str] = set()
    pending_asr_retries: dict[str, dict] = {}
    if args.resume and asr_checks_path.is_file():
        for line in asr_checks_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                check = json.loads(line)
                checkpoint = int(check.get("checkpoint", 0))
                if checkpoint > 0:
                    completed_asr_checkpoints.add(checkpoint)
                source_path = str(check.get("source_audio_path", ""))
                # A failed check remains pending: on the next launch it must
                # be retried (and can then fall through to VoxCPM2), whereas
                # an accepted line is safe to skip permanently.
                if source_path and bool(check.get("satisfactory")):
                    asr_checked_sources.add(source_path)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    if args.resume and asr_retry_path.is_file():
        for line in asr_retry_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                retry = json.loads(line)
                source_path = str(retry.get("source_audio_path", ""))
                if not source_path:
                    continue
                if bool(retry.get("satisfactory")):
                    pending_asr_retries.pop(source_path, None)
                else:
                    pending_asr_retries[source_path] = retry
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    if args.resume and args.asr_checkpoint_interval:
        # A retry journal from a partially generated block is not actionable
        # yet.  It will be revalidated from attempt 1 when that source-number
        # block reaches its closing boundary.  Only retry work belonging to a
        # block whose final source number already exists may hold up resume.
        pending_asr_retries = {
            source: entry for source, entry in pending_asr_retries.items()
            if int(entry.get("group", 0)) * args.asr_checkpoint_interval <= completed_max_number
        }
    preview_sequence = 0
    pause_requested = False
    started = datetime.now(timezone.utc)
    if args.preview_wav_playback:
        preview_ready_path.unlink(missing_ok=True)
        if args.preview_wav_initially_disabled:
            preview_root.mkdir(parents=True, exist_ok=True)
            (preview_root / '_preview_disabled').touch()
        else:
            start_background_wav_player(preview_root)
        # The GUI waits for this marker before applying checkbox changes. It
        # prevents an early enable request racing the initial-disabled setup.
        preview_ready_path.touch()
    try:
        with selection_path.open("r", encoding="utf-8") as selection, results_path.open("a" if args.resume else "w", encoding="utf-8", newline="\n") as results:
            deferred_exception_rows: list[dict] = []
            voxcpm_fallback_rows: dict[str, dict] = {}

            def pending_rows():
                for line in selection:
                    row = json.loads(line)
                    if str(row["source_audio_path"]) not in completed_sources:
                        if is_nonverbal_subtitle(str(row.get("official_subtitle", ""))):
                            # Asterisk subtitles are exceptions, not proof of
                            # silence.  Do not retain or synthesize them yet.
                            deferred_exception_rows.append(row)
                        elif len(subtitle_words(str(row.get("official_subtitle", "")))) < 3:
                            voxcpm_fallback_rows[str(row['source_audio_path'])] = row
                        else:
                            yield row

            pending = pending_rows()
            row = next(pending, None)
            prepared_future = None
            pending_outputs: list[dict] = []
            asr_pending_group: list[dict] = []
            # Retain WEMs already produced in the current incomplete
            # source-number group.  They join the newly generated remainder
            # and are ASR-checked together only at that group's next 500-line
            # boundary.
            for previous in resume_partial_rows:
                internal_path = str(previous['source_audio_path'])
                relative = Path(*internal_path.replace('\\', '/').split('/'))
                asr_pending_group.append({
                    'row': previous, 'number': int(previous.get('number', 0)),
                    'result': dict(previous), 'output_wem': Path(str(previous['output_wem_path'])),
                    'temp_wem': temp_root / '_asr_resume_partial' / relative,
                    'temp_wav': (temp_root / '_asr_resume_partial' / relative).with_suffix('.wav'),
                    'target_wav': (target_temp_root / '_asr_resume_partial' / relative).with_suffix('.wav'),
                })

            def persist_result(item: dict) -> None:
                """Commit only after its output task has finished and cleaned up."""
                result = item["result"]
                result["finished_at"] = datetime.now(timezone.utc).isoformat()
                write_json_line(results, result)
                record_database(connection, run_id, result)
                item["temp_wem"].unlink(missing_ok=True)
                item["temp_wav"].unlink(missing_ok=True)
                item["target_wav"].unlink(missing_ok=True)
                source_row = item["row"]
                print(f"PROGRESS {item['number']}/{total} generated={generated} failed={failed} dialogue={source_row.get('dialogue_id', '')} target_text={json.dumps(source_row.get('official_subtitle', ''), ensure_ascii=False)}", flush=True)

            def release_generation_model() -> None:
                """Free the TTS CUDA allocation before a checkpoint ASR load."""
                nonlocal model
                if model is None:
                    return
                if hasattr(model, 'close'):
                    model.close()
                del model
                model = None
                gc.collect()
                torch.cuda.empty_cache()

            def ensure_generation_model() -> None:
                nonlocal model
                if model is None:
                    print(f"MODEL reload {args.engine} on CUDA after ASR checkpoint", flush=True)
                    model = load_engine(args.engine, args.model)

            def run_asr_attempt(item: dict, attempt: int) -> dict:
                """ASR the exact normalised WAV used to create this item's WEM."""
                expected = str(item['row'].get('official_subtitle', '')).strip()
                try:
                    release_generation_model()
                    transcript = transcribe_checkpoint_wav(item['target_wav'], asr_model_path, args.language)
                    comparison = evaluate_asr_match(expected, transcript, args.language)
                    comparison.update({
                        'attempt': attempt,
                        'language': args.language,
                        'asr_language': asr_language_code(args.language),
                        'expected_text': expected,
                        'asr_transcript': transcript,
                    })
                    print(
                        f"ASR {item['number']}/{total} checkpoint expected={json.dumps(expected, ensure_ascii=False)}",
                        flush=True,
                    )
                    print(
                        f"ASR {item['number']}/{total} attempt={attempt}/{args.asr_max_attempts} "
                        f"transcript={json.dumps(transcript, ensure_ascii=False)} "
                        f"wer={comparison['word_error_rate']:.2%} coverage={comparison['expected_word_coverage']:.2%} "
                        f"satisfactory={comparison['satisfactory']}",
                        flush=True,
                    )
                    return comparison
                except Exception as error:
                    details = {
                        'attempt': attempt, 'language': args.language,
                        'expected_text': expected, 'asr_transcript': '',
                        'satisfactory': False, 'asr_runtime_error': f'{type(error).__name__}: {error}',
                    }
                    print(f"ASR {item['number']}/{total} attempt={attempt}/{args.asr_max_attempts} error={details['asr_runtime_error']}", flush=True)
                    return details

            def validate_asr_checkpoint(item: dict, checkpoint: int) -> None:
                """Retry a sampled long line until its target-language ASR is sound."""
                nonlocal asr_checked, asr_retry_recovered, asr_unresolved
                validation = run_asr_attempt(item, 1)
                attempts = [validation]
                while not validation.get('satisfactory') and len(attempts) < args.asr_max_attempts:
                    attempt = len(attempts) + 1
                    print(
                        f"ASR {item['number']}/{total} checkpoint={checkpoint} failed; "
                        f"regenerating attempt {attempt}/{args.asr_max_attempts}",
                        flush=True,
                    )
                    ensure_generation_model()
                    retry_seed = stable_seed(str(item['row']['source_audio_path'])) + attempt - 1
                    audio, generated_rate = generate_with_engine(
                        args.engine, model, str(item['row']['synthesis_text']), item['temp_wav'], temp_root,
                        args.language, args.voxcpm_steps, retry_seed,
                    )
                    # The initial Wwise task has completed before this function
                    # runs, so re-encoding the same final path is safe.
                    retry_metrics = normalize_and_encode_generated_wem(
                        audio, generated_rate, item['temp_wav'], item['target_wav'], item['output_wem'],
                        args.wwise_console, args.wwise_project, args.decoder, item['number'], total,
                    )
                    item['result'].update(retry_metrics)
                    item['result']['generation_seed'] = retry_seed
                    validation = run_asr_attempt(item, attempt)
                    attempts.append(validation)
                ensure_generation_model()
                asr_checked += 1
                entry = {
                    'checkpoint': checkpoint, 'completed_number': item['number'],
                    'source_audio_path': item['row']['source_audio_path'], 'target_language': args.language,
                    'attempts': attempts, 'satisfactory': bool(validation.get('satisfactory')),
                    'validated_at': datetime.now(timezone.utc).isoformat(),
                }
                with asr_checks_path.open('a', encoding='utf-8', newline='\n') as check_file:
                    write_json_line(check_file, entry)
                item['result']['asr_checkpoint_validation'] = entry
                if validation.get('satisfactory'):
                    if len(attempts) > 1:
                        asr_retry_recovered += 1
                    print(f"ASR {item['number']}/{total} checkpoint={checkpoint} accepted after {len(attempts)} attempt(s)", flush=True)
                else:
                    asr_unresolved += 1
                    error_entry = {**entry, 'status': 'unresolved_after_max_attempts'}
                    with asr_errors_path.open('a', encoding='utf-8', newline='\n') as error_file:
                        write_json_line(error_file, error_entry)
                    print(f"ASR {item['number']}/{total} checkpoint={checkpoint} unresolved after {len(attempts)} attempt(s); retained for end-of-run review", flush=True)

            def audit_asr_group(group: list[dict], group_number: int, resume_histories: dict[str, list[dict]] | None = None) -> None:
                """Audit a completed group with ASR, then retry only failures.

                XTTS and Whisper are deliberately loaded once per phase, never
                once per line: all ASR checks run together, then all failed
                lines are regenerated together.
                """
                nonlocal asr_checked, asr_retry_recovered, asr_unresolved, preview_sequence
                candidates = [item for item in group if len(subtitle_words(str(item['row'].get('official_subtitle', '')))) >= 3]
                if not candidates:
                    return
                print(f"ASR GROUP {group_number} start items={len(group)} eligible={len(candidates)}", flush=True)
                resume_histories = resume_histories or {}
                attempts: dict[str, list[dict]] = {
                    str(item['row']['source_audio_path']): list(resume_histories.get(str(item['row']['source_audio_path']), []))
                    for item in candidates
                }
                # Persist successful checks immediately.  A safe pause or an
                # application close can therefore resume from the first line
                # that was not yet accepted, rather than replaying a whole
                # 500-line ASR group.
                persisted_successes: dict[str, dict] = {}
                unresolved = list(candidates)
                # If this is a resumed retry queue, the first failed ASR
                # attempt has already happened.  Regenerate those exact
                # lines now; do not mix in a new 500-line inspection first.
                first_attempt = max((len(history) for history in attempts.values()), default=0) + 1
                if resume_histories and first_attempt <= args.asr_max_attempts:
                    ensure_generation_model()
                    print(f"ASR GROUP {group_number} resuming {len(unresolved)} failed item(s) with {args.engine} before new ASR checks", flush=True)
                    for item in unresolved:
                        if pause_request_path.is_file():
                            print('PAUSE requested before resumed ASR retry generation', flush=True)
                            raise PauseRequested()
                        row_data = item['row']
                        internal_path = str(row_data['source_audio_path'])
                        relative = Path(*internal_path.replace('\\', '/').split('/'))
                        item['temp_wem'] = temp_root / '_asr_retry' / relative
                        item['temp_wav'] = item['temp_wem'].with_suffix('.wav')
                        item['target_wav'] = (target_temp_root / '_asr_retry' / relative).with_suffix('.wav')
                        extract_and_decode_english_wav(args.reader, args.decoder, Path(row_data['original_archive_path']), internal_path, item['temp_wem'], item['temp_wav'], item['number'], total, False)
                        print(f"ITEM {item['number']}/{total} stage=generate_target english_subtitle={json.dumps(str(row_data.get('english_subtitle', '')), ensure_ascii=False)} target_subtitle={json.dumps(str(row_data.get('official_subtitle', '')), ensure_ascii=False)}", flush=True)
                        seed = stable_seed(internal_path) + max(1, first_attempt - 1)
                        audio, rate = generate_with_engine(args.engine, model, str(row_data['synthesis_text']), item['temp_wav'], temp_root, args.language, args.voxcpm_steps, seed)
                        preview_active = args.preview_wav_playback and not (preview_root / '_preview_disabled').is_file()
                        if preview_active:
                            preview_sequence += 1
                        item['result'].update(normalize_and_encode_generated_wem(audio, rate, item['temp_wav'], item['target_wav'], item['output_wem'], args.wwise_console, args.wwise_project, args.decoder, item['number'], total, preview_root if preview_active else None, preview_sequence if preview_active else None))
                        preview_path = item['result'].get('target_preview_wav_path')
                        if preview_active and preview_path:
                            print(f"OUTPUT {item['number']}/{total} stage=preview_target_wait", flush=True)
                            item['result']['target_preview_completed'] = wait_for_background_wav_preview(Path(preview_path), preview_root)
                        item['result']['generation_seed'] = seed
                        item['temp_wem'].unlink(missing_ok=True); item['temp_wav'].unlink(missing_ok=True); item['target_wav'].unlink(missing_ok=True)
                for attempt in range(first_attempt, args.asr_max_attempts + 1):
                    # One Whisper load for the complete unresolved set.
                    release_generation_model()
                    from faster_whisper import WhisperModel
                    asr_model = None
                    try:
                        print(f"MODEL loading Whisper for ASR group {group_number}: {len(unresolved)} item(s), attempt {attempt}/{args.asr_max_attempts}", flush=True)
                        asr_model = WhisperModel(str(asr_model_path), device='cuda', compute_type='int8_float16')
                        still_failed: list[dict] = []
                        for item in unresolved:
                            if pause_request_path.is_file():
                                print('PAUSE requested during ASR | stopping after the current ASR item boundary', flush=True)
                                raise PauseRequested()
                            target_wav = item['target_wav']
                            target_wav.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                decoded = subprocess.run([str(args.decoder), '-o', str(target_wav), str(item['output_wem'])], capture_output=True, text=True, encoding='utf-8', errors='replace')
                                if decoded.returncode or not target_wav.is_file():
                                    comparison = {'satisfactory': False, 'asr_runtime_error': decoded.stderr.strip() or decoded.stdout.strip() or 'WEM decode for ASR failed'}
                                else:
                                    segments, _info = asr_model.transcribe(str(target_wav), language=asr_language_code(args.language), task='transcribe', beam_size=5, vad_filter=False)
                                    transcript = ' '.join(segment.text.strip() for segment in segments).strip()
                                    comparison = evaluate_asr_match(str(item['row'].get('official_subtitle', '')), transcript, args.language)
                                    comparison['asr_transcript'] = transcript
                            except Exception as error:
                                comparison = {'satisfactory': False, 'asr_runtime_error': f'{type(error).__name__}: {error}'}
                            comparison.update({'attempt': attempt, 'language': args.language, 'asr_language': asr_language_code(args.language), 'expected_text': str(item['row'].get('official_subtitle', ''))})
                            attempts[str(item['row']['source_audio_path'])].append(comparison)
                            print(f"ASR {item['number']}/{total} expected={json.dumps(comparison['expected_text'], ensure_ascii=False)}", flush=True)
                            print(f"ASR {item['number']}/{total} attempt={attempt}/{args.asr_max_attempts} transcript={json.dumps(comparison.get('asr_transcript', ''), ensure_ascii=False)} satisfactory={comparison.get('satisfactory', False)}", flush=True)
                            target_wav.unlink(missing_ok=True)
                            source_path = str(item['row']['source_audio_path'])
                            if comparison.get('satisfactory'):
                                entry = {
                                    'group': group_number,
                                    'source_audio_path': source_path,
                                    'target_language': args.language,
                                    'attempts': list(attempts[source_path]),
                                    'satisfactory': True,
                                    'validated_at': datetime.now(timezone.utc).isoformat(),
                                }
                                with asr_checks_path.open('a', encoding='utf-8', newline='\n') as check_file:
                                    write_json_line(check_file, entry)
                                with asr_retry_path.open('a', encoding='utf-8', newline='\n') as retry_file:
                                    write_json_line(retry_file, {**entry, 'pending_retry': False})
                                persisted_successes[source_path] = entry
                                asr_checked_sources.add(source_path)
                            else:
                                with asr_retry_path.open('a', encoding='utf-8', newline='\n') as retry_file:
                                    write_json_line(retry_file, {
                                        'group': group_number,
                                        'source_audio_path': source_path,
                                        'target_language': args.language,
                                        'attempts': list(attempts[source_path]),
                                        'satisfactory': False,
                                        'pending_retry': True,
                                        'updated_at': datetime.now(timezone.utc).isoformat(),
                                    })
                                still_failed.append(item)
                    finally:
                        if asr_model is not None:
                            del asr_model
                        gc.collect(); torch.cuda.empty_cache()
                    unresolved = still_failed
                    if not unresolved:
                        break
                    if attempt == args.asr_max_attempts:
                        break
                    # One XTTS load for every failed line in this group.
                    ensure_generation_model()
                    print(f"ASR GROUP {group_number} regenerating {len(unresolved)} failed item(s) with {args.engine}", flush=True)
                    for item in unresolved:
                        if pause_request_path.is_file():
                            print('PAUSE requested before ASR retry generation', flush=True)
                            raise PauseRequested()
                        row_data = item['row']
                        internal_path = str(row_data['source_audio_path'])
                        relative = Path(*internal_path.replace('\\', '/').split('/'))
                        item['temp_wem'] = temp_root / '_asr_retry' / relative
                        item['temp_wav'] = item['temp_wem'].with_suffix('.wav')
                        item['target_wav'] = (target_temp_root / '_asr_retry' / relative).with_suffix('.wav')
                        extract_and_decode_english_wav(args.reader, args.decoder, Path(row_data['original_archive_path']), internal_path, item['temp_wem'], item['temp_wav'], item['number'], total, False)
                        print(f"ITEM {item['number']}/{total} stage=generate_target english_subtitle={json.dumps(str(row_data.get('english_subtitle', '')), ensure_ascii=False)} target_subtitle={json.dumps(str(row_data.get('official_subtitle', '')), ensure_ascii=False)}", flush=True)
                        seed = stable_seed(internal_path) + attempt
                        audio, rate = generate_with_engine(args.engine, model, str(row_data['synthesis_text']), item['temp_wav'], temp_root, args.language, args.voxcpm_steps, seed)
                        preview_active = args.preview_wav_playback and not (preview_root / '_preview_disabled').is_file()
                        if preview_active:
                            preview_sequence += 1
                        item['result'].update(normalize_and_encode_generated_wem(audio, rate, item['temp_wav'], item['target_wav'], item['output_wem'], args.wwise_console, args.wwise_project, args.decoder, item['number'], total, preview_root if preview_active else None, preview_sequence if preview_active else None))
                        preview_path = item['result'].get('target_preview_wav_path')
                        if preview_active and preview_path:
                            print(f"OUTPUT {item['number']}/{total} stage=preview_target_wait", flush=True)
                            item['result']['target_preview_completed'] = wait_for_background_wav_preview(Path(preview_path), preview_root)
                        item['result']['generation_seed'] = seed
                        item['temp_wem'].unlink(missing_ok=True); item['temp_wav'].unlink(missing_ok=True); item['target_wav'].unlink(missing_ok=True)
                asr_checked += len(candidates)
                for item in candidates:
                    source_path = str(item['row']['source_audio_path'])
                    history = attempts[source_path]
                    final = history[-1]
                    entry = persisted_successes.get(source_path)
                    if entry is None:
                        entry = {'group': group_number, 'source_audio_path': item['row']['source_audio_path'], 'target_language': args.language, 'attempts': history, 'satisfactory': bool(final.get('satisfactory')), 'validated_at': datetime.now(timezone.utc).isoformat()}
                        with asr_checks_path.open('a', encoding='utf-8', newline='\n') as check_file:
                            write_json_line(check_file, entry)
                        if entry['satisfactory']:
                            asr_checked_sources.add(source_path)
                    item['result']['asr_group_validation'] = entry
                    if len(history) > 1 and final.get('satisfactory'):
                        asr_retry_recovered += 1
                    if not final.get('satisfactory'):
                        asr_unresolved += 1
                        voxcpm_fallback_rows[str(item['row']['source_audio_path'])] = item['row']
                        with asr_errors_path.open('a', encoding='utf-8', newline='\n') as error_file:
                            write_json_line(error_file, {**entry, 'status': 'unresolved_after_max_attempts'})
                    item['result']['finished_at'] = datetime.now(timezone.utc).isoformat()
                    write_json_line(results, item['result'])
                    record_database(connection, run_id, item['result'])
                # Do not reload the generation model here.  Resume may move
                # directly to the next ASR group, which would immediately
                # unload XTTS and reload Whisper.  Leave it released until a
                # retry or normal production generation actually needs it.
                print(f"ASR GROUP {group_number} complete eligible={len(candidates)} unresolved={sum(not attempts[str(item['row']['source_audio_path'])][-1].get('satisfactory') for item in candidates)}", flush=True)

            def finish_output(item: dict, wait: bool, wait_for_preview: bool = False) -> bool:
                """Publish one output result, in source order, after Wwise is done."""
                nonlocal generated, failed
                future = item["output_future"]
                if not wait and not future.done():
                    return False
                if item.get("preview_required") and not wait_for_preview:
                    # Keep the item in the listening pipeline while preview
                    # remains enabled. If the checkbox was turned off, make
                    # the already encoded WEM immediately publishable.
                    if not (preview_root / '_preview_disabled').is_file():
                        return False
                    item["preview_required"] = False
                try:
                    item["result"].update(future.result())
                    preview_path = item["result"].get("target_preview_wav_path")
                    if wait_for_preview and preview_path:
                        print(f"OUTPUT {item['number']}/{total} stage=preview_target_wait", flush=True)
                        item["result"]["target_preview_completed"] = wait_for_background_wav_preview(Path(preview_path), preview_root)
                        if item["result"]["target_preview_completed"]:
                            print(f"OUTPUT {item['number']}/{total} stage=preview_target_complete", flush=True)
                    item["result"].update({"status": "wem_generated"})
                    generated += 1
                    asr_pending_group.append(item)
                except Exception as error:
                    item["result"].update({"status": "failed", "error": str(error), "error_traceback": traceback.format_exc()})
                    failed += 1
                    print(f"ERROR {item['number']}/{total} {type(error).__name__}: {error}", flush=True)
                persist_result(item)
                return True

            # A resumed run may pre-date ASR validation.  Audit every already
            # generated WEM in 500-item groups before creating a single new
            # one; only failed items are ever rewritten.
            if args.resume and args.asr_checkpoint_interval and generated >= args.asr_checkpoint_interval:
                newest_by_source: dict[str, dict] = {}
                for previous in completed_result_rows:
                    newest_by_source[str(previous.get('source_audio_path', ''))] = previous
                # Compatibility recovery for a run that was interrupted
                # before the retry journal existed.  This is derived only
                # from normal pipeline state: if the earliest 500-line block
                # has some accepted ASR rows, the remaining eligible rows in
                # that same block are its pending red retries.  They must be
                # regenerated before a later block is inspected.
                if not pending_asr_retries:
                    numbered_rows = sorted(
                        (row for row in newest_by_source.values() if int(row.get('number', 0)) > 0),
                        key=lambda row: int(row.get('number', 0)),
                    )
                    fixed_blocks: dict[int, list[dict]] = {}
                    for row in numbered_rows:
                        number = int(row.get('number', 0))
                        fixed_blocks.setdefault((number - 1) // args.asr_checkpoint_interval, []).append(row)
                    for fixed_block, block_rows in sorted(fixed_blocks.items()):
                        eligible_rows = [
                            row for row in block_rows
                            if len(subtitle_words(str(row.get('official_subtitle', '')))) >= 3
                        ]
                        if not eligible_rows:
                            continue
                        accepted_count = sum(str(row.get('source_audio_path', '')) in asr_checked_sources for row in eligible_rows)
                        if accepted_count == len(eligible_rows):
                            continue
                        if accepted_count:
                            inferred_group = fixed_block + 1
                            for row in eligible_rows:
                                source_path = str(row.get('source_audio_path', ''))
                                if source_path and source_path not in asr_checked_sources:
                                    pending_asr_retries[source_path] = {
                                        'group': inferred_group,
                                        'source_audio_path': source_path,
                                        'target_language': args.language,
                                        'attempts': [{'attempt': 1, 'satisfactory': False, 'recovered_from_pipeline_state': True}],
                                        'satisfactory': False,
                                        'pending_retry': True,
                                    }
                            if pending_asr_retries:
                                print(f"ASR GROUP {inferred_group} recovered retry queue items={len(pending_asr_retries)} from pipeline state", flush=True)
                        # A block with no accepted item simply has not begun
                        # its ASR pass; it belongs to the normal next group.
                        break
                # Finish a previously interrupted retry queue before looking
                # at any new WEM.  This preserves the strict 500 → retry →
                # recheck → next-500 order.
                retry_sources = set(pending_asr_retries)
                retry_rows = [newest_by_source[source] for source in retry_sources if source in newest_by_source]
                retry_rows.sort(key=lambda item: int(item.get('number', 0)))
                if retry_rows:
                    resume_retry_group: list[dict] = []
                    for previous in retry_rows:
                        internal_path = str(previous['source_audio_path'])
                        relative = Path(*internal_path.replace('\\', '/').split('/'))
                        resume_retry_group.append({
                            'row': previous, 'number': int(previous.get('number', 0)),
                            'result': dict(previous), 'output_wem': Path(str(previous['output_wem_path'])),
                            'temp_wem': temp_root / '_asr_resume_retry' / relative,
                            'temp_wav': (temp_root / '_asr_resume_retry' / relative).with_suffix('.wav'),
                            'target_wav': (target_temp_root / '_asr_resume_retry' / relative).with_suffix('.wav'),
                        })
                    retry_histories = {
                        source: list(entry.get('attempts', []))
                        for source, entry in pending_asr_retries.items()
                        if source in newest_by_source
                    }
                    # Never let an old five-attempt failure suppress the
                    # regeneration of fresh one-attempt failures.  Each
                    # retry depth gets its own pass, so 1/5 entries are
                    # regenerated immediately while 5/5 entries are merely
                    # finalized for the later fallback/report path.
                    retry_buckets: dict[int, list[dict]] = {}
                    for item in resume_retry_group:
                        source_path = str(item['row']['source_audio_path'])
                        retry_buckets.setdefault(len(retry_histories.get(source_path, [])), []).append(item)
                    print(f"ASR GROUP resume retry queue items={len(resume_retry_group)}", flush=True)
                    for retry_depth, retry_bucket in sorted(retry_buckets.items()):
                        first_number = min(int(item['number']) for item in retry_bucket)
                        fixed_group = ((first_number - 1) // args.asr_checkpoint_interval) + 1
                        print(f"ASR GROUP {fixed_group} resume retry depth={retry_depth}/{args.asr_max_attempts} items={len(retry_bucket)}", flush=True)
                        bucket_histories = {
                            str(item['row']['source_audio_path']): retry_histories[str(item['row']['source_audio_path'])]
                            for item in retry_bucket
                        }
                        audit_asr_group(retry_bucket, fixed_group, bucket_histories)
                # Do not audit an arbitrary partial group on resume.  A
                # production checkpoint is always based on completed WEM
                # count: 500, 1,000, 1,500, ... .  The normal output path
                # invokes ``audit_asr_group`` exactly when it reaches the
                # next multiple.  Here we recover only a retry journal from
                # a checkpoint already entered before shutdown; scanning
                # every unchecked WEM would wrongly start Whisper midway
                # through the next 500-WEM production group.

            # One input worker and one output worker keep the CUDA model fed.
            # The output queue is bounded: it can absorb short Wwise spikes
            # without retaining an unbounded number of generated WAV arrays.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="english-wav-prefetch") as prefetch_executor, \
                 ThreadPoolExecutor(max_workers=1, thread_name_prefix="wem-output") as output_executor:
                while row is not None:
                    if pause_request_path.is_file():
                        pause_requested = True
                        print("PAUSE requested | finishing the last active output before exit", flush=True)
                        break
                    number = int(row["number"])
                    internal_path = str(row["source_audio_path"])
                    relative_wem = Path(*internal_path.replace("\\", "/").split("/"))
                    temp_wem = temp_root / relative_wem
                    temp_wav = temp_wem.with_suffix(".wav")
                    target_wav = (target_temp_root / relative_wem).with_suffix(".wav")
                    output_wem = (wem_root / relative_wem).with_suffix(".wem")
                    target_wav.parent.mkdir(parents=True, exist_ok=True)
                    result = dict(row)
                    result['generation_engine'] = args.engine
                    if args.engine == 'voxcpm2':
                        result['voxcpm_steps'] = args.voxcpm_steps
                        result['generation_seed'] = stable_seed(internal_path)
                    result["output_wav_path"] = str(target_wav)
                    result["output_wem_path"] = str(output_wem)
                    result["wwise_conversion"] = "Vorbis Quality Medium"
                    result["started_at"] = datetime.now(timezone.utc).isoformat()
                    next_row = None
                    next_future = None
                    output_queued = False
                    try:
                        # Do not let the small async Wwise queue cross a
                        # 500-line ASR boundary.  The preceding group is
                        # fully written, verified, and checkpointed before
                        # XTTS begins the next group.
                        if (
                            args.asr_checkpoint_interval
                            and number > 1
                            and (number - 1) % args.asr_checkpoint_interval == 0
                        ):
                            print(
                                f"ASR BOUNDARY {number - 1}/{total} consolidating previous "
                                f"{args.asr_checkpoint_interval}-line group",
                                flush=True,
                            )
                            while pending_outputs:
                                preview_active = args.preview_wav_playback and not (preview_root / '_preview_disabled').is_file()
                                finish_output(pending_outputs.pop(0), wait=True, wait_for_preview=preview_active)
                            if asr_pending_group:
                                group = list(asr_pending_group)
                                asr_pending_group.clear()
                                audit_asr_group(group, (number - 1) // args.asr_checkpoint_interval)
                        preparation_error = None
                        try:
                            if prepared_future is None:
                                extract_and_decode_english_wav(
                                    args.reader, args.decoder, Path(row["original_archive_path"]), internal_path,
                                    temp_wem, temp_wav, number, total, prefetched=False,
                                )
                            else:
                                prepared_future.result()
                        except Exception as error:
                            preparation_error = error

                        # Start the next CPU-only read before entering the GPU
                        # call for this item.  The executor continues during
                        # VoxCPM2 synthesis and the subsequent Wwise encode.
                        next_row = next(pending, None)
                        if next_row is not None:
                            next_internal = str(next_row["source_audio_path"])
                            next_relative = Path(*next_internal.replace("\\", "/").split("/"))
                            next_wem = temp_root / next_relative
                            next_wav = next_wem.with_suffix(".wav")
                            next_future = prefetch_executor.submit(
                                extract_and_decode_english_wav,
                                args.reader, args.decoder, Path(next_row["original_archive_path"]), next_internal,
                                next_wem, next_wav, int(next_row["number"]), total, True,
                            )
                        if preparation_error is not None:
                            raise preparation_error

                        # Publish finished WEMs without holding up the next
                        # GPU call.  If Wwise ever falls persistently behind,
                        # the four-item cap is the only intentional backpressure.
                        while pending_outputs and finish_output(pending_outputs[0], wait=False):
                            pending_outputs.pop(0)
                        preview_active = args.preview_wav_playback and not (preview_root / '_preview_disabled').is_file()
                        output_buffer = 2 if preview_active else 4
                        while len(pending_outputs) >= output_buffer:
                            finish_output(pending_outputs.pop(0), wait=True, wait_for_preview=preview_active)

                        print(
                            f"ITEM {number}/{total} stage=generate_{args.engine} "
                            f"english_subtitle={json.dumps(row.get('english_subtitle', ''), ensure_ascii=False)} "
                            f"target_subtitle={json.dumps(row.get('official_subtitle', ''), ensure_ascii=False)}",
                            flush=True,
                        )
                        # An ASR group releases TTS to make its CUDA memory
                        # available to Whisper.  The normal production path
                        # must therefore reacquire it before the first item
                        # after that group (not only for retry items).
                        ensure_generation_model()
                        audio, generated_rate = generate_with_engine(
                            args.engine, model, str(row["synthesis_text"]), temp_wav, temp_root,
                            args.language, args.voxcpm_steps, result.get("generation_seed"),
                        )
                        if preview_active:
                            preview_sequence += 1
                            target_preview_sequence = preview_sequence
                        else:
                            target_preview_sequence = None
                        output_item = {
                            "row": row, "number": number, "result": result,
                            "temp_wem": temp_wem, "temp_wav": temp_wav,
                            "target_wav": target_wav, "output_wem": output_wem,
                            "preview_required": preview_active,
                        }
                        output_item["output_future"] = output_executor.submit(
                            normalize_and_encode_generated_wem,
                            audio, generated_rate, temp_wav, target_wav, output_wem,
                            args.wwise_console, args.wwise_project, args.decoder, number, total,
                            preview_root if preview_active else None, target_preview_sequence,
                        )
                        pending_outputs.append(output_item)
                        output_queued = True
                    except Exception as error:
                        result.update({"status": "failed", "error": str(error), "error_traceback": traceback.format_exc()})
                        failed += 1
                        print(f"ERROR {number}/{total} {type(error).__name__}: {error}", flush=True)
                    if not output_queued:
                        persist_result({
                            "row": row, "number": number, "result": result,
                            "temp_wem": temp_wem, "temp_wav": temp_wav,
                            "target_wav": target_wav,
                        })
                    row = next_row
                    prepared_future = next_future
                while pending_outputs:
                    preview_active = args.preview_wav_playback and not (preview_root / '_preview_disabled').is_file()
                    finish_output(pending_outputs.pop(0), wait=True, wait_for_preview=preview_active)
                if not pause_requested and args.asr_checkpoint_interval and asr_pending_group:
                    audit_asr_group(asr_pending_group, max(1, math.ceil(generated / args.asr_checkpoint_interval)))
                    asr_pending_group.clear()
                # The primary batch ends here.  No Vox fallback, exception
                # ASR, original-WEM retention, or BA2 packaging runs yet.
                # Persist the later work explicitly so nothing is lost.
                if not pause_requested:
                    deferred_jobs = args.run_dir / 'deferred_followup_jobs.jsonl'
                    with deferred_jobs.open('w', encoding='utf-8', newline='\n') as jobs_file:
                        for source_path, row_data in voxcpm_fallback_rows.items():
                            job = dict(row_data)
                            job['deferred_reason'] = (
                                'asr_unresolved_after_max_attempts'
                                if str(source_path) in retry_histories
                                else 'short_subtitle_requires_voxcpm'
                            )
                            write_json_line(jobs_file, job)
                        for row_data in deferred_exception_rows:
                            job = dict(row_data)
                            job['deferred_reason'] = 'asterisk_subtitle_requires_english_asr'
                            write_json_line(jobs_file, job)
                    print(
                        f"DEFERRED FOLLOW-UP saved={len(voxcpm_fallback_rows) + len(deferred_exception_rows)} "
                        f"path={deferred_jobs}",
                        flush=True,
                    )
                if pause_requested:
                    print(f"PAUSED generated={generated} failed={failed}", flush=True)
    except PauseRequested:
        pause_requested = True
        print(f"PAUSED generated={generated} failed={failed} during_asr=True", flush=True)
    finally:
        if args.preview_wav_playback:
            stop_background_wav_player(preview_root)
        if model is not None and hasattr(model, 'close'):
            model.close()
        if model is not None:
            del model
        gc.collect()
        torch.cuda.empty_cache()
        connection.close()

    final_report_json = args.run_dir / 'final_report.json'
    final_report_txt = args.run_dir / 'final_report.txt'
    final_counts: dict[str, int] = {}
    if results_path.is_file():
        latest: dict[str, dict] = {}
        for line in results_path.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                row = json.loads(line); latest[str(row.get('source_audio_path', ''))] = row
            except json.JSONDecodeError: pass
        for row in latest.values(): final_counts[str(row.get('status', 'unknown'))] = final_counts.get(str(row.get('status', 'unknown')), 0) + 1
    report = {'run_id': run_id, 'target_language': args.language, 'total': total, 'generated': generated, 'failed': failed, 'status_counts': final_counts, 'asr_checked': asr_checked, 'asr_retry_recovered': asr_retry_recovered, 'asr_unresolved': asr_unresolved, 'asr_error_register': str(asr_errors_path), 'results': str(results_path)}
    final_report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    final_report_txt.write_text('\n'.join(['GameDubber final report', f'Run: {run_id}', f'Language: {args.language}', f'Total: {total}', f'Generated: {generated}', f'Failed: {failed}', *[f'{key}: {value}' for key,value in sorted(final_counts.items())], f'ASR error register: {asr_errors_path}']), encoding='utf-8')
    summary = {"engine": args.engine, "run_id": run_id, "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(), "target_language": args.language, "total": total, "generated": generated, "failed": failed, "paused": pause_requested, "resumed": args.resume, "wem_root": str(wem_root), "wem_format": "Wwise Custom Vorbis", "wwise_conversion": "Vorbis Quality Medium", "wwise_project": str(args.wwise_project), "background_wav_preview": args.preview_wav_playback, "results": str(results_path), "final_report_json": str(final_report_json), "final_report_txt": str(final_report_txt), "status_counts": final_counts, "asr_error_register": str(asr_errors_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(temp_root, ignore_errors=True)
    shutil.rmtree(target_temp_root, ignore_errors=True)
    print(f"DONE generated={generated} failed={failed} paused={pause_requested} original_retained={retained_original} asr_checked={asr_checked} asr_recovered={asr_retry_recovered} asr_unresolved={asr_unresolved}", flush=True)
    print(f"FINAL REPORT {final_report_txt} | {json.dumps(final_counts, ensure_ascii=False)}", flush=True)
    if asr_unresolved:
        print(f"ASR ERROR REGISTER {asr_errors_path}", flush=True)
    return 3 if pause_requested else (0 if not failed else 1)


if __name__ == "__main__":
    raise SystemExit(main())
