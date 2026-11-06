"""Recover missing recipe photos only from recipe-specific official structured metadata.

Never restores quarantined files or overwrites active photos. Resume by rerunning.
"""
import argparse,collections,hashlib,json,time,urllib.error
from pathlib import Path
import server

def main():
 p=argparse.ArgumentParser()
 p.add_argument("--apply",action="store_true",help="Download approved official images; default dry run")
 p.add_argument("--limit",type=int,default=0)
 p.add_argument("--report",default="/data/missing_photo_recovery.json")
 args=p.parse_args()
 catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 counts=collections.Counter()
 examples=collections.defaultdict(list)
 # Account for existing active photos and quarantined bytes; never reuse either blindly.
 known=collections.defaultdict(set)
 for path in server.PHOTO_DIR.iterdir():
  if path.suffix.lower() not in (".jpg",".png",".webp",".gif") or not path.stem.isdigit():continue
  try:
   if server.valid_photo_cache_entry(path):
    known[hashlib.sha256(path.read_bytes()).hexdigest()].add(int(path.stem))
  except OSError:pass
 checked=0
 for rid,recipe in sorted(catalog.items()):
  if server.cached_photo(rid):
   counts["already_active"]+=1
   continue
  if args.limit and checked>=args.limit:
   counts["not_checked_limit"]+=1
   continue
  checked+=1
  urls=server.official_images(recipe)
  if len(urls)!=1:reason="no_unique_official_image"
  else:
   url=server.clean_official_photo_candidate(urls[0])
   if not url or not server.complete_recipe_photo_url(url):reason="invalid_official_url"
   elif not args.apply:reason="official_candidate_dry_run"
   else:
    # download_photo enforces URL allowlist, rejected digests, content limits and format.
    # Check its returned bytes against all known active image hashes, not just first page.
    try:
     diagnostics=[]
     downloaded=None
     variants=[url]
     for candidate in variants:
      try:
       downloaded=server.download_photo(candidate,rid,prefer_jpeg=True,diagnostics=diagnostics)
       if downloaded:break
      except urllib.error.HTTPError as exc:
       diagnostics.append("HTTP_"+str(exc.code))
       if exc.code==404 and candidate==url:
        variants.extend(server.official_photo_url_variants(url))
      except (OSError,ValueError,TimeoutError,urllib.error.URLError) as exc:
       diagnostics.append(type(exc).__name__)
     if not downloaded:
      reason="download_rejected"
      for detail in set(diagnostics):counts["download_detail_"+detail]+=1
     else:
      cached=server.cached_photo(rid)
      if not cached:reason="download_not_cached"
      else:
       path=cached[0]
       h=hashlib.sha256(path.read_bytes()).hexdigest()
       if h in known and known[h]!={rid}:
        backup=path.with_name(path.name+".suspect")
        path.rename(backup)
        reason="duplicate_quarantined"
       else:
        known[h].add(rid)
        reason="restored_from_official"
    except Exception:reason="network_or_download_error"
  counts[reason]+=1
  if len(examples[reason])<8:examples[reason].append({"id":rid,"name":recipe.get("name","")})
  if checked%100==0:print("Checked missing:",checked,"restored:",counts["restored_from_official"],flush=True)
 result={"mode":"APPLY" if args.apply else "DRY_RUN","checked":checked,"counts":dict(counts),"examples":dict(examples)}
 Path(args.report).write_text(json.dumps(result,indent=2))
 print(json.dumps({"checked":checked,"counts":dict(counts),"report":args.report},indent=2),flush=True)
if __name__=="__main__":main()
