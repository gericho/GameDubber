"""Audit XTTS reliability against the duration of the English source WEM.

This is intentionally read-only for game data.  It samples completed XTTS
output, temporarily extracts each corresponding English WEM, obtains its
duration through vgmstream metadata, and joins the result to the existing ASR
checkpoint journal.  No generated WEM is changed.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
RUNS = WORK / "output" / "target_voice" / "runs"
READER = ROOT / "archive_reader" / "target" / "release" / "archive_reader.exe"
VGMSTREAM = ROOT / "tools" / "vgmstream" / "vgmstream-cli.exe"
OUTPUT = WORK / "analysis" / "xtts_english_duration_audit.jsonl"
SUMMARY = WORK / "analysis" / "xtts_english_duration_summary.json"
TEMP = WORK / "analysis" / "_xtts_duration_temp"
SAMPLE_PER_GENERATED_BUCKET = 200


def read_latest_rows(path: Path, key: str) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    if not path.is_file():
        return latest
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = str(row.get(key, ""))
            if value:
                latest[value] = row
    return latest


def generated_bucket(duration_ms: int) -> str:
    if duration_ms < 1400:
        return "under_1_4s"
    if duration_ms < 2000:
        return "1_4_to_2_0s"
    if duration_ms < 3000:
        return "2_0_to_3_0s"
    return "over_3_0s"


def original_bucket(duration_ms: int) -> str:
    if duration_ms < 1000:
        return "under_1_0s"
    if duration_ms < 1500:
        return "1_0_to_1_5s"
    if duration_ms < 2000:
        return "1_5_to_2_0s"
    if duration_ms < 3000:
        return "2_0_to_3_0s"
    if duration_ms < 5000:
        return "3_0_to_5_0s"
    return "over_5_0s"


def stable_order(row: dict) -> str:
    return hashlib.sha256(str(row["source_audio_path"]).encode("utf-8")).hexdigest()


def source_duration_ms(row: dict) -> int:
    internal = str(row["source_audio_path"])
    token = hashlib.sha1(internal.encode("utf-8")).hexdigest()[:16]
    wem = TEMP / f"{token}.wem"
    try:
        extract = subprocess.run(
            [str(READER), "extract", str(row["original_archive_path"]), internal, str(wem)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if extract.returncode or not wem.is_file():
            raise RuntimeError(extract.stderr.strip() or extract.stdout.strip() or "BA2 extraction failed")
        probe = subprocess.run(
            [str(VGMSTREAM), "-m", str(wem)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if probe.returncode:
            raise RuntimeError(probe.stderr.strip() or probe.stdout.strip() or "vgmstream metadata failed")
        samples = re.search(r"stream total samples:\s*(\d+)", probe.stdout)
        rate = re.search(r"sample rate:\s*(\d+)\s*Hz", probe.stdout)
        if not samples or not rate:
            raise RuntimeError("WEM duration metadata was not found")
        return round(int(samples.group(1)) * 1000 / int(rate.group(1)))
    finally:
        wem.unlink(missing_ok=True)


def main() -> int:
    if not READER.is_file() or not VGMSTREAM.is_file():
        raise SystemExit("archive_reader.exe or vgmstream-cli.exe is unavailable")
    run_dirs = sorted((path for path in RUNS.glob("run-*") if path.is_dir()), key=lambda path: path.stat().st_mtime)
    if not run_dirs:
        raise SystemExit("No production run directory is available")
    run = run_dirs[-1]
    results = read_latest_rows(run / "results.jsonl", "source_audio_path")
    asr = read_latest_rows(run / "asr_checkpoint_checks.jsonl", "source_audio_path")
    candidates = [
        row for row in results.values()
        if row.get("generation_engine") == "xtts_v2" and row.get("status") == "wem_generated"
        and int(row.get("duration_ms") or 0) > 0
    ]
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        buckets[generated_bucket(int(row["duration_ms"]))].append(row)
    selected: list[dict] = []
    for name in ("under_1_4s", "1_4_to_2_0s", "2_0_to_3_0s", "over_3_0s"):
        selected.extend(sorted(buckets[name], key=stable_order)[:SAMPLE_PER_GENERATED_BUCKET])
    existing = read_latest_rows(OUTPUT, "source_audio_path")
    selected = [row for row in selected if row["source_audio_path"] not in existing]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(parents=True, exist_ok=True)
    print(f"XTTS duration audit | run={run.name} | candidates={len(candidates)} | new samples={len(selected)}", flush=True)
    with OUTPUT.open("a", encoding="utf-8", newline="\n") as handle:
        for index, row in enumerate(selected, 1):
            source = str(row["source_audio_path"])
            try:
                duration = source_duration_ms(row)
                asr_row = asr.get(source, {})
                attempts = list(asr_row.get("attempts") or [])
                first_pass = bool(attempts and attempts[0].get("satisfactory"))
                record = {
                    "run_id": run.name,
                    "number": row.get("number"),
                    "source_audio_path": source,
                    "speaker_id": row.get("speaker_id"),
                    "english_subtitle": row.get("english_subtitle"),
                    "target_subtitle": row.get("official_subtitle"),
                    "english_duration_ms": duration,
                    "target_duration_ms": int(row.get("duration_ms") or 0),
                    "target_to_english_ratio": round((int(row.get("duration_ms") or 0) / duration), 3) if duration else None,
                    "asr_attempts": len(attempts),
                    "asr_first_pass": first_pass,
                    "asr_final_pass": bool(asr_row.get("satisfactory")) if attempts else None,
                    "audited_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as error:  # retain failures for an auditable report
                record = {"run_id": run.name, "number": row.get("number"), "source_audio_path": source,
                          "audit_error": str(error), "audited_at": datetime.now(timezone.utc).isoformat()}
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            if index % 25 == 0 or index == len(selected):
                print(f"XTTS duration audit | {index}/{len(selected)}", flush=True)
    all_rows = read_latest_rows(OUTPUT, "source_audio_path")
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"samples": 0, "first_pass": 0, "final_pass": 0, "errors": 0})
    for row in all_rows.values():
        if "english_duration_ms" not in row:
            stats["errors"]["errors"] += 1
            continue
        bucket = original_bucket(int(row["english_duration_ms"]))
        stats[bucket]["samples"] += 1
        stats[bucket]["first_pass"] += int(bool(row.get("asr_first_pass")))
        stats[bucket]["final_pass"] += int(bool(row.get("asr_final_pass")))
    recommendation = None
    lower_bounds = {"under_1_0s": 0, "1_0_to_1_5s": 1000, "1_5_to_2_0s": 1500,
                    "2_0_to_3_0s": 2000, "3_0_to_5_0s": 3000, "over_5_0s": 5000}
    for bucket in ("under_1_0s", "1_0_to_1_5s", "1_5_to_2_0s", "2_0_to_3_0s", "3_0_to_5_0s", "over_5_0s"):
        value = stats[bucket]
        if value["samples"] >= 40 and value["first_pass"] / value["samples"] >= 0.90 and value["final_pass"] / value["samples"] >= 0.95:
            recommendation = lower_bounds[bucket]
            break
    summary = {
        "run_id": run.name,
        "generated_sample_buckets": {name: len(rows) for name, rows in buckets.items()},
        "analysed_samples": len(all_rows),
        "recommended_minimum_xtts_english_duration_ms": recommendation,
        "criterion": "first-pass ASR >= 90% and final ASR >= 95%, with at least 40 samples in the duration band",
        "by_english_duration": {
            key: {**value,
                  "first_pass_rate": round(value["first_pass"] / value["samples"], 4) if value["samples"] else None,
                  "final_pass_rate": round(value["final_pass"] / value["samples"], 4) if value["samples"] else None}
            for key, value in stats.items()
        },
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    shutil.rmtree(TEMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
