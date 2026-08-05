"""Migrate legacy short XTTS WEMs into the deferred VoxCPM2 queue.

The script is deliberately append-only for the production journal: it keeps
the old WEMs on disk as a rollback/reference copy while making the newest
result state ``deferred_short_reference``.  Resume then skips these sources
until the later VoxCPM2 phase is implemented.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def latest_records(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            source = str(record.get("source_audio_path", ""))
            if source:
                records[source] = record
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--minimum-ms", type=int, default=1500)
    parser.add_argument("--expect-count", type=int, required=True)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    results_path = run_dir / "results.jsonl"
    checks_path = run_dir / "asr_checkpoint_checks.jsonl"
    if not results_path.is_file() or not checks_path.is_file():
        raise SystemExit("The run must contain results.jsonl and asr_checkpoint_checks.jsonl.")

    latest = latest_records(results_path)
    latest_asr = latest_records(checks_path)
    candidates: list[dict] = []
    for source, record in latest.items():
        duration = record.get("reference_duration_ms")
        if record.get("status") != "wem_generated" or not isinstance(duration, (int, float)):
            continue
        if duration >= args.minimum_ms:
            continue
        asr = latest_asr.get(source)
        if asr is not None and bool(asr.get("satisfactory")):
            continue
        migrated = dict(record)
        migrated.update({
            "status": "deferred_short_reference",
            "deferred_reason": "legacy_unvalidated_xtts_reference_below_minimum_duration",
            "xtts_minimum_reference_duration_ms": args.minimum_ms,
            "legacy_wem_retained": True,
            "legacy_asr_outcome": "failed" if asr is not None else "not_checked",
            "migration_timestamp": datetime.now(timezone.utc).isoformat(),
        })
        candidates.append(migrated)

    if len(candidates) != args.expect_count:
        raise SystemExit(
            f"Refusing migration: found {len(candidates):,} candidates, expected {args.expect_count:,}."
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path("backups") / f"{stamp}-legacy-short-reference-migration"
    backup_dir.mkdir(parents=True, exist_ok=False)
    database_backup = backup_dir / "voice_pipeline.db"
    with sqlite3.connect(args.database) as source_db, sqlite3.connect(database_backup) as backup_db:
        source_db.backup(backup_db)

    manifest = run_dir / "legacy_short_reference_deferred.jsonl"
    if manifest.exists():
        raise SystemExit(f"Migration manifest already exists: {manifest}")
    temporary_manifest = manifest.with_suffix(".jsonl.tmp")
    with temporary_manifest.open("w", encoding="utf-8", newline="\n") as handle:
        for record in candidates:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary_manifest.replace(manifest)

    with results_path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in candidates:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    with sqlite3.connect(args.database) as connection:
        connection.executemany(
            """UPDATE production_voice_outputs
               SET status = ?, error = ?
               WHERE run_id = ? AND source_audio_path = ? AND target_language = ?""",
            [
                (
                    "deferred_short_reference",
                    "Legacy short reference deferred for later VoxCPM2 processing; existing WEM retained.",
                    run_dir.name,
                    str(record["source_audio_path"]),
                    str(record.get("target_language", "")),
                )
                for record in candidates
            ],
        )
        connection.commit()

    print(f"Deferred {len(candidates):,} legacy short unvalidated WEMs.")
    print(f"Migration manifest: {manifest}")
    print(f"Database backup: {database_backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
