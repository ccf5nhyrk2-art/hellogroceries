"""Read-only audit of alternative HelloFresh recipe-slug photo provenance."""
import collections
import json
from pathlib import Path
import server

def main():
 recipes={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 owners=collections.defaultdict(set)
 for rid,recipe in recipes.items():
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if url:owners[url].add(rid)
 counts=collections.Counter()
 samples=[]
 for path in server.PHOTO_DIR.iterdir():
  if not path.name.endswith((".suspect",".unverified")):continue
  original=path.with_suffix("")
  if not original.stem.isdigit():continue
  rid=int(original.stem)
  if rid not in recipes:continue
  counts["examined"]+=1
  recipe=recipes[rid]
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if not url or owners[url]!={rid}:
   counts["missing_or_shared_url"]+=1
   continue
  if server.trusted_imported_photo_url(recipe)!=url:
   counts["slug_or_source_identity_not_verified"]+=1
   continue
  if not server.valid_photo_cache_entry(path):
   counts["invalid_file"]+=1
   continue
  if original.exists():
   counts["already_active"]+=1
   continue
  counts["provenance_candidates"]+=1
  if len(samples)<25:samples.append({"id":rid,"name":recipe.get("name",""),"image_url":url})
 print(json.dumps({"read_only":True,"counts":dict(counts),"samples":samples},indent=2))
 print("No images were restored. Candidate count is not a validated restoration count.")

if __name__=="__main__":main()
