"""Conservative staging recovery of original quarantined recipe photos.

Dry run: python -u /app/restore_verified_original_photos.py
Apply only strictly verified originals: python -u /app/restore_verified_original_photos.py --apply
Does not alter rejected digests or unrelated recipes.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path
import server

EXTS={".jpg",".png",".webp",".gif"}

def source_id(recipe):
 path=urllib.parse.urlsplit(recipe.get("url") or "").path.rstrip("/")
 match=re.search(r"([0-9a-f]{24})$",path,re.I)
 return match.group(1).lower() if match else ""

def source_url_verified(recipe, owners):
 """Only an image URL containing exact source ID in its asset filename qualifies."""
 rid=int(recipe["id"])
 url=server.clean_official_photo_candidate(recipe.get("image",""))
 if not url or not server.complete_recipe_photo_url(url) or owners.get(url)!={rid}:return False
 filename=urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit("/",1)[-1].lower()
 identity=source_id(recipe)
 return bool(identity and identity in filename and re.search(r"\.(?:jpe?g|png|webp)$",filename))

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument("--apply",action="store_true",help="Restore only strictly verified originals")
 parser.add_argument("--limit",type=int,default=0,help="Maximum restored files, 0 for all")
 args=parser.parse_args()
 catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 owners=collections.defaultdict(set)
 for rid,recipe in catalog.items():
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if url:owners[url].add(rid)
 originals=[]
 digest_owners=collections.defaultdict(set)
 for path in server.PHOTO_DIR.iterdir():
  if not path.name.endswith((".suspect",".unverified")):continue
  base=path.with_suffix("")
  if base.suffix.lower() not in EXTS or not base.stem.isdigit():continue
  rid=int(base.stem)
  if rid not in catalog:continue
  try:digest=hashlib.sha256(path.read_bytes()).hexdigest()
  except OSError:continue
  originals.append((rid,path,base,digest))
  digest_owners[digest].add(rid)
 counts=collections.Counter()
 samples=[]
 restored=0
 for rid,path,base,digest in originals:
  counts["examined"]+=1
  reason=None
  if len(digest_owners[digest])!=1:reason="shared_image_bytes"
  elif digest in server.photo_rejected_digests:reason="rejected_digest"
  elif not server.valid_photo_cache_entry(path):reason="invalid_image"
  elif not source_url_verified(catalog[rid],owners):reason="unverified_source_identity"
  elif base.exists():reason="active_photo_already_exists"
  if reason:
   counts[reason]+=1
   continue
  counts["strictly_eligible"]+=1
  if len(samples)<10:samples.append({"id":rid,"name":catalog[rid].get("name",""),"file":path.name})
  if args.apply and (args.limit<=0 or restored<args.limit):
   # Atomic rename preserves the original bytes; no quarantine hash changes.
   os.replace(path,base)
   restored+=1
 counts["restored"]=restored
 print(json.dumps({"mode":"APPLY" if args.apply else "DRY_RUN","counts":dict(counts),"eligible_samples":samples},indent=2),flush=True)
 if not args.apply:print("DRY RUN: no photos changed. Review eligibility before using --apply.",flush=True)

if __name__=="__main__":main()
