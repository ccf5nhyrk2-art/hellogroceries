"""Read-only diagnosis of rejected targeted HelloFresh photo downloads.

Samples the saved audit; never writes photos or changes quarantine.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request
import server


def inspect(url):
    if not server.complete_recipe_photo_url(url):
        return "invalid_or_incomplete_url"
    url = server.force_verified_photo_jpeg_url(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; HelloGroceries/1.0)",
            "Accept": "image/jpeg,image/png;q=0.9,image/webp;q=0.5,image/avif;q=0.4",
        },
    )
    try:
        opener = urllib.request.build_opener(server.SafePhotoRedirect)
        with opener.open(req, timeout=12) as response:
            if not server.allowed_photo_url(response.geturl()):
                return "unsafe_redirect"
            if int(response.headers.get("Content-Length") or 0) > 8_000_000:
                return "oversized_content_length"
            raw = response.read(8_000_001)
    except urllib.error.HTTPError as exc:
        return "http_" + str(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return "network_" + type(exc).__name__
    if not 2048 <= len(raw) <= 8_000_000:
        return "invalid_image_size"
    if hashlib.sha256(raw).hexdigest() in server.photo_rejected_digests:
        return "rejected_digest"
    if not server.image_type(raw[:16]):
        return "invalid_image_format"
    return "download_valid"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--source", default="/data/targeted_photo_recheck.json")
    p.add_argument("--report", default="/data/photo_download_diagnostics.json")
    args = p.parse_args()
    if not 1 <= args.limit <= 500:
        p.error("--limit must be 1 through 500")
    data = json.loads(Path(args.source).read_text())
    if not isinstance(data.get("results"), list):
        p.error("Invalid targeted audit")
    catalog = {int(r["id"]): r for r in server.browse_catalog()
               if 1_000_000 <= int(r["id"]) < 2_000_000}
    counts = collections.Counter()
    examples = collections.defaultdict(list)
    attempted = 0
    for row in data["results"]:
        if row.get("status") not in ("official_photo", "official_embedded_photo"):
            continue
        try:
            rid = int(row["id"])
        except (KeyError, ValueError, TypeError):
            continue
        recipe = catalog.get(rid)
        if not recipe or str(recipe.get("name", "")) != str(row.get("name", "")):
            continue
        if server.cached_photo(rid):
            continue
        url = server.clean_official_photo_candidate(row.get("official_image", ""))
        reason = inspect(url)
        counts[reason] += 1
        if len(examples[reason]) < 5:
            examples[reason].append({"id": rid, "name": recipe.get("name", ""), "url": url})
        attempted += 1
        if attempted % 25 == 0:
            print("Diagnosed:", attempted, dict(counts), flush=True)
        if attempted >= args.limit:
            break
    result = {"mode": "READ_ONLY", "attempted": attempted,
              "counts": dict(counts), "examples": dict(examples)}
    Path(args.report).write_text(json.dumps(result, indent=2))
    print(json.dumps({"attempted": attempted, "counts": dict(counts),
                      "report": args.report}, indent=2), flush=True)


if __name__ == "__main__":
    main()
