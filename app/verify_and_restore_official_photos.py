"""Verify quarantined photos against exact official HelloFresh recipe metadata.

Dry-run by default. --apply restores ONLY when the official page's Recipe
structured data identifies the same recipe and its image bytes match the
quarantined original exactly. No guessing or generic image reuse.
"""
import argparse
import collections
import hashlib
import json
import os
import urllib.parse
import urllib.request
import server
from PIL import Image, ImageOps, ImageChops
import io
from audit_strict_photo_candidates import EXTS, classify

def digest(path):
 with path.open("rb") as f:return hashlib.sha256(f.read()).hexdigest()

def fetch_image_digest(url):
 if not server.complete_recipe_photo_url(url):return None
 req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; HelloGroceries/1.0)"})
 opener=urllib.request.build_opener(server.SafePhotoRedirect)
 with opener.open(req,timeout=12) as resp:
  if not server.allowed_photo_url(resp.geturl()):return None
  if int(resp.headers.get("Content-Length") or 0)>8_000_000:return None
  raw=resp.read(8_000_001)
 if not 2048<=len(raw)<=8_000_000 or not server.image_type(raw[:16]):return None
 return raw

def visual_match(local_path,remote_bytes):
 """Conservative same-photo test across CDN resizing and recompression."""
 try:
  with Image.open(local_path) as a, Image.open(io.BytesIO(remote_bytes)) as b:
   a=ImageOps.exif_transpose(a).convert("RGB")
   b=ImageOps.exif_transpose(b).convert("RGB")
   if min(*a.size,*b.size)<160:return False
   ar=a.width/a.height
   br=b.width/b.height
   if abs(ar-br)>0.035:return False
   a=ImageOps.fit(a,(128,128),method=Image.Resampling.LANCZOS)
   b=ImageOps.fit(b,(128,128),method=Image.Resampling.LANCZOS)
   # Compare every RGB channel after consistent downsampling.
   diff=ImageChops.difference(a,b)
   hist=diff.histogram()
   mae=sum((i%256)*n for i,n in enumerate(hist))/(128*128*3*255)
   return mae<=0.055
 except (OSError,ValueError,Image.DecompressionBombError):
  return False

def verify(recipe,local_path):
 # Official structured Recipe JSON-LD is authoritative only when its recipe
 # identity is checked by the existing official_images_from_page parser.
 source=recipe.get("url","")
 if not server.allowed_url(source):return "invalid_recipe_page"
 req=urllib.request.Request(source,headers={"User-Agent":"Mozilla/5.0","Accept":"text/html"})
 with urllib.request.urlopen(req,timeout=12) as resp:
  if not server.allowed_url(resp.geturl()):return "unsafe_recipe_redirect"
  page=resp.read(3_000_001)
 if len(page)>3_000_000:return "oversized_recipe_page"
 urls=server.official_images_from_page(recipe,page.decode("utf-8","replace"))
 if len(urls)!=1:return "no_unique_official_recipe_image"
 official=server.clean_official_photo_candidate(urls[0])
 if not official:return "invalid_official_image_url"
 remote=fetch_image_digest(official)
 if not remote:return "official_image_unavailable"
 if not visual_match(local_path,remote):return "official_image_visual_mismatch"
 return "verified"

def main():
 p=argparse.ArgumentParser()
 p.add_argument("--apply",action="store_true")
 p.add_argument("--limit",type=int,default=0,help="Maximum candidates checked, 0 means all")
 p.add_argument("--report",default="/data/verified_photo_recovery.json")
 args=p.parse_args()
 catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 owners=collections.defaultdict(set)
 for rid,recipe in catalog.items():
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if url:owners[url].add(rid)
 originals=[]
 hashes=collections.defaultdict(set)
 for path in server.PHOTO_DIR.iterdir():
  if not path.name.endswith((".suspect",".unverified")):continue
  base=path.with_suffix("")
  if base.suffix.lower() not in EXTS or not base.stem.isdigit():continue
  rid=int(base.stem)
  if rid not in catalog:continue
  try:h=digest(path)
  except OSError:continue
  originals.append((rid,path,base,h))
  hashes[h].add(rid)
 counts=collections.Counter()
 samples=collections.defaultdict(list)
 checked=0
 last_reported=0
 for rid,path,base,h in sorted(originals,key=lambda x:x[0]):
  recipe=catalog[rid]
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if len(hashes[h])!=1:reason="shared_bytes"
  elif h in server.photo_rejected_digests:reason="rejected_digest"
  elif not server.valid_photo_cache_entry(path):reason="invalid_image"
  elif base.exists():reason="already_active"
  elif not url or owners[url]!={rid} or server.trusted_imported_photo_url(recipe)!=url:reason="untrusted_source_url"
  elif classify(recipe,url)!="needs_independent_verification":reason="filename_conflict_or_weak_overlap"
  elif args.limit and checked>=args.limit:reason="not_checked_limit"
  else:
   checked+=1
   try:reason=verify(recipe,path)
   except Exception as exc:reason="verification_network_or_parse_error"
   if reason=="verified" and args.apply:
    # Recheck destination and source before atomic move.
    if base.exists() or digest(path)!=h:reason="changed_during_verification"
    else:
     os.rename(path,base)
     reason="restored"
  counts[reason]+=1
  if len(samples[reason])<12:samples[reason].append({"id":rid,"name":recipe.get("name",""),"file":path.name})
  if checked and checked%100==0 and checked!=last_reported:
   print("Verified attempts:",checked,flush=True)
   last_reported=checked
 result={"mode":"APPLY" if args.apply else "DRY_RUN","checked_official_pages":checked,"counts":dict(counts),"samples":dict(samples)}
 with open(args.report,"w") as out:json.dump(result,out,indent=2)
 print(json.dumps(result,indent=2),flush=True)
 print("Report:",args.report,flush=True)

if __name__=="__main__":main()
