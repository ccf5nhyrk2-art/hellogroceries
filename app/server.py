from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from pathlib import Path
from http.cookies import SimpleCookie
import recipe_quality
import households as auth
import household_sharing as sharing
import secrets
import cloudflare_auth
import os,math,json,sqlite3,threading,re,html,io,zipfile,datetime,urllib.request,urllib.parse,urllib.robotparser,xml.etree.ElementTree as ET,time,subprocess,sys,fcntl,os,ipaddress,socket
BASE=Path(__file__).parent;DB=Path('/data/shopping.db');DB.parent.mkdir(exist_ok=True,parents=True)
RECIPES=json.loads((BASE/'recipes.json').read_text());lock=threading.Lock()
# Generate a persistent first-run secret when the operator has not configured one.
# Keep it outside the image in the existing /data bind mount; never expose it via HTTP.

job={"running":False,"discovered":0,"processed":0,"imported":0,"skipped":0,"errors":0,"message":"Idle","paused":False,"country":"US","limit":0}
BULK_STATE=Path("/data/bulk_import_progress.json")
BULK_COUNTRIES=Path('/data/bulk_import_countries.json')
def bulk_country_history():
 try:
  data=json.loads(BULK_COUNTRIES.read_text())
  return data if isinstance(data,dict) else {}
 except (OSError,ValueError):return {}
def save_bulk_country():
 data=bulk_country_history()
 data[str(job.get('country','US'))]={k:job.get(k) for k in ('country','limit','running','paused','discovered','processed','imported','skipped','errors','message','started_at','updated_at')}
 tmp=BULK_COUNTRIES.with_suffix('.tmp')
 tmp.write_text(json.dumps(data));tmp.replace(BULK_COUNTRIES)
BULK_PAUSE=threading.Event()
BULK_RECOVER=False
def save_bulk():
 job['updated_at']=time.time()
 save_bulk_country()
 tmp=BULK_STATE.with_suffix(".tmp");tmp.write_text(json.dumps(job));tmp.replace(BULK_STATE)
if BULK_STATE.exists():
 try:
  job.update(json.loads(BULK_STATE.read_text()));BULK_RECOVER=bool(job.get("running") and not job.get("paused"));job["running"]=False
  if not job.get("paused") and job.get("message")!="Completed":job["message"]="Interrupted; press Resume to continue"
 except (OSError,ValueError):pass
IMPORT_LOCK=Path('/data/us_import.lock')
IMPORT_LOG=Path('/data/us_import.log')
import_launch_lock=threading.Lock()
import_process=None

TASK_HISTORY_FILE=Path('/data/admin_task_history.json')
TASK_HISTORY_LOCK=threading.Lock()
def task_record(name,status):
 with TASK_HISTORY_LOCK:
  try:data=json.loads(TASK_HISTORY_FILE.read_text())
  except (OSError,ValueError,TypeError):data={}
  if not isinstance(data,dict):data={}
  row=data.setdefault(name,{})
  row['last_'+('started' if status=='running' else 'finished')]=datetime.datetime.now(datetime.timezone.utc).isoformat()
  row['status']=status
  tmp=TASK_HISTORY_FILE.with_suffix('.tmp')
  try:tmp.write_text(json.dumps(data));os.replace(tmp,TASK_HISTORY_FILE)
  except OSError:pass

def serving_coverage():
 with db() as conn:
  rows=conn.execute('SELECT payload FROM imported').fetchall()
 total=len(rows);known=0
 for (payload,) in rows:
  try:
   value=json.loads(payload).get('source_servings')
   if value is not None and math.isfinite(float(value)) and float(value)>0:known+=1
  except (ValueError,TypeError,AttributeError):pass
 return {'known':known,'missing':total-known,'total':total}

def task_overview():
 try:history=json.loads(TASK_HISTORY_FILE.read_text())
 except (OSError,ValueError,TypeError):history={}
 if not isinstance(history,dict):history={}
 def item(key,name,frequency,running=False,last=None,action=None):
  row=history.get(key,{})
  return {'key':key,'name':name,'frequency':frequency,'running':bool(running),'last_started':row.get('last_started') or last,'last_finished':row.get('last_finished'),'status':'running' if running else row.get('status','Not recorded'),'action':action}
 progress=Path('/data/us_import_progress.json')
 nutrition=history.get('nutrition',{})
 return [
  {'key':'us_import','name':'HelloFresh US recipe import','frequency':'Automatic initial import; weekly thereafter','running':us_import_running() or us_import_schedule['running'],'last_started':us_import_schedule['last_started'],'last_finished':us_import_schedule['last_finished'],'next_run':us_import_schedule['next_run'],'status':'Running' if us_import_schedule['running'] else ('Error: '+str(us_import_schedule['last_error']) if us_import_schedule['last_error'] else 'Waiting for next cycle'),'action':'us_import'},
  {'key':'nutrition','name':'Bulk recipe nutrition scan','frequency':'Automatic initial scan; weekly thereafter (2 seconds between recipes)','running':nutrition_scan['running'],'last_started':nutrition_scan['last_started'],'last_finished':nutrition_scan['last_finished'],'next_run':nutrition_scan['next_run'],'status':'Running' if nutrition_scan['running'] else 'Waiting for next cycle','processed':nutrition_scan['processed'],'total':nutrition_scan['total'],'updated':nutrition_scan['updated'],'missing':nutrition_scan['missing'],'failed':nutrition_scan['failed'],'servings_backfilled':nutrition_scan.get('servings_backfilled',0),'servings_coverage':serving_coverage(),'action':None},
  item('store_directory','Nationwide store directory refresh','Automatic when cache expires (7 days) or store search needs it',False,datetime.datetime.fromtimestamp(ATP_CACHE_FILE.stat().st_mtime,datetime.timezone.utc).isoformat() if ATP_CACHE_FILE.exists() else None,'store_directory'),
  item('store_geocode','Store address geocoding','Automatic during store search (only uncached addresses)',RETAILER_GEOCODE_RUNNING,datetime.datetime.fromtimestamp(Path('/data/retailer_address_coordinates.json').stat().st_mtime,datetime.timezone.utc).isoformat() if Path('/data/retailer_address_coordinates.json').exists() else None,'store_geocode'),
  item('reef_prices','ReefAPI grocery price refresh','Manual only; cached prices expire after 365 days',False,None,None),
  {'key':'recommendation_photos','name':'Recommendation photo caching','frequency':'Background: checks hourly; 15 seconds between downloads (5 GB cache limit)','running':recommendation_photo_warmup['running'],'last_started':recommendation_photo_warmup['last_started'],'last_finished':recommendation_photo_warmup['last_finished'],'next_run':recommendation_photo_warmup['next_run'],'status':'Running' if recommendation_photo_warmup['running'] else 'Waiting for next cycle','cached':recommendation_photo_warmup['cached'],'target':len([r for r in allrecipes() if r.get('image') and not non_recipe_reason(r)]),'cache_bytes':recommendation_photo_cache_bytes(),'cache_limit_bytes':RECOMMENDATION_PHOTO_CACHE_LIMIT,'checked':recommendation_photo_warmup['checked'],'errors':recommendation_photo_warmup['errors'],'action':None}
 ]

def us_import_running():
 fd=IMPORT_LOCK.open('a+')
 try:
  try:fcntl.flock(fd.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return True
  return False
 finally:fd.close()

def start_us_import():
 global import_process
 with import_launch_lock:
  if us_import_running() or (import_process is not None and import_process.poll() is None):
   raise ValueError('US importer is already running')
  # The child holds the shared advisory lock; no Docker socket is required.
  with IMPORT_LOG.open('ab') as logfile:
   import_process=subprocess.Popen([sys.executable,'-u',str(BASE/'import_us_all.py')],cwd=str(BASE),stdin=subprocess.DEVNULL,stdout=logfile,stderr=subprocess.STDOUT,close_fds=True)
  time.sleep(0.15)
  if import_process.poll() is not None:
   raise ValueError('Importer exited immediately; check /data/us_import.log')
  task_record('us_import','running')
  return {'ok':True,'message':'US importer started. Existing imported URLs will be skipped.'}

US_IMPORT_SCHEDULE=Path('/data/us_import_schedule.json')
us_import_wakeup=threading.Event()
us_import_schedule={'running':False,'last_started':None,'last_finished':None,'next_run':None,'last_error':None}
def save_us_import_schedule():
 try:
  tmp=US_IMPORT_SCHEDULE.with_suffix('.tmp');tmp.write_text(json.dumps(us_import_schedule));os.replace(tmp,US_IMPORT_SCHEDULE)
 except OSError:pass
def us_import_schedule_worker():
 try:
  saved=json.loads(US_IMPORT_SCHEDULE.read_text())
  for key in us_import_schedule:
   if key in saved and key!='running':us_import_schedule[key]=saved[key]
 except (OSError,ValueError,TypeError):pass
 while True:
  now=datetime.datetime.now(datetime.timezone.utc)
  due=us_import_schedule.get('next_run')
  if due:
   try:delay=max(0,(datetime.datetime.fromisoformat(due)-now).total_seconds())
   except (ValueError,TypeError):delay=0
   if delay>0:
    us_import_wakeup.wait(min(delay,86400))
    if not us_import_wakeup.is_set():continue
    us_import_wakeup.clear()
  if us_import_running() or (import_process is not None and import_process.poll() is None):
   us_import_schedule['next_run']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat()
   save_us_import_schedule()
   continue
  try:
   us_import_schedule['last_started']=datetime.datetime.now(datetime.timezone.utc).isoformat()
   us_import_schedule['running']=True
   save_us_import_schedule()
   start_us_import()
   while us_import_running() or (import_process is not None and import_process.poll() is None):time.sleep(30)
   us_import_schedule['last_finished']=datetime.datetime.now(datetime.timezone.utc).isoformat()
   us_import_schedule['last_error']=None
   us_import_schedule['next_run']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=7)).isoformat()
  except Exception as exc:
   us_import_schedule['last_error']=str(exc)[:200]
   us_import_schedule['next_run']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat()
  finally:
   us_import_schedule['running']=False
   save_us_import_schedule()

def db():
 c=sqlite3.connect(DB)
 c.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS imported (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, payload TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS photos (recipe_id INTEGER PRIMARY KEY, image_url TEXT NOT NULL)')
 return c


# Privacy-minimal product analytics: event counts and distinct users, 30-day retention.
ANALYTICS_EVENTS={'analytics_test','page_recipes','page_planner','page_shopping','page_pantry','page_stores','page_selected-meals','page_my-recipes','page_imports','recipe_search','recipe_search_empty','recipe_saved','shopping_list_generated','meal_planned','price_lookup_failed'}
def analytics_record(user_id,event):
 if event not in ANALYTICS_EVENTS:return
 try:
  with db() as c:
   c.execute('CREATE TABLE IF NOT EXISTS hg_analytics (day TEXT NOT NULL,user_id INTEGER NOT NULL,event TEXT NOT NULL,count INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(day,user_id,event))')
   c.execute("DELETE FROM hg_analytics WHERE day < date('now','-30 days')")
   c.execute("INSERT INTO hg_analytics(day,user_id,event,count) VALUES (date('now'),?,?,1) ON CONFLICT(day,user_id,event) DO UPDATE SET count=count+1",(user_id,event))
   c.commit()
 except sqlite3.Error as exc:
  print('Analytics database write failed:',repr(exc),file=sys.stderr)
  raise
def record_scan_usage(user_id,model,usage):
 try:
  input_tokens=int(usage.get('prompt_tokens',0))
  output_tokens=int(usage.get('completion_tokens',0))
  if input_tokens<0 or output_tokens<0:return
  # USD per million tokens, model-specific published list prices.
  rates={'gpt-4.1-mini':(0.40,1.60)}
  rate=rates.get(model)
  cost_micro=round(input_tokens*rate[0]+output_tokens*rate[1]) if rate else None
  with db() as c:
   c.execute('CREATE TABLE IF NOT EXISTS hg_ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,user_id INTEGER NOT NULL,model TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,cost_micro_usd INTEGER)')
   c.execute('INSERT INTO hg_ai_usage(user_id,model,input_tokens,output_tokens,cost_micro_usd) VALUES (?,?,?,?,?)',(user_id,model,input_tokens,output_tokens,cost_micro))
   c.commit()
 except Exception as exc:print('AI usage tracking failed:',type(exc).__name__,flush=True)

def ai_scan_limits():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS admin_preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
  values=dict(c.execute("SELECT key,value FROM admin_preferences WHERE key IN ('ai_monthly_budget_usd','ai_user_monthly_scans')"))
 return {'monthly_budget_usd':float(values.get('ai_monthly_budget_usd','0')),'user_monthly_scans':int(values.get('ai_user_monthly_scans','0'))}

def ai_scan_allowed(user_id):
 limits=ai_scan_limits()
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS hg_ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,user_id INTEGER NOT NULL,model TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,cost_micro_usd INTEGER)')
  used=c.execute("SELECT COUNT(*) FROM hg_ai_usage WHERE user_id=? AND at>=strftime('%Y-%m-01','now')",(user_id,)).fetchone()[0]
  spent=c.execute("SELECT COALESCE(SUM(cost_micro_usd),0) FROM hg_ai_usage WHERE at>=strftime('%Y-%m-01','now')").fetchone()[0]/1000000
 if limits['user_monthly_scans'] and used>=limits['user_monthly_scans']:return 'Your monthly AI scan allowance has been reached.'
 if limits['monthly_budget_usd'] and spent>=limits['monthly_budget_usd']:return 'The monthly AI scanning budget has been reached.'
 return None

def scan_usage_summary():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS hg_ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,user_id INTEGER NOT NULL,model TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,cost_micro_usd INTEGER)')
  rows=c.execute("SELECT u.user_id,COALESCE(a.email,'Deleted account'),COUNT(*),SUM(u.input_tokens),SUM(u.output_tokens),SUM(u.cost_micro_usd),SUM(CASE WHEN u.cost_micro_usd IS NULL THEN 1 ELSE 0 END) FROM hg_ai_usage u LEFT JOIN hg_users a ON a.id=u.user_id WHERE u.at>=datetime('now','-30 days') GROUP BY u.user_id ORDER BY SUM(u.cost_micro_usd) DESC").fetchall()
  month=c.execute("SELECT COUNT(*),COALESCE(SUM(cost_micro_usd),0) FROM hg_ai_usage WHERE at>=strftime('%Y-%m-01','now')").fetchone()
  limits=ai_scan_limits()
  return {'month_scans':month[0],'month_estimated_usd':round(month[1]/1000000,6),'limits':limits,'period_days':30,'users':[{'user_id':r[0],'user':r[1],'scans':r[2],'input_tokens':r[3],'output_tokens':r[4],'estimated_usd':round((r[5] or 0)/1000000,6),'unpriced_scans':r[6]} for r in rows]}

def analytics_summary():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS hg_analytics (day TEXT NOT NULL,user_id INTEGER NOT NULL,event TEXT NOT NULL,count INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(day,user_id,event))')
  rows=c.execute("SELECT day,event,SUM(count),COUNT(DISTINCT user_id) FROM hg_analytics WHERE day>=date('now','-29 days') GROUP BY day,event").fetchall()
  registered=c.execute('SELECT COUNT(*) FROM hg_users').fetchone()[0]
  active=c.execute("SELECT COUNT(DISTINCT user_id) FROM hg_analytics WHERE day>=date('now','-29 days')").fetchone()[0]
  daily=c.execute("SELECT day,COUNT(DISTINCT user_id) FROM hg_analytics WHERE day>=date('now','-29 days') GROUP BY day ORDER BY day").fetchall()
  weekly=c.execute("SELECT COUNT(DISTINCT user_id) FROM hg_analytics WHERE day>=date('now','-6 days')").fetchone()[0]
  return {'total_users':registered,'active_30d':active,'active_7d':weekly,'daily_active':[{'day':d,'users':n} for d,n in daily],'events':[{'day':r[0],'event':r[1],'count':r[2],'users':r[3]} for r in rows],'retention_days':30}

def pricing_visibility():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS admin_preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
  values=dict(c.execute("SELECT key,value FROM admin_preferences WHERE key IN ('show_prices_admin','show_prices_user')"))
 return {'admin':values.get('show_prices_admin','1')=='1','user':values.get('show_prices_user','0')=='1'}

