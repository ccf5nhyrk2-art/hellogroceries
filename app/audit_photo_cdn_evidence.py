"""Read-only CDN and quarantined-photo evidence audit for exact official recipes.

Never writes image files, changes rejected hashes, or treats CDN variants as
independent recipe identity. Use before running any recovery with --apply.
"""
import argparse
import collections
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import server


def asset_key(url):
    parsed = urllib.parse.urlsplit(url)
    parts = parsed.path.split("/")
    for index, part in enumerate(parts):
        if part in ("recipes", "hellofresh_s3") and index + 2 < len(parts) and parts[index + 1] == "image":
            return "/".join(parts[index:]).lower()
    return ""


def variants(url):
    """Return distinct, approved deliveries of precisely the same asset."""
    key = asset_key(url)
    if not key:
        return []
    parsed = urllib.parse.urlsplit(url)
    candidates = [url]
    for host in ("img.hellofresh.com", "media.hellofresh.com"):
        for transform in ("f_jpg,q_85,w_1200/", "f_webp,q_85,w_1200/", "f_auto,fl_lossy,h_640,q_auto,w_1200/", ""):
            candidates.append("https://" + host + "/" + transform + key)
    result = []
    for candidate in candidates:
        if (candidate not in result and asset_key(candidate) == key
                and server.complete_recipe_photo_url(candidate)):
            result.append(candidate)
    return result


def fetch(url):
    if not server.complete_recipe_photo_url(url):
        return {"status": "unsafe_url"}
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; HelloGroceries/1.0)",
        "Accept": "image/jpeg,image/png,image/webp;q=0.9",
    })
    try:
        with urllib.request.build_opener(server.SafePhotoRedirect).open(request, timeout=12) as response:
            if not server.allowed_photo_url(response.geturl()):
                return {"status": "unsafe_redirect"}
            if int(response.headers.get("Content-Length") or 0) > 8_000_000:
                return {"status": "oversized"}
            raw = response.read(8_000_001)
        if not 2048 <= len(raw) <= 8_000_000 or not server.image_type(raw[:16]):
            return {"status": "invalid_image"}
        digest = hashlib.sha256(raw).hexdigest()
        return {"status": "rejected_digest" if digest in server.photo_rejected_digests else "valid_image",
                "digest": digest, "bytes": len(raw)}
    except urllib.error.HTTPError as exc:
        return {"status": "http_" + str(exc.code)}
    except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
        return {"status": "error_" + type(exc).__name__}


def run(limit, start_after, report):
    recipes = sorted(
        (recipe for recipe in server.browse_catalog()
         if 1000000 <= int(recipe["id"]) < 2000000 and int(recipe["id"]) > start_after),
        key=lambda recipe: int(recipe["id"]))
    counts = collections.Counter()
    entries = []
    checked = 0
    for recipe in recipes:
        rid = int(recipe["id"])
        if server.cached_photo(rid):
            continue
        if checked >= limit:
            break
        checked += 1
        candidates = list(dict.fromkeys(
            server.clean_official_photo_candidate(u) for u in server.official_images(recipe)))
        candidates = [u for u in candidates if server.complete_recipe_photo_url(u)]
        keys = {asset_key(u) for u in candidates}
        if len(keys) != 1 or not next(iter(keys), ""):
            counts["no_unique_official_asset"] += 1
            entries.append({"id": rid, "name": recipe.get("name", ""),
                            "status": "no_unique_official_asset", "candidate_count": len(candidates)})
            continue
        attempts = []
        for url in variants(candidates[0]):
            result = fetch(url)
            attempts.append({"url": url, **result})
            counts["attempt_" + result["status"]] += 1
        digests = {a["digest"] for a in attempts if "digest" in a}
        usable = [a for a in attempts if a["status"] == "valid_image"]
        status = "usable_variant_found" if usable else "no_usable_variant"
        counts[status] += 1
        entries.append({"id": rid, "name": recipe.get("name", ""),
                        "status": status, "asset": next(iter(keys)),
                        "distinct_image_hashes": len(digests),
                        "attempts": attempts})
        if checked % 10 == 0:
            print("Checked:", checked, "usable:", counts["usable_variant_found"], flush=True)
    result = {"mode": "READ_ONLY", "checked": checked, "counts": dict(counts),
              "entries": entries}
    Path(report).write_text(json.dumps(result, indent=2))
    print(json.dumps({"mode": result["mode"], "checked": checked,
                      "counts": result["counts"], "report": report}, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--start-after", type=int, default=0)
    parser.add_argument("--report", default="/data/photo_cdn_evidence.json")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or args.start_after < 0:
        parser.error("limit must be 1..100 and start-after must be nonnegative")
    run(args.limit, args.start_after, args.report)
