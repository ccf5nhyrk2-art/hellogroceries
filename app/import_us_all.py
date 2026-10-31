"""Resumable US HelloFresh public recipe metadata importer. Run as Compose profile us-import."""
from pathlib import Path
import sqlite3, json, time, urllib.request, urllib.parse, urllib.robotparser, xml.etree.ElementTree as ET, gzip, os, sys, fcntl
from server import db, recipe_from_url, allowed_url
ROOT='https://www.hellofresh.com/sitemap_recipe_pages.xml'
STATE=Path('/data/us_import_progress.json')
LOCK=Path('/data/us_import.lock')

def save_progress(info):
 temp=STATE.with_suffix('.json.tmp')
 temp.write_text(json.dumps(info,indent=2))
 temp.replace(STATE)
DELAY=max(2.0,float(os.environ.get('HF_IMPORT_DELAY','3')))
UA='PersonalRecipeImporter/1.0'
def fetch(url,limit=12000000):
 req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept-Encoding':'gzip'})
 with urllib.request.urlopen(req,timeout=25) as response:
  if not allowed_url(response.geturl()):raise ValueError('Unexpected redirect')
  raw=response.read(limit+1)
  if len(raw)>limit:raise ValueError('Sitemap too large')
  if response.headers.get('Content-Encoding')=='gzip' or url.endswith('.gz'):raw=gzip.decompress(raw)
  return raw

def discover(rp):
 queue=[ROOT];seen=set();out=set()
 while queue:
  url=queue.pop(0)
  if url in seen or not allowed_url(url) or not rp.can_fetch(UA,url):continue
  seen.add(url)
  try:
   root=ET.fromstring(fetch(url))
   locs=[(e.text or '').strip() for e in root.iter() if e.tag.split('}')[-1]=='loc']
   for loc in locs:
    if not allowed_url(loc) or urllib.parse.urlsplit(loc).hostname!='www.hellofresh.com':continue
    if loc.endswith(('.xml','.xml.gz')) and loc not in seen and loc not in queue:queue.append(loc)
    elif '/recipes/' in loc and rp.can_fetch(UA,loc):out.add(loc.split('#')[0])
   print(f'Sitemap {len(seen)}; discovered {len(out)} recipes',flush=True)
  except Exception as exc:print('Sitemap unavailable:',url,str(exc)[:160],flush=True)
  time.sleep(DELAY)
 return sorted(out)

def main():
 with LOCK.open('a+') as lockfile:
  try:fcntl.flock(lockfile.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:
   print('Another US importer is already running; exiting.',flush=True)
   raise SystemExit(2)
  return run_import()

def retry_db(operation, label, attempts=6):
 for attempt in range(attempts):
  try:return operation()
  except sqlite3.OperationalError as exc:
   if not any(x in str(exc).lower() for x in ('locked','busy')) or attempt==attempts-1:raise
   delay=min(0.5*2**attempt,8)
   print(f'Database busy during {label}; retrying in {delay:g}s',flush=True)
   time.sleep(delay)

def ensure_attempts_table():
 with db() as conn:conn.execute('CREATE TABLE IF NOT EXISTS import_attempts(url TEXT PRIMARY KEY, status TEXT, detail TEXT, checked_at TEXT DEFAULT CURRENT_TIMESTAMP)')

def previously_seen(url):
 with db() as conn:
  return bool(conn.execute('SELECT 1 FROM imported WHERE url=?',(url,)).fetchone() or conn.execute('SELECT 1 FROM import_attempts WHERE url=?',(url,)).fetchone())

def store_recipe(url,recipe):
 with db() as conn:
  cur=conn.execute('INSERT OR IGNORE INTO imported(url,payload) VALUES(?,?)',(url,'{}'))
  if cur.rowcount:
   recipe['id']=1000000+cur.lastrowid
   conn.execute('UPDATE imported SET payload=? WHERE url=?',(json.dumps(recipe),url))
  conn.execute('INSERT OR REPLACE INTO import_attempts(url,status,detail) VALUES(?,?,?)',(url,'imported',''))
  return bool(cur.rowcount)

def record_failure(url,exc):
 with db() as conn:conn.execute('INSERT OR REPLACE INTO import_attempts(url,status,detail) VALUES(?,?,?)',(url,'error',str(exc)[:300]))

def run_import():
 save_progress({'status':'discovering','discovered':0,'processed':0,'imported_this_run':0,'errors_this_run':0,'skipped':0})
 rp=urllib.robotparser.RobotFileParser();rp.set_url('https://www.hellofresh.com/robots.txt')
 try:rp.read()
 except Exception as exc:raise SystemExit('Cannot check robots.txt: '+str(exc))
 if not rp.can_fetch(UA,ROOT):raise SystemExit('Recipe sitemap disallowed by robots.txt')
 urls=discover(rp)
 if not urls:raise SystemExit('No permitted recipe pages discovered. No changes made.')
 retry_db(ensure_attempts_table,'schema setup')
 successes=errors=skipped=0
 for i,url in enumerate(urls,1):
  if not rp.can_fetch(UA,url):skipped+=1;continue
  try:
   if retry_db(lambda: previously_seen(url),'recipe lookup'):
    skipped+=1
    continue
   recipe=recipe_from_url(url)
   # Keep exact original ingredient lines; the app cannot safely normalize international quantities.
   if retry_db(lambda: store_recipe(url,recipe),'recipe save'):successes+=1
   else:skipped+=1
  except sqlite3.OperationalError as exc:
   if any(x in str(exc).lower() for x in ('locked','busy')):
    print(f'Database still busy; stopping for a later resume: {exc}',flush=True)
    save_progress({'status':'paused','discovered':len(urls),'processed':i-1,'imported_this_run':successes,'errors_this_run':errors,'skipped':skipped})
    return
   raise
  except Exception as exc:
   try:retry_db(lambda: record_failure(url,exc),'error recording')
   except sqlite3.OperationalError as db_exc:
    if any(x in str(db_exc).lower() for x in ('locked','busy')):
     print(f'Database still busy while recording an error; stopping for a later resume: {db_exc}',flush=True)
     save_progress({'status':'paused','discovered':len(urls),'processed':i-1,'imported_this_run':successes,'errors_this_run':errors,'skipped':skipped})
     return
    raise
   errors+=1
  if i%25==0 or i==len(urls):
   save_progress({'status':'running','discovered':len(urls),'processed':i,'imported_this_run':successes,'errors_this_run':errors,'skipped':skipped})
   print(f'{i}/{len(urls)} checked; {successes} new; {errors} errors; {skipped} skipped',flush=True)
  time.sleep(DELAY)
 save_progress({'status':'completed','discovered':len(urls),'processed':len(urls),'imported_this_run':successes,'errors_this_run':errors,'skipped':skipped})
 print('Import complete. Some pages may be unavailable or lack structured Recipe data.',flush=True)
if __name__=='__main__':main()