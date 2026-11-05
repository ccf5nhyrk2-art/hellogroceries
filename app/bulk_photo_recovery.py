"""Resumable, bounded recovery of missing imported HelloFresh photos (staging only).

Run: python -u /app/bulk_photo_recovery.py --batch-size 100
Re-run to resume. Never touches already-valid photos or quarantine digests.
"""
import argparse
import datetime
import json
import os
import time
from pathlib import Path

import server


def atomic_json(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2))
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--state-dir", default="/data")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error("--batch-size must be between 1 and 500")
    if not 0 <= args.delay <= 30:
        parser.error("--delay must be between 0 and 30")
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = state_dir / "bulk_photo_recovery_checkpoint.json"
    failures = state_dir / "bulk_photo_recovery_failures.jsonl"
    try:
        state = json.loads(checkpoint.read_text())
    except (OSError, ValueError, TypeError):
        state = {}
    completed = set(map(int, state.get("completed", [])))
    failed = set(map(int, state.get("failed", [])))
    recipes = sorted(
        (r for r in server.browse_catalog() if 1000000 <= int(r["id"]) < 2000000),
        key=lambda r: int(r["id"]),
    )
    stats = {"checked": 0, "recovered": 0, "already_cached": 0, "failed": 0}
    eligible = [r for r in recipes if int(r["id"]) not in completed and
                (args.retry_failures or int(r["id"]) not in failed)]
    batch = eligible[:args.batch_size]
    print(f"Imported: {len(recipes)} | pending: {len(eligible)} | batch: {len(batch)}", flush=True)
    for recipe in batch:
        rid = int(recipe["id"])
        stats["checked"] += 1
        try:
            if server.cached_photo(rid):
                stats["already_cached"] += 1
                completed.add(rid)
                failed.discard(rid)
            else:
                result = server.resolve_photo(recipe, refresh=False)
                if result and server.cached_photo(rid):
                    stats["recovered"] += 1
                    completed.add(rid)
                    failed.discard(rid)
                else:
                    stats["failed"] += 1
                    failed.add(rid)
                    with failures.open("a") as output:
                        output.write(json.dumps({"id": rid, "name": recipe.get("name", ""),
                            "error": "No verified photo candidate", "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}) + "\n")
        except Exception as exc:
            stats["failed"] += 1
            failed.add(rid)
            with failures.open("a") as output:
                output.write(json.dumps({"id": rid, "name": recipe.get("name", ""),
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}) + "\n")
        if stats["checked"] % 10 == 0 or stats["checked"] == len(batch):
            atomic_json(checkpoint, {"completed": sorted(completed), "failed": sorted(failed),
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "last_batch": stats})
            print(f"Progress {stats['checked']}/{len(batch)} | recovered {stats['recovered']} | "
                  f"cached {stats['already_cached']} | failed {stats['failed']}", flush=True)
        if args.delay:
            time.sleep(args.delay)
    print("Batch complete. Re-run to process next batch.", flush=True)


if __name__ == "__main__":
    main()
