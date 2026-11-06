"""Inspect official HelloFresh photo presence without modifying catalog records."""
import argparse
import collections
import json
import urllib.request
import server

def inspect(recipe):
    url = str(recipe.get("url") or "")
    if not server.allowed_url(url):
        return "unverified"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=12) as response:
            if not server.allowed_url(response.geturl()) or response.status != 200:
                return "unverified"
            content = response.read(3000001)
        if len(content) > 3000000:
            return "unverified"
        images = server.official_images_from_page(recipe, content.decode("utf-8", "replace"))
        return "photo_present" if images else "unverified"
    except Exception:
        return "unverified"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--report", default="/data/official_photo_presence_audit.json")
    args = parser.parse_args()
    results = []
    counts = collections.Counter()
    recipes = [r for r in server.browse_catalog() if 1000000 <= int(r["id"]) < 2000000]
    for recipe in recipes[:max(0, args.limit)]:
        status = inspect(recipe)
        counts[status] += 1
        results.append({"id": recipe["id"], "name": recipe.get("name"), "status": status})
    report = {"checked": len(results), "counts": dict(counts), "results": results}
    with open(args.report, "w") as output:
        json.dump(report, output, indent=2)
    print(json.dumps({"checked": len(results), "counts": dict(counts)}, indent=2))

if __name__ == "__main__":
    main()
