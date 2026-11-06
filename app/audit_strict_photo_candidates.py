"""Read-only strict second-pass audit of quarantined HelloFresh photos.

Never restores photos. Filename/title consistency is a rejection heuristic,
not independent proof of photo provenance.
"""
import collections
import hashlib
import json
import re
import urllib.parse
import server

EXTS={".jpg",".png",".webp",".gif"}
STOP={"with","and","the","plus","over","from","recipe","fresh","hello","style","crispy","creamy","roasted","served","topped","sauce","side","sides","rice","potato","potatoes","vegetables","veggies","garlic","butter","herb","herbs","onion","pepper","peppers","lemon","lime","sweet","spicy","classic","easy","quick","family","meal","dinner","lunch","pan","bowl","salad","tomato","tomatoes"}
PROTEINS={"chicken","pork","beef","steak","turkey","salmon","shrimp","tilapia","cod","tofu","lamb","fish","sausage","bacon","lobster","tuna","duck"}
def tokens(value):
 return {w for w in re.findall(r"[a-z]{4,}",value.lower()) if w not in STOP}
def classify(recipe,url):
 path=urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit("/",1)[-1]
 name=tokens(recipe.get("name",""))
 filename=tokens(re.sub(r"([a-z])([A-Z])",r"\1 \2",path))
 recipe_proteins=name&PROTEINS
 photo_proteins=filename&PROTEINS
 if recipe_proteins and photo_proteins and not recipe_proteins&photo_proteins:return "conflicting_protein"
 if len(filename)>=2 and len(name&filename)<2:return "weak_filename_title_overlap"
 return "needs_independent_verification"

def main():
 catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 owners=collections.defaultdict(set)
 for rid,recipe in catalog.items():
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if url:owners[url].add(rid)
 originals=[]
 digests=collections.defaultdict(set)
 for path in server.PHOTO_DIR.iterdir():
  if not path.name.endswith((".suspect",".unverified")):continue
  base=path.with_suffix("")
  if base.suffix.lower() not in EXTS or not base.stem.isdigit():continue
  rid=int(base.stem)
  if rid not in catalog:continue
  try:digest=hashlib.sha256(path.read_bytes()).hexdigest()
  except OSError:continue
  originals.append((rid,path,base,digest))
  digests[digest].add(rid)
 counts=collections.Counter()
 samples=collections.defaultdict(list)
 for rid,path,base,digest in originals:
  counts["examined"]+=1
  recipe=catalog[rid]
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if len(digests[digest])!=1:reason="shared_image_bytes"
  elif digest in server.photo_rejected_digests:reason="rejected_digest"
  elif not server.valid_photo_cache_entry(path):reason="invalid_image"
  elif base.exists():reason="already_active"
  elif not url or owners[url]!={rid} or server.trusted_imported_photo_url(recipe)!=url:reason="unverified_or_shared_source_url"
  else:reason=classify(recipe,url)
  counts[reason]+=1
  if len(samples[reason])<12:samples[reason].append({"id":rid,"name":recipe.get("name",""),"file":path.name,"url":url})
 print(json.dumps({"read_only":True,"counts":dict(counts),"samples":dict(samples)},indent=2))
 print("No images restored. Even 'needs_independent_verification' is NOT safe to auto-restore.")

if __name__=="__main__":main()
