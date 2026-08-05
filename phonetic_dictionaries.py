"""User-editable, model-specific phonetic dictionaries for GameDubber."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


DEFAULT_DICTIONARIES = {
    "voxcpm2": {"version": 1, "engine": "voxcpm2", "language": "it", "replacements": [
        {"pattern": r"\b[nN]\.\s*1\b", "replacement": "numero 1", "enabled": True, "note": "Read n.1 as numero 1."},
        {"pattern": r"^Ci si vede\.$", "replacement": "Cì si vede", "enabled": True, "note": "Verified VoxCPM2 special case."},
        {"pattern": r"\b([Tt])u\b", "replacement": r"\1ù", "enabled": True, "note": "Avoid English Tuesday-like pronunciation."},
        {"pattern": r"([Tt]ù)\?", "replacement": r"\1 ?", "enabled": True, "note": "Keep the verified question-boundary cue."},
        {"pattern": r"\b([Rr])ecluta\b", "replacement": r"\1èècluta", "enabled": True, "note": "User-validated VoxCPM2 pronunciation."},
        {"pattern": r"\b([Cc])i\b", "replacement": r"\1ì", "enabled": True, "note": "Avoid English onset for standalone ci."},
    ], "options": {"remove_terminal_period": False, "expand_numbers": True}},
    "xtts_v2": {"version": 1, "engine": "xtts_v2", "language": "it", "replacements": [
        {"pattern": r"\b[nN]\.\s*1\b", "replacement": "numero 1", "enabled": True, "note": "Read n.1 as numero 1."},
    ], "options": {"remove_terminal_period": True}},
    "qwen_0_6b": {"version": 1, "engine": "qwen_0_6b", "language": "it", "replacements": [], "options": {"remove_terminal_period": False}},
    "qwen_1_7b": {"version": 1, "engine": "qwen_1_7b", "language": "it", "replacements": [], "options": {"remove_terminal_period": False}},
    "cosyvoice3": {"version": 1, "engine": "cosyvoice3", "language": "it", "replacements": [], "options": {"remove_terminal_period": False}},
    "chatterbox_v3": {"version": 1, "engine": "chatterbox_v3", "language": "it", "replacements": [], "options": {"remove_terminal_period": False}},
}


def dictionary_path(root: Path, engine: str) -> Path:
    return root / f"{engine}.json"


def ensure_dictionary(root: Path, engine: str) -> Path:
    if engine not in DEFAULT_DICTIONARIES:
        raise ValueError(f"No phonetic dictionary schema for {engine}")
    root.mkdir(parents=True, exist_ok=True)
    path = dictionary_path(root, engine)
    if not path.is_file():
        path.write_text(json.dumps(deepcopy(DEFAULT_DICTIONARIES[engine]), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_dictionary(root: Path, engine: str) -> dict:
    path = ensure_dictionary(root, engine)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"Invalid phonetic dictionary: {path}: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("replacements", []), list):
        raise ValueError(f"Invalid phonetic dictionary structure: {path}")
    return value
