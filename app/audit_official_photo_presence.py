"""Conservatively audit official HelloFresh pages and hide only confirmed photo-less recipes.

No cache deletion. Dry-run by default. --apply records definitive status.
"""
import argparse
import collections
import html
import json
import re
import urllib.parse
import urllib.request
import server

def matching_recipe_nodes(recipe, page):
    expected=urllib.parse.urlsplit(str(recipe.get("url") or "")).path.rstrip("/")
    if not expected:return []
    nodes=[]
    def walk(value):
        if isinstance(value,list):
            for v in value:yield from walk(v)
        elif isinstance(value,dict):
            if "@graph" in value:yield from walk(value["@graph"])
            yield value
    for raw in re.findall(r'<script\\b[^>]*type=["\\\']application/ld\\+json["\\\'][^>]*>(.*?)</script>',page,re.I|re.S):
        try: data=json.loads(html.unescape(raw))
        except (ValueError,TypeError):continue
        for node in walk(data):
            kind=node.get("@type",[])
            if isinstance(kind,str):kind=[kind]
            if "Recipe" not in kind:continue
            identity=node.get("url") or node.get("mainEntityOfPage") or node.get("@id") or ""
            if isinstance(identity,dict):identity=identity.get("@id") or identity.get("url") or ""
            path=urllib.parse.urlsplit(urllib.parse.urljoin(recipe["url"],str(identity))).path.rstrip("/")
            if identity and path==expected:nodes.append(node)
    return nodes

def inspect(recipe):
    url=str(recipe.get("url") or "")
    if not server.allowed_url(url):return "unverified"
    try:
        request=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"text/html"})
        with urllib.request.urlopen(request,timeout=12) as response:
            if not server.allowed_url(response.geturl()) or response.status!=200:return "unverified"
            data=response.read(3000001)
        if len(data)>3000000:return "unverified"
        page=data.decode("utf-8","replace")
        if server.official_images_from_page(recipe,page):return "photo_present"
        nodes=matching_recipe_nodes(recipe,page)
        # Require an explicit exact-identity Recipe node with no image,
        # and no exact-source embedded recipe image. Anything else is unknown.
        if len(nodes)==1 and not nodes[0].get("image") and not server.official_images_from_embedded_recipe(recipe,page):
            return "confirmed_no_photo"
        return "unverified"
    except Exception:return "unverified"

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--apply",action="store_true")
    p.add_argument("--limit",type=int,default=100,help="0 checks all imported recipes")
    p.add_argument("--report",default="/data/official_photo_presence_audit.json")
    args=p.parse_args()
    recipes=[r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000]
    if args.limit:recipes=recipes[:max(0,args.limit)]
    counts=collections.Counter()
    examples=collections.defaultdict(list)
    for index,recipe in enumerate(recipes,1):
        status=inspect(recipe)
        counts[status]+=1
        if len(examples[status])<15:examples[status].append({"id":recipe["id"],"name":recipe.get("name")})
        if args.apply and status in ("confirmed_no_photo","photo_present"):
            with server.db() as c:
                c.execute("CREATE TABLE IF NOT EXISTS official_photo_status (recipe_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, status TEXT NOT NULL)")
                c.execute("INSERT OR REPLACE INTO official_photo_status(recipe_id,source_url,status) VALUES(?,?,?)",(str(recipe["id"]),str(recipe.get("url") or ""),status))
                c.commit()
        if index%100==0:print("Official photo pages checked:",index,dict(counts),flush=True)
    report={"mode":"APPLY" if args.apply else "DRY_RUN","checked":sum(counts.values()),"counts":dict(counts),"examples":dict(examples)}
    with open(args.report,"w") as output:json.dump(report,output,indent=2)
    print(json.dumps(report,indent=2),flush=True)

if __name__=="__main__":main()
