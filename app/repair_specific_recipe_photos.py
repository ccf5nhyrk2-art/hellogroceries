"""Repair visibly duplicated photos for a small explicit recipe set.

Dry run by default. Never guess an image, delete a cache, or touch other recipes.
"""
import argparse,collections,hashlib,json
from pathlib import Path
import server

TARGETS=("1 Pan Chicken Sausage Orzotto","Greek Chicken and Pepper Lettuce Wraps")
def main():
 p=argparse.ArgumentParser()
 p.add_argument("--apply",action="store_true")
 p.add_argument("--report",default="/data/targeted_photo_corrections.json")
 args=p.parse_args()
 catalog=server.browse_catalog()
 matches=[r for r in catalog if str(r.get("name","")).strip().casefold() in {t.casefold() for t in TARGETS}]
 out=[]
 for recipe in matches:
  rid=int(recipe["id"])
  cached=server.cached_photo(rid)
  old_hash=hashlib.sha256(cached[0].read_bytes()).hexdigest() if cached else None
  official=server.official_images(recipe)
  entry={"id":rid,"name":recipe["name"],"official_count":len(official),"old_sha256":old_hash}
  if len(official)!=1:
   entry["status"]="skipped_no_unique_official_photo"
  elif not server.complete_recipe_photo_url(official[0]):
   entry["status"]="skipped_invalid_official_url"
  elif not args.apply:
   entry["status"]="candidate_dry_run"
  else:
   # Keep the original file intact if retrieval fails. The standard downloader
   # enforces official URL allowlist, image size and rejected digest checks.
   try:
    result=server.download_photo(official[0],rid,prefer_jpeg=True)
    if result:
     new_hash=hashlib.sha256(result[0].read_bytes()).hexdigest()
     entry["status"]="updated" if new_hash!=old_hash else "unchanged_same_image"
     entry["new_sha256"]=new_hash
    else:entry["status"]="download_rejected"
   except Exception as exc:
    entry["status"]="download_failed"
    entry["error_type"]=type(exc).__name__
  out.append(entry)
 Path(args.report).write_text(json.dumps({"mode":"APPLY" if args.apply else "DRY_RUN","results":out},indent=2))
 print(json.dumps({"mode":"APPLY" if args.apply else "DRY_RUN","results":out},indent=2),flush=True)
if __name__=="__main__":main()