def allrecipes():
 with db() as c: imported=[json.loads(x[0]) for x in c.execute('SELECT payload FROM imported ORDER BY id DESC')]
 for recipe in imported:
  if isinstance(recipe,dict) and recipe.get('url'):
   code=recipe_country_from_url(recipe['url'])
   if code!='other':recipe['country']=code
 recipes=RECIPES+imported
 # Recovered source lines are cached separately from original recipe payloads.
 # Only apply cache entries for the exact URL they were fetched from.
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS hg_recipe_source_cache (recipe_id TEXT PRIMARY KEY,url TEXT NOT NULL,lines_json TEXT NOT NULL,checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
  cached={rid:(url,lines) for rid,url,lines in c.execute('SELECT recipe_id,url,lines_json FROM hg_recipe_source_cache')}
 for recipe in recipes:
  entry=cached.get(str(recipe.get('id')))
  if entry and entry[0]==recipe.get('url'):
   try:
    lines=json.loads(entry[1])
    if isinstance(lines,list) and lines:recipe['ingredient_text']=list(dict.fromkeys((recipe.get('ingredient_text') if isinstance(recipe.get('ingredient_text'),list) else [])+lines))
   except (ValueError,TypeError):pass
 with db() as c:photos=dict(c.execute('SELECT recipe_id,image_url FROM photos'))
 return [dict(r,image=photos.get(r['id'],r.get('image',''))) for r in recipes]

def _selected_recipe_payload(payload,selected_ids):
 try:
  recipe=json.loads(payload)
  return str(recipe.get('id')) in selected_ids if isinstance(recipe,dict) else False
 except (ValueError,TypeError):return False


def recipe_source_enrichment_status():
 """Read-only coverage of cached source lookups, not a recovery success claim."""
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS hg_recipe_source_cache (recipe_id TEXT PRIMARY KEY,url TEXT NOT NULL,lines_json TEXT NOT NULL,checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
  rows=c.execute('SELECT recipe_id,lines_json,checked_at FROM hg_recipe_source_cache ORDER BY checked_at DESC').fetchall()
 with_lines=with_measurements=names_only=0
 recent=[]
 for rid,raw,at in rows:
  try:lines=json.loads(raw)
  except (ValueError,TypeError):lines=[]
  if not isinstance(lines,list):lines=[]
  lines=[line for line in lines if isinstance(line,str) and line.strip()]
  measured=sum(1 for line in lines if recipe_quality.parse_source_line(line))
  if lines:
   with_lines+=1
   if measured:with_measurements+=1
   else:names_only+=1
  if len(recent)<20:
   recent.append({'recipe_id':rid,'lines_found':bool(lines),'measured_lines':measured,'checked_at':at})
 return {'checked':len(rows),'with_source_lines':with_lines,'without_source_lines':len(rows)-with_lines,
         'with_measured_lines':with_measurements,'names_only':names_only,'recent':recent}



def recipe_source_sample_diagnostic(limit=20):
 """Bounded, read-only comparison of cached original lines and unresolved items.
 This does not claim to inspect other page fields or fetch external pages.
 """
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS hg_recipe_source_cache (recipe_id TEXT PRIMARY KEY,url TEXT NOT NULL,lines_json TEXT NOT NULL,checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
  rows=c.execute('SELECT recipe_id,lines_json FROM hg_recipe_source_cache ORDER BY checked_at DESC LIMIT ?', (limit,)).fetchall()
  payloads={str(json.loads(payload).get('id')):json.loads(payload) for (payload,) in c.execute('SELECT payload FROM imported') if payload}
 result={'recipes_checked':0,'unresolved_entries':0,'matched_measured':0,'matched_without_quantity':0,'unmatched':0,'examples':[]}
 for rid,raw in rows:
  recipe=payloads.get(str(rid))
  if not isinstance(recipe,dict):continue
  try:lines=json.loads(raw)
  except (ValueError,TypeError):lines=[]
  if not isinstance(lines,list):lines=[]
  candidate=dict(recipe,ingredient_text=lines)
  result['recipes_checked']+=1
  for ingredient,factor in (recipe.get('ingredients') or {}).items():
   if recipe_quality.inspect_ingredient(ingredient,factor)['status'] not in ('needs_source','unit_labeled'):continue
   result['unresolved_entries']+=1
   status=recipe_quality.source_match_diagnostic(candidate,ingredient)['status']
   if status=='matched':result['matched_measured']+=1
   elif status in ('unit_without_quantity','source_without_verified_quantity','to_taste_no_amount'):
    result['matched_without_quantity']+=1
   else:result['unmatched']+=1
   if len(result['examples'])<8:
    result['examples'].append({'recipe_id':rid,'ingredient':ingredient[:90],'diagnosis':status})
 return result



def inspect_alternative_recipe_fields(limit=3):
 """Read-only, bounded inspection of Next.js page ingredient metadata."""
 with db() as c:
  rows=c.execute('SELECT payload FROM imported ORDER BY id DESC LIMIT 100').fetchall()
 samples=[]
 for (payload,) in rows:
  if len(samples)>=limit:break
  try:recipe=json.loads(payload)
  except (TypeError,ValueError):continue
  if not isinstance(recipe,dict) or not allowed_url(recipe.get('url','')):continue
  if not any(recipe_quality.inspect_ingredient(k,v)['status'] in ('needs_source','unit_labeled') for k,v in (recipe.get('ingredients') or {}).items()):continue
  samples.append(recipe)
 output=[]
 for recipe in samples:
  result={'recipe_id':str(recipe.get('id')),'alternative_ingredient_objects':0,'quantity_fields':0,'field_names':[],'page_signals':{},'next_data_inspection':{'parsed':False,'ingredient_key_paths':[],'quantity_key_paths':[],'candidate_objects':0}}
  try:
   req=urllib.request.Request(recipe['url'],headers={'User-Agent':'Mozilla/5.0 (compatible; PersonalRecipeImporter/1.0)'})
   with urllib.request.urlopen(req,timeout=12) as resp:
    if not allowed_url(resp.geturl()):raise ValueError('Unexpected redirect')
    page=resp.read(1500000).decode('utf-8','replace')
   result['page_signals']={
    'next_data':bool(re.search(r'<script[^>]+id=["\\x27]__NEXT_DATA__["\\x27]',page,re.I)),
    'microdata_ingredient':bool(re.search(r'itemprop=["\\x27]recipeIngredient["\\x27]',page,re.I)),
    'data_attributes_ingredient':bool(re.search(r'data-[\\w-]*ingredient[\\w-]*=',page,re.I)),
    'hydration_markers':bool(re.search(r'__NUXT__|__APOLLO_STATE__|__INITIAL_STATE__|self\\.__next_f',page,re.I))}
   match=re.search(r'<script\\b[^>]*id=["\\x27]__NEXT_DATA__["\\x27][^>]*>(.*?)</script>',page,re.I|re.S)
   if match:
    try:
     data=json.loads(html.unescape(match.group(1)))
     info=result['next_data_inspection']
     info['parsed']=True
     ingredient_paths=set()
     quantity_paths=set()
     def inspect_tree(obj,path='',depth=0):
      if depth>16:return
      if isinstance(obj,list):
       for child in obj[:150]:inspect_tree(child,path+'[]',depth+1)
      elif isinstance(obj,dict):
       keys={str(k).lower() for k in obj}
       if any('ingredient' in k for k in keys) and len(ingredient_paths)<20:ingredient_paths.add(path or '$')
       if keys.intersection({'quantity','amount','unit','unitofmeasure','measure','servings','yield'}) and len(quantity_paths)<20:quantity_paths.add(path or '$')
       if any('ingredient' in k for k in keys) and keys.intersection({'quantity','amount','unit','unitofmeasure','measure','value'}):info['candidate_objects']+=1
       for key,value in list(obj.items())[:120]:inspect_tree(value,(path+'.'+str(key))[:160],depth+1)
     inspect_tree(data)
     info['ingredient_key_paths']=sorted(ingredient_paths)
     info['quantity_key_paths']=sorted(quantity_paths)
    except (ValueError,TypeError,RecursionError):result['next_data_inspection']['error']='Unable to parse page data'
  except (ValueError,OSError,TimeoutError) as exc:result['error']=type(exc).__name__
  output.append(result)
  time.sleep(2)
 return {'sample_size':len(output),'results':output,'note':'Next.js field inspection only; no quantities validated or applied'}


def recipe_source_enrichment_worker():
 """Automatically recover original ingredient lines in small, rate-limited batches.
 Never overwrite source recipes or invent missing measurements.
 """
 while True:
  try:
   with db() as c:
    c.execute('CREATE TABLE IF NOT EXISTS hg_recipe_source_cache (recipe_id TEXT PRIMARY KEY,url TEXT NOT NULL,lines_json TEXT NOT NULL,checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    rows=c.execute('SELECT payload FROM imported ORDER BY id DESC').fetchall()
    existing={rid:url for rid,url in c.execute('SELECT recipe_id,url FROM hg_recipe_source_cache')}
    # Selected meals are the priority; all households share verified source data.
    selected_ids=set()
    try:
     for (payload,) in c.execute('SELECT payload FROM hg_household_state'):
      state=json.loads(payload)
      if isinstance(state,dict) and isinstance(state.get('selected'),dict):
       selected_ids.update(str(k) for k in state['selected'])
    except (sqlite3.Error,ValueError,TypeError):pass
    try:
     for (payload,) in c.execute('SELECT payload FROM state'):
      state=json.loads(payload)
      if isinstance(state,dict) and isinstance(state.get('selected'),dict):
       selected_ids.update(str(k) for k in state['selected'])
    except (sqlite3.Error,ValueError,TypeError):pass
   rows.sort(key=lambda row: 0 if _selected_recipe_payload(row[0],selected_ids) else 1)
   processed=0
   for (payload,) in rows:
    if processed>=8:break
    try:recipe=json.loads(payload)
    except (ValueError,TypeError):continue
    if not isinstance(recipe,dict):continue
    url=recipe.get('url')
    rid=str(recipe.get('id',''))
    if not rid or not isinstance(url,str) or not allowed_url(url):continue
    if existing.get(rid)==url:continue
    if isinstance(recipe.get('ingredient_text'),list) and all(recipe_quality.inspect_ingredient(k,v)['status'] not in ('needs_source','unit_labeled') or recipe_quality.recover_from_stored_source(recipe,k) for k,v in (recipe.get('ingredients') or {}).items()):continue
    # Only enrich recipes whose current ingredient labels lack source units.
    ingredients=recipe.get('ingredients')
    if not isinstance(ingredients,dict) or not any(recipe_quality.inspect_ingredient(k,v)['status'] in ('needs_source','unit_labeled') for k,v in ingredients.items()):continue
    processed+=1
    try:
     original=recipe_from_url(url)
     lines=original.get('ingredient_text',[])
     if not isinstance(lines,list):lines=[]
    except (ValueError,TypeError,OSError,TimeoutError) as exc:
     print('Recipe source enrichment unavailable:',rid,type(exc).__name__,flush=True)
     lines=[]
    # Cache both successful and unavailable lookups to avoid repeat requests.
    with db() as c:
     c.execute('INSERT OR REPLACE INTO hg_recipe_source_cache(recipe_id,url,lines_json,checked_at) VALUES (?,?,?,CURRENT_TIMESTAMP)',(rid,url,json.dumps(lines)))
     c.commit()
    time.sleep(2)
  except Exception as exc:
   print('Recipe source enrichment worker:',type(exc).__name__,str(exc)[:160],flush=True)
  time.sleep(1800)


def official_images(recipe):
 url=recipe.get('url','')
 if not url or not allowed_url(url):return []
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html'})
  with urllib.request.urlopen(req,timeout=12) as response:
   if not allowed_url(response.geturl()):return []
   page=response.read(3_000_000).decode('utf-8','replace')
  candidates=[]
  def walk(obj):
   if isinstance(obj,list):
    for item in obj:yield from walk(item)
   elif isinstance(obj,dict):
    if '@graph' in obj:yield from walk(obj['@graph'])
    yield obj
  for block in re.findall(r'<script\b[^>]*type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>',page,re.I|re.S):
   try:
    for node in walk(json.loads(html.unescape(block))):
     types=node.get('@type',[])
     if isinstance(types,str):types=[types]
     if 'Recipe' not in types:continue
     images=node.get('image',[])
     if not isinstance(images,list):images=[images]
     for item in images:
      if isinstance(item,dict):item=item.get('url') or item.get('contentUrl') or ''
      candidates.append(item)
   except (ValueError,TypeError):continue
  for tag in re.findall(r'<meta\b[^>]*>',page,re.I):
   attrs=dict((k.lower(),html.unescape(v)) for k,v in re.findall(r'([\w:-]+)\s*=\s*["\x27]([^"\x27]*)["\x27]',tag))
   if attrs.get('property','').lower() in ('og:image','og:image:secure_url') or attrs.get('name','').lower() in ('twitter:image','twitter:image:src'):
    candidates.append(attrs.get('content',''))
  title=str(recipe.get('name','')).lower()
  words=[w for w in re.findall(r'[a-z]{5,}',title) if w not in ('recipe','hello','fresh','with')]
  for tag in re.findall(r'<img\b[^>]*>',page,re.I):
   attrs=dict((k.lower(),html.unescape(v)) for k,v in re.findall(r'([\w:-]+)\s*=\s*["\x27]([^"\x27]*)["\x27]',tag))
   description=(attrs.get('alt','')+' '+attrs.get('class','')).lower()
   if not ('recipe' in description or 'hero' in description or (words and sum(w in description for w in words)>=min(2,len(words)))):continue
   candidates.extend([attrs.get('src',''),attrs.get('data-src','')])
   candidates.extend(part.strip().split(' ')[0] for part in attrs.get('srcset','').split(','))
  # Embedded application data often carries the actual CDN image URL.
  for raw in re.findall(r'https?:\\?/\\?/[^"\'\s<>]{10,400}',page):
   candidate=raw.replace('\\/','/').replace('\\u002F','/')
   if ('hellofresh_s3/image/' in candidate or '/recipe/' in candidate) and any(host in candidate for host in ('img.hellofresh.com','media.hellofresh.com','images.ctfassets.net')):
    candidates.append(candidate)
  # Favor full recipe hero photos, not the site's social-sharing logo.
  candidates.sort(key=lambda u: (0 if isinstance(u,str) and '/recipes/image/' in u else 1 if isinstance(u,str) and '/hellofresh_s3/image/' in u else 2))
  results=[]
  for img in candidates:
   if not isinstance(img,str):continue
   img=html.unescape(img).replace('\\/','/').strip()
   if allowed_photo_url(img) and len(img)<2048 and img not in results and not re.search(r'(?:logo|placeholder|brandmark|default-image)',urllib.parse.urlsplit(img).path,re.I):results.append(img)
  return results
 except Exception:return []

def official_image(recipe):
 images=official_images(recipe)
 return images[0] if images else ''


PHOTO_DIR=Path('/data/photos')
PHOTO_DIR.mkdir(parents=True,exist_ok=True)
photo_locks={}
photo_locks_guard=threading.Lock()
class SafePhotoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if not allowed_photo_url(newurl):raise ValueError('Untrusted image redirect')
  return super().redirect_request(req,fp,code,msg,headers,newurl)
def allowed_photo_url(url):
 try:
  u=urllib.parse.urlsplit(url)
  host=(u.hostname or '').lower().rstrip('.')
  if u.scheme!='https' or u.username or u.password or u.port not in (None,443):return False
  # Only known image hosts; never fetch arbitrary URLs from the server.
  return host in ('images.ctfassets.net','res.cloudinary.com','img.hellofresh.com','cdn.hellofresh.com','media.hellofresh.com') or host.endswith('.hellofresh.com') or host.endswith('.hellofresh.co.uk') or host.endswith('.hellofresh.com.au') or host.endswith('.hellofresh.co.nz')
 except ValueError:return False
def image_type(raw):
 if raw.startswith(b'\xff\xd8\xff'):return 'image/jpeg','.jpg'
 if raw.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png','.png'
 if raw[:4]==b'RIFF' and raw[8:12]==b'WEBP':return 'image/webp','.webp'
 if raw.startswith((b'GIF87a',b'GIF89a')):return 'image/gif','.gif'
 return None
def cached_photo(rid):
 for ext,kind in (('.jpg','image/jpeg'),('.png','image/png'),('.webp','image/webp'),('.gif','image/gif')):
  p=PHOTO_DIR/(str(rid)+ext)
  if p.is_file():return p,kind
 return None
def download_photo(url,rid):
 if not allowed_photo_url(url):return None
 req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; HelloGroceries/1.0)','Accept':'image/avif,image/webp,image/png,image/jpeg,image/gif'})
 opener=urllib.request.build_opener(SafePhotoRedirect)
 with opener.open(req,timeout=12) as resp:
  if not allowed_photo_url(resp.geturl()):return None
  if int(resp.headers.get('Content-Length') or 0)>8_000_000:return None
  raw=resp.read(8_000_001)
 if len(raw)>8_000_000 or len(raw)<100:return None
 kind=image_type(raw)
 if not kind:return None
 mime,ext=kind
 dest=PHOTO_DIR/(str(rid)+ext)
 tmp=PHOTO_DIR/(str(rid)+'.tmp-'+str(threading.get_ident()))
 try:
  tmp.write_bytes(raw)
  os.replace(tmp,dest)
 finally:
  tmp.unlink(missing_ok=True)
 return dest,mime
def resolve_photo(recipe,refresh=False):
 rid=int(recipe['id'])
 with photo_locks_guard:
  item_lock=photo_locks.setdefault(rid,threading.Lock())
 with item_lock:
  if not refresh:
   cached=cached_photo(rid)
   if cached:return cached
  urls=[]
  existing=recipe.get('image','')
  if existing and not re.search(r'(?:logo|placeholder|brandmark|default-image)',urllib.parse.urlsplit(existing).path,re.I):urls.append(existing)
  # Attempt the known photo first; only fetch the recipe page when it fails.
  for url in urls:
   try:
    result=download_photo(url,rid)
    if result:return result
   except (OSError,ValueError,TimeoutError,urllib.error.URLError):pass
  urls=[url for url in official_images(recipe) if url!=existing]
  # Exact official hero image verified for this historical HelloFresh recipe.
  if '5c670341c445fa54f6615252' in recipe.get('url',''):
   hero='https://media.hellofresh.com/q_100,w_1200,f_auto,c_limit,fl_lossy/recipes/image/2019-w14-r14-one-pan-chicken-sausage-orzo-475ebd8c.jpg'
   urls=[hero]+[url for url in urls if url!=hero]
  for url in urls:
   try:
    result=download_photo(url,rid)
    if result:
     if url!=existing:
      with db() as c:c.execute('INSERT OR REPLACE INTO photos(recipe_id,image_url) VALUES(?,?)',(rid,url))
     return result
   except (OSError,ValueError,TimeoutError,urllib.error.URLError):continue
  return None if refresh else cached_photo(rid)

# Low-rate background warmup for recommendation photos; never blocks a page request.
RECOMMENDATION_PHOTO_CACHE_LIMIT=5*1024**3
RECOMMENDATION_PHOTO_MIN_FREE=2*1024**3
def recommendation_photo_cache_bytes():
 try:return sum(p.stat().st_size for p in PHOTO_DIR.iterdir() if p.is_file())
 except OSError:return 0
def recommendation_photo_cache_has_room():
 try:return recommendation_photo_cache_bytes()<RECOMMENDATION_PHOTO_CACHE_LIMIT and __import__('shutil').disk_usage(PHOTO_DIR).free>RECOMMENDATION_PHOTO_MIN_FREE
 except OSError:return False
recommendation_photo_wakeup=threading.Event()
recommendation_photo_warmup={'last_attempt':None,'cached':0,'checked':0,'errors':0,'running':False,'last_started':None,'last_finished':None,'next_run':None}
def recommendation_photo_warmup_worker():
 import random
 while True:
  recommendation_photo_warmup['running']=True
  recommendation_photo_warmup['last_started']=datetime.datetime.now(datetime.timezone.utc).isoformat()
  try:
   recipes=[r for r in allrecipes() if r.get('image') and not non_recipe_reason(r)]
   random.shuffle(recipes)
   cached=sum(1 for r in recipes if cached_photo(int(r['id'])))
   recommendation_photo_warmup['cached']=cached
   # Keep the warmup deliberately small and low-rate to limit external traffic.
   for recipe in recipes:
    if not recommendation_photo_cache_has_room():break
    if cached_photo(int(recipe['id'])):continue
    recommendation_photo_warmup['last_attempt']=int(recipe['id'])
    recommendation_photo_warmup['checked']+=1
    try:
     if resolve_photo(recipe):cached+=1
    except Exception:
     recommendation_photo_warmup['errors']+=1
    recommendation_photo_warmup['cached']=cached
    time.sleep(15)
  except Exception:
   recommendation_photo_warmup['errors']+=1
  finally:
   recommendation_photo_warmup['running']=False
   recommendation_photo_warmup['last_finished']=datetime.datetime.now(datetime.timezone.utc).isoformat()
   recommendation_photo_warmup['next_run']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat()
  recommendation_photo_wakeup.wait(3600)
  recommendation_photo_wakeup.clear()

# Persistent, low-rate nutrition scan. It resumes after restarts and checks again weekly.
NUTRITION_SCAN_FILE=Path('/data/nutrition_background.json')
nutrition_wakeup=threading.Event()
nutrition_scan={'running':False,'cursor':0,'total':0,'processed':0,'updated':0,'missing':0,'failed':0,'servings_backfilled':0,'last_started':None,'last_finished':None,'next_run':None}
def nutrition_scan_save():
 try:
  tmp=NUTRITION_SCAN_FILE.with_suffix('.tmp')
  tmp.write_text(json.dumps(nutrition_scan));os.replace(tmp,NUTRITION_SCAN_FILE)
 except OSError:pass
def nutrition_scan_worker():
 try:
  saved=json.loads(NUTRITION_SCAN_FILE.read_text())
  for key in nutrition_scan:
   if key in saved and key!='running':nutrition_scan[key]=saved[key]
 except (OSError,ValueError,TypeError):pass
 while True:
  now=datetime.datetime.now(datetime.timezone.utc)
  due=nutrition_scan.get('next_run')
  if due:
   try:wait=max(0,(datetime.datetime.fromisoformat(due)-now).total_seconds())
   except (TypeError,ValueError):wait=0
   if wait>0:
    nutrition_wakeup.wait(min(wait,86400))
    if not nutrition_wakeup.is_set():continue
    nutrition_wakeup.clear()
  nutrition_scan['running']=True
  nutrition_scan['last_started']=now.isoformat()
  nutrition_scan['next_run']=None
  nutrition_scan_save()
  try:
   while True:
    with db() as conn:
     rows=conn.execute('SELECT id,url,payload FROM imported WHERE id>? ORDER BY id LIMIT 3',(nutrition_scan['cursor'],)).fetchall()
     nutrition_scan['total']=conn.execute('SELECT COUNT(*) FROM imported').fetchone()[0]
    if not rows:break
    for rid,url,payload in rows:
     try:
      original=json.loads(payload)
      if not original.get('nutrition') or not original.get('source_servings'):
       fresh=recipe_from_url(url)
       changed=False
       if not original.get('source_servings') and fresh.get('source_servings'):
        original['source_servings']=fresh['source_servings']
        nutrition_scan['servings_backfilled']=nutrition_scan.get('servings_backfilled',0)+1
        changed=True
       if not original.get('nutrition'):
        nutrition=fresh.get('nutrition') or {}
        labels=fresh.get('dietary_labels') or []
        if nutrition or labels:
         original['nutrition']=nutrition
         original['dietary_labels']=labels
         changed=True
       if changed:
        with lock:
         with db() as conn:conn.execute('UPDATE imported SET payload=? WHERE id=?',(json.dumps(original),rid))
        nutrition_scan['updated']+=1
       else:nutrition_scan['missing']+=1
     except Exception:nutrition_scan['failed']+=1
     nutrition_scan['cursor']=rid
     nutrition_scan['processed']+=1
     nutrition_scan_save()
     time.sleep(2)
   nutrition_scan['cursor']=0
   nutrition_scan['processed']=0
   nutrition_scan['last_finished']=datetime.datetime.now(datetime.timezone.utc).isoformat()
   nutrition_scan['next_run']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=7)).isoformat()
  except Exception:
   nutrition_scan['next_run']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat()
  finally:
   nutrition_scan['running']=False
   nutrition_scan_save()

def allowed_url(url):
 u=urllib.parse.urlsplit(url)
 host=(u.hostname or '').lower()
 return u.scheme=='https' and recipe_country_from_url(url)!='other' and not u.username and not u.password

def nutrition_fallback(page):
 # Parse embedded HelloFresh nutrition arrays without inferring missing values.
 fields={'energyKcal':'calories','calories':'calories','protein':'proteinContent','carbohydrate':'carbohydrateContent','fat':'fatContent','fiber':'fiberContent','sugar':'sugarContent','sodium':'sodiumContent','saturatedFat':'saturatedFatContent'}
 output={}
 for match in re.finditer(r'"(?:nutrition|nutritionalValues|nutritionalInformation)"\\s*:\\s*(\\[[^\\]\\n]{0,12000}\\]|\\{[^}\\n]{0,12000}\\})',page,re.I):
  try:data=json.loads(match.group(1))
  except (ValueError,TypeError):continue
  if isinstance(data,dict):data=[dict(name=k,amount=v) for k,v in data.items()]
  if not isinstance(data,list):continue
  for item in data:
   if not isinstance(item,dict):continue
   name=str(item.get('name') or item.get('type') or item.get('key') or '').rsplit('.',1)[-1]
   key=fields.get(name)
   if not key:continue
   value=item.get('amount',item.get('value'))
   unit=item.get('unit','')
   if isinstance(value,dict):value=value.get('amount',value.get('value'))
   if isinstance(value,(int,float,str)) and str(value).strip() and len(str(value))<30:
    output[key]=str(value).strip()+(' '+str(unit).strip() if unit else '')
  if output:return output
 return {}

def recipe_country_from_url(url):
 host=(urllib.parse.urlsplit(url).hostname or '').lower()
 domains={'hellofresh.com':'US','hellofresh.co.uk':'UK','hellofresh.com.au':'AU','hellofresh.ca':'CA','hellofresh.de':'DE','hellofresh.nl':'NL','hellofresh.co.nz':'NZ'}
 for domain,code in domains.items():
  if host==domain or host.endswith('.'+domain):return code
 return 'other'

# One classification policy for both existing catalog entries and new imports.
NON_RECIPE_PATTERNS=(
 r'\b(?:factor(?:75)?|factor form)\b.*\b(?:box|kit|trial|greens|creatine|supplement|ready.made)\b',
 r'\b(?:greens\s*(?:trial|powder|kit)|creatine\s*\+?\s*(?:kit|powder|supplement)|protein\s*(?:powder|shake|supplement)|dietary\s*supplement|supplement\s*(?:kit|pack))\b',
 r'\b(?:ready.made|prepared)\s*meal\s*(?:box|pack)\b',
 r'\b(?:pet(?:s)?\s*(?:food|table)|dog\s*(?:food|treats?)|cat\s*(?:food|treats?)|kibble)\b',
 r'\b(?:air.dried|freeze.dried)\s*(?:beef|chicken|turkey|salmon)\b.*\b(?:box|pack|bag|lb|lbs)\b',
 r'\b(?:variety|assortment|bulk)\s*(?:box|pack)\b',
 r'\b(?:ground|whole.bean|instant)\s+coffee\b.*\b(?:oz|lb|bag|roast|notes?\s+of)\b',
)
def non_recipe_reason(recipe):
 title=str(recipe.get('name','')).lower().strip()
 if not title:return 'Missing recipe title'
 if any(re.search(pattern,title) for pattern in NON_RECIPE_PATTERNS):
  return 'Product or promotional listing'
 if re.search(r'\b\d+\s*(?:lb|lbs|oz|kg)\b',title) and re.search(r'\b(?:box|pack|case|bundle)\b',title):
  return 'Bulk packaged product'
 return None

def validate_import_recipe(recipe):
 """Apply catalog policy at the import boundary; do not delete prior entries."""
 reason=non_recipe_reason(recipe)
 if reason:raise ValueError('Skipped non-recipe listing: '+reason)
 if len(recipe.get('ingredients') or {})<2:
  raise ValueError('Skipped listing without a usable ingredient list')
 return recipe

def recipe_from_url(url):
 if not allowed_url(url):raise ValueError('Only public HTTPS HelloFresh recipe URLs are supported')
 req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; PersonalRecipeImporter/1.0)'})
 with urllib.request.urlopen(req,timeout=12) as resp:
  if not allowed_url(resp.geturl()):raise ValueError('Unexpected redirect')
  raw=resp.read(1500000).decode('utf-8','replace')
 blocks=re.findall(r'<script[^>]+type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>',raw,re.I|re.S)
 def nodes(o):
  if isinstance(o,list):
   for v in o:yield from nodes(v)
  elif isinstance(o,dict):
   if '@graph' in o:yield from nodes(o['@graph'])
   yield o
 found=None
 for block in blocks:
  try:
   for node in nodes(json.loads(html.unescape(block))):
    types=node.get('@type',[]);types=[types] if isinstance(types,str) else types
    if 'Recipe' in types:found=node;break
  except (ValueError,TypeError):pass
  if found:break
 if not found:raise ValueError('This page has no accessible structured recipe data. Try a different public recipe URL.')
 name=str(found.get('name','')).strip()
 if not name:raise ValueError('Recipe title missing')
 photo=found.get('image','');photo=photo[0] if isinstance(photo,list) and photo else photo
 if isinstance(photo,dict):photo=photo.get('url','')
 items=found.get('recipeIngredient',[]) or []
 if isinstance(items,str):items=[items]
 ingredients={}
 for item in items:
  item=str(item).strip()
  if item:ingredients[item]=ingredients.get(item,0)+1
 # Only store nutrition and dietary attributes explicitly supplied by the source.
 nutrition={}
 source_nutrition=found.get('nutrition')
 if isinstance(source_nutrition,dict):
  fields=('calories','proteinContent','carbohydrateContent','fatContent','fiberContent','sugarContent','sodiumContent','saturatedFatContent')
  for key in fields:
   value=source_nutrition.get(key)
   if isinstance(value,(str,int,float)) and 0<len(str(value).strip())<=70:
    nutrition[key]=str(value).strip()
 if not nutrition:nutrition=nutrition_fallback(raw)
 diet_labels=[]
 diet=found.get('suitableForDiet',[])
 if isinstance(diet,str):diet=[diet]
 if isinstance(diet,list):
  allowed={'GlutenFreeDiet':'Gluten-free','VegetarianDiet':'Vegetarian','VeganDiet':'Vegan','LowFatDiet':'Low fat','LowLactoseDiet':'Low lactose','LowSaltDiet':'Low sodium','DiabeticDiet':'Diabetic-friendly'}
  for entry in diet:
   if isinstance(entry,str):
    label=allowed.get(entry.rsplit('/',1)[-1])
    if label and label not in diet_labels:diet_labels.append(label)
 source_yield=found.get('recipeYield',found.get('yield'))
 if isinstance(source_yield,list):source_yield=source_yield[0] if source_yield else None
 serving_match=re.search(r'\d+(?:\.\d+)?',str(source_yield or ''))
 source_servings=float(serving_match.group()) if serving_match else None
 if source_servings is not None and not (0<source_servings<=20):source_servings=None
 return validate_import_recipe({'name':name,'country':recipe_country_from_url(url),'url':url,'image':str(photo),'ingredients':ingredients,'ingredient_text':items,'source':'HelloFresh page','source_servings':source_servings,'instructions_url':url,'nutrition':nutrition,'dietary_labels':diet_labels})

