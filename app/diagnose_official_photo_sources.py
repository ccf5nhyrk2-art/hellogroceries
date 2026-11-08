"""Read-only diagnostics for recovering exact HelloFresh recipe images.

No downloads are saved and no quarantined file is changed.
"""
import argparse
import collections
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import server


def inspect(recipe):
    source = str(recipe.get("url") or "")
    stored = str(recipe.get("image") or "")
    result = {"id": int(recipe["id"]), "name": recipe.get("name", "")}
    result["stored_trusted"] = bool(server.trusted_imported_photo_url(recipe))
    result["stored_valid_format"] = bool(server.complete_recipe_photo_url(stored))
    if not server.allowed_url(source):
        result["status"] = "invalid_recipe_url"
        return result
    try:
        request = urllib.request.Request(source, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=12) as response:
            if not server.allowed_url(response.geturl()):
                result["status"] = "unsafe_recipe_redirect"
                return result
            body = response.read(3_000_001)
        if len(body) > 3_000_000:
            result["status"] = "oversized_recipe_page"
            return result
        page = body.decode("utf-8", "replace")
        candidates = server.official_images_from_page(recipe, page)
        result["official_candidate_count"] = len(candidates)
        result["official_candidates"] = candidates[:3]
        result["recipe_jsonld_blocks"] = len(re.findall(
            r'<script\\b[^>]*application/ld\\+json', page, re.I))
        if not candidates:
            result["status"] = "no_source_bound_image"
            return result
        # Report whether the official URL resolves; never cache or restore it.
        result["status"] = "source_bound_image_found"
        for candidate in candidates[:1]:
            if not server.complete_recipe_photo_url(candidate):
                result["status"] = "invalid_official_url"
                break
            try:
                url = server.force_verified_photo_jpeg_url(candidate)
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0", "Accept": "image/jpeg,image/png"})
                with urllib.request.build_opener(server.SafePhotoRedirect).open(
                        req, timeout=12) as resp:
                    if not server.allowed_photo_url(resp.geturl()):
                        result["status"] = "unsafe_image_redirect"
                        break
                    raw = resp.read(8_000_001)
                if len(raw) > 8_000_000 or len(raw) < 2048 or not server.image_type(raw[:16]):
                    result["status"] = "invalid_official_image"
                else:
                    result["status"] = "official_image_downloadable"
            except urllib.error.HTTPError as exc:
                result["status"] = "official_image_http_" + str(exc.code)
            except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
                result["status"] = "official_image_error_" + type(exc).__name__
    except urllib.error.HTTPError as exc:
        result["status"] = "recipe_page_http_" + str(exc.code)
    except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
        result["status"] = "recipe_page_error_" + type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--start-after", type=int, default=0)
    parser.add_argument("--report", default="/data/official_photo_source_diagnostics.json")
    args = parser.parse_args()
    if not 1 <= args.limit <= 12954:
        parser.error("--limit must be 1..12954")
    catalog = sorted(
        (r for r in server.browse_catalog()
         if 1000000 <= int(r["id"]) < 2000000 and int(r["id"]) > args.start_after),
        key=lambda r: int(r["id"]))
    results = [inspect(r) for r in catalog[:args.limit]]
    summary = {"checked": len(results),
               "counts": dict(collections.Counter(r["status"] for r in results)),
               "results": results}
    with open(args.report, "w") as out:
        json.dump(summary, out, indent=2)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
