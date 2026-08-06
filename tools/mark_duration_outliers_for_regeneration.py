"""Mark oversized completed target WEMs for a safe, resume-driven retry.

This migration is append-only for results.jsonl and never removes an existing
WEM. The next production resume treats the latest status as pending and
overwrites the old WEM only after a duration-compliant replacement is ready.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def write_json_line(handle, row: dict) -> None:
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--ratio", type=float, default=1.75)
    parser.add_argument("--expect-count", type=int)
    args = parser.parse_args()
    if args.ratio <= 1.0:
        raise SystemExit("--ratio must be greater than 1.0")

    results_path = args.run_dir / "results.jsonl"
    if not results_path.is_file():
        raise SystemExit(f"Missing results journal: {results_path}")
    latest: dict[str, dict] = {}
    for line in results_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            source = str(row.get("source_audio_path", ""))
            if source:
                latest[source] = row
        except json.JSONDecodeError:
            continue

    connection = sqlite3.connect(args.database)
    run_id = args.run_dir.name
    rows = connection.execute(
        """SELECT source_audio_path, reference_duration_ms, output_duration_ms
           FROM production_voice_outputs
           WHERE run_id=? AND status='wem_generated'
             AND reference_duration_ms >= 1500 AND output_duration_ms > 0
             AND CAST(output_duration_ms AS REAL) / reference_duration_ms > ?
           ORDER BY source_audio_path""",
        (run_id, args.ratio),
    ).fetchall()
    if args.expect_count is not None and len(rows) != args.expect_count:
        raise SystemExit(f"Expected {args.expect_count} outliers, found {len(rows)}; no changes made")

    timestamp = datetime.now(timezone.utc).isoformat()
    queue_path = args.run_dir / "duration_outlier_regeneration_queue.jsonl"
    marked = 0
    with results_path.open("a", encoding="utf-8", newline="\n") as results, \
         queue_path.open("w", encoding="utf-8", newline="\n") as queue:
        for source, reference_ms, output_ms in rows:
            prior = latest.get(str(source))
            if prior is None:
                continue
            limit_ms = round(int(reference_ms) * args.ratio)
            row = dict(prior)
            row.update({
                "status": "duration_outlier_pending_regeneration",
                "duration_recheck_pending": True,
                "duration_validation": {
                    "reference_duration_ms": int(reference_ms),
                    "output_duration_ms": int(output_ms),
                    "maximum_duration_ms": limit_ms,
                    "duration_ratio": round(int(output_ms) / int(reference_ms), 4),
                    "duration_satisfactory": False,
                    "maximum_ratio": args.ratio,
                },
                "duration_marked_at": timestamp,
            })
            write_json_line(results, row)
            write_json_line(queue, row)
            marked += 1

    connection.executemany(
        """UPDATE production_voice_outputs
           SET status='duration_outlier_pending_regeneration', error=?
           WHERE run_id=? AND source_audio_path=?""",
        [
            (f"target duration exceeds {args.ratio:.2f}x English reference; regeneration pending", run_id, str(source))
            for source, _reference, _output in rows
            if str(source) in latest
        ],
    )
    connection.commit()
    connection.close()
    print(f"Marked {marked} duration outlier(s); queue: {queue_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