def discover_sitemap(country,limit):
 roots={'US':'https://www.hellofresh.com/sitemap_recipe_pages.xml','UK':'https://www.hellofresh.co.uk/sitemap.xml','AU':'https://www.hellofresh.com.au/sitemap.xml','CA':'https://www.hellofresh.ca/sitemap.xml','DE':'https://www.hellofresh.de/sitemap.xml','NL':'https://www.hellofresh.nl/sitemap.xml'}
 root=roots[country];host=urllib.parse.urlsplit(root).hostname
 rp=urllib.robotparser.RobotFileParser();rp.set_url('https://'+host+'/robots.txt')
 try:rp.read()
 except Exception as exc:raise ValueError('Could not check robots.txt; import aborted: '+str(exc)[:160])
 if not rp.can_fetch('PersonalRecipeImporter/1.0',root):
  raise ValueError('Sitemap access is disallowed by robots.txt for '+host)
 # Prefer sitemap locations published by the site's own robots.txt.
 published=[]
 try:
  req=urllib.request.Request('https://'+host+'/robots.txt',headers={'User-Agent':'PersonalRecipeImporter/1.0'})
  with urllib.request.urlopen(req,timeout=15) as response:
   robots_text=response.read(250000).decode('utf-8','replace')
  for line in robots_text.splitlines():
   if line.strip().lower().startswith('sitemap:'):
    candidate=line.split(':',1)[1].strip()
    if allowed_url(candidate) and recipe_country_from_url(candidate)==country and candidate not in published:published.append(candidate)
 except Exception:pass
 candidates=published+[root,'https://'+host+'/sitemap_index.xml','https://'+host+'/sitemap_recipe_pages.xml','https://'+host+'/sitemap-recipes.xml']
 queue=list(dict.fromkeys(candidate for candidate in candidates if allowed_url(candidate)))
 seen=set();recipes=[];recipe_seen=set();sitemaps=0;errors=[]
 while queue and (limit==0 or len(recipes)<limit) and sitemaps<100:
  url=queue.pop(0)
  if url in seen or not allowed_url(url) or not rp.can_fetch('PersonalRecipeImporter/1.0',url):continue
  seen.add(url);sitemaps+=1
  try:
   req=urllib.request.Request(url,headers={'User-Agent':'PersonalRecipeImporter/1.0','Accept':'application/xml,text/xml,*/*'})
   with urllib.request.urlopen(req,timeout=20) as resp:
    if not allowed_url(resp.geturl()):raise ValueError('Unexpected sitemap redirect')
    content=resp.read(5_000_001)
   if len(content)>5_000_000:raise ValueError('Sitemap exceeds size limit')
   if content[:2]==b'\\x1f\\x8b':
    import gzip
    content=gzip.decompress(content)
    if len(content)>10_000_000:raise ValueError('Uncompressed sitemap exceeds size limit')
   tree=ET.fromstring(content)
   locs=[(e.text or '').strip() for e in tree.iter() if e.tag.endswith('}loc') or e.tag=='loc']
   for loc in locs:
    if not allowed_url(loc):continue
    if recipe_country_from_url(loc)!=country:continue
    parsed=urllib.parse.urlsplit(loc)
    if parsed.path.lower().endswith(('.xml','.xml.gz')) and len(queue)<500:
     if loc not in seen and loc not in queue:queue.append(loc)
    elif re.search(r'/recipes?/',parsed.path,re.I) and loc not in recipe_seen:
     recipe_seen.add(loc);recipes.append(loc)
    if limit and len(recipes)>=limit:break
  except Exception as exc:
   errors.append(url+': '+str(exc)[:150])
  time.sleep(0.5)
 if not recipes:
  detail='; '.join(errors[:3]) if errors else 'No recipe URLs found in accessible sitemaps'
  raise ValueError('No recipe URLs discovered for '+country+'. '+detail)
 return recipes,rp

def bulk_worker(country,limit):
 try:
  urls,rp=discover_sitemap(country,limit)
  job['discovered']=len(urls)
  for url in urls:
   if BULK_PAUSE.is_set():job['message']='Paused';job['paused']=True;save_bulk();return
   if not rp.can_fetch('PersonalRecipeImporter/1.0',url):
    job['skipped']+=1;job['processed']+=1;save_bulk();continue
   try:
    with db() as c:
     if c.execute('SELECT 1 FROM imported WHERE url=?',(url,)).fetchone():
      job['skipped']+=1;job['processed']+=1;save_bulk();continue
    recipe=recipe_from_url(url)
    with lock:
     with db() as c:
      cur=c.execute('INSERT OR IGNORE INTO imported(url,payload) VALUES(?,?)',(url,'{}'))
      if cur.rowcount:
       recipe['id']=1000000+cur.lastrowid
       c.execute('UPDATE imported SET payload=? WHERE url=?',(json.dumps(recipe),url))
       job['imported']+=1
      else:job['skipped']+=1
   except ValueError as exc:
    if str(exc).startswith('Skipped '):job['skipped']+=1
    else:job['errors']+=1
   except Exception:job['errors']+=1
   job['processed']+=1
   save_bulk()
   time.sleep(1.5)
  job['message']='Completed; some sites may not expose importable recipe data'
 except Exception as e:job['message']='Stopped: '+str(e)
 finally:job['running']=False;save_bulk()

def default():return {'selected':{},'list':[],'revision':0}
# Authenticated request household scope is thread-local for ThreadingHTTPServer.
request_scope=threading.local()
def read():
 hid=getattr(request_scope,'household_id',None)
 with db() as c:
  if hid is not None:
   row=c.execute('SELECT payload FROM hg_household_state WHERE household_id=?',(hid,)).fetchone()
  else:
   row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
 return json.loads(row[0]) if row else default()
