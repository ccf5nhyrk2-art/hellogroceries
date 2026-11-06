"""Targeted official-photo recheck without clearing caches or hiding uncertain recipes.

Prior full audit reports retain only examples, not all 2,180 unverified IDs.
Select high-risk records locally: absent, shared or untrusted imported photo URLs.
Dry-run default. --apply stores only definitive official page findings.
"""
import argparse
import collections
import json
import urllib.request
import server

def candidates():
    recipes=[r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000]
    owners=collections.Counter(server.clean_official_photo_candidate(r.get("image","")) for r in recipes)
    for recipe in recipes:
        url=server.clean_official_photo_candidate(recipe.get("image",""))
        if not url or owners[url]>1 or not server.trusted_imported_photo_url(recipe):
            yield recipe

def inspect(recipe):
    source=str(recipe.get("url") or "")
    if not server.allowed_url(source):return "unverified",None
    try:
        req=urllib.request.Request(source,headers={"User-Agent":"Mozilla/5.0","Accept":"text/html"})
        with urllib.request.urlopen(req,timeout=12) as response:
            if response.status!=200 or not server.allowed_url(response.geturl()):return "unverified",None
            raw=response.read(3000001)
        if len(raw)>3000000:return "unverified",None
        page=raw.decode("utf-8","replace")
        urls=server.official_images_from_page(recipe,page)
        if len(urls)==1:return "official_photo",urls[0]
        if len(urls)>1:return "ambiguous",None
        embedded=server.official_images_from_embedded_recipe(recipe,page)
        if len(embedded)==1:return "official_embedded_photo",embedded[0]
        return "unverified",None
    except Exception:return "unverified",None

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--apply",action="store_true",help="Save verified presence metadata only")
    p.add_argument("--limit",type=int,default=0,help="0 means all high-risk candidates")
    p.add_argument("--report",default="/data/targeted_photo_recheck.json")
    args=p.parse_args()
    selected=list(candidates())
    if args.limit:selected=selected[:max(0,args.limit)]
    counts=collections.Counter()
    results=[]
    for index,recipe in enumerate(selected,1):
        status,url=inspect(recipe)
        counts[status]+=1
        results.append({"id":recipe["id"],"name":recipe.get("name"),"status":status,"official_image":url})
        if args.apply and status in ("official_photo","official_embedded_photo"):
            with server.db() as c:
                c.execute("CREATE TABLE IF NOT EXISTS official_photo_status (recipe_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, status TEXT NOT NULL)")
                c.execute("INSERT OR REPLACE INTO official_photo_status(recipe_id,source_url,status) VALUES(?,?,?)",(str(recipe["id"]),str(recipe.get("url") or ""),"photo_present"))
                c.commit()
        if index%100==0:print("Targeted pages checked:",index,dict(counts),flush=True)
    report={"mode":"APPLY" if args.apply else "DRY_RUN","selected":len(selected),"counts":dict(counts),"results":results}
    with open(args.report,"w") as output:json.dump(report,output,indent=2)
    print(json.dumps({"mode":report["mode"],"selected":len(selected),"counts":dict(counts),"report":args.report},indent=2),flush=True)

if __name__=="__main__":main()
