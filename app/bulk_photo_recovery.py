"""Resumable, bounded recovery of missing imported HelloFresh photos (staging only).

Run: python -u /app/bulk_photo_recovery.py --batch-size 100
Re-run to resume. Never touches already-valid photos or quarantine digests.
"""
import argparse
import datetime
import json
import os
import urllib.error
import urllib.request
import time
from pathlib import Path

import server


def atomic_json(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2))
    os.replace(temporary, path)


def classify_failure(recipe):
    """Identify the upstream blocker without accepting unverified artwork."""
    stored = recipe.get("image") or ""
    if not server.complete_recipe_photo_url(stored):
        stored_state = "incomplete_or_missing_image_url"
    elif server.trusted_imported_photo_url(recipe):
        stored_state = "trusted_image_url"
    else:
        stored_state = "unverified_image_url"

    source = recipe.get("url") or ""
    if not source or not server.allowed_url(source):
        return stored_state + ":invalid_recipe_page_url"
    try:
        req = urllib.request.Request(
            source, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            if not server.allowed_url(response.geturl()):
                return stored_state + ":unsafe_redirect"
            page = response.read(3_000_000).decode("utf-8", "replace")
        if "application/ld+json" not in page.lower():
            return stored_state + ":no_structured_data"
        if not server.official_images_from_page(recipe, page):
            return stored_state + ":no_matching_recipe_image_metadata"
        return stored_state + ":verified_image_download_rejected"
    except urllib.error.HTTPError as exc:
        return stored_state + ":source_http_" + str(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return stored_state + ":source_fetch_" + type(exc).__name__



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--retry-failures", action="store_true", help="Include previously failed recipes")
    parser.add_argument("--failed-only", action="store_true", help="Retry only IDs recorded as failed")
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
    reasons = {}
    eligible = [r for r in recipes if int(r["id"]) not in completed and
                ((int(r["id"]) in failed) if args.failed_only else
                 (args.retry_failures or int(r["id"]) not in failed))]
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
                download_reasons = []
                result = server.resolve_photo(recipe, refresh=False, diagnostics=download_reasons)
                if result and server.cached_photo(rid):
                    stats["recovered"] += 1
                    completed.add(rid)
                    failed.discard(rid)
                else:
                    stats["failed"] += 1
                    failed.add(rid)
                    reason = ('download:' + ','.join(sorted(set(download_reasons)))) if download_reasons else classify_failure(recipe)
                    reasons[reason] = reasons.get(reason, 0) + 1
                    with failures.open("a") as output:
                        output.write(json.dumps({"id": rid, "name": recipe.get("name", ""),
                            "error": reason, "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}) + "\n")
        except Exception as exc:
            stats["failed"] += 1
            failed.add(rid)
            reasons["exception"] = reasons.get("exception", 0) + 1
            with failures.open("a") as output:
                output.write(json.dumps({"id": rid, "name": recipe.get("name", ""),
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}) + "\n")
        if stats["checked"] % 10 == 0 or stats["checked"] == len(batch):
            atomic_json(checkpoint, {"completed": sorted(completed), "failed": sorted(failed),
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "last_batch": stats, "failure_reasons": reasons})
            print(f"Progress {stats['checked']}/{len(batch)} | recovered {stats['recovered']} | "
                  f"cached {stats['already_cached']} | failed {stats['failed']}", flush=True)
        if args.delay:
            time.sleep(args.delay)
    print("Failure reasons:", json.dumps(reasons, sort_keys=True), flush=True)
    print("Batch complete. Re-run to process next batch.", flush=True)


if __name__ == "__main__":
    main()