def pantry_audit(before,after):
 """Record changed household pantry items, never infer historical ownership."""
 user_id=getattr(request_scope,'user_id',None)
 hid=getattr(request_scope,'household_id',None)
 if user_id is None or hid is None:return
 old=set(str(x) for x in before.get('pantry',[]) if isinstance(x,str))
 new=set(str(x) for x in after.get('pantry',[]) if isinstance(x,str))
 changes=[('added',name,None,None) for name in sorted(new-old)]+[('removed',name,None,None) for name in sorted(old-new)]
 old_inv=before.get('pantry_inventory',{}) or {}
 new_inv=after.get('pantry_inventory',{}) or {}
 for key in sorted(set(old_inv)|set(new_inv)):
  previous=old_inv.get(key,0);current=new_inv.get(key,0)
  if previous!=current:
   changes.append(('quantity_changed',key,str(previous),str(current)))
 if not changes:return
 with db() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS hg_pantry_audit (
   id INTEGER PRIMARY KEY AUTOINCREMENT,household_id INTEGER NOT NULL,
   user_id INTEGER NOT NULL,occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
   action TEXT NOT NULL,item TEXT NOT NULL,previous_value TEXT,new_value TEXT)""")
  c.executemany("INSERT INTO hg_pantry_audit(household_id,user_id,action,item,previous_value,new_value) VALUES(?,?,?,?,?,?)",
   [(hid,user_id,action,item,previous,current) for action,item,previous,current in changes])
  c.execute("DELETE FROM hg_pantry_audit WHERE occurred_at < datetime('now','-90 days')")

def save(state):
 hid=getattr(request_scope,'household_id',None)
 with db() as c:
  if hid is not None:
   c.execute('UPDATE hg_household_state SET payload=? WHERE household_id=?',(json.dumps(state),hid))
  else:
   c.execute('INSERT INTO state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(json.dumps(state),))
STORE_CACHE={};STORE_CACHE_LOCK=threading.Lock()
STORE_CACHE_FILE=Path('/data/store_lookup_cache.json')
STORE_CACHE_TTL=21600
STORE_CACHE_VERSION=18
STORE_CACHE_STALE_TTL=30*86400
try:
 raw_cache=json.loads(STORE_CACHE_FILE.read_text())
 if isinstance(raw_cache,dict):
  STORE_CACHE={key:(float(value[0]),value[1]) for key,value in raw_cache.items() if re.fullmatch(r'[0-9]{5}(?:-(?:10|15|20|25))?',key) and isinstance(value,list) and len(value)==2 and isinstance(value[1],dict) and time.time()-float(value[0])<STORE_CACHE_STALE_TTL}
except (OSError,ValueError,TypeError,IndexError):pass

def store_cache_save(cache_key,result):
 result['cache_version']=STORE_CACHE_VERSION
 with STORE_CACHE_LOCK:
  STORE_CACHE[cache_key]=(time.time(),result)
  try:
   tmp=STORE_CACHE_FILE.with_suffix('.tmp')
   tmp.write_text(json.dumps({key:[ts,data] for key,(ts,data) in STORE_CACHE.items() if time.time()-ts<STORE_CACHE_STALE_TTL}))
   os.replace(tmp,STORE_CACHE_FILE)
  except OSError as error:print('Store cache write failed:',repr(error),flush=True)

def store_cache_stale(zipcode):
 with STORE_CACHE_LOCK:cached=STORE_CACHE.get(zipcode)
 if cached and time.time()-cached[0]<STORE_CACHE_STALE_TTL and cached[1].get('cache_version')==STORE_CACHE_VERSION:
  result=dict(cached[1]);result['stale']=True
  result['note']='Showing previously found stores (cached results); live store directories are unavailable.'
  return result
 return None

def store_distance(lat,lon,la,lo):
 return 3958.8*2*math.asin(min(1,math.sqrt(math.sin(math.radians(la-lat)/2)**2+math.cos(math.radians(lat))*math.cos(math.radians(la))*math.sin(math.radians(lo-lon)/2)**2)))

def google_nearby_stores(lat,lon,radius=10):
 # Optional nationwide discovery. Text searches per chain avoid the 20-result
 # combined-nearby cap and return street-level coordinates.
 key=os.environ.get('GOOGLE_PLACES_API_KEY','').strip()
 if not key:return None
 matches=[]
 for chain in ('ALDI','Walmart','Hy-Vee','Price Chopper','Hen House',"Sam's Club",'Costco','Target'):
  payload={'textQuery':chain+' grocery store','pageSize':20,'locationBias':{'circle':{'center':{'latitude':lat,'longitude':lon},'radius':radius*1609.344}}}
  req=urllib.request.Request('https://places.googleapis.com/v1/places:searchText',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Goog-Api-Key':key,'X-Goog-FieldMask':'places.id,places.displayName,places.formattedAddress,places.location'},method='POST')
  try:
   with urllib.request.urlopen(req,timeout=7) as response:body=json.load(response)
   for item in body.get('places',[]):
    name=str((item.get('displayName') or {}).get('text',''))
    if chain.lower() not in name.lower() and not (chain=="Sam's Club" and ('sams club' in name.lower() or 'sam’s club' in name.lower())):continue
    loc=item.get('location') or {}
    if 'latitude' not in loc or 'longitude' not in loc:continue
    distance=store_distance(lat,lon,float(loc['latitude']),float(loc['longitude']))
    if distance>radius:continue
    matches.append({'id':'google-'+str(item.get('id','')),'chain':chain,'name':name,'address':item.get('formattedAddress',''),'distance_miles':round(distance,1)})
  except (OSError,ValueError,TypeError,KeyError) as error:
   print('Google retailer search failed:',chain,repr(error),flush=True)
 return sorted(matches,key=lambda item:item['distance_miles'])


# All the Places publishes public-domain retailer store-locator snapshots.
# Only request individual retailer exports, never the massive full dataset.
ATP_SPIDERS={'aldi_sud_us':'ALDI','walmart_us':'Walmart','hy_vee_us':'Hy-Vee','costco_us':'Costco','sams_club_us':"Sam's Club",'target_us':'Target'}
ATP_CACHE_FILE=Path('/data/retailer_store_directory.json')
ATP_CACHE_TTL=7*86400
ATP_LOCK=threading.Lock()

def retailer_directory():
 with ATP_LOCK:
  saved={}
  try:
   saved=json.loads(ATP_CACHE_FILE.read_text())
   if not isinstance(saved,dict):saved={}
  except (OSError,ValueError,TypeError):pass
  updated=float(saved.get('updated',0) or 0)
  stores=saved.get('stores',[])
  if not isinstance(stores,list):stores=[]
  if stores and time.time()-updated<ATP_CACHE_TTL and set((saved.get('by_spider') or {}).keys())>=set(ATP_SPIDERS):return stores
  all_stores=[]
  success=0
  prior_by_spider=saved.get('by_spider',{}) if isinstance(saved.get('by_spider'),dict) else {}
  refreshed_by_spider={}
  failed_spiders=[]
  for spider,chain in ATP_SPIDERS.items():
   url='https://data.alltheplaces.xyz/runs/latest/output/'+spider+'.geojson'
   try:
    req=urllib.request.Request(url,headers={'User-Agent':'HelloGroceries/1.0 (store directory cache)','Accept':'application/geo+json,application/json,*/*'})
    with urllib.request.urlopen(req,timeout=10) as resp:
     # Individual retailer exports only; bound downloads to avoid exhausting RAM.
     raw=resp.read(12_000_001)
    if len(raw)>12_000_000:raise ValueError('Store export too large')
    parsed=json.loads(raw)
    features=parsed.get('features',[]) if isinstance(parsed,dict) else []
    if not isinstance(features,list):raise ValueError('Invalid retailer export')
    success+=1
    spider_stores=[]
    for item in features:
     if not isinstance(item,dict):continue
     geometry=item.get('geometry') or {}
     coordinates=geometry.get('coordinates') if geometry.get('type')=='Point' else None
     if not isinstance(coordinates,list) or len(coordinates)<2:continue
     try:lon,lat=float(coordinates[0]),float(coordinates[1])
     except (ValueError,TypeError):continue
     if not (-125<=lon<=-66 and 24<=lat<=50):continue
     props=item.get('properties') or {}
     if not isinstance(props,dict):continue
     if props.get('addr:country','US') not in ('US','USA','United States'):continue
     name=str(props.get('name') or chain)[:150]
     address=str(props.get('addr:full') or props.get('addr:street_address') or '')[:180]
     if not address:
      address=' '.join(str(props.get(k,'')) for k in ('addr:housenumber','addr:street')).strip()
     city=str(props.get('addr:city') or '')[:80]
     if city and city.lower() not in address.lower():address=', '.join(filter(None,(address,city)))
     spider_stores.append({'id':'atp-'+spider+'-'+str(item.get('id') or props.get('ref') or len(all_stores))[:110],'chain':chain,'name':name,'address':address,'lat':lat,'lon':lon})
    refreshed_by_spider[spider]=spider_stores
   except (OSError,ValueError,TypeError) as error:
    failed_spiders.append(spider)
    print('Free retailer directory failed:',spider,repr(error),flush=True)
  # Keep each chain's last successful export when its current download fails.
  # Never discard healthy chains merely because another spider is unavailable.
  for spider in ATP_SPIDERS:
   entries=refreshed_by_spider.get(spider)
   if entries is None:
    entries=prior_by_spider.get(spider)
    if not isinstance(entries,list):
     entries=[x for x in stores if isinstance(x,dict) and str(x.get('id','')).startswith('atp-'+spider+'-')]
   all_stores.extend(entries or [])
  if success:
   result={'updated':time.time(),'stores':all_stores,'by_spider':{spider:refreshed_by_spider.get(spider,prior_by_spider.get(spider,[])) for spider in ATP_SPIDERS},'failed_spiders':failed_spiders}
   try:
    tmp=ATP_CACHE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(result))
    os.replace(tmp,ATP_CACHE_FILE)
   except OSError as error:print('Retailer directory cache write failed:',repr(error),flush=True)
   return all_stores
  return all_stores or stores

def retailer_nearby(lat,lon,radius=10):
 directory=retailer_directory()
 if not directory:return None
 matches=[]
 for item in directory:
  try:distance=store_distance(lat,lon,float(item['lat']),float(item['lon']))
  except (ValueError,KeyError,TypeError):continue
  if distance>radius:continue
  matches.append({key:item[key] for key in ('id','chain','name','address')})
  matches[-1]['distance_miles']=round(distance,1)
 return sorted(matches,key=lambda item:item['distance_miles'])[:40]

# Supplement incomplete map datasets with retailer-published location addresses.
# ZIP centroids are approximate; exact street coordinates are not claimed.
RETAILER_PUBLISHED_STORES=[
 ('Price Chopper','109 N Cedar St','Belton','MO','64012'),
 ('Price Chopper','900 W Foxwood Dr','Raymore','MO','64083'),
 ('Price Chopper','501 Schug Ave','Peculiar','MO','64078'),
 ('Price Chopper','12220 S 71 Hwy','Grandview','MO','64030'),
 ('Price Chopper','520 S Commercial St','Harrisonville','MO','64701'),
 ('Price Chopper','937 NE Woods Chapel Rd',"Lee's Summit",'MO','64064'),
 ('Price Chopper','251 SW Greenwich Dr',"Lee's Summit",'MO','64082'),
 ('Price Chopper','8430 Wornall Rd','Kansas City','MO','64114'),
 ('Price Chopper','1030 W 103rd St','Kansas City','MO','64114'),
 ('Price Chopper','3700 W 95th St','Leawood','KS','66206'),
 ('Price Chopper','7418 W 119th St','Overland Park','KS','66213'),
 ('Price Chopper','7000 W 75th St','Overland Park','KS','66204'),
 ('Price Chopper','15970 S Murlen Rd','Olathe','KS','66062'),
 ('Price Chopper','12010 W 63rd St','Shawnee','KS','66216'),
 ('Price Chopper','4950 Roe Blvd','Roeland Park','KS','66205'),
 ('Price Chopper','660 E Main St','Gardner','KS','66030'),
 ('Hen House','15000 W 87th Street Pkwy','Lenexa','KS','66215'),
 ('Hen House','4050 W 83rd St','Prairie Village','KS','66208'),
 ('Hen House','11721 Roe Ave','Leawood','KS','66211'),
 ('Hen House','8120 Parallel Pkwy','Kansas City','KS','66112'),
 ('Hen House','6950 Mission Rd','Prairie Village','KS','66208'),
 ('Hen House','13600 S Blackbob Rd','Olathe','KS','66062'),
 ('Hen House','6900 W 135th St','Overland Park','KS','66223'),
 ('Hen House','11930 College Blvd','Overland Park','KS','66210'),
 ('Hen House','2724 W 53rd St','Fairway','KS','66205'),
 ('Hen House','1904 Southwest Blvd','Jefferson City','MO','65109'),
 ("Sam's Club",'141 N Dean Ave','Raymore','MO','64083'),
 ('Walmart','2015 W Foxwood Dr','Raymore','MO','64083'),
 ('Costco','12221 Blue Valley Pkwy','Overland Park','KS','66213'),
 ("Sam's Club",'13550 W 95th St','Lenexa','KS','66215'),
 ("Sam's Club",'8300 W 135th St','Overland Park','KS','66223')
]
RETAILER_ZIP_CENTROIDS={}
RETAILER_ZIP_LOCK=threading.Lock()
RETAILER_GEOCODE_LOCK=threading.Lock()
RETAILER_GEOCODE_RUNNING=False
RETAILER_GEOCODE_STATS={'processed':0,'matched':0,'failed':0,'finished':False}

def geocode_retailer_addresses():
 global RETAILER_GEOCODE_RUNNING
 with RETAILER_GEOCODE_LOCK:RETAILER_GEOCODE_STATS.update(processed=0,matched=0,failed=0,finished=False)
 path=Path('/data/retailer_address_coordinates.json')
 try:
  try:
   cached=json.loads(path.read_text())
   if not isinstance(cached,dict):cached={}
  except (OSError,ValueError,TypeError):cached={}
  for chain,street,city,state,code in RETAILER_PUBLISHED_STORES:
   key=(street+', '+city+', '+state+' '+code).lower()
   if key in cached:continue
   with RETAILER_GEOCODE_LOCK:RETAILER_GEOCODE_STATS['processed']+=1
   try:
    url='https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress?'+urllib.parse.urlencode({'address':street+', '+city+', '+state+' '+code,'benchmark':'Public_AR_Current','vintage':'Current_Current','format':'json'})
    req=urllib.request.Request(url,headers={'User-Agent':'HelloGroceries/1.0'})
    with urllib.request.urlopen(req,timeout=6) as response:geo=json.load(response)
    candidates=geo.get('result',{}).get('addressMatches',[])
    if candidates:
     point=candidates[0]['coordinates']
     glat,glon=float(point['y']),float(point['x'])
     if -125<=glon<=-66 and 24<=glat<=50:
      cached[key]=[glat,glon]
      tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(cached));os.replace(tmp,path)
      with RETAILER_GEOCODE_LOCK:RETAILER_GEOCODE_STATS['matched']+=1
      with STORE_CACHE_LOCK:STORE_CACHE.clear()
   except (OSError,ValueError,TypeError,KeyError,IndexError) as error:
    with RETAILER_GEOCODE_LOCK:RETAILER_GEOCODE_STATS['failed']+=1
    print('Background store geocoding failed:',repr(error),flush=True)
   time.sleep(1)
 finally:
  with RETAILER_GEOCODE_LOCK:
   RETAILER_GEOCODE_RUNNING=False
   RETAILER_GEOCODE_STATS['finished']=True

def published_retailer_nearby(lat,lon,radius):
 # Location list is independent of the searched ZIP, unlike a ZIP-specific patch.
 with RETAILER_ZIP_LOCK:
  known=dict(RETAILER_ZIP_CENTROIDS)
 zips={item[4] for item in RETAILER_PUBLISHED_STORES if item[4] not in known}
 if zips:
  from concurrent.futures import ThreadPoolExecutor
  def lookup_zip(code):
   try:
    req=urllib.request.Request('https://api.zippopotam.us/us/'+code,headers={'User-Agent':'HelloGroceries/1.0'})
    with urllib.request.urlopen(req,timeout=3) as response:data=json.load(response)
    p=data['places'][0]
    return code,(float(p['latitude']),float(p['longitude']))
   except (OSError,ValueError,KeyError,IndexError,TypeError):return code,None
  with ThreadPoolExecutor(max_workers=8) as pool:
   for code,coords in pool.map(lookup_zip,sorted(zips)):
    if coords:
     with RETAILER_ZIP_LOCK:RETAILER_ZIP_CENTROIDS[code]=coords
 with RETAILER_ZIP_LOCK:known=dict(RETAILER_ZIP_CENTROIDS)
 matches=[]
 coord_file=Path('/data/retailer_address_coordinates.json')
 try:
  coord_cache=json.loads(coord_file.read_text())
  if not isinstance(coord_cache,dict):coord_cache={}
 except (OSError,ValueError,TypeError):coord_cache={}
 # Resolve remaining addresses asynchronously without blocking the store finder.
 global RETAILER_GEOCODE_RUNNING
 with RETAILER_GEOCODE_LOCK:
  if not RETAILER_GEOCODE_RUNNING:
   RETAILER_GEOCODE_RUNNING=True
   threading.Thread(target=geocode_retailer_addresses,daemon=True).start()
 for chain,street,city,state,code in RETAILER_PUBLISHED_STORES:
  if code not in known:continue
  key=(street+', '+city+', '+state+' '+code).lower()
  coords=coord_cache.get(key) or known[code]
  precise=key in coord_cache
  distance=store_distance(lat,lon,*coords)
  # ZIP centroid is an approximation: allow a small margin at the radius boundary.
  if distance>radius+2:continue
  matches.append({'id':'retailer-published-'+re.sub(r'[^a-z0-9]','-',(chain+'-'+street+'-'+code).lower()),'chain':chain,'name':chain,'address':street+', '+city+', '+state+' '+code,'distance_miles':round(distance,1),'distance_approximate':not precise,'outside_radius':distance>radius})
 return matches

def merge_store_results(*groups):
 result=[];seen=set()
 for group in groups:
  for item in group or []:
   if not isinstance(item,dict):continue
   chain=str(item.get('chain','')).strip()
   address=str(item.get('address','')).strip()
   if not chain or not address:continue
   normalized=re.sub(r'[^a-z0-9]','',(chain+' '+address).lower())
   if normalized in seen:continue
   seen.add(normalized);result.append(item)
 return sorted(result,key=lambda x:float(x.get('distance_miles') or 999))[:60]

def nearby_stores(zipcode,radius=10):
 if radius not in (5,10,15,20,25):raise ValueError('Choose a radius of 5, 10, 15, 20, or 25 miles')
 if not re.fullmatch(r'[0-9]{5}',zipcode):raise ValueError('Enter a valid five-digit US ZIP code')
 cache_key=zipcode+'-'+str(radius)
 with STORE_CACHE_LOCK:cached=STORE_CACHE.get(cache_key)
 if cached and time.time()-cached[0]<STORE_CACHE_TTL and cached[1].get('cache_version')==STORE_CACHE_VERSION:return cached[1]
 headers={'User-Agent':'HelloGroceries/1.0 (self-hosted grocery planner)','Accept':'application/json'}
 def getjson(url):
  with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=6) as resp:return json.load(resp)
 try:
  place=getjson('https://nominatim.openstreetmap.org/search?'+urllib.parse.urlencode({'postalcode':zipcode,'country':'USA','format':'json','limit':1}))
  if not place:raise ValueError('ZIP code not found')
  lat=float(place[0]['lat']);lon=float(place[0]['lon'])
 except (OSError,ValueError,KeyError,TypeError,IndexError) as first_error:
  try:
   location=getjson('https://api.zippopotam.us/us/'+zipcode)
   candidate=location['places'][0]
   lat=float(candidate['latitude']);lon=float(candidate['longitude'])
  except (OSError,ValueError,KeyError,IndexError,TypeError) as second_error:
   print('Store ZIP lookup failed:',repr(first_error),'fallback:',repr(second_error),flush=True)
   stale=store_cache_stale(cache_key)
   if stale:return stale
   with STORE_CACHE_LOCK:legacy=STORE_CACHE.get(zipcode)
   if legacy and isinstance(legacy[1],dict) and legacy[1].get('stores'):
    result=dict(legacy[1]);result['stale']=True;result['source']=str(result.get('source','Store directory'))+' (saved fallback)';result['note']='ZIP lookup unavailable; showing previously saved store locations.'
    return result
   raise RuntimeError('ZIP lookup services unavailable') from second_error
 published_matches=published_retailer_nearby(lat,lon,radius)
 retailer_matches=[]
 try:
  retailer_matches=retailer_nearby(lat,lon,radius) or []
 except (OSError,ValueError,TypeError) as error:
  print('Retailer directory lookup failed:',repr(error),flush=True)
 query='[out:json][timeout:10];(nwr["shop"~"supermarket|wholesale|department_store|general|convenience"]["name"~"Hy.Vee|Price Chopper|Hen House|ALDI|Walmart|Sam.s Club|Costco|Target",i](around:'+str(int(radius*1609.344))+','+str(lat)+','+str(lon)+');nwr["shop"~"supermarket|wholesale|department_store|general|convenience"]["brand"~"Hy.Vee|Price Chopper|Hen House|ALDI|Walmart|Sam.s Club|Costco|Target",i](around:'+str(int(radius*1609.344))+','+str(lat)+','+str(lon)+'););out center;'
 payload=urllib.parse.urlencode({'data':query}).encode()
 elements=None
 for endpoint in ('https://overpass.nchc.org.tw/api/interpreter','https://overpass.private.coffee/api/interpreter','https://overpass.kumi.systems/api/interpreter','https://overpass-api.de/api/interpreter'):
  try:
   request=urllib.request.Request(endpoint,data=payload,headers=headers,method='POST')
   with urllib.request.urlopen(request,timeout=8) as resp:candidate=json.load(resp)
   if not isinstance(candidate,dict) or not isinstance(candidate.get('elements'),list):raise ValueError('Invalid directory response')
   elements=candidate['elements'];break
  except (OSError,ValueError) as error:print('Store directory endpoint failed:',endpoint,repr(error),flush=True)
 if elements is not None:
  matches=[]
  for item in elements:
   tags=item.get('tags') or {};name=tags.get('name','');brand=(tags.get('brand','')+' '+name).lower()
   chain=next((x for x in ('Hy-Vee','Price Chopper','Hen House','ALDI','Walmart',"Sam's Club",'Costco','Target') if x.lower() in brand or (x=="Sam's Club" and ('sams club' in brand or 'sam’s club' in brand))),None)
   loc=item.get('center') or item
   if not chain or 'lat' not in loc or 'lon' not in loc:continue
   distance=store_distance(lat,lon,float(loc['lat']),float(loc['lon']))
   if distance>radius:continue
   matches.append({'id':str(item.get('type',''))+'-'+str(item.get('id','')),'chain':chain,'name':name,'address':', '.join(x for x in [tags.get('addr:housenumber','')+' '+tags.get('addr:street','') if tags.get('addr:street') else '',tags.get('addr:city','')] if x.strip()),'distance_miles':round(distance,1)})
  google_matches=google_nearby_stores(lat,lon,radius) or []
  combined=merge_store_results(retailer_matches,published_matches,matches,google_matches)
  result={'zip':zipcode,'radius_miles':radius,'directory_status':{'all_the_places':len(retailer_matches),'openstreetmap':len(matches),'retailer_locations':len(published_matches),'google_places':len(google_matches)},'stores':combined,'source':'All the Places + OpenStreetMap'+(' + Google Places' if google_matches else ''),'note':'Merged store directories; listings may still be incomplete. Prices are not store-specific.'}
  store_cache_save(cache_key,result);return result
 try:
  matches=google_nearby_stores(lat,lon,radius)
  if matches is not None:
   result={'zip':zipcode,'stores':merge_store_results(retailer_matches,published_matches,matches),'source':'All the Places + retailer locations + Google Places','note':'Merged store directories; coverage varies.'}
   store_cache_save(cache_key,result);return result
 except (OSError,ValueError,KeyError,TypeError) as error:print('Independent store directory failed:',repr(error),flush=True)
 if retailer_matches or published_matches:
  result={'zip':zipcode,'radius_miles':radius,'directory_status':{'all_the_places':len(retailer_matches),'openstreetmap':'unavailable','retailer_locations':len(published_matches)},'stores':merge_store_results(retailer_matches,published_matches),'source':'All the Places + retailer locations','note':'Other directories unavailable; results may be incomplete.'}
  store_cache_save(cache_key,result);return result
 stale=store_cache_stale(cache_key)
 if stale:return stale
 with STORE_CACHE_LOCK:legacy=STORE_CACHE.get(zipcode)
 if legacy and isinstance(legacy[1],dict) and legacy[1].get('stores'):
  result=dict(legacy[1]);result['stale']=True;result['source']=str(result.get('source','Store directory'))+' (saved fallback)'
  return result
 raise RuntimeError('Store directory lookup unavailable; configure GOOGLE_PLACES_API_KEY for an independent fallback')

# Read-only public retailer ad preview. A public page is not a location-specific price feed.
PUBLIC_AD_PAGES={
 'ALDI':'https://www.aldi.us/store/aldi/pages/weekly-specials',
 'Hy-Vee':'https://www.hy-vee.com/deals/weekly-ads',
 'Walmart':'https://www.walmart.com/c/kp/weekly-ads-grocery',
}
AD_PREVIEW_CACHE={}
AD_PREVIEW_LOCK=threading.Lock()
# Optional automatic multi-retailer circular feed. Requires operator APIFY_TOKEN.
# Regional circulars are NOT guaranteed to apply to an exact store location.
AUTO_AD_CACHE={}
AUTO_AD_LOCK=threading.Lock()

# Free community price observations; never present these as current store shelf prices.
FREE_PRICE_CACHE={}
FREE_PRICE_LOCK=threading.Lock()
def clean_price_ingredient(item):
 name=str(item).strip()
 name=re.sub(r'^\s*(?:\d+(?:[./]\d+)?|\d+\s+\d+/\d+)\s*(?:(?:tablespoons?|tbsp|teaspoons?|tsp|cups?|ounces?|oz|pounds?|lbs?|grams?|g|kg|ml|liters?|litres?|units?|pieces?|pcs?|cloves?|cans?|packages?|packs?|bunches?|heads?|slices?)\b\.?\s*)?', '',name,flags=re.I)
 name=re.sub(r'^(?:of\s+)', '',name,flags=re.I)
 name=re.sub(r'\s*\([^)]{0,60}\)\s*',' ',name)
 return re.sub(r'\s+',' ',name).strip(' ,.-')[:90] or str(item).strip()[:90]

def free_community_prices(items):
 if not isinstance(items,list) or not 1<=len(items)<=10 or any(not isinstance(x,str) or not 2<=len(x.strip())<=120 for x in items):raise ValueError('Provide 1–10 ingredient names')
 def read_json(url):
  req=urllib.request.Request(url,headers={'User-Agent':'HelloGroceries/1.0 (community pricing research)','Accept':'application/json'})
  for attempt in range(3):
   try:
    with urllib.request.urlopen(req,timeout=10) as response:
     if urllib.parse.urlsplit(response.geturl()).hostname not in ('world.openfoodfacts.org','prices.openfoodfacts.org'):raise ValueError('Unexpected provider redirect')
     raw=response.read(1_000_001)
    if len(raw)>1_000_000:raise ValueError('Response too large')
    return json.loads(raw)
   except urllib.error.HTTPError as exc:
    if exc.code not in (429,500,502,503,504) or attempt==2:raise
    time.sleep(1.5*(attempt+1))
   except (TimeoutError,urllib.error.URLError):
    if attempt==2:raise
    time.sleep(1.5*(attempt+1))
 output=[]
 for item in items:
  cleaned=clean_price_ingredient(item)
  key=cleaned.casefold()
  with FREE_PRICE_LOCK:cached=FREE_PRICE_CACHE.get(key)
  if cached and time.time()-cached[0]<86400:
   output.append(dict(cached[1],item=item));continue
  result={'item':item,'search_term':cleaned,'observations':[],'status':'No matching USD observations'}
  errors=[]
  try:
   url='https://world.openfoodfacts.org/cgi/search.pl?'+urllib.parse.urlencode({'search_terms':cleaned,'search_simple':'1','action':'process','json':'1','page_size':'5','fields':'code,product_name,quantity'})
   products=read_json(url).get('products',[])
   for product in products[:5]:
    code=str(product.get('code',''))
    if not re.fullmatch(r'\d{8,14}',code):continue
    try:
     prices=read_json('https://prices.openfoodfacts.org/api/v1/prices?'+urllib.parse.urlencode({'product_code':code,'page_size':10}))
     for entry in prices.get('items',[])[:10]:
      amount=entry.get('price')
      if not isinstance(amount,(int,float)) or not 0<amount<10000 or entry.get('currency')!='USD':continue
      location=entry.get('location') or {}
      result['observations'].append({'product':str(product.get('product_name') or cleaned)[:120],'quantity':str(product.get('quantity') or '')[:60],'price':round(amount,2),'date':str(entry.get('date') or '')[:10],'location':str(location.get('osm_name') or '')[:120] if isinstance(location,dict) else '', 'code':code})
    except Exception as exc:
     errors.append(type(exc).__name__+': '+str(exc)[:100])
   if result['observations']:result['status']='Community observations found'
   elif errors:result['status']='No observations; price provider errors: '+('; '.join(errors[:2]))[:180]
  except Exception as exc:
   result['status']='Product search failed: '+type(exc).__name__+': '+str(exc)[:140]
  time.sleep(0.25)
  with FREE_PRICE_LOCK:
   if len(FREE_PRICE_CACHE)>2000:FREE_PRICE_CACHE.clear()
   FREE_PRICE_CACHE[key]=(time.time(),result)
  output.append(result)
 return {'results':output,'notice':'Historical community observations, not current prices at your selected store.'}

# Operator-supplied retailer catalog feed. No API key or subscription is required.
# Each row must be a verifiable price from a specific retailer location.
RETAIL_PRICE_PATH=Path('/data/retailer_prices.json')
RESEND_KEY_PATH=Path('/data/resend_api_key')
RESEND_FROM='HelloGroceries <accounts@greenlee.uk>'
def resend_key():
 try:return RESEND_KEY_PATH.read_text().strip() or os.environ.get('RESEND_API_KEY','').strip()
 except OSError:return os.environ.get('RESEND_API_KEY','').strip()
def save_resend_key(key):
 if not isinstance(key,str) or not re.fullmatch(r're_[A-Za-z0-9_-]{12,500}',key):raise ValueError('Enter a valid Resend API key')
 RESEND_KEY_PATH.parent.mkdir(parents=True,exist_ok=True)
 import tempfile
 fd,path=tempfile.mkstemp(prefix='.resend-key-',dir=str(RESEND_KEY_PATH.parent))
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'w') as out:out.write(key)
  os.replace(path,RESEND_KEY_PATH)
 except BaseException:
  try:os.unlink(path)
  except OSError:pass
  raise
def resend_send_email(recipient,subject,body_text):
 recipient=auth.validate_email(recipient)
 key=resend_key()
 if not key:raise ValueError('Save a Resend API key first')
 payload=json.dumps({'from':RESEND_FROM,'to':[recipient],'subject':subject,'text':body_text})
 # Pass the secret through curl stdin, not command arguments, so it never appears in process listings.
 # curl reads the Authorization header from a temporary config passed on stdin.
 import tempfile
 fd,body_path=tempfile.mkstemp(prefix='.resend-body-',dir='/tmp')
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'w') as out:out.write(payload)
  config='header = "Authorization: Bearer '+key+'"'+'\nheader = "Content-Type: application/json"\n'
  result=subprocess.run(['curl','--silent','--show-error','--max-time','20','--config','-','--request','POST','--data-binary','@'+body_path,'--write-out','\\n%{http_code}','https://api.resend.com/emails'],input=config,text=True,capture_output=True,timeout=25)
  if result.returncode!=0:raise ValueError('Resend connection failed. Check server network and try again.')
  response,separator,status=result.stdout.rpartition('\n')
  if not separator or not status.isdigit():raise ValueError('Unexpected response from Resend')
  try:body=json.loads(response)
  except ValueError:body={}
  if 200<=int(status)<300:return bool(isinstance(body,dict) and body.get('id'))
  detail=str(body.get('message') or body.get('error') or '')[:350] if isinstance(body,dict) else ''
  detail=re.sub(r're_[A-Za-z0-9_-]{12,}','[redacted API key]',detail)
  detail=re.sub(r'[\\r\\n\\t]+',' ',detail).strip()
  raise ValueError('Resend rejected the test email (HTTP '+status+')'+(': '+detail if detail else '. Check the key, domain, and recipient.'))
 except subprocess.TimeoutExpired:raise ValueError('Resend request timed out') from None
 finally:
  try:os.unlink(body_path)
  except OSError:pass

def resend_test_email(recipient):
 return resend_send_email(recipient,'HelloGroceries email test','HelloGroceries email delivery is configured. This is a test message sent by an administrator.')

REEF_KEY_PATH=Path('/data/reef_api_key')
OPENAI_KEY_PATH=Path('/data/openai_api_key')
def openai_scan_key():
 try:return OPENAI_KEY_PATH.read_text().strip() or os.environ.get('OPENAI_API_KEY','').strip()
 except OSError:return os.environ.get('OPENAI_API_KEY','').strip()
def save_openai_scan_key(key):
 if not isinstance(key,str) or not 20<=len(key)<=512 or any(c.isspace() for c in key):raise ValueError('Enter a valid OpenAI API key')
 import tempfile
 fd,path=tempfile.mkstemp(prefix='.openai-key-',dir=str(OPENAI_KEY_PATH.parent))
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'w') as out:out.write(key)
  os.replace(path,OPENAI_KEY_PATH)
 except BaseException:
  try:os.unlink(path)
  except OSError:pass
  raise

def reef_key_configured():
 try:return bool(REEF_KEY_PATH.read_text().strip())
 except OSError:return bool(os.environ.get('REEF_API_KEY','').strip())
def save_reef_key(key):
 if not isinstance(key,str) or not 12<=len(key)<=512 or any(c.isspace() for c in key):raise ValueError('Enter a valid API key (12–512 characters, no whitespace)')
 REEF_KEY_PATH.parent.mkdir(parents=True,exist_ok=True)
 import tempfile
 fd,path=tempfile.mkstemp(prefix='.reef-key-',dir=str(REEF_KEY_PATH.parent))
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'w') as out:out.write(key)
  os.replace(path,REEF_KEY_PATH)
 except BaseException:
  try:os.unlink(path)
  except OSError:pass
  raise

def reef_test_search(zipcode,query):
 if not re.fullmatch(r'\d{5}',str(zipcode)):raise ValueError('Enter a five-digit US ZIP code')
 query=str(query).strip()
 if not 2<=len(query)<=90:raise ValueError('Enter a grocery item (2–90 characters)')
 try:key=REEF_KEY_PATH.read_text().strip()
 except OSError:key=os.environ.get('REEF_API_KEY','').strip()
 if not key:raise ValueError('Save a ReefAPI key in Household admin first')
 payload=json.dumps({'query':query,'postal_code':zipcode,'country':'us','max_retailers':20,'per_retailer':5}).encode()
 req=urllib.request.Request('https://api.reefapi.com/instacart/v1/search',data=payload,headers={'x-api-key':key,'Content-Type':'application/json'},method='POST')
 try:
  with urllib.request.urlopen(req,timeout=35) as response:
   raw=response.read(2000000)
  data=json.loads(raw)
 except urllib.error.HTTPError as exc:raise ValueError('ReefAPI HTTP '+str(exc.code)) from None
 except (urllib.error.URLError,TimeoutError) as exc:raise ValueError('ReefAPI connection timed out or failed') from None
 if not isinstance(data,dict) or not data.get('ok'):raise ValueError('ReefAPI did not return a successful search')
 groups=data.get('data',{}).get('groups',[])
 allowed=('price chopper','walmart','wal-mart','wal mart','hy-vee','aldi','sam\'s club','sams-club','costco','target','hen house','hen-house','henhouse')
 result=[];retailer_names=[]
 for group in groups if isinstance(groups,list) else []:
  if not isinstance(group,dict):continue
  retailer=group.get('retailer') or {}
  if not isinstance(retailer,dict):continue
  name=str(retailer.get('name',''))
  if name:retailer_names.append({'name':name[:100],'slug':str(retailer.get('slug',''))[:100]})
  if not any(x in (name+' '+str(retailer.get('slug',''))).casefold() for x in allowed):continue
  products=[]
  for product in (group.get('items') or [])[:5]:
   if not isinstance(product,dict):continue
   try:price=float(product.get('price'))
   except (TypeError,ValueError):continue
   if not 0<price<10000:continue
   products.append({'name':str(product.get('name',''))[:180],'size':str(product.get('size',''))[:80],'price':round(price,2),'price_is_estimate':bool(product.get('price_is_estimate')),'url':str(product.get('url',''))[:500]})
  result.append({'retailer':name[:100],'slug':str(retailer.get('slug',''))[:100],'products':products})
 return {'query':query,'postal_code':zipcode,'retailers':result,'retailer_names':retailer_names[:40],'credits_reported':(data.get('meta') or {}).get('charged_credits'),'note':'Instacart online prices; not in-store shelf prices. Retailers absent from the response are not verified as unsupported.'}

REEF_CACHE_PATH=Path('/data/reef_price_cache.json')
REEF_CACHE_LOCK=threading.Lock()
REEF_CACHE_SECONDS=365*86400
def reef_cache_read():
 try:
  if REEF_CACHE_PATH.stat().st_size>3000000:return {}
  value=json.loads(REEF_CACHE_PATH.read_text())
  return value if isinstance(value,dict) else {}
 except (OSError,ValueError):return {}
def reef_cache_write(data):
 import tempfile
 REEF_CACHE_PATH.parent.mkdir(parents=True,exist_ok=True)
 fd,path=tempfile.mkstemp(prefix='.reef-cache-',dir=str(REEF_CACHE_PATH.parent))
 try:
  with os.fdopen(fd,'w') as out:json.dump(data,out)
  os.replace(path,REEF_CACHE_PATH)
 except BaseException:
  try:os.unlink(path)
  except OSError:pass
  raise
def reef_normalize_item(item):
 value=str(item).strip()
 value=re.sub(r'^\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*', '', value)
 value=re.sub(r'^(?:ounces?|oz|pounds?|lbs?|grams?|g|kg|ml|liters?|l|cups?|tablespoons?|tbsp|teaspoons?|tsp|cloves?|cans?|pieces?|units?|each|packets?|packages?)\b(?:\s*\((?:tsp|tbsp|oz|lb|g|ml)\))?\s*', '', value, flags=re.I)
 value=re.sub(r'^of\s+', '', value, flags=re.I)
 return ' '.join(value.casefold().split()) or str(item).casefold().strip()
def reef_cache_key(zipcode,item):
 return zipcode+'|'+reef_normalize_item(item)
def reef_cached_comparison(zipcode,items):
 if not re.fullmatch(r'\d{5}',str(zipcode)):raise ValueError('Enter a five-digit ZIP code')
 if not isinstance(items,list) or not 1<=len(items)<=60 or any(not isinstance(x,str) or not x.strip() or len(x)>120 for x in items):raise ValueError('Provide 1–60 shopping items')
 now=datetime.datetime.now(datetime.timezone.utc).timestamp()
 with REEF_CACHE_LOCK:data=reef_cache_read()
 chains=['Price Chopper','Walmart','Hy-Vee','ALDI',"Sam's Club",'Costco','Target','Hen House']
 stores={x:{'chain':x,'prices':[],'priced':0,'requested':len(items),'partial_total':0} for x in chains}
 cached=0
 for item in items:
  record=data.get(reef_cache_key(zipcode,item))
  if not isinstance(record,dict) or now-float(record.get('time',0))>REEF_CACHE_SECONDS:continue
  cached+=1
  for chain in chains:
   match=next((x for x in record.get('matches',[]) if x.get('chain')==chain),None)
   if match:
    stores[chain]['prices'].append(dict(match,item=item))
    stores[chain]['priced']+=1
    stores[chain]['partial_total']+=match['price']
 # Diagnostics are derived from cached searches only; never trigger paid lookups here.
 def retailer_seen(chain,retailers):
  aliases={'Walmart':['walmart','wal-mart','wal mart'],'Hen House':['hen house','henhouse','hen-house'],"Sam's Club":["sam's club",'sams club','sams-club','sam’s club']}
  terms=aliases.get(chain,[chain.casefold()])
  return any(any(term in (str(entry.get('name',''))+' '+str(entry.get('slug',''))).casefold() for term in terms) for entry in retailers if isinstance(entry,dict))
 for chain,store in stores.items():
  counts={'not_searched':0,'retailer_not_returned':0,'no_matching_product':0,'price_found':0,'unknown_legacy':0}
  for item in items:
   record=data.get(reef_cache_key(zipcode,item))
   if not isinstance(record,dict) or now-float(record.get('time',0))>REEF_CACHE_SECONDS:
    counts['not_searched']+=1;continue
   if any(m.get('chain')==chain for m in record.get('matches',[]) if isinstance(m,dict)):
    counts['price_found']+=1;continue
   seen=record.get('retailers_seen')
   if not isinstance(seen,list) or not seen:
    counts['unknown_legacy']+=1
   elif retailer_seen(chain,seen):counts['no_matching_product']+=1
   else:counts['retailer_not_returned']+=1
  store['coverage_diagnostics']=counts
 # Aggregate actual retailer identifiers already saved in the cache, without paid API calls.
 retailer_summary={}
 for item in items:
  record=data.get(reef_cache_key(zipcode,item))
  if not isinstance(record,dict) or now-float(record.get('time',0))>REEF_CACHE_SECONDS:continue
  seen=record.get('retailers_seen')
  if not isinstance(seen,list):continue
  for entry in seen:
   if not isinstance(entry,dict):continue
   name=str(entry.get('name','')).strip()[:100]
   slug=str(entry.get('slug','')).strip()[:100]
   if not name and not slug:continue
   key=(name.casefold(),slug.casefold())
   if key not in retailer_summary:retailer_summary[key]={'name':name,'slug':slug,'searches':0}
   retailer_summary[key]['searches']+=1
 for store in stores.values():store['partial_total']=round(store['partial_total'],2)
 return {'stores':list(stores.values()),'cached':cached,'requested':len(items),'missing':len(items)-cached,'cached_items':[item for item in items if isinstance(data.get(reef_cache_key(zipcode,item)),dict) and now-float(data[reef_cache_key(zipcode,item)].get('time',0))<=REEF_CACHE_SECONDS],'unpriced_cached':sum(1 for item in items if isinstance(data.get(reef_cache_key(zipcode,item)),dict) and now-float(data[reef_cache_key(zipcode,item)].get('time',0))<=REEF_CACHE_SECONDS and not data[reef_cache_key(zipcode,item)].get('matches')),'cache_days':365,'retailer_summary':sorted(retailer_summary.values(),key=lambda x:(-x['searches'],x['name']))[:100],'retailer_diagnostics':[{'item':item,'retailers_seen':data[reef_cache_key(zipcode,item)].get('retailers_seen',[])} for item in items if isinstance(data.get(reef_cache_key(zipcode,item)),dict) and data[reef_cache_key(zipcode,item)].get('retailers_seen')][:10],'message':'Instacart online prices. One matching product per ingredient; package sizes and quantities may differ. Partial totals are not directly comparable.'}
def reef_package_unit_price(price,size):
 # Only compare explicitly stated package weights or volumes.
 # Counts, ambiguous sizes and multipacks remain unpriced per unit.
 value=str(size or '').strip().casefold()
 if not value or re.search(r'\b(?:pack|pk|count|ct|each|ea)\b|\d+\s*[x×]\s*\d',value):return None
 match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|fluid\s+ounces?|oz|ounces?|lbs?|pounds?|g|grams?|kg|kilograms?|ml|milliliters?|l|liters?)\.?',value)
 if not match:return None
 amount=float(match.group(1))
 if amount<=0:return None
 unit=match.group(2).replace(' ','').replace('.','')
 if unit in ('floz','fluidounces','fluidounce','ml','milliliter','milliliters','l','liter','liters'):
  divisor=amount*(33.8140227 if unit in ('l','liter','liters') else 0.0338140227 if unit in ('ml','milliliter','milliliters') else 1)
  label='fl oz'
 else:
  divisor=amount*(16 if unit in ('lb','lbs','pound','pounds') else 35.27396195 if unit in ('kg','kilogram','kilograms') else 0.03527396195 if unit in ('g','gram','grams') else 1)
  label='oz'
 if divisor<=0:return None
 return {'amount':round(price/divisor,4),'unit':label}

def reef_refresh_batch(zipcode,items):
 if not re.fullmatch(r'\d{5}',str(zipcode)):raise ValueError('Enter a five-digit ZIP code')
 if not isinstance(items,list) or not 1<=len(items)<=10 or any(not isinstance(x,str) or not 2<=len(x.strip())<=90 for x in items):raise ValueError('Provide 1–10 grocery items')
 unique=list(dict.fromkeys(x.strip() for x in items))
 now=datetime.datetime.now(datetime.timezone.utc).timestamp()
 searched=0;skipped=0;errors=[];details=[];saved_matches=0
 for item in unique:
  key=reef_cache_key(zipcode,item)
  with REEF_CACHE_LOCK:
   cache=reef_cache_read()
   old=cache.get(key)
   if isinstance(old,dict) and now-float(old.get('time',0))<REEF_CACHE_SECONDS:
    skipped+=1;details.append({'item':item,'status':'cached','matches':len(old.get('matches',[]))});continue
  try:result=reef_test_search(zipcode,reef_normalize_item(item))
  except ValueError as exc:
   errors.append({'item':item,'error':str(exc)})
   details.append({'item':item,'status':'error','reason':str(exc)})
   break
  matches=[]
  for group in result['retailers']:
   name=(group['retailer']+' '+str(group.get('slug',''))).casefold()
   chain=next((x for x in ['Price Chopper','Walmart','Hy-Vee','ALDI',"Sam's Club",'Costco','Target','Hen House'] if x.casefold() in name or (x=='Walmart' and ('wal-mart' in name or 'wal mart' in name)) or (x=="Sam's Club" and any(a in name for a in ('sams club','sams-club','sam’s club'))) or (x=='Hen House' and any(a in name for a in ('henhouse','hen-house')))),None)
   if not chain:continue
   products=group['products']
   if not products:continue
   # Prefer a close name match; preserve actual package size rather than normalize unsupported units.
   normalized=reef_normalize_item(item)
   aliases={'spices':'spice','seasonings':'seasoning','tomatoes':'tomato','potatoes':'potato','onions':'onion','lemons':'lemon','limes':'lime','beans':'bean','peppers':'pepper','chillies':'chili','chilies':'chili','chilli':'chili','scallions':'scallion','cucumbers':'cucumber','shallots':'shallot','cloves':'clove'}
   ignored={'and','of','the','fresh','unit','units','tablespoon','tablespoons','teaspoon','teaspoons','cup','cups','ounce','ounces','oz','lb','lbs','pound','pounds'}
   def tokens_for(value):
    return {aliases.get(t,t) for t in re.findall(r'[a-z0-9]+',value.casefold()) if t not in ignored}
   tokens=tokens_for(normalized)
   def rank(p):
    words=tokens_for(p['name'])
    overlap=len(tokens&words)
    # Ingredient identity matters more than the cheapest or shortest product title.
    # Reject broad one-word overlap for multiword ingredients (e.g. stock concentrate).
    coverage=overlap/max(1,len(tokens))
    return (coverage,overlap,-abs(len(words)-len(tokens)),-len(words))
   # Evaluate every returned product, not only the top-ranked candidate:
   # a misleading concentrate/mix must not hide an otherwise valid grocery item.
   incompatible={'concentrate','powder','mix','flavored','flavour','flavor','substitute','imitation'}
   candidates=[p for p in products if tokens and rank(p)[0]>=1.0 and not ((tokens_for(p['name'])&incompatible)-(tokens&incompatible))]
   if not candidates:continue
   best=max(candidates,key=rank)
   matches.append({'chain':chain,'price':best['price'],'unit':best['size'],'unit_price':reef_package_unit_price(best['price'],best['size']),'product':best['name'],'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
  with REEF_CACHE_LOCK:
   cache=reef_cache_read()
   cache[key]={'time':datetime.datetime.now(datetime.timezone.utc).timestamp(),'matches':matches,'retailers_seen':result.get('retailer_names',[])}
   reef_cache_write(cache)
  searched+=1
  saved_matches+=len(matches)
  details.append({'item':item,'status':'priced' if matches else 'no_match','matches':len(matches),'retailers_returned':len(result['retailers']),'products_returned':sum(len(g['products']) for g in result['retailers']),'retailers_seen':result.get('retailer_names',[])})
 return {'searched':searched,'cached_skipped':skipped,'errors':errors,'credits_estimate':searched*2,'batch_limit':10,'saved_matches':saved_matches,'details':details}

def retailer_catalog_prices(chain,store_name,address,items):
 if not isinstance(chain,str) or len(chain)>80 or not isinstance(store_name,str) or len(store_name)>180 or not isinstance(address,str) or len(address)>250:raise ValueError('Invalid store')
 if not isinstance(items,list) or not 1<=len(items)<=60 or any(not isinstance(x,str) or len(x)>120 for x in items):raise ValueError('Invalid ingredient list')
 if not RETAIL_PRICE_PATH.is_file():return {'prices':[],'configured':False,'message':'No retailer catalog feed installed. Place verified prices in /data/retailer_prices.json.'}
 try:
  if RETAIL_PRICE_PATH.stat().st_size>5_000_000:raise ValueError('Retailer feed exceeds 5 MB')
  data=json.loads(RETAIL_PRICE_PATH.read_text())
  rows=data.get('prices',[]) if isinstance(data,dict) else data
  if not isinstance(rows,list):raise ValueError('Invalid retailer feed')
  today=datetime.date.today()
  matched=[]
  for row in rows[:20000]:
   if not isinstance(row,dict):continue
   if str(row.get('chain','')).casefold()!=chain.casefold():continue
   if str(row.get('store_name','')).casefold()!=store_name.casefold() or str(row.get('address','')).casefold()!=address.casefold():continue
   item=str(row.get('item','')).strip()
   if not any(item.casefold()==x.casefold() for x in items):continue
   try:price=float(row.get('price',0))
   except (TypeError,ValueError):continue
   if not 0<price<10000:continue
   date=str(row.get('checked_at',''))[:10]
   try:age=(today-datetime.date.fromisoformat(date)).days
   except ValueError:continue
   if age<0 or age>14:continue
   unit=str(row.get('unit','')).strip()[:60]
   source=str(row.get('source_url','')).strip()
   parsed=urllib.parse.urlsplit(source)
   if not unit or parsed.scheme!='https' or not parsed.hostname:continue
   matched.append({'item':item,'price':round(price,2),'unit':unit,'checked_at':date,'source_url':source,'kind':'Verified retailer catalog feed'})
  return {'prices':matched,'configured':True,'message':str(len(matched))+' recent store-specific catalog prices matched'}
 except (OSError,ValueError,TypeError,json.JSONDecodeError) as exc:return {'prices':[],'configured':False,'message':'Retailer feed could not be loaded: '+str(exc)[:120]}

def compare_retailer_catalog_prices(items):
 if not isinstance(items,list) or not 1<=len(items)<=60 or any(not isinstance(x,str) or not x.strip() or len(x)>120 for x in items):raise ValueError('Provide 1–60 ingredient names')
 chains=['Price Chopper','Walmart','Hy-Vee','ALDI',"Sam's Club",'Costco','Target','Hen House']
 result={'stores':[], 'configured':False}
 if not RETAIL_PRICE_PATH.is_file():
  result['message']='No verified retailer price feed connected yet.'
  return result
 try:
  if RETAIL_PRICE_PATH.stat().st_size>5_000_000:raise ValueError('Feed exceeds 5 MB')
  data=json.loads(RETAIL_PRICE_PATH.read_text())
  rows=data.get('prices',[]) if isinstance(data,dict) else data
  if not isinstance(rows,list):raise ValueError('Invalid feed')
  today=datetime.date.today()
  for chain in chains:
   candidates={}
   for row in rows[:20000]:
    if not isinstance(row,dict) or str(row.get('chain','')).casefold()!=chain.casefold():continue
    name=str(row.get('item','')).strip()
    if name.casefold() not in {x.casefold() for x in items}:continue
    try:price=float(row.get('price',0));date=datetime.date.fromisoformat(str(row.get('checked_at',''))[:10])
    except (ValueError,TypeError):continue
    if not 0<price<10000 or not 0<=(today-date).days<=14:continue
    source=str(row.get('source_url','')).strip()
    parsed=urllib.parse.urlsplit(source)
    if parsed.scheme!='https' or not parsed.hostname or not str(row.get('unit','')).strip():continue
    store_name=str(row.get('store_name','')).strip()[:180]
    address=str(row.get('address','')).strip()[:250]
    if not store_name or not address:continue
    key=(store_name.casefold(),address.casefold())
    if key not in candidates:candidates[key]={'chain':chain,'store_name':store_name,'address':address,'prices':{}}
    prior=candidates[key]['prices'].get(name.casefold())
    if prior is None or str(row.get('checked_at',''))>prior['checked_at']:
     candidates[key]['prices'][name.casefold()]={'item':name,'price':round(price,2),'unit':str(row['unit'])[:60],'checked_at':str(row['checked_at'])[:10],'source_url':source}
   for store in candidates.values():
    values=list(store['prices'].values())
    result['stores'].append({'chain':store['chain'],'store_name':store['store_name'],'address':store['address'],'priced':len(values),'requested':len(items),'partial_total':round(sum(x['price'] for x in values),2),'prices':values})
  result['configured']=True
  result['message']='Prices shown are only from the installed verified feed; partial totals are not full basket estimates.'
 except (OSError,ValueError,TypeError) as exc:result['message']='Retailer feed unavailable: '+str(exc)[:120]
 return result

def automatic_weekly_ads(zipcode):
 if not re.fullmatch(r'\d{5}',zipcode):raise ValueError('Enter a valid five-digit ZIP')
 with AUTO_AD_LOCK:
  cached=AUTO_AD_CACHE.get(zipcode)
  if cached and time.time()-cached[0]<43200:return cached[1]
 base='https://backflipp.wishabi.com/flipp/flyers'
 headers={'User-Agent':'Mozilla/5.0 (compatible; HelloGroceries/1.0)','Accept':'application/json'}
 def get_json(url):
  req=urllib.request.Request(url,headers=headers)
  with urllib.request.urlopen(req,timeout=18) as response:
   if urllib.parse.urlsplit(response.geturl()).hostname!='backflipp.wishabi.com':raise ValueError('Unexpected flyer redirect')
   raw=response.read(4_000_001)
  if len(raw)>4_000_000:raise ValueError('Flyer response too large')
  return json.loads(raw)
 try:
  listing=get_json(base+'?'+urllib.parse.urlencode({'locale':'en-us','postal_code':zipcode}))
  flyers=listing if isinstance(listing,list) else listing.get('flyers',listing.get('data',[]))
  if not isinstance(flyers,list):raise ValueError('Unexpected flyer list')
  targets=('aldi','walmart','hy-vee','hy vee','price chopper','hen house')
  selected=[]
  for flyer in flyers:
   if not isinstance(flyer,dict):continue
   merchant=flyer.get('merchant_name') or flyer.get('merchant') or flyer.get('merchantName') or ''
   if isinstance(merchant,dict):merchant=merchant.get('name','')
   merchant=str(merchant)
   if any(x in merchant.lower() for x in targets) and flyer.get('id'):selected.append((flyer,merchant))
  selected=selected[:20]
  offers=[]
  today=datetime.date.today().isoformat()
  for flyer,merchant in selected:
   try:
    details=get_json(base+'/'+urllib.parse.quote(str(flyer['id']),safe='')+'?'+urllib.parse.urlencode({'locale':'en-us','postal_code':zipcode}))
    items=details if isinstance(details,list) else details.get('items',details.get('flyer_items',[]))
    if not isinstance(items,list):continue
    chain=next((name for name,aliases in (('ALDI',('aldi',)),('Walmart',('walmart',)),('Hy-Vee',('hy-vee','hy vee')),('Price Chopper',('price chopper',)),('Hen House',('hen house',)),("Sam's Club",('sam\'s club','sams club')),('Costco',('costco',)),('Target',('target',))) if any(alias in merchant.lower() for alias in aliases)),None)
    if not chain:continue
    expires=str(flyer.get('valid_to') or flyer.get('end_date') or flyer.get('validTo') or '')[:10]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',expires) or expires<today:continue
    for row in items:
     if not isinstance(row,dict):continue
     item=str(row.get('name') or row.get('title') or row.get('description') or '')[:100].strip()
     price_value=row.get('price') or row.get('sale_price') or row.get('current_price')
     if isinstance(price_value,dict):price_value=price_value.get('value') or price_value.get('amount')
     try:price=float(re.sub(r'[^0-9.]','',str(price_value)))
     except (ValueError,TypeError):continue
     if not item or not 0<price<=10000:continue
     source=str(row.get('url') or flyer.get('url') or 'https://flipp.com/en-us')
     if not source.startswith('https://'):source='https://flipp.com/en-us'
     offers.append({'store':chain,'item':item,'price':price,'unit':str(row.get('unit') or 'advertised item')[:30],'expires':expires,'source':source,'regional':True})
   except (ValueError,KeyError,TimeoutError,OSError,urllib.error.URLError) as error:
    print('Skipping unavailable weekly flyer:',type(error).__name__,flush=True)
  result={'configured':True,'offers':offers[:3000],'message':'Regional advertised offers; verify prices and availability with each store.','checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'chains_found':sorted(set(x['store'] for x in offers)),'flyers_found':len(selected)}
 except Exception as error:
  print('Free weekly flyer retrieval failed:',type(error).__name__,flush=True)
  raise ValueError('Free weekly-ad source unavailable; retry later or consult official ads') from error
 with AUTO_AD_LOCK:AUTO_AD_CACHE[zipcode]=(time.time(),result)
 return result

def retailer_ad_preview(chain):
 if chain not in PUBLIC_AD_PAGES:raise ValueError('No supported public ad page for this retailer')
 with AD_PREVIEW_LOCK:
  cached=AD_PREVIEW_CACHE.get(chain)
  if cached and time.time()-cached[0]<3600:return cached[1]
 url=PUBLIC_AD_PAGES[chain]
 req=urllib.request.Request(url,headers={'User-Agent':'HelloGroceries/1.0 (self-hosted grocery planner)','Accept':'text/html'})
 try:
  with urllib.request.urlopen(req,timeout=9) as response:
   if urllib.parse.urlsplit(response.geturl()).hostname not in ('www.aldi.us','www.hy-vee.com','www.walmart.com'):raise ValueError('Unexpected retailer redirect')
   raw=response.read(2_000_001)
  if len(raw)>2_000_000:raise ValueError('Ad page too large')
  page=raw.decode('utf-8','replace')
  title=re.search(r'<title[^>]*>(.*?)</title>',page,re.I|re.S)
  label=html.unescape(re.sub('<[^>]+>','',title.group(1))).strip()[:180] if title else chain+' weekly ads'
  # Never parse generic product prices as a local discount.
  result={'chain':chain,'url':url,'title':label,'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'verified_store_prices':False,'offers':[],'message':'Official public ad page reachable. Store-specific sale prices and expiration dates could not be verified automatically. Open the ad and select your local store to confirm.'}
 except (OSError,ValueError) as error:
  result={'chain':chain,'url':url,'verified_store_prices':False,'offers':[],'message':'Official ad page could not be checked automatically. Open the retailer ad directly to verify offers.'}
 with AD_PREVIEW_LOCK:AD_PREVIEW_CACHE[chain]=(time.time(),result)
 return result

AUTH_RATE_LOCK=threading.Lock()
AUTH_ATTEMPTS={}
def auth_rate_allowed(ip, route, limit=8, window=900):
 now_ts=time.monotonic()
 key=(ip,route)
 with AUTH_RATE_LOCK:
  if len(AUTH_ATTEMPTS)>10000:
   for k,v in list(AUTH_ATTEMPTS.items()):
    if not v or now_ts-v[-1]>window:AUTH_ATTEMPTS.pop(k,None)
  recent=[t for t in AUTH_ATTEMPTS.get(key,[]) if now_ts-t<window]
  if len(recent)>=limit:
   AUTH_ATTEMPTS[key]=recent
   return False
  recent.append(now_ts)
  AUTH_ATTEMPTS[key]=recent
  return True


def parse_grocery_measure(label):
 """Extract quantity, unit and ingredient from a grocery label."""
 if not isinstance(label,str):return None
 number=r'(?:\d+(?:\.\d+)?|\d+/\d+|[¼½¾⅓⅔⅛⅜⅝⅞])'
 pattern=r'^\s*('+number+r')\s+(?:(teaspoons?|tsp|tablespoons?|tbsp|cups?|ounces?|oz|pounds?|lbs?|grams?|g|kilograms?|kg|milliliters?|ml|liters?|l|cloves?|thumbs?|pieces?|count)\s+)?(.+?)\s*$'
 match=re.match(pattern,label,re.I)
 if not match:return None
 raw,unit,name=match.groups()
 fractions={'¼':.25,'½':.5,'¾':.75,'⅓':1/3,'⅔':2/3,'⅛':.125,'⅜':.375,'⅝':.625,'⅞':.875}
 try:amount=fractions[raw] if raw in fractions else float(raw.split('/')[0])/float(raw.split('/')[1]) if '/' in raw else float(raw)
 except (ValueError,ZeroDivisionError):return None
 units={'teaspoons':'teaspoon','tsp':'teaspoon','tablespoons':'tablespoon','tbsp':'tablespoon','cups':'cup','ounces':'ounce','oz':'ounce','pounds':'pound','lbs':'pound','grams':'gram','g':'gram','kilograms':'kilogram','kg':'kilogram','milliliters':'milliliter','ml':'milliliter','liters':'liter','l':'liter','cloves':'clove','thumbs':'thumb','pieces':'piece'}
 unit=units.get((unit or '').lower(),(unit or 'count').lower())
 return {'amount':amount,'unit':unit,'ingredient':name.strip()}


def grocery_display_name(name):
 """Normalize common source typos without dropping meaningful qualifiers."""
 if not isinstance(name,str):return name
 name=re.sub(r'\bshitake\b','Shiitake',name,flags=re.I)
 name=re.sub(r'\s+',' ',name).strip()
 # In shopping lists, unqualified Pepper means Black Pepper. Keep chili,
 # crushed red pepper, bell pepper, and package-only labels distinct.
 name=re.sub(r'^(?:(teaspoons?|tablespoons?|cups?|ounces?|grams?)\s+)?Pepper$',lambda m:(m.group(1)+' ' if m.group(1) else '')+'Black Pepper',name,flags=re.I)
 # Normalize only known countable produce names; retain meaningful qualifiers.
 # Do not merge uncertain package units (e.g. box Pepper) into measured amounts.
 name=re.sub(r'\b(bell pepper|bell peppers|onion|onions|tomato|tomatoes|lime|limes|lemon|lemons|carrot|carrots|mushroom|mushrooms)\b',
             lambda m:{'bell pepper':'Bell Pepper','bell peppers':'Bell Pepper',
                       'onion':'Onion','onions':'Onion','tomato':'Tomato',
                       'tomatoes':'Tomato','lime':'Limes','limes':'Limes',
                       'lemon':'Lemon','lemons':'Lemon','carrot':'Carrot',
                       'carrots':'Carrot','mushroom':'Mushrooms',
                       'mushrooms':'Mushrooms'}[m.group(0).lower()],name,flags=re.I)
 return name


def rebuild_grocery_list(state):
 totals={}
 sources={}
 canonical_labels={}
 def add_ingredient(label,amount,source):
  label=grocery_display_name(label)
  # Water is a household utility, not a grocery purchase.
  water_name=re.sub(r'^\\s*(?:(?:\\d+(?:\\.\\d+)?|\\d+/\\d+)\\s+)?(?:teaspoons?|tablespoons?|cups?|ounces?|pounds?|milliliters?|liters?|ml|l)\\s+', '', label, flags=re.I).strip().casefold()
  if water_name in ('water','tap water','drinking water'):return
  # Case-only and spelling differences are duplicates, but unit and
  # qualifiers remain part of the key (e.g. herb butter != butter).
  key=label.casefold()
  label=canonical_labels.setdefault(key,label)
  totals[label]=totals.get(label,0)+amount
  sources.setdefault(label,[]).append(source)
 for recipe in allrecipes():
  servings=state['selected'].get(str(recipe['id']))
  if servings is None:continue
  # Scale against the original recipe's declared yield, when available.
  source_servings=recipe.get('source_servings')
  try:baseline=float(source_servings)
  except (TypeError,ValueError):baseline=2.0
  if not math.isfinite(baseline) or baseline<=0:baseline=2.0
  scale=float(servings)/baseline
  ingredients=recipe.get('ingredients')
  if not isinstance(ingredients,dict):continue
  for name,qty in ingredients.items():
   if not isinstance(name,str) or not name.strip():continue
   try:quantity=float(qty)
   except (ValueError,TypeError,OverflowError):continue
   if not math.isfinite(quantity) or quantity<=0:continue
   # Recipe ingredient labels can contain the actual measure while the stored
   # numeric value is only a source factor (often 1). Preserve both separately.
   source_factor=quantity
   embedded=re.match(r'^\s*(\d+(?:\.\d+)?|\d+/\d+|[¼½¾⅓⅔⅛⅜⅝⅞])\s+(teaspoons?|tablespoons?|cups?|ounces?|pounds?|grams?|kilograms?|milliliters?|liters?)\s+(.+)$',name,re.I)
   if embedded:
    raw=embedded.group(1)
    measured={'¼':.25,'½':.5,'¾':.75,'⅓':1/3,'⅔':2/3,'⅛':.125,'⅜':.375,'⅝':.625,'⅞':.875}.get(raw) if raw in '¼½¾⅓⅔⅛⅜⅝⅞' else float(raw.split('/')[0])/float(raw.split('/')[1]) if '/' in raw else float(raw)
    quantity=source_factor*measured
    normalized_unit={'teaspoons':'teaspoon','tablespoons':'tablespoon','cups':'cup','ounces':'ounce','pounds':'pound','grams':'gram','kilograms':'kilogram','milliliters':'milliliter','liters':'liter'}.get(embedded.group(2).lower(),embedded.group(2).lower())
    label_name=normalized_unit+' '+embedded.group(3)
   else:
    label_name=name
   correction=state.get('recipe_ingredient_corrections',{}).get(str(recipe['id'])+'|'+name)
   recovered=None
   if not isinstance(correction,dict) and not embedded:
    recovered=recipe_quality.verified_grocery_recovery(recipe,name,source_factor)
   if isinstance(correction,dict):
    ingredient_name=correction['name']
    unit=correction.get('unit','')
    corrected_amount=float(correction['amount'])
    label='__corrected__|'+unit+'|'+ingredient_name.casefold()
    amount=corrected_amount*float(scale)
   elif recovered:
    label=recovered['unit']+' '+recovered['name']
    amount=recovered['amount']*float(scale)
   else:
    label=label_name
    amount=quantity*float(scale)
   if not (0<amount<100000):continue
   add_ingredient(label,amount,{'recipe_id':recipe['id'],'recipe':recipe['name'],'original_name':name,'corrected':bool(correction),'source_recovered':bool(recovered),'source_line':recovered['source'] if recovered else None,'source_quantity':qty,'multiplier':scale,'servings':servings,'source_servings':baseline,'contribution':round(amount,3)})
 # Do not silently discard recipe ingredients with missing or malformed amounts.
 # Keep them as unverified single entries until a correction supplies a quantity.
 for recipe in allrecipes():
  if str(recipe['id']) not in state.get('selected',{}):continue
  ingredients=recipe.get('ingredients')
  if not isinstance(ingredients,dict):continue
  for name,qty in ingredients.items():
   if not isinstance(name,str) or not name.strip():continue
   try:valid=math.isfinite(float(qty)) and float(qty)>0
   except (ValueError,TypeError,OverflowError):valid=False
   if valid:continue
   label=name.strip()
   add_ingredient(label,1,{'recipe_id':recipe['id'],'recipe':recipe['name'],'original_name':name,'source_quantity':qty,'quantity_unverified':True,'contribution':1})
 for item in state.get('manual',[]):
  if isinstance(item,str) and item.strip():
   name=item.strip()
   add_ingredient(name,1,{'recipe':'Manually added','contribution':1})
 def pantry_match_name(value):
  value=str(value).strip().casefold()
  value=re.sub(r'^(?:\d+(?:[.,]\d+)?|\d+/\d+|[¼½¾⅓⅔])\s+', '', value)
  value=re.sub(r'^(?:tsp|tbsp|teaspoons?|tablespoons?|cups?|oz|ounces?|lbs?|pounds?|grams?|kg|ml|liters?|pieces?|units?|pick|cloves?)\s+', '', value)
  value=re.sub(r'\s+',' ',value).strip()
  value=value.replace('shitake mushroom','shiitake mushroom')
  # Only singularize a simple trailing plural. Preserve ingredient qualifiers
  # (herb butter vs butter) and words such as asparagus or couscous.
  if value.endswith('ies') and len(value)>5:value=value[:-3]+'y'
  elif value.endswith('s') and not value.endswith(('ss','us','is','ous')) and len(value)>3:value=value[:-1]
  return value
 inventory=state.get('pantry_inventory',{})
 pantry={pantry_match_name(x) for x in state.get('pantry',[]) if isinstance(x,str) and x.strip()}
 for key,amount in inventory.items():
  if isinstance(key,str) and type(amount) in (int,float) and math.isfinite(amount) and amount>0:pantry.add(pantry_match_name(key.rsplit('|',1)[0]))
 conversions={'teaspoon':('volume',4.92892159375),'tablespoon':('volume',14.78676478125),'cup':('volume',236.5882365),'milliliter':('volume',1),'liter':('volume',1000),'gram':('mass',1),'kilogram':('mass',1000),'ounce':('mass',28.349523125),'pound':('mass',453.59237)}
 # Keep a shared pool so a pantry amount can never be subtracted twice.
 remaining_inventory={key:float(amount) for key,amount in inventory.items() if isinstance(key,str) and type(amount) in (int,float) and math.isfinite(amount) and 0<=amount<=100000}
 def consume(name,unit,required):
  if required<=0:return 0
  normalized=pantry_match_name(name)
  keys=[k for k in remaining_inventory if '|' in k and pantry_match_name(k.rsplit('|',1)[0])==normalized]
  if unit in conversions:
   keys=[k for k in keys if k.rsplit('|',1)[1] in conversions and conversions[k.rsplit('|',1)[1]][0]==conversions[unit][0]]
  else:
   keys=[k for k in keys if k.rsplit('|',1)[1]==unit]
  needed=required
  for k in sorted(keys):
   stored_unit=k.rsplit('|',1)[1]
   factor=conversions[stored_unit][1]/conversions[unit][1] if unit in conversions else 1
   available=remaining_inventory[k]*factor
   taken=min(needed,available)
   remaining_inventory[k]=max(0,remaining_inventory[k]-taken/factor)
   needed-=taken
   if needed<=0.000001:break
  return max(0,needed)
 corrected_labels=set()
 for key in list(totals):
  if not key.startswith('__corrected__|'):continue
  _,unit,ingredient=key.split('|',2)
  required=totals.pop(key)
  needed=consume(ingredient,unit,required)
  original_sources=sources.pop(key,[])
  if needed<=0.000001:continue
  label=(str(round(needed,3)).rstrip('0').rstrip('.') if needed%1 else str(int(needed)))+' '+(unit+' ' if unit and unit!='count' else '')+ingredient
  totals[label]=totals.get(label,0)+1
  corrected_labels.add(label)
  sources.setdefault(label,[]).extend(original_sources)
 for label in list(totals):
  if label.startswith('__corrected__|') or label in corrected_labels:continue
  match=re.match(r'^(\d+(?:\.\d+)?)\s+(?:(pick|unit|piece|pieces|count|teaspoon|tablespoon|cup|ounce|pound|gram|kilogram|milliliter|liter)\s+)?(.+)$',label,re.I)
  parsed=parse_grocery_measure(label)
  if parsed:
   per_item=parsed['amount']
   unit=parsed['unit']
   ingredient=parsed['ingredient']
  else:
   # Aggregated measured labels store the number in totals.
   measured=re.match(r'^(teaspoon|tablespoon|cup|ounce|pound|gram|kilogram|milliliter|liter)\s+(.+)$',label,re.I)
   if not measured:continue
   per_item=1
   unit=measured.group(1).lower()
   ingredient=measured.group(2)
  needed=consume(ingredient,unit,per_item*totals[label])
  if needed>=per_item*totals[label]-0.000001:continue
  original_sources=sources.pop(label,[])
  totals.pop(label,None)
  if needed<=0.000001:continue
  new_label=(str(round(needed,3)).rstrip('0').rstrip('.') if needed%1 else str(int(needed)))+' '+(unit+' ' if unit!='count' else '')+ingredient
  totals[new_label]=totals.get(new_label,0)+1
  sources.setdefault(new_label,[]).extend(original_sources)
 old={item['name']:item for item in state.get('list',[])}
 generated=[{'name':name,'quantity':round(qty,3),'purchased':False,'sources':sources.get(name,[])} for name,qty in sorted(totals.items()) if qty>0 and (any(src.get('corrected') or src.get('quantity_unverified') for src in sources.get(name,[])) or any(isinstance(k,str) and pantry_match_name(k.rsplit('|',1)[0])==pantry_match_name(name) for k in inventory) or pantry_match_name(name) not in pantry)]
 for item in generated:
  previous=old.get(item['name'])
  if previous and previous.get('quantity')==item['quantity']:item['purchased']=bool(previous.get('purchased'))
 state['list']=generated


class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*a,**kw):super().__init__(*a,directory=str(BASE),**kw)
 def sendjson(self,obj,status=200):
  b=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def identity(self):
  cookie=SimpleCookie()
  try:cookie.load(self.headers.get('Cookie',''))
  except Exception:return None,None
  token=cookie.get('hg_session')
  if not token:return None,None
  with db() as c:
   auth.ensure_schema(c)
   user=auth.session_user(c,token.value)
   if not user:return None,None
   membership=c.execute('SELECT household_id FROM hg_memberships WHERE user_id=? ORDER BY household_id LIMIT 1',(user[0],)).fetchone()
  return user,membership[0] if membership else None
 def gate(self,write=False):
  # Deny every resource until setup/login. No implicit trusted-network bypass.
  with db() as c:
   auth.ensure_schema(c)
   setup=auth.instance_needs_setup(c)
  path=urllib.parse.urlsplit(self.path).path
  if path in ('/auth.html','/api/auth/status','/api/auth/login','/api/auth/setup','/api/auth/register','/api/auth/verify','/api/auth/resend-verification'):
   return True
  user,hid=self.identity()
  if user is None or hid is None:
   if path.startswith('/api/'):
    self.sendjson({'error':'Authentication required','setup_required':setup},401)
   else:
    self.send_response(302);self.send_header('Location','/auth.html');self.send_header('Content-Length','0');self.end_headers()
   return False
  request_scope.household_id=hid
  request_scope.user_id=user[0]
  request_scope.admin=bool(user[2])
  if write:
   origin=self.headers.get('Origin','')
   host=self.headers.get('Host','')
   scheme=urllib.parse.urlsplit(origin).scheme if origin else 'https'
   if origin not in (scheme+'://'+host,):
    self.sendjson({'error':'Invalid request origin'},403);return False
   cookie=SimpleCookie()
   try:cookie.load(self.headers.get('Cookie',''))
   except Exception:pass
   csrf=cookie.get('hg_csrf')
   if not csrf or not secrets.compare_digest(csrf.value,self.headers.get('X-HG-CSRF','')):
    self.sendjson({'error':'CSRF token required'},403);return False
  return True
 def auth_post(self):
  path=urllib.parse.urlsplit(self.path).path
  origin=self.headers.get('Origin','')
  host=self.headers.get('Host','')
  scheme=urllib.parse.urlsplit(origin).scheme if origin else 'https'
  if origin != scheme+'://'+host:return self.sendjson({'error':'Invalid origin'},403)
  size=int(self.headers.get('Content-Length','0'))
  if size<1 or size>8192:return self.sendjson({'error':'Invalid request size'},400)
  if path=='/api/auth/login' and not auth_rate_allowed(self.client_address[0], 'login', 10):return self.sendjson({'error':'Too many attempts. Try again later.'},429)
  if path in ('/api/auth/register','/api/auth/resend-verification') and not auth_rate_allowed(self.client_address[0], 'register', 4):return self.sendjson({'error':'Too many registrations. Try again later.'},429)
  try:
   data=json.loads(self.rfile.read(size))
   with db() as c:
    auth.ensure_schema(c)
    if path in ('/api/auth/register','/api/auth/resend-verification'):
     if auth.instance_needs_setup(c):return self.sendjson({'error':'Administrator setup required'},403)
     if not resend_key():return self.sendjson({'error':'Verification email service is temporarily unavailable. Please try later.'},503)
     email=auth.validate_email(data.get('email'))
     if path=='/api/auth/register':
      existing=c.execute("SELECT status FROM hg_registration_requests WHERE email=?",(email,)).fetchone()
      if not existing or (existing[0]=='approved' and not c.execute("SELECT 1 FROM hg_users WHERE email=?",(email,)).fetchone()):
       auth.request_registration(c,email,data.get('password'),data.get('household'))
     token=auth.issue_registration_verification(c,email)
     c.commit()
     if not token:
      return self.sendjson({'ok':True,'sent':False,'message':'If verification is pending, an email may have been requested recently. Wait 60 seconds and use Resend verification email. Check Spam and Promotions.'},202)
     verification_host='staging-groceries.greenlee.uk' if self.headers.get('Host','').split(':')[0].lower()=='staging-groceries.greenlee.uk' else 'groceries.greenlee.uk'
     link='https://'+verification_host+'/auth.html#verify='+urllib.parse.quote(token,safe='')
     try:
      accepted=resend_send_email(email,'Verify your HelloGroceries email','Welcome to HelloGroceries! Verify your email within 24 hours:\n'+link+'\n\nIf you did not register, ignore this message.')
      if not accepted:raise ValueError('Email provider did not confirm the message')
     except Exception:
      auth.release_failed_verification(c,email,token)
      return self.sendjson({'error':'Verification email could not be sent. Please try Resend verification email. If this keeps happening, contact the administrator.'},502)
     auth.confirm_verification_sent(c,email,token)
     return self.sendjson({'ok':True,'sent':True,'message':'Verification email submitted to our email provider. Check Inbox, Spam and Promotions. Delivery may take a few minutes.'},202)
    if path=='/api/auth/verify':
     auth.activate_verified_registration(c,data.get('token'))
     return self.sendjson({'ok':True,'message':'Email verified! You can now sign in.'})
    if path=='/api/auth/setup':
     verified_email=cloudflare_auth.verified_email(self.headers.get('Cf-Access-Jwt-Assertion',''))
     if not verified_email or verified_email.casefold()!=auth.validate_email(data.get('email')):
      return self.sendjson({'error':'Verified Cloudflare identity must match account email'},403)
     uid,hid=auth.create_first_admin(c,data.get('email'),data.get('password'),data.get('household','My Household'))
    elif path=='/api/auth/login':
     uid=auth.authenticate(c,data.get('email'),data.get('password'))
    else:return self.sendjson({'error':'Not found'},404)
    token=auth.create_session(c,uid)
    c.commit()
   csrf=secrets.token_urlsafe(32)
   payload=json.dumps({'ok':True}).encode()
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
   secure='; Secure' if scheme=='https' else ''
   self.send_header('Set-Cookie','hg_session='+token+'; HttpOnly; SameSite=Lax; Path=/'+secure)
   self.send_header('Set-Cookie','hg_csrf='+csrf+'; SameSite=Lax; Path=/'+secure)
   self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
  except (ValueError,TypeError,KeyError) as exc:self.sendjson({'error':str(exc)},400)
 def logout_post(self):
  if not self.gate(write=True):return
  cookie=SimpleCookie()
  try:cookie.load(self.headers.get('Cookie',''))
  except Exception:pass
  token=cookie.get('hg_session')
  if token:
   with db() as c:
    auth.revoke_session(c,token.value)
    c.commit()
  self.send_response(204)
  self.send_header('Set-Cookie','hg_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0; Secure')
  self.send_header('Set-Cookie','hg_csrf=; SameSite=Lax; Path=/; Max-Age=0; Secure')
  self.send_header('Content-Length','0');self.end_headers()
 def invite_post(self):
  if not self.gate(write=True):return
  if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if not 0<size<=8192:raise ValueError('Invalid request size')
   data=json.loads(self.rfile.read(size))
   with db() as c:
    # Shared invites join the administrator's existing household; private invites
    # still create a separate household as before.
    mode=data.get('mode','shared')
    if mode not in ('shared','private'):raise ValueError('Invalid invitation type')
    household_id=request_scope.household_id if mode=='shared' else None
    token=auth.issue_invite(c,request_scope.user_id,data.get('email'),household_id)
    c.commit()
   return self.sendjson({'invite_url':'https://groceries.greenlee.uk/auth.html#invite='+urllib.parse.quote(token,safe=''),'expires_in_hours':auth.INVITE_HOURS})
  except (ValueError,TypeError,KeyError) as exc:return self.sendjson({'error':str(exc)},400)
 def redeem_post(self):
  origin=self.headers.get('Origin','')
  host=self.headers.get('Host','')
  if origin not in ('https://'+host,'http://'+host):return self.sendjson({'error':'Invalid origin'},403)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if not 0<size<=8192:raise ValueError('Invalid request size')
   data=json.loads(self.rfile.read(size))
   with db() as c:
    auth.ensure_schema(c)
    uid,hid=auth.redeem_invite(c,data.get('token'),data.get('email'),data.get('password'),data.get('household'))
    session=auth.create_session(c,uid)
    c.commit()
   csrf=secrets.token_urlsafe(32)
   payload=b'{"ok":true}'
   self.send_response(200)
   self.send_header('Content-Type','application/json')
   self.send_header('Cache-Control','no-store')
   secure='; Secure' if origin.startswith('https://') else ''
   self.send_header('Set-Cookie','hg_session='+session+'; HttpOnly; SameSite=Lax; Path=/'+secure)
   self.send_header('Set-Cookie','hg_csrf='+csrf+'; SameSite=Lax; Path=/'+secure)
   self.send_header('Content-Length',str(len(payload)))
   self.end_headers();self.wfile.write(payload)
  except (ValueError,TypeError,KeyError) as exc:return self.sendjson({'error':str(exc)},400)
 def do_GET(self):
  request_scope.household_id=None
  if self.path=='/api/household':
   if not self.gate():return
   with db() as c:
    auth.ensure_schema(c)
    result=sharing.details(c,request_scope.user_id,request_scope.household_id)
   return self.sendjson(result)
  if self.path=='/api/pricing-visibility':
   if not self.gate():return
   return self.sendjson({'visibility':pricing_visibility(),'is_admin':bool(getattr(request_scope,'admin',False))})
  if self.path=='/api/admin/recipe-alternative-source-probe':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson(inspect_alternative_recipe_fields())
  if self.path=='/api/admin/recipe-source-sample':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson(recipe_source_sample_diagnostic())
  if self.path=='/api/admin/recipe-source-status':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson(recipe_source_enrichment_status())
  if self.path=='/api/admin/recipe-quality':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   recipes=allrecipes()
   selected_ids=set()
   with db() as c:
    try:
     for (payload,) in c.execute('SELECT payload FROM hg_household_state'):
      state=json.loads(payload)
      if isinstance(state,dict) and isinstance(state.get('selected'),dict):
       selected_ids.update(str(k) for k in state['selected'])
    except (sqlite3.Error,ValueError,TypeError):pass
    try:
     for (payload,) in c.execute('SELECT payload FROM state'):
      state=json.loads(payload)
      if isinstance(state,dict) and isinstance(state.get('selected'),dict):
       selected_ids.update(str(k) for k in state['selected'])
    except (sqlite3.Error,ValueError,TypeError):pass
   recipes.sort(key=lambda recipe: 0 if str(recipe.get('id')) in selected_ids else 1)
   result=recipe_quality.audit_catalog(recipes)
   result['selected_recipes']=len(selected_ids)
   return self.sendjson(result)
  if self.path=='/api/admin/pantry-activity':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS hg_pantry_audit (
     id INTEGER PRIMARY KEY AUTOINCREMENT,household_id INTEGER NOT NULL,
     user_id INTEGER NOT NULL,occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
     action TEXT NOT NULL,item TEXT NOT NULL,previous_value TEXT,new_value TEXT)""")
    rows=c.execute("""SELECT a.occurred_at,u.email,h.name,a.action,a.item,a.previous_value,a.new_value
     FROM hg_pantry_audit a LEFT JOIN hg_users u ON u.id=a.user_id
     LEFT JOIN hg_households h ON h.id=a.household_id
     ORDER BY a.id DESC LIMIT 100""").fetchall()
   return self.sendjson({'entries':[{'at':x[0],'user':x[1] or 'Deleted account','household':x[2] or 'Unknown','action':x[3],'item':x[4],'before':x[5],'after':x[6]} for x in rows]})
  if self.path=='/api/admin/ai-usage':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson(scan_usage_summary())
  if self.path=='/api/admin/analytics':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson(analytics_summary())
  if self.path=='/api/admin/recipe-photos':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
   offset=max(0,min(100000,int(q.get('offset',['0'])[0])))
   items=[{'id':r['id'],'name':r.get('name',''),'image_url':r.get('image',''),'recipe_url':r.get('url',''),'cached':bool(cached_photo(int(r['id'])))} for r in allrecipes() if isinstance(r.get('id'),int)]
   missing=[r for r in items if not r['cached']]
   return self.sendjson({'total':len(missing),'items':missing[offset:offset+50]})
  if self.path=='/api/admin/tasks':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson({'tasks':task_overview()})
  if self.path.startswith('/api/reef-cached-prices?'):
   if not self.gate():return
   q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
   try:return self.sendjson(reef_cached_comparison(q.get('zip',[''])[0],q.get('item',[])))
   except ValueError as exc:return self.sendjson({'error':str(exc)},400)
  if self.path.startswith('/api/reef-test-search?'):
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
   try:return self.sendjson(reef_test_search(q.get('zip',[''])[0],q.get('item',[''])[0]))
   except ValueError as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/admin/nutrition/run':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   if nutrition_scan['running']:return self.sendjson({'ok':True,'message':'Nutrition scan is already running.'})
   nutrition_wakeup.set()
   return self.sendjson({'ok':True,'message':'Nutrition scan queued.'})
  if self.path=='/api/admin/recommendation-photos/run':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   if recommendation_photo_warmup['running']:
    return self.sendjson({'ok':True,'message':'Photo caching is already running.'})
   recommendation_photo_wakeup.set()
   return self.sendjson({'ok':True,'message':'Photo cache check queued.'})
  if self.path=='/api/admin/recipe-photos/repair':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=2048:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    rid=data.get('id')
    if type(rid)!=int:raise ValueError('Invalid recipe ID')
    recipe=next((r for r in allrecipes() if r.get('id')==rid),None)
    if not recipe:raise ValueError('Recipe not found')
    url=str(data.get('url','')).strip()
    if url and not allowed_photo_url(url):raise ValueError('Photo URL must use an approved HTTPS image host')
    with photo_locks_guard:item_lock=photo_locks.setdefault(rid,threading.Lock())
    with item_lock:
     result=download_photo(url,rid) if url else None
    if not url:result=resolve_photo(recipe,refresh=True)
    if not result:return self.sendjson({'error':'Could not retrieve a valid photo. Try a supported direct image URL.'},422)
    return self.sendjson({'ok':True,'id':rid})
   except (ValueError,TypeError,OSError,urllib.error.URLError,TimeoutError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/admin/openai-key':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson({'configured':bool(openai_scan_key())})
  if self.path=='/api/admin/resend-key':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson({'configured':bool(resend_key()),'from':RESEND_FROM,'registration_mode':'email_verification'})
  if self.path=='/api/admin/reef-key':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   return self.sendjson({'configured':reef_key_configured()})
  if self.path=='/api/auth/session-settings':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   with db() as c:
    auth.ensure_schema(c)
    return self.sendjson({'days':auth.session_duration_days(c),'options':list(auth.SESSION_DAY_OPTIONS)})
  if self.path=='/api/auth/blocked-emails':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   with db() as c:
    auth.ensure_schema(c)
    return self.sendjson({'blocked':auth.blocked_emails(c,request_scope.user_id)})
  if self.path=='/api/auth/accounts':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   with db() as c:
    auth.ensure_schema(c)
    return self.sendjson({'accounts':auth.list_accounts(c,request_scope.user_id)})
  if self.path=='/api/auth/registrations':
   if not self.gate():return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   with db() as c:
    auth.ensure_schema(c)
    return self.sendjson({'requests':auth.pending_registrations(c)})
  if self.path=='/api/auth/status':
   with db() as c:
    user,hid=self.identity()
    return self.sendjson({'setup_required':auth.instance_needs_setup(c),'authenticated':user is not None,'admin':bool(user and user[2]),'email':user[1] if user else None})
  if not self.gate():return
  if self.path.startswith('/api/photo/'):
   try:
    parsed=urllib.parse.urlsplit(self.path)
    rid=int(parsed.path.rsplit('/',1)[1])
    recipe=next((r for r in allrecipes() if r['id']==rid),None)
    if not recipe:return self.send_error(404)
    refresh=urllib.parse.parse_qs(parsed.query).get('refresh',['0'])[0]=='1'
    result=resolve_photo(recipe,refresh)
    if not result:return self.send_error(404)
    file,mime=result
    payload=file.read_bytes()
    self.send_response(200);self.send_header('Content-Type',mime)
    self.send_header('Cache-Control','private, max-age=3600')
    self.send_header('Content-Length',str(len(payload)))
    self.end_headers();self.wfile.write(payload);return
   except (ValueError,TypeError,OSError):return self.send_error(404)
  if self.path.startswith('/api/compare-retailer-prices?'):
   q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
   try:return self.sendjson(compare_retailer_catalog_prices(q.get('item',[])))
   except ValueError as error:return self.sendjson({'error':str(error)},400)
  if self.path.startswith('/api/retailer-catalog-prices?'):
   q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
   try:return self.sendjson(retailer_catalog_prices(q.get('chain',[''])[0],q.get('store_name',[''])[0],q.get('address',[''])[0],q.get('item',[])))
   except ValueError as error:return self.sendjson({'error':str(error)},400)
  if self.path.startswith('/api/free-community-prices?'):
   items=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('item',[])
   try:return self.sendjson(free_community_prices(items))
   except ValueError as error:return self.sendjson({'error':str(error)},400)
  if self.path.startswith('/api/automatic-weekly-ads?'):
   zipcode=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('zip',[''])[0]
   try:return self.sendjson(automatic_weekly_ads(zipcode))
   except ValueError as error:return self.sendjson({'error':str(error)},400)
  if self.path.startswith('/api/retailer-ad?'):
   chain=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('chain',[''])[0]
   try:return self.sendjson(retailer_ad_preview(chain))
   except ValueError as e:return self.sendjson({'error':str(e)},400)
  if self.path.startswith('/api/stores/geocode-status'):
   with RETAILER_GEOCODE_LOCK:status=dict(RETAILER_GEOCODE_STATS, running=RETAILER_GEOCODE_RUNNING)
   try:
    saved=json.loads(Path('/data/retailer_address_coordinates.json').read_text())
    if not isinstance(saved,dict):saved={}
   except (OSError,ValueError,TypeError):saved={}
   total=len(RETAILER_PUBLISHED_STORES)
   matched=sum(1 for chain,street,city,state,code in RETAILER_PUBLISHED_STORES if (street+', '+city+', '+state+' '+code).lower() in saved)
   status.update(total_addresses=total,cached_coordinates=matched,remaining_estimates=total-matched)
   return self.sendjson(status)
  if self.path.startswith('/api/stores?'):
   try:
    params=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
    zipcode=params.get('zip',[''])[0]
    radius=int(params.get('radius',['10'])[0])
    return self.sendjson(nearby_stores(zipcode,radius))
   except ValueError as e:return self.sendjson({'error':str(e)},400)
   except Exception as error:
    print('Store lookup failed:',repr(error),flush=True)
    return self.sendjson({'error':'Store lookup could not reach the directory services. Check the HelloGroceries container logs for DNS or network errors.'},503)
  if self.path=='/api/recommendation-photos':
   # Cached-only lookup: never fetch external images on a page request.
   cached_ids=[]
   for photo in PHOTO_DIR.iterdir():
    if photo.suffix.lower() in ('.jpg','.png','.webp','.gif') and photo.stem.isdigit() and photo.is_file() and photo.stat().st_size>0:
     cached_ids.append(int(photo.stem))
   return self.sendjson({'ids':cached_ids})
  if self.path=='/api/recipes':return self.sendjson([r for r in allrecipes() if not non_recipe_reason(r)])
  if self.path=='/api/state':
   with lock:
    state=read()
    return self.sendjson(state)
  if self.path=='/api/backup':
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   with lock:
    with db() as conn:
     data={'version':1,'exported_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'state':read(),'imported':[dict(url=u,payload=json.loads(v)) for u,v in conn.execute('SELECT url,payload FROM imported')],'photos':[dict(recipe_id=i,image_url=u) for i,u in conn.execute('SELECT recipe_id,image_url FROM photos')]}
   raw=json.dumps(data).encode()
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Disposition','attachment; filename="hellogroceries-backup.json"');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  if self.path=='/api/bulk-status':
   countries=bulk_country_history()
   us_progress=Path('/data/us_import_progress.json')
   if us_progress.is_file():
    try:
     u=json.loads(us_progress.read_text())
     if isinstance(u,dict):
      countries['US']={'country':'US','discovered':u.get('discovered',0),'processed':u.get('processed',0),'imported':u.get('imported_this_run',0),'skipped':u.get('skipped',0),'errors':u.get('errors_this_run',0),'message':u.get('status','Idle'),'updated_at':us_progress.stat().st_mtime,'source':'us'}
    except (OSError,ValueError):pass
   return self.sendjson(dict(job,countries=countries,us_running=us_import_running() or (import_process is not None and import_process.poll() is None)))
  if self.path=='/api/us-import-control-status':return self.sendjson({'running':us_import_running() or (import_process is not None and import_process.poll() is None),'message':'Importer running' if us_import_running() or (import_process is not None and import_process.poll() is None) else 'Importer idle — ready to start / resume'})
  if self.path=='/api/us-import-status':
   progress=Path('/data/us_import_progress.json')
   if not progress.is_file():return self.sendjson({'available':False,'message':'No US import progress file yet'})
   try:
    result=json.loads(progress.read_text())
    if not isinstance(result,dict):raise ValueError('Invalid progress file')
    result['available']=True
    result['updated_at']=progress.stat().st_mtime
    return self.sendjson(result)
   except (OSError,ValueError,json.JSONDecodeError) as exc:
    return self.sendjson({'available':False,'message':'Progress temporarily unavailable'})
  if self.path.startswith('/api/'):return self.sendjson({'error':'Not found'},404)
  # Never serve application source, configuration, database or other internal files.
  # SimpleHTTPRequestHandler otherwise exposes everything inside /app to logged-in users.
  static_path=urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
  if static_path=='/':static_path='/index.html'
  allowed_pages={'/index.html','/auth.html','/admin.html','/household.html'}
  allowed_extensions={'.css','.js','.png','.jpg','.jpeg','.webp','.gif','.svg','.ico','.woff','.woff2'}
  if (static_path not in allowed_pages and
      (not static_path.startswith('/assets/') or Path(static_path).suffix.lower() not in allowed_extensions)):
   return self.send_error(404)
  return super().do_GET()
 def end_headers(self):
  self.send_header('X-Content-Type-Options','nosniff')
  self.send_header('X-Frame-Options','DENY')
  self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
  self.send_header('Permissions-Policy','geolocation=(), microphone=(), payment=()')
  # Static HTML/JS must be revalidated after a container image update.
  path=urllib.parse.urlsplit(self.path).path
  if path in ('/','/index.html','/auth.html') or path.endswith(('.js','.css')):
   self.send_header('Cache-Control','no-cache, must-revalidate')
  super().end_headers()
 def do_POST(self):
  request_scope.household_id=None
  if self.path in ('/api/household/invite','/api/household/respond'):
   if not self.gate(write=True):return
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=4096:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    with db() as c:
     auth.ensure_schema(c)
     if self.path.endswith('/invite'):
      sharing.invite(c,request_scope.user_id,request_scope.household_id,auth.validate_email(data.get('email')))
      c.commit()
     else:
      if data.get('decision') not in ('accept','decline'):raise ValueError('Invalid response')
      sharing.respond(c,request_scope.user_id,int(data.get('id')),data['decision']=='accept')
   except (ValueError,TypeError,KeyError,sqlite3.Error) as exc:return self.sendjson({'error':str(exc)},400)
   except PermissionError as exc:return self.sendjson({'error':str(exc)},403)
   return self.sendjson({'ok':True})
  if self.path=='/api/admin/ai-settings':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=1024:raise ValueError('Invalid settings')
    data=json.loads(self.rfile.read(size))
    budget=float(data['monthly_budget_usd'])
    scans=int(data['user_monthly_scans'])
    if not math.isfinite(budget) or budget<0 or budget>100000 or scans<0 or scans>100000:raise ValueError('Invalid limits')
    with db() as c:
     c.execute('CREATE TABLE IF NOT EXISTS admin_preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
     c.executemany('INSERT INTO admin_preferences(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',[('ai_monthly_budget_usd',str(budget)),('ai_user_monthly_scans',str(scans))])
     c.commit()
    return self.sendjson({'ok':True,'limits':ai_scan_limits()})
   except (ValueError,KeyError,TypeError,sqlite3.Error):return self.sendjson({'error':'Invalid AI scan limits'},400)
  if self.path.startswith('/api/pantry/barcode?'):
   if not self.gate():return
   if not auth_rate_allowed(str(request_scope.user_id),'pantry-barcode',40,3600):return self.sendjson({'error':'Too many barcode lookups.'},429)
   barcode=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('code',[''])[0].strip()
   if not re.fullmatch(r'[0-9]{8,14}',barcode):return self.sendjson({'error':'Enter an 8 to 14 digit barcode.'},400)
   with db() as conn:
    conn.execute('CREATE TABLE IF NOT EXISTS barcode_products (household_id TEXT NOT NULL, barcode TEXT NOT NULL, name TEXT NOT NULL, size TEXT NOT NULL DEFAULT "", PRIMARY KEY(household_id,barcode))')
    saved=conn.execute('SELECT name,size FROM barcode_products WHERE household_id=? AND barcode=?',(str(request_scope.household_id),barcode)).fetchone()
   if saved:return self.sendjson({'found':True,'barcode':barcode,'item':{'name':saved[0],'quantity':1,'unit':'count','package_size':saved[1],'source':'Household saved product'}})
   catalog_error=False
   for host in ('world.openfoodfacts.org','world.openproductsfacts.org','world.openbeautyfacts.org'):
    try:
     url='https://'+host+'/api/v2/product/'+barcode+'.json?fields=code,product_name,brands,quantity'
     req=urllib.request.Request(url,headers={'User-Agent':'HelloGroceries/1.0 (grocery pantry scanner)','Accept':'application/json'})
     with urllib.request.urlopen(req,timeout=7) as response:data=json.load(response)
     product=data.get('product') or {}
     if data.get('status')!=1:continue
     brand=str(product.get('brands') or '').split(',')[0].strip()[:65]
     name=str(product.get('product_name') or '').strip()[:100]
     size=str(product.get('quantity') or '').strip()[:32]
     if not name:continue
     if brand and not name.lower().startswith(brand.lower()):name=brand+' '+name
     if size and size.lower() not in name.lower():name+=' '+size
     return self.sendjson({'found':True,'barcode':barcode,'item':{'name':name[:140],'quantity':1,'unit':'count','brand':brand,'package_size':size,'source':host}})
    except (urllib.error.URLError,TimeoutError,ValueError,TypeError,KeyError):
     catalog_error=True
   return self.sendjson({'found':False,'barcode':barcode,'catalog_unavailable':catalog_error})
  if self.path=='/api/pantry/barcode/save':
   if not self.gate(write=True):return
   try:
    length=int(self.headers.get('Content-Length','0'))
    if not 0<length<=2048:raise ValueError('Invalid request')
    body=json.loads(self.rfile.read(length))
    barcode=str(body.get('barcode','')).strip()
    name=str(body.get('name','')).strip()
    size=str(body.get('size','')).strip()
    if not re.fullmatch(r'[0-9]{8,14}',barcode) or not 1<=len(name)<=140 or len(size)>32:raise ValueError('Invalid barcode or product')
    with db() as conn:
     conn.execute('CREATE TABLE IF NOT EXISTS barcode_products (household_id TEXT NOT NULL, barcode TEXT NOT NULL, name TEXT NOT NULL, size TEXT NOT NULL DEFAULT "", PRIMARY KEY(household_id,barcode))')
     conn.execute('INSERT INTO barcode_products(household_id,barcode,name,size) VALUES(?,?,?,?) ON CONFLICT(household_id,barcode) DO UPDATE SET name=excluded.name,size=excluded.size',(str(request_scope.household_id),barcode,name,size))
    return self.sendjson({'ok':True})
   except (ValueError,KeyError,TypeError,sqlite3.Error):return self.sendjson({'error':'Could not save product'},400)
  if self.path=='/api/pantry/scan':
   if not self.gate(write=True):return
   if not auth_rate_allowed(str(request_scope.user_id),'pantry-scan',15,3600):
    return self.sendjson({'error':'Scan limit reached. Try again later.'},429)
   denied=ai_scan_allowed(request_scope.user_id)
   if denied:return self.sendjson({'error':denied},429)
   key=openai_scan_key()
   if not key:return self.sendjson({'error':'AI scanning is not configured. Set OPENAI_API_KEY on the server.'},503)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=8500000:raise ValueError('Photo too large (maximum 6 MB)')
    data=json.loads(self.rfile.read(size))
    mime=data.get('mime','')
    photo=data.get('image','')
    mode=data.get('mode','groceries')
    if mime not in ('image/jpeg','image/png','image/webp') or mode not in ('groceries','receipt') or not isinstance(photo,str) or len(photo)>8400000:
     raise ValueError('Choose a JPEG, PNG, or WebP photo')
    import base64
    binary=base64.b64decode(photo,validate=True)
    if not 0<len(binary)<=6000000:raise ValueError('Photo too large (maximum 6 MB)')
    if mime=='image/jpeg' and not binary[:2]==bytes((255,216)):raise ValueError('Invalid JPEG')
    if mime=='image/png' and not binary[:4]==bytes((137,80,78,71)):raise ValueError('Invalid PNG')
    if mime=='image/webp' and binary[:4]!=b'RIFF':raise ValueError('Invalid WebP')
    instruction=('Read the grocery receipt and extract purchased food and pantry items; ignore taxes, totals, discounts, fees and non-food purchases.' if mode=='receipt' else 'Identify grocery and pantry products actually visible in this photograph.')
    prompt=instruction+' Return JSON with items array. Each item: brand (actual manufacturer/brand printed on package, or empty if not legible), product (short canonical product name or variety, not a marketing tagline), package_size (only a legible printed net quantity, otherwise empty), quantity (number), unit (count, can, package, bag, pound, ounce, gram, kilogram, liter, milliliter, cup, bunch, head, loaf, dozen), confidence (high, medium, low). IMPORTANT: distinguish logos/brand names from advertising slogans and descriptive copy. For example, on a Miller Lite label, the product name is Miller Lite; A Fine Pilsner is marketing copy, NOT a product name. Do not treat Lite alone as the manufacturer when the manufacturer name is visible. Do not assume the image is Miller Lite merely because of this example. Only identify a specific brand or size if visual evidence supports it; otherwise use a generic descriptive product with brand and package_size empty and confidence low. If brand and product overlap, return the canonical product once, not twice. Do not add packaging type to product when it is already represented by unit. For receipts interpret abbreviated names conservatively. Never guess unreadable details or include personal data. Use quantity 1 and count if uncertain. Maximum 40 items.'
    payload={'model':os.environ.get('PANTRY_SCAN_MODEL','gpt-4.1-mini'),'response_format':{'type':'json_object'},'messages':[{'role':'user','content':[{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':'data:'+mime+';base64,'+photo,'detail':'high'}}]}],'max_tokens':1800}
    req=urllib.request.Request('https://api.openai.com/v1/chat/completions',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=45) as response:result=json.load(response)
    record_scan_usage(request_scope.user_id,payload['model'],result.get('usage') or {})
    parsed=json.loads(result['choices'][0]['message']['content'])
    # When the first pass sees only generic label text, inspect the image a second time.
    # Never use a product-name guess as evidence: a missing brand remains missing.
    initial=parsed.get('items',[])
    if mode=='groceries' and isinstance(initial,list) and len(initial)==1 and isinstance(initial[0],dict):
     candidate=initial[0]
     brand_hint=str(candidate.get('brand') or '').strip().lower()
     product_hint=str(candidate.get('product') or candidate.get('name') or '').strip().lower()
     ambiguous=(brand_hint in ('','lite','light','original','classic','premium','fresh') or product_hint in ('beer','lite beer','a fine pilsner','lite a fine pilsner','can','drink','beverage'))
     if ambiguous:
      try:
       verification_prompt=('Independently inspect this grocery package photo for small print and logos, not just the largest lettering. '
        'Identify the actual printed brand/manufacturer, canonical product name and printed package size. '
        'Marketing slogans are not product names. Do NOT assume a particular brand from the style or from prior examples. '
        'If the brand is not readable, leave brand empty and confidence low. If size is not readable, leave package_size empty. '
        'Return JSON with one item containing brand, product, package_size, confidence, quantity and unit. '
        'Be conservative: no guesses.')
       verify_payload={'model':payload['model'],'response_format':{'type':'json_object'},'messages':[{'role':'user','content':[{'type':'text','text':verification_prompt},{'type':'image_url','image_url':{'url':'data:'+mime+';base64,'+photo,'detail':'high'}}]}],'max_tokens':350}
       verify_req=urllib.request.Request('https://api.openai.com/v1/chat/completions',data=json.dumps(verify_payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
       with urllib.request.urlopen(verify_req,timeout=45) as verify_response:verification=json.load(verify_response)
       record_scan_usage(request_scope.user_id,payload['model'],verification.get('usage') or {})
       verified=json.loads(verification['choices'][0]['message']['content']).get('items',[])
       if isinstance(verified,list) and len(verified)==1 and isinstance(verified[0],dict):
        second=verified[0]
        second_brand=str(second.get('brand') or '').strip()
        second_product=str(second.get('product') or '').strip()
        if second_product:
         candidate['product']=second_product
         candidate['brand']=second_brand
         candidate['package_size']=str(second.get('package_size') or '').strip()
         candidate['confidence']=second.get('confidence','low')
      except (urllib.error.URLError,TimeoutError,ValueError,KeyError,TypeError,IndexError):
       pass  # Keep the initial result if the optional verification is unavailable.
    allowed={'count','can','package','bag','pound','ounce','gram','kilogram','liter','milliliter','cup','bunch','head','loaf','dozen'}
    items=[]
    for item in parsed.get('items',[])[:40]:
     if not isinstance(item,dict):continue
     brand=str(item.get('brand') or '').strip()[:65]
     product=str(item.get('product') or item.get('name') or '').strip()[:100]
     package_size=str(item.get('package_size') or '').strip()[:32]
     # Compose a readable pantry name while preserving printed package size.
     name=' '.join(part for part in (brand,product,package_size) if part).strip()[:140]
     if brand and product.lower().startswith(brand.lower()+' '):name=' '.join(part for part in (product,package_size) if part)[:140]
     if not name:name=str(item.get('name') or '').strip()[:140]
     try:quantity=float(item.get('quantity',1))
     except (ValueError,TypeError):quantity=1
     unit=str(item.get('unit','count')).lower()
     if name and '|' not in name and 0<quantity<=100000:
      items.append({'name':name,'quantity':quantity,'unit':unit if unit in allowed else 'count','confidence':str(item.get('confidence','low')) if item.get('confidence') in ('high','medium','low') else 'low','brand':brand,'product':product,'package_size':package_size})
    return self.sendjson({'items':items,'requires_review':True})
   except urllib.error.HTTPError as exc:
    status=exc.code
    # Log only the upstream status, never provider bodies or credentials.
    print('Pantry scan OpenAI HTTP status:',status,flush=True)
    if status==401:return self.sendjson({'error':'OpenAI rejected the API key (401). Check the saved key in Admin settings.'},502)
    if status==403:return self.sendjson({'error':'OpenAI denied access (403). Check project permissions and model access.'},502)
    if status==429:return self.sendjson({'error':'OpenAI rate or billing limit reached (429). Check API billing, credits, and usage limits.'},502)
    if status==400:return self.sendjson({'error':'OpenAI rejected the scan request (400). The scanner request format may need updating.'},502)
    return self.sendjson({'error':'OpenAI request failed (HTTP '+str(status)+'). Check API status and server logs.'},502)
   except (ValueError,TypeError,KeyError,IndexError,UnicodeError) as exc:return self.sendjson({'error':str(exc)[:180]},400)
   except Exception as exc:
    print('Pantry scan provider failed:',type(exc).__name__,flush=True)
    return self.sendjson({'error':'Scanner unavailable. Please try again later.'},502)
  if self.path=='/api/analytics/event':
   if not self.gate(write=True):return
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=256:raise ValueError('Invalid event')
    event=json.loads(self.rfile.read(size)).get('event')
    if event not in ANALYTICS_EVENTS:raise ValueError('Invalid event')
    analytics_record(request_scope.user_id,event)
    if event=='analytics_test':
     with db() as c:
      count=c.execute("SELECT count FROM hg_analytics WHERE day=date('now') AND user_id=? AND event='analytics_test'",(request_scope.user_id,)).fetchone()
     return self.sendjson({'ok':True,'saved':bool(count),'count':count[0] if count else 0})
    return self.sendjson({'ok':True})
   except sqlite3.Error:
    return self.sendjson({'error':'Analytics storage unavailable'},503)
   except (ValueError,TypeError,AttributeError):return self.sendjson({'error':'Invalid event'},400)
  if self.path=='/api/admin/pricing-visibility':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=512:raise ValueError('Invalid request size')
    values=json.loads(self.rfile.read(size))
    if not isinstance(values,dict) or any(type(values.get(k)) is not bool for k in ('admin','user')):raise ValueError('Both visibility options must be booleans')
    with db() as c:
     c.execute('CREATE TABLE IF NOT EXISTS admin_preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
     for role in ('admin','user'):c.execute('INSERT OR REPLACE INTO admin_preferences(key,value) VALUES (?,?)',('show_prices_'+role,'1' if values[role] else '0'))
     c.commit()
    return self.sendjson({'ok':True,'visibility':pricing_visibility()})
   except (ValueError,TypeError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/admin/tasks/run':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=512:raise ValueError('Invalid request size')
    name=json.loads(self.rfile.read(size)).get('task')
    if name=='us_import':return self.sendjson(start_us_import())
    if name=='store_directory':
     with ATP_LOCK:
      ATP_CACHE_FILE.unlink(missing_ok=True)
     with STORE_CACHE_LOCK:STORE_CACHE.clear()
     task_record(name,'queued')
     return self.sendjson({'ok':True,'message':'Directory cache cleared; next store search will fetch fresh nationwide data.'})
    if name=='store_geocode':
     global RETAILER_GEOCODE_RUNNING
     with RETAILER_GEOCODE_LOCK:
      if RETAILER_GEOCODE_RUNNING:raise ValueError('Geocoding is already running')
      RETAILER_GEOCODE_RUNNING=True
     threading.Thread(target=geocode_retailer_addresses,daemon=True).start()
     task_record(name,'running')
     return self.sendjson({'ok':True,'message':'Store address geocoding started.'})
    raise ValueError('Unsupported task; use the existing nutrition or price controls')
   except (ValueError,TypeError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path in ('/api/auth/setup','/api/auth/login','/api/auth/register','/api/auth/verify','/api/auth/resend-verification'):return self.auth_post()
  if self.path=='/api/admin/nutrition-batch':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=1024:raise ValueError('Invalid request size')
    args=json.loads(self.rfile.read(size))
    cursor=max(0,int(args.get('cursor',0)))
    with db() as conn:
     rows=conn.execute('SELECT id,url,payload FROM imported WHERE id>? ORDER BY id LIMIT 3',(cursor,)).fetchall()
     total=conn.execute('SELECT COUNT(*) FROM imported').fetchone()[0]
    updated=skipped=failed=missing=0
    errors=[]
    for rid,url,payload in rows:
     try:
      original=json.loads(payload)
      if original.get('nutrition'):
       skipped+=1
       continue
      fresh=recipe_from_url(url)
      nutrition=fresh.get('nutrition') or {}
      labels=fresh.get('dietary_labels') or []
      if not nutrition and not labels:
       missing+=1
       errors.append(str(rid)+': No source nutrition found')
       continue
      original['nutrition']=nutrition
      original['dietary_labels']=labels
      with lock:
       with db() as conn:conn.execute('UPDATE imported SET payload=? WHERE id=?',(json.dumps(original),rid))
      updated+=1
     except (ValueError,TypeError,OSError,urllib.error.URLError,TimeoutError) as exc:
      failed+=1
      errors.append(str(rid)+': '+str(exc)[:120])
    next_cursor=rows[-1][0] if rows else cursor
    with db() as conn:remaining=conn.execute('SELECT COUNT(*) FROM imported WHERE id>?',(next_cursor,)).fetchone()[0]
    task_record('nutrition','completed' if remaining==0 else 'running')
    return self.sendjson({'ok':True,'cursor':next_cursor,'total':total,'processed':total-remaining,'updated':updated,'skipped':skipped,'failed':failed,'missing':missing,'errors':errors,'done':remaining==0})
   except (ValueError,TypeError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/nutrition-refresh':
   if not self.gate(write=True):return
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=2048:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size));rid=int(data.get('id'))
    with db() as c:
     row=c.execute('SELECT url,payload FROM imported WHERE id=?',(rid-1000000,)).fetchone()
    if not row:return self.sendjson({'error':'Nutrition refresh is available for imported HelloFresh recipes only'},400)
    updated=recipe_from_url(row[0])
    original=json.loads(row[1])
    original['nutrition']=updated.get('nutrition',{})
    original['dietary_labels']=updated.get('dietary_labels',[])
    if updated.get('source_servings'):original['source_servings']=updated['source_servings']
    with lock:
     with db() as c:c.execute('UPDATE imported SET payload=? WHERE id=?',(json.dumps(original),rid-1000000))
    return self.sendjson({'ok':True,'nutrition':original['nutrition'],'dietary_labels':original['dietary_labels']})
   except (ValueError,TypeError,OSError,urllib.error.URLError,TimeoutError) as exc:
    return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/reef-refresh':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=4096:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    return self.sendjson(reef_refresh_batch(data.get('zip',''),data.get('items',[])))
   except (ValueError,TypeError,OSError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/admin/openai-key':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=2048:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    if data.get('remove') is True:
     OPENAI_KEY_PATH.unlink(missing_ok=True)
     return self.sendjson({'ok':True,'configured':bool(openai_scan_key())})
    save_openai_scan_key(data.get('key'))
    return self.sendjson({'ok':True,'configured':True})
   except (ValueError,TypeError,OSError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path in ('/api/admin/resend-key','/api/admin/resend-test'):
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=2048:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    if self.path=='/api/admin/resend-test':
     if not resend_test_email(data.get('email')):raise ValueError('Resend did not confirm the email')
     return self.sendjson({'ok':True,'message':'Test email accepted by Resend. Check the inbox.'})
    if data.get('remove') is True:
     RESEND_KEY_PATH.unlink(missing_ok=True)
     return self.sendjson({'ok':True,'configured':bool(resend_key())})
    save_resend_key(data.get('key'))
    return self.sendjson({'ok':True,'configured':True})
   except (ValueError,TypeError,OSError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/admin/reef-key':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=2048:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    if data.get('remove') is True:
     REEF_KEY_PATH.unlink(missing_ok=True)
     return self.sendjson({'ok':True,'configured':reef_key_configured()})
    save_reef_key(data.get('key'))
    return self.sendjson({'ok':True,'configured':True})
   except (ValueError,TypeError,OSError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/auth/session-settings':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=1024:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    with db() as c:
     auth.ensure_schema(c)
     auth.set_session_duration_days(c,data.get('days'))
    return self.sendjson({'ok':True,'days':data['days']})
   except (ValueError,TypeError,KeyError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/auth/unblock-email':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=8192:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    with db() as c:
     auth.ensure_schema(c)
     auth.unblock_email(c,request_scope.user_id,data.get('email'))
    return self.sendjson({'ok':True})
   except (ValueError,TypeError,KeyError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/auth/remove-account':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=8192:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    with db() as c:
     auth.ensure_schema(c)
     auth.remove_account(c,request_scope.user_id,data.get('id'),data.get('block_future',False))
    return self.sendjson({'ok':True})
   except (ValueError,TypeError,KeyError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/auth/review':
   if not self.gate(write=True):return
   if not getattr(request_scope,'admin',False):return self.sendjson({'error':'Administrator required'},403)
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=8192:raise ValueError('Invalid request size')
    data=json.loads(self.rfile.read(size))
    with db() as c:
     auth.ensure_schema(c)
     auth.review_registration(c,request_scope.user_id,data.get('id'),data.get('approve'))
    return self.sendjson({'ok':True})
   except (ValueError,TypeError,KeyError) as exc:return self.sendjson({'error':str(exc)},400)
  if self.path=='/api/auth/redeem':return self.redeem_post()
  if self.path=='/api/auth/invite':return self.invite_post()
  if self.path=='/api/auth/logout':return self.logout_post()
  if not self.gate(write=True):return
  if self.path!='/api/action':return self.sendjson({'error':'Not found'},404)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if size<1 or size>25000000:return self.sendjson({'error':'Too large (25 MB maximum)'},413)
   p=json.loads(self.rfile.read(size));action=p['action']
   if action in ('start-us-import','pause-bulk','resume-bulk','restore-backup','bulk','photo') and not getattr(request_scope,'admin',False):
    return self.sendjson({'error':'Administrator required'},403)
   if action=='start-us-import':return self.sendjson(start_us_import())
   if action=='pause-bulk':
    BULK_PAUSE.set();return self.sendjson({'ok':True,'message':'Pause requested; finishes current recipe first'})
   if action=='restore-backup':
    backup=p.get('backup');mode=p.get('mode','merge')
    if not isinstance(backup,dict) or backup.get('version')!=1 or mode!='merge':raise ValueError('Invalid backup or restore mode')
    imports=backup.get('imported',[]);photos=backup.get('photos',[])
    if not isinstance(imports,list) or len(imports)>50000 or not isinstance(photos,list) or len(photos)>50000:raise ValueError('Backup too large')
    with lock:
     with db() as conn:
      added=0;idmap={}
      for row in imports:
       if not isinstance(row,dict) or not isinstance(row.get('url'),str) or not isinstance(row.get('payload'),dict):raise ValueError('Invalid recipe entry')
       if not allowed_url(row['url']):raise ValueError('Invalid recipe URL')
       cur=conn.execute('INSERT OR IGNORE INTO imported(url,payload) VALUES(?,?)',(row['url'],'{}'))
       if cur.rowcount:
        rid=1000000+cur.lastrowid;recipe=dict(row['payload'],id=rid)
        conn.execute('UPDATE imported SET payload=? WHERE url=?',(json.dumps(recipe),row['url']));added+=1
       else:rid=1000000+conn.execute('SELECT id FROM imported WHERE url=?',(row['url'],)).fetchone()[0]
       if 'id' in row['payload']:idmap[str(row['payload']['id'])]=rid
      for photo in photos:
       if not isinstance(photo,dict):continue
       rid=idmap.get(str(photo.get('recipe_id')),photo.get('recipe_id'))
       if isinstance(rid,int) and isinstance(photo.get('image_url'),str) and len(photo['image_url'])<2048:
        conn.execute('INSERT OR IGNORE INTO photos(recipe_id,image_url) VALUES(?,?)',(rid,photo['image_url']))
      old=read();saved=backup.get('state',{})
      if isinstance(saved,dict):
       for field in ('favorites','pantry','manual'):
        if isinstance(saved.get(field),list):
         values=[idmap.get(str(x),x) if field=='favorites' else x for x in saved[field]]
         old[field]=list(dict.fromkeys(old.get(field,[])+values))[:20000]
       if isinstance(saved.get('collections'),dict):
        collections=old.setdefault('collections',{})
        for name,ids in saved['collections'].items():
         if isinstance(name,str) and isinstance(ids,list) and len(name)<=60:
          collections[name]=list(dict.fromkeys(collections.get(name,[])+[idmap.get(str(x),x) for x in ids]))[:20000]
       if isinstance(saved.get('recipe_reviews'),dict):
        reviews=old.setdefault('recipe_reviews',{})
        for rid,review in saved['recipe_reviews'].items():
         mapped=str(idmap.get(str(rid),rid))
         if mapped not in reviews and isinstance(review,dict) and type(review.get('rating'))==int and 0<=review['rating']<=5 and isinstance(review.get('note',''),str) and len(review.get('note',''))<=2000:
          reviews[mapped]={'rating':review['rating'],'note':review.get('note','')}
       if not old.get('weekly') and isinstance(saved.get('weekly'),dict):old['weekly']={day:idmap.get(str(id),id) for day,id in saved['weekly'].items()}
       if not old.get('list') and isinstance(saved.get('list'),list):old['list']=saved['list']
       if not old.get('selected') and isinstance(saved.get('selected'),dict):old['selected']={str(idmap.get(str(k),k)):v for k,v in saved['selected'].items()}
       old['revision']=old.get('revision',0)+1
     # Commit recipe inserts before saving state through its own connection.
     if isinstance(saved,dict):save(old)
    return self.sendjson({'ok':True,'message':f'Restored {added} missing recipes and merged saved preferences; existing data retained'})
   if action=='bulk':
    country=str(p.get('country','US')).upper();limit=int(p.get('limit',50))
    if country not in ('US','UK','AU','CA','DE','NL') or not 0<=limit<=10000:raise ValueError('Choose a supported country and a limit of 0–10,000')
    with lock:
     if job['running']:raise ValueError('An import is already running')
     BULK_PAUSE.clear()
     job.update(running=True,paused=False,country=country,limit=limit,discovered=0,processed=0,imported=0,skipped=0,errors=0,started_at=time.time(),message='Discovering public recipe pages');save_bulk()
     threading.Thread(target=bulk_worker,args=(country,limit),daemon=True).start()
    return self.sendjson({'ok':True,'message':'Bulk import started'})
   if action=='resume-bulk':
    if job['running']:raise ValueError('Import already running')
    if str(p.get('country',job.get('country','US'))).upper()!=job.get('country','US'):raise ValueError('Select the paused country to resume it')
    BULK_PAUSE.clear();country=job.get('country','US');limit=int(job.get('limit',0))
    job.update(running=True,paused=False,discovered=0,processed=0,imported=0,skipped=0,errors=0,started_at=time.time(),message='Resuming (already imported recipes will be skipped)');save_bulk()
    threading.Thread(target=bulk_worker,args=(country,limit),daemon=True).start()
    return self.sendjson({'ok':True,'message':'Resuming bulk import'})
   if action=='photo':
    rid=int(p['id']);url=str(p.get('image','')).strip()
    if rid not in {r['id'] for r in allrecipes()}:raise ValueError('Unknown recipe')
    if url:
     u=urllib.parse.urlsplit(url)
     if u.scheme!='https' or not u.hostname or u.username or u.password or len(url)>2048:raise ValueError('Use a valid HTTPS image URL')
    with lock:
     with db() as c:c.execute('INSERT INTO photos(recipe_id,image_url) VALUES(?,?) ON CONFLICT(recipe_id) DO UPDATE SET image_url=excluded.image_url',(rid,url))
     for oldfile in PHOTO_DIR.glob(str(rid)+'.*'):oldfile.unlink(missing_ok=True)
    return self.sendjson({'ok':True})
   if action=='import':
    if not getattr(request_scope,'admin',False):
     return self.sendjson({'error':'Recipe importing is restricted to administrators.'},403)
    url=str(p.get('url','')).strip()
    recipe=recipe_from_url(url)
    with lock:
     with db() as c:
      row=c.execute('SELECT id FROM imported WHERE url=?',(url,)).fetchone()
      if row:recipe['id']=1000000+row[0]
      else:
       cursor=c.execute('INSERT INTO imported(url,payload) VALUES(?,?)',(url,'{}'))
       recipe['id']=1000000+cursor.lastrowid
      c.execute('UPDATE imported SET payload=? WHERE url=?',(json.dumps(recipe),url))
    return self.sendjson({'ok':True,'recipe':recipe})
   with lock:
    state=read()
    pantry_before={'pantry':list(state.get('pantry',[])),'pantry_inventory':dict(state.get('pantry_inventory',{}))}
    if action in ('preview_cooked_recipe','confirm_cooked_recipe','undo_cooked_recipe'):
     inventory=state.setdefault('pantry_inventory',{})
     history=state.setdefault('pantry_cooking_history',[])
     if not isinstance(history,list):raise ValueError('Invalid cooking history')
     if action=='undo_cooked_recipe':
      event_id=p.get('event_id')
      event=next((x for x in history if x.get('id')==event_id),None)
      if not event or event.get('undone'):raise ValueError('Cooking event unavailable or already undone')
      for key,delta in event['deductions'].items():
       inventory[key]=round(float(inventory.get(key,0))+float(delta),4)
      event['undone']=True
     else:
      rid=p.get('recipe_id')
      if type(rid)!=int:raise ValueError('Invalid recipe')
      recipe=next((r for r in allrecipes() if r['id']==rid),None)
      if recipe is None:raise ValueError('Unknown recipe')
      multiplier=p.get('servings',1)
      if type(multiplier) not in (int,float) or not 0<float(multiplier)<=20:raise ValueError('Invalid serving multiplier')
      corrections=state.get('recipe_ingredient_corrections',{})
      conversions={'gram':('mass',1),'kilogram':('mass',1000),'ounce':('mass',28.349523125),'pound':('mass',453.59237),'milliliter':('volume',1),'liter':('volume',1000),'teaspoon':('volume',4.92892159375),'tablespoon':('volume',14.78676478125),'cup':('volume',236.5882365)}
      remaining={k:float(v) for k,v in inventory.items() if type(v) in (int,float) and v>0}
      deductions={}
      unmatched=[]
      for original in recipe.get('ingredients',{}):
       correction=corrections.get(str(rid)+'|'+original)
       if not isinstance(correction,dict):continue
       name=str(correction.get('name','')).strip().casefold()
       unit=correction.get('unit','')
       amount=correction.get('amount')
       if not name or '|' in name or type(amount) not in (int,float) or amount<=0:continue
       required=float(amount)*float(multiplier)
       matches=[]
       for key,available in remaining.items():
        if not key.startswith(name+'|') or available<=0:continue
        stock_unit=key.rsplit('|',1)[-1]
        if stock_unit==unit:factor=1
        elif unit in conversions and stock_unit in conversions and conversions[unit][0]==conversions[stock_unit][0]:
         factor=conversions[unit][1]/conversions[stock_unit][1]
        else:continue
        matches.append((key,factor))
       for key,factor in matches:
        take=min(remaining[key],required*factor)
        if take<=0:continue
        remaining[key]=round(remaining[key]-take,6)
        deductions[key]=round(deductions.get(key,0)+take,6)
        required=max(0,required-take/factor)
        if required<0.000001:break
       if required>0.000001:unmatched.append(name+' ('+unit+')')
      if action=='preview_cooked_recipe':
       return self.sendjson({'deductions':deductions,'not_deducted':unmatched,'recipe_name':recipe.get('name','Recipe')})
      if not isinstance(p.get('event_id'),str) or not (8<=len(p['event_id'])<=80) or not re.fullmatch(r'[A-Za-z0-9_-]+',p['event_id']):raise ValueError('Invalid cooking event')
      if any(x.get('id')==p['event_id'] for x in history):raise ValueError('This cooking event was already recorded')
      if not deductions:raise ValueError('No verified pantry quantities to deduct')
      for key,delta in deductions.items():inventory[key]=round(max(0,float(inventory[key])-delta),4)
      history.append({'id':p['event_id'],'recipe_id':rid,'deductions':deductions,'undone':False,'at':time.time()})
      if len(history)>100:del history[:-100]
    elif action=='select':
     rid=str(int(p['id']));servings=float(p.get('servings',1))
     if not (0<servings<=20):raise ValueError('Serving multiplier must be 0–20')
     if p.get('checked') and rid not in {str(r['id']) for r in allrecipes()}:raise ValueError('Unknown recipe')
     if p.get('checked'):
      state['selected'][rid]=servings
     else:state['selected'].pop(rid,None)
    elif action=='correct_recipe_ingredient':
     rid=p.get('recipe_id');original=p.get('original')
     if type(rid)!=int or not isinstance(original,str) or not original or len(original)>250:raise ValueError('Invalid ingredient reference')
     recipe=next((r for r in allrecipes() if r['id']==rid),None)
     if recipe is None or original not in recipe.get('ingredients',{}):raise ValueError('Recipe ingredient not found')
     corrections=state.setdefault('recipe_ingredient_corrections',{})
     key=str(rid)+'|'+original
     if p.get('remove') is True:corrections.pop(key,None)
     else:
      name=p.get('name');unit=p.get('unit');amount=p.get('amount')
      allowed={'','count','teaspoon','tablespoon','cup','ounce','pound','gram','kilogram','milliliter','liter','clove','bunch','head','stalk','slice','piece','pinch','can','package','bag'}
      if not isinstance(name,str) or not name.strip() or len(name)>140:raise ValueError('Ingredient name required')
      if not isinstance(unit,str) or unit not in allowed:raise ValueError('Invalid measurement unit')
      if type(amount) not in (int,float) or not (0<float(amount)<=10000):raise ValueError('Invalid ingredient amount')
      if len(corrections)>=5000 and key not in corrections:raise ValueError('Too many corrections')
      corrections[key]={'name':name.strip(),'unit':unit,'amount':float(amount)}
    elif action=='edit_ingredient':
     name=str(p.get('name',''))
     if not any(i['name']==name for i in state['list']):raise ValueError('Unknown item')
     display=str(p.get('display','')).strip()
     department=str(p.get('department','Auto'))
     if not display or len(display)>180:raise ValueError('Ingredient label required (max 180 characters)')
     if department not in ('Auto','Produce','Meat & Seafood','Dairy & Refrigerated','Bakery & Bread','Frozen','Pantry & Seasonings','Other / Uncategorized'):raise ValueError('Invalid department')
     state.setdefault('ingredient_overrides',{})[name]={'display':display,'department':department}
    elif action=='reset_ingredient':
     state.setdefault('ingredient_overrides',{}).pop(str(p.get('name','')),None)
    elif action=='set_preferences':
     favorites=p.get('favorites',[]);collections=p.get('collections',{});weekly=p.get('weekly',{});pantry=p.get('pantry',[]);manual=p.get('manual',[]);previous_week=p.get('previous_week',{});recommendation_hidden=p.get('recommendation_hidden',[])
     if not isinstance(favorites,list) or len(favorites)>20000 or not isinstance(collections,dict) or not isinstance(weekly,dict) or not isinstance(pantry,list) or not isinstance(manual,list) or not isinstance(previous_week,dict) or len(previous_week)>7 or not isinstance(recommendation_hidden,list) or len(recommendation_hidden)>2000 or any(type(v)!=int or v<0 for v in recommendation_hidden):raise ValueError('Invalid preferences')
     if len(collections)>100 or len(weekly)>7 or len(pantry)>500 or len(manual)>500:raise ValueError('Too many items')
     # Removing an item with the pantry X must remove every stored unit too.
     if any(not isinstance(x,str) or not x.strip() for x in pantry):raise ValueError('Invalid pantry ingredient')
     remaining_names={x.strip().casefold() for x in pantry}
     removed_names={x.strip().casefold() for x in state.get('pantry',[]) if isinstance(x,str) and x.strip()}-remaining_names
     if removed_names:
      inventory=state.setdefault('pantry_inventory',{})
      for key in list(inventory):
       if isinstance(key,str) and key.rsplit('|',1)[0].strip().casefold() in removed_names:inventory.pop(key,None)
     state.update(favorites=favorites,collections=collections,weekly=weekly,pantry=pantry,manual=manual,previous_week=previous_week,recommendation_hidden=recommendation_hidden)
    elif action=='rate_recipe':
     rid=p.get('id');rating=p.get('rating');note=p.get('note','')
     if type(rid)!=int or rid<0 or rid not in {int(r['id']) for r in allrecipes()}:raise ValueError('Unknown recipe')
     if type(rating)!=int or rating<0 or rating>5:raise ValueError('Rating must be 0–5')
     if not isinstance(note,str) or len(note)>2000:raise ValueError('Note must be at most 2000 characters')
     reviews=state.setdefault('recipe_reviews',{})
     if rating or note.strip():reviews[str(rid)]={'rating':rating,'note':note.strip()}
     else:reviews.pop(str(rid),None)
    elif action in ('generate','preview_grocery'):
     rebuild_grocery_list(state)
    elif action=='pantry_scan_apply':
     items=p.get('items',[])
     if not isinstance(items,list) or not 0<len(items)<=40:raise ValueError('Select up to 40 items')
     inventory=state.setdefault('pantry_inventory',{})
     pantry=state.setdefault('pantry',[])
     allowed={'count','can','package','bag','pound','ounce','gram','kilogram','liter','milliliter','cup','bunch','head','loaf','dozen'}
     validated=[]
     for item in items:
      if not isinstance(item,dict):raise ValueError('Invalid scanned item')
      name=item.get('name','');unit=item.get('unit','count');quantity=item.get('quantity')
      if not isinstance(name,str) or not name.strip() or len(name)>140 or '|' in name or not isinstance(unit,str) or unit not in allowed or type(quantity) not in (int,float) or not 0<quantity<=100000:raise ValueError('Invalid scanned item')
      validated.append((name.strip(),unit,float(quantity)))
     if len(inventory)+len(validated)>1040 or len(pantry)+len(validated)>540:raise ValueError('Pantry capacity reached')
     for name,unit,quantity in validated:
      key=name.casefold()+'|'+unit
      inventory[key]=round(float(inventory.get(key,0))+quantity,4)
      if not any(x.casefold()==name.casefold() for x in pantry if isinstance(x,str)):pantry.append(name)
    elif action=='delete_pantry_item':
     name=p.get('name')
     if not isinstance(name,str) or not name.strip() or len(name)>140:raise ValueError('Invalid pantry ingredient')
     normalized=name.strip().casefold()
     state['pantry']=[x for x in state.get('pantry',[]) if not (isinstance(x,str) and x.strip().casefold()==normalized)]
     inventory=state.setdefault('pantry_inventory',{})
     for key in list(inventory):
      if isinstance(key,str) and key.rsplit('|',1)[0].strip().casefold()==normalized:inventory.pop(key,None)
    elif action in ('set_pantry_inventory','adjust_pantry_inventory'):
     name=p.get('name');unit=p.get('unit','');amount=p.get('amount')
     allowed={'','count','teaspoon','tablespoon','cup','ounce','pound','gram','kilogram','milliliter','liter','clove','bunch','head','stalk','slice','piece','pinch','can','package','bag'}
     if not isinstance(name,str) or not name.strip() or len(name)>140 or '|' in name:raise ValueError('Invalid pantry ingredient')
     if not isinstance(unit,str) or unit not in allowed:raise ValueError('Invalid pantry unit')
     if type(amount) not in (int,float) or not math.isfinite(amount) or not 0<=amount<=100000:raise ValueError('Invalid pantry amount')
     inventory=state.setdefault('pantry_inventory',{})
     key=name.strip().casefold()+'|'+unit
     if len(inventory)>=1000 and key not in inventory:raise ValueError('Inventory limit reached')
     mode=p.get('mode','set') if action=='adjust_pantry_inventory' else 'set'
     if mode not in ('set','add','use'):raise ValueError('Invalid quantity action')
     conversions={'teaspoon':('volume',4.92892159375),'tablespoon':('volume',14.78676478125),'cup':('volume',236.5882365),'milliliter':('volume',1),'liter':('volume',1000),'gram':('mass',1),'kilogram':('mass',1000),'ounce':('mass',28.349523125),'pound':('mass',453.59237)}
     target_key=key
     # For Add/Use, prefer an exact-unit entry; otherwise convert to a single compatible tracked unit.
     if mode in ('add','use') and key not in inventory and unit in conversions:
      compatible=[k for k in inventory if k.startswith(name.strip().casefold()+'|') and k.rsplit('|',1)[-1] in conversions and conversions[k.rsplit('|',1)[-1]][0]==conversions[unit][0]]
      if len(compatible)>1:raise ValueError('Multiple compatible units tracked. Select one of the existing units to update')
      if compatible:target_key=compatible[0]
     if mode=='use' and target_key not in inventory:raise ValueError('Set a quantity before using this ingredient')
     target_unit=target_key.rsplit('|',1)[-1]
     factor=conversions[unit][1]/conversions[target_unit][1] if target_unit!=unit and unit in conversions and target_unit in conversions else 1
     delta=float(amount)*factor
     current=float(inventory.get(target_key,0))
     if mode=='add':updated=current+delta
     elif mode=='use':updated=current-delta
     else:updated=float(amount)
     if not math.isfinite(updated) or not 0<=updated<=100000:raise ValueError('Not enough pantry quantity or inventory limit exceeded')
     if len(inventory)>=1000 and target_key not in inventory:raise ValueError('Inventory limit reached')
     inventory[target_key]=round(updated,4)
     pantry=state.setdefault('pantry',[])
     if not any(isinstance(x,str) and x.casefold()==name.strip().casefold() for x in pantry):
      if len(pantry)>=500:raise ValueError('Pantry capacity reached')
      pantry.append(name.strip())
    elif action=='clear':state['list']=[]
    elif action=='purchase_many':
     names=p.get('names',[])
     if not isinstance(names,list) or len(names)>250 or any(not isinstance(n,str) for n in names):raise ValueError('Invalid ingredient selection')
     available={i['name']:i for i in state['list']}
     if not names or any(n not in available for n in names):raise ValueError('Unknown grocery item')
     for name in names:available[name]['purchased']=bool(p['purchased'])
    elif action=='purchase':
     name=p['name'];found=next((i for i in state['list'] if i['name']==name),None)
     if found is None:raise ValueError('Unknown item')
     found['purchased']=bool(p['purchased'])
    else:raise ValueError('Unknown action')
    if action=='preview_grocery':return self.sendjson({'items':state['list']})
    state['revision']+=1;save(state)
    pantry_audit(pantry_before,state)
   return self.sendjson(state)
  except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:return self.sendjson({'error':str(e)},400)
if __name__=='__main__':
 with db() as c:
  if auth.instance_needs_setup(c):
   print('HelloGroceries: first administrator setup requires a verified Cloudflare Access session.',flush=True)
 if BULK_RECOVER:
  job.update(running=True,paused=False,processed=0,discovered=0,imported=0,skipped=0,errors=0,message='Recovering interrupted import; saved recipes will be skipped')
  threading.Thread(target=bulk_worker,args=(job.get('country','US'),int(job.get('limit',0))),daemon=True).start()
 threading.Thread(target=recommendation_photo_warmup_worker,daemon=True,name='recommendation-photo-warmup').start()
 threading.Thread(target=nutrition_scan_worker,daemon=True,name='nutrition-background-scan').start()
 threading.Thread(target=recipe_source_enrichment_worker,daemon=True,name='recipe-source-enrichment').start()
 threading.Thread(target=us_import_schedule_worker,daemon=True,name='us-import-scheduler').start()
 ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
