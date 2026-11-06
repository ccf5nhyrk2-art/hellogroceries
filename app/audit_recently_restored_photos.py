"""Read-only contact sheet of recently restored photos for human inspection."""
import argparse,base64,html,json,random,time
from pathlib import Path
from PIL import Image,ImageOps
import io
import server

def main():
 p=argparse.ArgumentParser()
 p.add_argument("--report",default="/data/verified_photo_recovery.json")
 p.add_argument("--output",default="/data/restored_photo_audit.html")
 p.add_argument("--sample",type=int,default=60)
 p.add_argument("--window-hours",type=float,default=3)
 args=p.parse_args()
 report=Path(args.report)
 data=json.loads(report.read_text())
 if data.get("mode")!="APPLY" or not data.get("counts",{}).get("restored"):
  raise SystemExit("Report must be a completed APPLY run with restored images")
 catalog={int(r["id"]):r for r in server.browse_catalog() if 1000000<=int(r["id"])<2000000}
 # A restored file inherits its old inode mtime after rename; use ctime (rename changes ctime).
 finish=report.stat().st_ctime
 start=finish-args.window_hours*3600
 candidates=[]
 for path in server.PHOTO_DIR.iterdir():
  if path.suffix.lower() not in {".jpg",".png",".webp",".gif"} or not path.stem.isdigit():continue
  rid=int(path.stem)
  if rid not in catalog or not start<=path.stat().st_ctime<=finish+60:continue
  if not server.valid_photo_cache_entry(path):continue
  candidates.append((rid,path))
 known={int(x["id"]) for x in data.get("samples",{}).get("restored",[])}
 by_id={rid:path for rid,path in candidates}
 for rid in known:
  if rid not in by_id:
   for ext in (".webp",".jpg",".png",".gif"):
    path=server.PHOTO_DIR/(str(rid)+ext)
    if path.is_file() and server.valid_photo_cache_entry(path):by_id[rid]=path;break
 rng=random.Random(20261106)
 selected=sorted(known & by_id.keys())
 remaining=sorted(set(by_id)-set(selected))
 rng.shuffle(remaining)
 selected=(selected+remaining)[:max(1,args.sample)]
 cards=[]
 for rid in selected:
  path=by_id[rid]
  try:
   with Image.open(path) as im:
    im=ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((480,320))
    buf=io.BytesIO()
    im.save(buf,format="JPEG",quality=75)
   uri="data:image/jpeg;base64,"+base64.b64encode(buf.getvalue()).decode()
  except (OSError,ValueError):continue
  recipe=catalog[rid]
  title=html.escape(str(recipe.get("name","")))
  url=recipe.get("url","")
  link='<a href="'+html.escape(url,quote=True)+'" target="_blank" rel="noopener noreferrer">Official recipe</a>' if server.allowed_url(url) else ""
  cards.append('<article><img loading="lazy" src="'+uri+'"><h3>'+title+'</h3><p>Recipe #'+str(rid)+'</p>'+link+'</article>')
 page='''<!doctype html><html lang="en"><meta charset="utf-8"><title>Restored Photo Audit</title><style>body{font:16px system-ui;background:#111;color:#eee;margin:2rem}h1{color:#a5e6ae}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px}article{background:#222;border:1px solid #555;border-radius:12px;padding:12px}img{width:100%;height:190px;object-fit:contain;background:#000}h3{font-size:16px}p{color:#bbb}a{color:#9ed4ff}</style><h1>Recently restored recipe photos — staging</h1><p>Read-only sample based on file change times around the recovery report, plus known restored IDs. Review each image against the title and official page. This does not prove the identity of every restored photo.</p><div class="grid">'''+''.join(cards)+'</div></html>'
 Path(args.output).write_text(page,encoding="utf-8")
 print(json.dumps({"report":args.output,"restored_report_count":data["counts"]["restored"],"recent_candidate_files":len(candidates),"sampled":len(cards),"known_restored_ids":len(known),"read_only":True},indent=2))
if __name__=="__main__":main()
