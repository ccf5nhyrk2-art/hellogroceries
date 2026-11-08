"""Recover missing photos from current, recipe-bound HelloFresh images.

The saved targeted audit selects eligible recipes, but its stale image URLs
must never be treated as proof of a correct asset. Dry-run is the default.
No existing cache or quarantined file is overwritten.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request
import server

EXTS=(".jpg",".png",".webp",".gif")


def active_hashes():
    hashes=collections.defaultdict(set)
    for path in server.PHOTO_DIR.iterdir():
        if path.suffix.lower() not in EXTS or not path.stem.isdigit():
            continue
        if server.valid_photo_cache_entry(path):
            try:
                hashes[hashlib.sha256(path.read_bytes()).hexdigest()].add(int(path.stem))
            except OSError:
                pass
    return hashes


def source_images(recipe):
    # The source page is checked afresh. Never use a saved audit image as a
    # fallback, nor an unverified imported image URL.
    urls=server.official_images(recipe)
    urls=[server.clean_official_photo_candidate(u) for u in urls]
    urls=list(dict.fromkeys(u for u in urls if u and server.complete_recipe_photo_url(u)))
    if len(urls)!=1:
        return []
    official=urls[0]
    # Different delivery transforms for the SAME official image asset.
    candidates=[server.force_verified_photo_jpeg_url(official),official]
    candidates+=server.official_photo_url_variants(official)
    return list(dict.fromkeys(u for u in candidates if server.complete_recipe_photo_url(u)))[:6]


def fetch(url):
    req=urllib.request.Request(url,headers={
        "User-Agent":"Mozilla/5.0 (compatible; HelloGroceries/1.0)",
        "Accept":"image/jpeg,image/png;q=0.9,image/webp;q=0.5",
    })
    with urllib.request.build_opener(server.SafePhotoRedirect).open(req,timeout=12) as resp:
        if not server.allowed_photo_url(resp.geturl()):
            raise ValueError("unsafe_image_redirect")
        if int(resp.headers.get("Content-Length") or 0)>8_000_000:
            raise ValueError("oversized_image")
        raw=resp.read(8_000_001)
    if not 2048<=len(raw)<=8_000_000:
        raise ValueError("invalid_image_size")
    kind=server.image_type(raw)
    if not kind:
        raise ValueError("invalid_image_type")
    return raw,kind


def recover(recipe, known, apply):
    rid=int(recipe["id"])
    if server.cached_photo(rid):
        return "already_active"
    urls=source_images(recipe)
    if not urls:
        return "no_unique_current_official_image"
    rejected=0
    duplicate=0
    errors=0
    for url in urls:
        try:
            raw,(mime,ext)=fetch(url)
        except (urllib.error.URLError,OSError,TimeoutError,ValueError):
            errors+=1
            continue
        digest=hashlib.sha256(raw).hexdigest()
        if digest in server.photo_rejected_digests:
            rejected+=1
            continue
        if digest in known and known[digest]!={rid}:
            duplicate+=1
            continue
        if not apply:
            return "verified_candidate_dry_run"
        # Recheck before any write; never overwrite an active file.
        if server.cached_photo(rid) or any((server.PHOTO_DIR/(str(rid)+e)).exists() for e in EXTS):
            return "already_active_or_file_exists"
        target=server.PHOTO_DIR/(str(rid)+ext)
        # Exclusive creation prevents races and accidental replacement.
        try:
            with target.open("xb") as out:
                out.write(raw)
        except FileExistsError:
            return "already_active_or_file_exists"
        known[digest].add(rid)
        return "restored"
    if rejected:
        return "all_available_variants_rejected"
    if duplicate:
        return "duplicate_official_image"
    return "download_failed" if errors else "no_candidate"


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--apply",action="store_true")
    p.add_argument("--limit",type=int,default=25,help="Number of missing eligible recipes to inspect; 0 means all")
    p.add_argument("--source",default="/data/targeted_photo_recheck.json")
    p.add_argument("--report",default="/data/independent_official_photo_recovery.json")
    args=p.parse_args()
    if args.limit<0:p.error("--limit cannot be negative")
    audit=json.loads(Path(args.source).read_text())
    if not isinstance(audit.get("results"),list):p.error("Invalid audit report")
    catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
    known=active_hashes()
    counts=collections.Counter()
    samples=collections.defaultdict(list)
    checked=0
    for row in audit["results"]:
        if row.get("status") not in ("official_photo","official_embedded_photo"):
            counts["unverified_skipped"]+=1
            continue
        try:rid=int(row["id"])
        except (ValueError,KeyError,TypeError):continue
        recipe=catalog.get(rid)
        if not recipe or recipe.get("name")!=row.get("name"):
            counts["catalog_changed"]+=1
            continue
        if server.cached_photo(rid):
            counts["already_active"]+=1
            continue
        if args.limit and checked>=args.limit:
            counts["limit_skipped"]+=1
            continue
        checked+=1
        result=recover(recipe,known,args.apply)
        counts[result]+=1
        if len(samples[result])<8:samples[result].append({"id":rid,"name":recipe.get("name","")})
        if checked%25==0:
            print("Checked:",checked,"results:",dict(counts),flush=True)
    report={"mode":"APPLY" if args.apply else "DRY_RUN","eligible_checked":checked,
            "counts":dict(counts),"samples":dict(samples)}
    Path(args.report).write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)


if __name__=="__main__":
    main()
