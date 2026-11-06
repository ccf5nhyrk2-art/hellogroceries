"""Read-only audit of original imported photo cache and current source URLs.

Run inside staging: python -u /app/audit_original_photos.py
Never changes images, quarantine files, checkpoint or database.
"""
import collections
import hashlib
import json
from pathlib import Path
import server

def main():
 catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 groups=collections.defaultdict(list)
 state=collections.Counter()
 examples=collections.defaultdict(list)
 for path in server.PHOTO_DIR.iterdir():
  suffix=path.suffix.lower()
  if suffix not in (".suspect",".unverified",".jpg",".png",".webp",".gif"):continue
  stem=path.name.split(".")[0]
  if not stem.isdigit() or int(stem) not in catalog:continue
  rid=int(stem)
  try:
   raw=path.read_bytes()
   digest=hashlib.sha256(raw).hexdigest()
  except OSError:continue
  groups[digest].append((rid,path.name))
  if suffix in (".suspect",".unverified"):
   state["quarantined_files"]+=1
   if digest in server.photo_rejected_digests:state["quarantined_rejected_digest"]+=1
  else:state["active_files"]+=1
 for digest,items in groups.items():
  ids={rid for rid,_ in items}
  if len(ids)>1:
   state["shared_digest_groups"]+=1
   state["shared_digest_recipe_ids"]+=len(ids)
   if len(examples["shared"])<5:examples["shared"].append({"recipes":sorted(ids)[:8],"file_count":len(items)})
  else:
   rid=next(iter(ids))
   if any(name.endswith((".suspect",".unverified")) for _,name in items):
    state["unique_quarantined_recipe_ids"]+=1
    if digest not in server.photo_rejected_digests:state["unique_quarantined_not_rejected_digest"]+=1
    if len(examples["unique"])<5:examples["unique"].append({"id":rid,"name":catalog[rid].get("name","")})
 stored=collections.defaultdict(set)
 for rid,recipe in catalog.items():
  url=server.clean_official_photo_candidate(recipe.get("image",""))
  if url and server.complete_recipe_photo_url(url):
   stored[url].add(rid)
   state["complete_stored_image_urls"]+=1
 for url,ids in stored.items():
  if len(ids)>1:
   state["shared_stored_url_groups"]+=1
   state["recipes_with_shared_stored_urls"]+=len(ids)
   if len(examples["shared_urls"])<5:examples["shared_urls"].append({"recipe_ids":sorted(ids)[:8],"url":url[:220]})
 print(json.dumps({"counts":dict(state),"examples":dict(examples)},indent=2),flush=True)
 print("READ ONLY: No quarantined photos were restored.",flush=True)

if __name__=="__main__":main()
