"""Backfill English-reference duration for completed target WEMs.

The game archives are read only.  Sources are extracted in archive-sized
batches into a resumable local cache, metadata is read in vgmstream batches,
and the cache is removed only after every requested duration is persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
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


def latest_rows(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            source = str(row.get("source_audio_path", ""))
            if source:
                rows[source] = row
    return rows


def cache_path(cache: Path, archive: str, source: str) -> Path:
    archive_key = hashlib.sha1(archive.encode("utf-8")).hexdigest()[:12]
    return cache / archive_key / Path(*source.replace("\\", "/").split("/"))


def parse_metadata(output: str) -> dict[str, int]:
    pattern = re.compile(
        r"metadata for (?P<path>.+?)\r?\n"
        r"sample rate:\s*(?P<rate>\d+)\s*Hz.*?"
        r"stream total samples:\s*(?P<samples>\d+)",
        re.DOTALL,
    )
    durations: dict[str, int] = {}
    for match in pattern.finditer(output):
        path = str(Path(match.group("path")).resolve()).casefold()
        durations[path] = round(int(match.group("samples")) * 1000 / int(match.group("rate")))
    return durations


def append_result(handle, row: dict, duration_ms: int) -> None:
    updated = dict(row)
    updated["reference_duration_ms"] = duration_ms
    updated["reference_duration_backfilled_at"] = datetime.now(timezone.utc).isoformat()
    handle.write(json.dumps(updated, ensure_ascii=False) + "\n")
    handle.flush()


def open_database() -> sqlite3.Connection | None:
    database = WORK / "voice_pipeline.db"
    if not database.is_file():
        return None
    connection = sqlite3.connect(database, timeout=5)
    columns = {item[1] for item in connection.execute("PRAGMA table_info(production_voice_outputs)")}
    if "reference_duration_ms" not in columns:
        connection.execute("ALTER TABLE production_voice_outputs ADD COLUMN reference_duration_ms INTEGER")
    return connection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, help="Production run directory; default is the newest run")
    # vgmstream accepts multiple files, but its Windows command-line parser
    # becomes unreliable with very long argument lists. Sixteen keeps each
    # metadata invocation compact while avoiding one process per WEM.
    parser.add_argument("--metadata-batch", type=int, default=16)
    args = parser.parse_args()
    if not READER.is_file() or not VGMSTREAM.is_file():
        raise SystemExit("archive_reader.exe or vgmstream-cli.exe is unavailable")
    run = args.run
    if run is None:
        options = sorted((path for path in RUNS.glob("run-*") if path.is_dir()), key=lambda path: path.stat().st_mtime)
        if not options:
            raise SystemExit("No production run exists")
        run = options[-1]
    run = run.resolve()
    results_path = run / "results.jsonl"
    rows = latest_rows(results_path)
    pending = [
        row for row in rows.values()
        if row.get("status") == "wem_generated" and not isinstance(row.get("reference_duration_ms"), (int, float))
    ]
    cache = WORK / "analysis" / "_generated_reference_duration_cache" / run.name
    manifests = WORK / "analysis" / "_generated_reference_duration_manifests" / run.name
    cache.mkdir(parents=True, exist_ok=True)
    manifests.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in pending:
        grouped[str(row["original_archive_path"])].append(row)
    print(f"DURATION BACKFILL start | run={run.name} | pending={len(pending)} | archives={len(grouped)}", flush=True)
    for archive, group in grouped.items():
        manifest = manifests / f"{hashlib.sha1(archive.encode('utf-8')).hexdigest()[:12]}.tsv"
        with manifest.open("w", encoding="utf-8", newline="\n") as handle:
            for row in group:
                handle.write(f"{row['source_audio_path']}\t{cache_path(cache, archive, str(row['source_audio_path']))}\n")
        process = subprocess.run(
            [str(READER), "extract-manifest", archive, str(manifest)],
            text=True, encoding="utf-8", errors="replace",
        )
        if process.returncode:
            raise RuntimeError(f"Batch extraction failed for {archive}")
    paths_to_rows = {
        str(cache_path(cache, str(row["original_archive_path"]), str(row["source_audio_path"])).resolve()).casefold(): row
        for row in pending
    }
    measured: dict[str, int] = {}
    all_paths = list(paths_to_rows)
    for start in range(0, len(all_paths), max(1, args.metadata_batch)):
        batch = all_paths[start:start + max(1, args.metadata_batch)]
        process = subprocess.run(
            [str(VGMSTREAM), "-m", *batch], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "vgmstream metadata batch failed")
        measured.update(parse_metadata(process.stdout))
        done = min(start + len(batch), len(all_paths))
        if done % 1024 == 0 or done == len(all_paths):
            print(f"DURATION BACKFILL metadata {done}/{len(all_paths)}", flush=True)
    missing = [path for path in all_paths if path not in measured]
    if missing:
        raise RuntimeError(f"Metadata duration was not found for {len(missing)} extracted WEM(s)")
    connection = open_database()
    try:
        with results_path.open("a", encoding="utf-8", newline="\n") as output:
            for index, path in enumerate(all_paths, 1):
                row = paths_to_rows[path]
                duration_ms = measured[path]
                append_result(output, row, duration_ms)
                if connection is not None:
                    connection.execute(
                        "UPDATE production_voice_outputs SET reference_duration_ms=? WHERE run_id=? AND source_audio_path=? AND target_language=?",
                        (duration_ms, run.name, str(row.get("source_audio_path", "")), str(row.get("target_language", ""))),
                    )
                if index % 1024 == 0 or index == len(all_paths):
                    if connection is not None:
                        connection.commit()
                    print(f"DURATION BACKFILL saved {index}/{len(all_paths)}", flush=True)
    finally:
        if connection is not None:
            connection.close()
    report = {
        "run_id": run.name,
        "backfilled": len(all_paths),
        "cache_removed": True,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    (WORK / "analysis" / "generated_reference_duration_backfill.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    shutil.rmtree(cache)
    shutil.rmtree(manifests)
    print(f"DURATION BACKFILL done | saved={len(all_paths)} | cache removed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
