"""Read-only diagnostics for failed imported HelloFresh photos (staging)."""
import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import server

def probe(url):
 if not server.complete_recipe_photo_url(url) or not server.allowed_photo_url(url):
  return "invalid_or_unsafe"
 req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; HelloGroceries/1.0)","Range":"bytes=0-0"})
 try:
  with urllib.request.build_opener(server.SafePhotoRedirect).open(req,timeout=12) as resp:
   return str(resp.status)+" "+str(resp.headers.get("Content-Type",""))
 except urllib.error.HTTPError as exc:
  return str(exc.code)
 except (urllib.error.URLError,TimeoutError,OSError) as exc:
  return type(exc).__name__

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument("--limit",type=int,default=5)
 args=parser.parse_args()
 checkpoint=Path("/data/bulk_photo_recovery_checkpoint.json")
 state=json.loads(checkpoint.read_text())
 failed=set(map(int,state.get("failed",[])))
 catalog={int(r["id"]):r for r in server.browse_catalog()}
 for rid in sorted(failed)[:max(1,min(args.limit,10))]:
  recipe=catalog.get(rid)
  if not recipe:continue
  source=recipe.get("url","")
  print("\\nRECIPE",rid,recipe.get("name",""),"source",source,flush=True)
  original=recipe.get("image","")
  print("stored",original,"status",probe(original),flush=True)
  if not server.allowed_url(source):continue
  try:
   req=urllib.request.Request(source,headers={"User-Agent":"Mozilla/5.0","Accept":"text/html"})
   with urllib.request.urlopen(req,timeout=12) as resp:
    page=resp.read(3000000).decode("utf-8","replace")
  except (urllib.error.URLError,TimeoutError,OSError) as exc:
   print("source_error",type(exc).__name__,flush=True)
   continue
  official=server.official_images_from_page(recipe,page)
  print("verified_candidates",len(official),flush=True)
  for url in official[:3]:
   print("candidate",url,"status",probe(url),flush=True)
   for alt in server.official_photo_url_variants(url)[:4]:
    print("same_asset_variant",alt,"status",probe(alt),flush=True)
  identity=re.search(r"([0-9a-f]{24})(?:[/?#]|$)",urllib.parse.urlsplit(source).path,re.I)
  if identity:
   print("source_id_occurrences",len(re.findall(re.escape(identity.group(1)),page,re.I)),flush=True)
  print("json_ld_recipe_blocks",len(re.findall(r'application/ld\\+json',page,re.I)),flush=True)

if __name__=="__main__":main()
