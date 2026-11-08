"""Safely recover missing photos from the exact official HelloFresh recipe page.

Dry-run by default. Never use the stored imported URL as proof of identity,
never overwrite an active photo, and never restore quarantined files.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import urllib.error

import server


def run(limit=0, start_after=0, apply=False, report="/data/official_source_photo_recovery.json"):
    recipes = sorted(
        (r for r in server.browse_catalog()
         if 1000000 <= int(r["id"]) < 2000000 and int(r["id"]) > start_after),
        key=lambda r: int(r["id"]))
    counts = collections.Counter()
    samples = collections.defaultdict(list)
    seen_assets = {}
    # Active images are protected from accidental reuse across different recipes.
    known_digests = {}
    for p in server.PHOTO_DIR.iterdir():
        if p.suffix.lower() not in (".jpg", ".png", ".webp", ".gif") or not p.stem.isdigit():
            continue
        if server.valid_photo_cache_entry(p):
            try:
                known_digests.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), set()).add(int(p.stem))
            except OSError:
                pass
    checked = 0
    for recipe in recipes:
        rid = int(recipe["id"])
        if server.cached_photo(rid):
            counts["already_active"] += 1
            continue
        if limit and checked >= limit:
            break
        checked += 1
        reason = ""
        url = ""
        try:
            candidates = server.official_images(recipe)
            if len(candidates) != 1:
                reason = "no_unique_official_image"
            else:
                url = server.clean_official_photo_candidate(candidates[0])
                if not url or not server.complete_recipe_photo_url(url):
                    reason = "invalid_official_url"
                elif url in seen_assets and seen_assets[url] != rid:
                    reason = "shared_official_asset"
                else:
                    seen_assets[url] = rid
                    if apply:
                        # download_photo enforces approved HTTPS hosts, redirects,
                        # size, file format, and rejected digest restrictions.
                        diagnostics = []
                        downloaded = server.download_photo(url, rid, prefer_jpeg=True, diagnostics=diagnostics)
                        if not downloaded:
                            reason = "download_rejected_" + (diagnostics[-1] if diagnostics else "unknown")
                        else:
                            path = downloaded[0]
                            digest = hashlib.sha256(path.read_bytes()).hexdigest()
                            owners = known_digests.get(digest, set())
                            if digest in server.photo_rejected_digests or (owners and owners != {rid}):
                                # Quarantine only the newly downloaded file.
                                path.rename(path.with_name(path.name + ".suspect"))
                                reason = "duplicate_or_rejected_digest"
                            else:
                                known_digests.setdefault(digest, set()).add(rid)
                                reason = "restored"
                    else:
                        reason = "eligible_dry_run"
        except urllib.error.HTTPError as exc:
            reason = "http_" + str(exc.code)
        except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
            reason = "error_" + type(exc).__name__
        counts[reason] += 1
        if len(samples[reason]) < 10:
            samples[reason].append({"id": rid, "name": recipe.get("name", ""), "url": url})
        if checked % 25 == 0:
            print("Checked:", checked, "restored:", counts["restored"],
                  "no official image:", counts["no_unique_official_image"], flush=True)
    result = {"mode": "APPLY" if apply else "DRY_RUN", "checked": checked,
              "counts": dict(counts), "samples": dict(samples)}
    Path(report).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-after", type=int, default=0)
    parser.add_argument("--report", default="/data/official_source_photo_recovery.json")
    args = parser.parse_args()
    if args.limit < 0 or args.start_after < 0:
        parser.error("limit and start-after must be nonnegative")
    run(args.limit, args.start_after, args.apply, args.report)


if __name__ == "__main__":
    main()
