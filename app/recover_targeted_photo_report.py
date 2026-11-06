"""Recover missing photos from the saved targeted HelloFresh audit.

Never overwrite an active photo, clear quarantine, or trust unverified records.
Dry-run by default. Resume with --apply; skip existing images.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import urllib.error
import server

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--limit",type=int,default=0)
    parser.add_argument("--source",default="/data/targeted_photo_recheck.json")
    parser.add_argument("--report",default="/data/targeted_photo_recovery.json")
    args=parser.parse_args()
    data=json.loads(Path(args.source).read_text())
    if data.get("mode") not in ("DRY_RUN","APPLY") or not isinstance(data.get("results"),list):
        raise SystemExit("Invalid targeted audit report")
    catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
    known=collections.defaultdict(set)
    for path in server.PHOTO_DIR.iterdir():
        if path.suffix.lower() not in (".jpg",".png",".webp",".gif") or not path.stem.isdigit():continue
        if server.valid_photo_cache_entry(path):
            try:known[hashlib.sha256(path.read_bytes()).hexdigest()].add(int(path.stem))
            except OSError:pass
    counts=collections.Counter()
    checked=0
    for row in data["results"]:
        try:rid=int(row["id"])
        except (KeyError,ValueError,TypeError):continue
        recipe=catalog.get(rid)
        if not recipe or str(recipe.get("name",""))!=str(row.get("name","")):
            counts["catalog_changed"]+=1;continue
        if server.cached_photo(rid):
            counts["already_active"]+=1;continue
        if row.get("status") not in ("official_photo","official_embedded_photo"):
            counts["unverified_skipped"]+=1;continue
        url=server.clean_official_photo_candidate(row.get("official_image",""))
        if not url or not server.complete_recipe_photo_url(url):
            counts["invalid_url"]+=1;continue
        if args.limit and checked>=args.limit:
            counts["limit_skipped"]+=1;continue
        checked+=1
        if not args.apply:
            counts["eligible_dry_run"]+=1;continue
        diagnostics=[]
        try:
            downloaded=server.download_photo(url,rid,prefer_jpeg=True,diagnostics=diagnostics)
            if not downloaded:
                counts["download_rejected"]+=1;continue
            path=downloaded[0]
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in server.photo_rejected_digests or (digest in known and known[digest]!={rid}):
                path.rename(path.with_name(path.name+".suspect"))
                counts["duplicate_or_rejected_quarantined"]+=1
                continue
            known[digest].add(rid)
            counts["restored"]+=1
        except (OSError,ValueError,TimeoutError,urllib.error.URLError):
            counts["download_error"]+=1
        if checked%100==0:
            print("Attempted:",checked,"restored:",counts["restored"],flush=True)
    summary={"mode":"APPLY" if args.apply else "DRY_RUN","eligible_checked":checked,"counts":dict(counts)}
    Path(args.report).write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary,indent=2),flush=True)

if __name__=="__main__":main()
