"""Generate a local, read-only HTML contact sheet for manual photo/title review.

Run: python -u /app/review_recipe_photos.py
Copy out: docker cp hellogroceries-staging:/data/photo_review.html ./photo_review.html
Never restores, removes, downloads, or trusts any photo.
"""
import base64
import collections
import hashlib
import html
import json
import random
import urllib.parse
from pathlib import Path
import server
from restore_verified_original_photos import EXTS, source_url_verified

OUT=Path("/data/photo_review.html")
MIME={".jpg":"image/jpeg",".png":"image/png",".webp":"image/webp",".gif":"image/gif"}

def main():
 catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 owners=collections.defaultdict(set)
 for rid,recipe in catalog.items():
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if url:owners[url].add(rid)
 groups=collections.defaultdict(set)
 candidates=[]
 for path in server.PHOTO_DIR.iterdir():
  original=path
  quarantine=path.name.endswith((".suspect",".unverified"))
  if quarantine:original=path.with_suffix("")
  if original.suffix.lower() not in EXTS or not original.stem.isdigit():continue
  rid=int(original.stem)
  if rid not in catalog or not server.valid_photo_cache_entry(path):continue
  try:
   raw=path.read_bytes()
   digest=hashlib.sha256(raw).hexdigest()
  except OSError:continue
  groups[digest].add(rid)
  candidates.append((rid,path,digest,quarantine))
 eligible=collections.defaultdict(list)
 for rid,path,digest,quarantine in candidates:
  if digest in server.photo_rejected_digests or len(groups[digest])!=1:continue
  if quarantine:category="Quarantined — not yet verified"
  elif source_url_verified(catalog[rid],owners):category="Active — source-ID matched"
  else:category="Active — provenance needs review"
  eligible[category].append((rid,path))
 rng=random.Random(20261105)
 sections=[]
 counts={}
 for category,items in eligible.items():
  counts[category]=len(items)
  rng.shuffle(items)
  cards=[]
  for rid,path in items:
   try:
    raw=path.read_bytes()
    if len(raw)>1200000:continue
    uri="data:"+MIME[path.with_suffix("").suffix.lower() if path.name.endswith((".suspect",".unverified")) else path.suffix.lower()]+";base64,"+base64.b64encode(raw).decode("ascii")
   except (OSError,KeyError):continue
   recipe=catalog[rid]
   name=html.escape(str(recipe.get("name","")))
   url=recipe.get("url","")
   link=('<a href="'+html.escape(url,quote=True)+'" target="_blank" rel="noopener noreferrer">Official recipe page</a>') if isinstance(url,str) and url.startswith("https://") and urllib.parse.urlsplit(url).hostname and urllib.parse.urlsplit(url).hostname.lower().endswith("hellofresh.com") else ""
   cards.append('<article><img loading="lazy" src="'+uri+'"><h3>'+name+'</h3><p>Recipe #'+str(rid)+'</p>'+link+'</article>')
   if len(cards)>=20:break
  sections.append('<h2>'+html.escape(category)+' ('+str(len(items))+' candidates; '+str(len(cards))+' sampled)</h2><div class="grid">'+''.join(cards)+'</div>')
 page='''<!doctype html><html lang="en"><meta charset="utf-8"><title>HelloGroceries Photo Review</title><style>body{font:16px system-ui;background:#111;color:#eee;margin:2rem}h1,h2{color:#a5e6ae}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}article{background:#222;border:1px solid #555;border-radius:12px;padding:12px}img{width:100%;height:180px;object-fit:contain;background:#000}h3{font-size:16px}p{color:#bbb}a{color:#9ed4ff}</style><h1>Photo identity review — staging</h1><p>Read-only deterministic sample. Check whether the image actually depicts the recipe title. No images were restored by this report. Do not restore quarantined images based only on visual similarity.</p>'''+''.join(sections)+'</html>'
 OUT.write_text(page,encoding="utf-8")
 print(json.dumps({"report":str(OUT),"candidate_counts":counts,"sample_limit_per_group":20,"read_only":True},indent=2),flush=True)

if __name__=="__main__":main()
