"""Safely reconcile recipe photos with their own HelloFresh Recipe structured data.

Dry-run by default. Never delete originals, clear rejected digests or use generic
page images. Exact source page identity and a single official hero are required.
"""
import argparse
import collections
import hashlib
import io
import json
import os
import re
import urllib.parse
from pathlib import Path
import urllib.request

from PIL import Image, ImageOps, ImageStat
import server

EXTS = (".jpg", ".png", ".webp", ".gif")


def fingerprint(raw):
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if im.width < 120 or im.height < 100:
            raise ValueError("too_small")
        grey = im.resize((17, 16), Image.Resampling.LANCZOS).convert("L")
        pixels = list(grey.getdata())
        bits = 0
        for y in range(16):
            for x in range(16):
                bits = (bits << 1) | (pixels[y * 17 + x] > pixels[y * 17 + x + 1])
        color = im.resize((16, 16), Image.Resampling.LANCZOS)
        return bits, list(color.getdata())


def visual_match(a, b):
    """Conservative AND gate: same structure AND near-identical color layout."""
    ah, ac = fingerprint(a)
    bh, bc = fingerprint(b)
    distance = (ah ^ bh).bit_count()
    color_error = sum(abs(x - y) for p, q in zip(ac, bc) for x, y in zip(p, q))
    color_error /= 16 * 16 * 3 * 255
    return distance <= 24 and color_error <= 0.075, distance, round(color_error, 4)


def fetch(url):
    if not server.complete_recipe_photo_url(url):
        raise ValueError("unsafe_photo_url")
    req = urllib.request.Request(
        server.force_verified_photo_jpeg_url(url),
        headers={"User-Agent": "Mozilla/5.0 (compatible; HelloGroceries/1.0)",
                 "Accept": "image/jpeg,image/png;q=0.9,image/webp;q=0.5"},
    )
    with urllib.request.build_opener(server.SafePhotoRedirect).open(req, timeout=12) as resp:
        if not server.allowed_photo_url(resp.geturl()):
            raise ValueError("unsafe_redirect")
        if int(resp.headers.get("Content-Length") or 0) > 8_000_000:
            raise ValueError("oversized_image")
        raw = resp.read(8_000_001)
    if not 2048 <= len(raw) <= 8_000_000 or not server.image_type(raw[:16]):
        raise ValueError("invalid_image")
    if hashlib.sha256(raw).hexdigest() in server.photo_rejected_digests:
        raise ValueError("rejected_digest")
    fingerprint(raw)
    return raw


def active_path(rid):
    for ext in EXTS:
        path = server.PHOTO_DIR / (str(rid) + ext)
        if path.is_file() and server.valid_photo_cache_entry(path):
            return path
    return None


def originals(rid):
    for ext in EXTS:
        for suffix in (".unverified", ".suspect"):
            path = server.PHOTO_DIR / (str(rid) + ext + suffix)
            if path.is_file() and server.valid_photo_cache_entry(path):
                yield path


def official_url(recipe):
    urls = server.official_images(recipe)
    urls = list(dict.fromkeys(server.clean_official_photo_candidate(u) for u in urls))
    urls = [u for u in urls if server.complete_recipe_photo_url(u)]
    # Different transformations of the same asset are one identity.
    # Different assets remain ambiguous and are never auto-restored.
    identities = {}
    for url in urls:
        path = urllib.parse.urlsplit(url).path
        match = re.search(r"/(?:recipes|hellofresh_s3)/image/([^/?]+)", path, re.I)
        identity = match.group(1).lower() if match else path.lower()
        identities.setdefault(identity, url)
    return next(iter(identities.values())) if len(identities) == 1 else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--start-after", type=int, default=0)
    p.add_argument("--report", default="/data/official_photo_identity_audit.json")
    args = p.parse_args()
    if not 1 <= args.limit <= 12954:
        p.error("--limit must be 1..12954")
    recipes = sorted((r for r in server.browse_catalog()
                      if 1000000 <= int(r["id"]) < 2000000
                      and int(r["id"]) > args.start_after),
                     key=lambda r: int(r["id"]))
    counts = collections.Counter()
    examples = collections.defaultdict(list)
    checked = 0
    for recipe in recipes:
        rid = int(recipe["id"])
        if not active_path(rid) and not any(originals(rid)):
            continue
        if checked >= args.limit:
            break
        checked += 1
        reason = ""
        try:
            url = official_url(recipe)
            if not url:
                reason = "no_unique_official_image"
            else:
                remote = fetch(url)
                active = active_path(rid)
                if active:
                    try:
                        matched, dist, color = visual_match(active.read_bytes(), remote)
                        if matched:
                            reason = "active_verified"
                        else:
                            reason = "active_mismatch_review"
                    except (OSError, ValueError):
                        reason = "active_invalid_review"
                else:
                    reason = "missing"
                if reason == "missing":
                    for path in originals(rid):
                        try:
                            raw = path.read_bytes()
                            digest = hashlib.sha256(raw).hexdigest()
                            if digest in server.photo_rejected_digests:
                                continue
                            matched, dist, color = visual_match(raw, remote)
                            if matched:
                                reason = "quarantined_visual_match"
                                if args.apply and not active_path(rid):
                                    destination = path.with_suffix("")
                                    if not destination.exists():
                                        os.rename(path, destination)
                                        reason = "restored_verified_original"
                                break
                        except (OSError, ValueError):
                            continue
                    if reason == "missing":
                        reason = "missing_no_matching_original"
        except Exception as exc:
            detail = str(exc).strip()[:120]
            reason = "verification_error_" + type(exc).__name__
            if detail:
                reason += ":" + detail.replace("\n", " ")
        counts[reason] += 1
        if len(examples[reason]) < 8:
            examples[reason].append({"id": rid, "name": recipe.get("name", "")})
        if checked % 25 == 0:
            print("Checked:", checked, dict(counts), flush=True)
    result = {"mode": "APPLY" if args.apply else "DRY_RUN",
              "checked": checked, "counts": dict(counts), "examples": dict(examples)}
    Path(args.report).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
