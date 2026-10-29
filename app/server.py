from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from pathlib import Path
import json,sqlite3,threading,re,html,io,zipfile,datetime,urllib.request,urllib.parse,urllib.robotparser,xml.etree.ElementTree as ET,time,subprocess,sys,fcntl,os,ipaddress,socket
BASE=Path(__file__).parent;DB=Path('/data/shopping.db');DB.parent.mkdir(exist_ok=True,parents=True)
RECIPES=json.loads((BASE/'recipes.json').read_text());lock=threading.Lock()
job={"running":False,"discovered":0,"processed":0,"imported":0,"skipped":0,"errors":0,"message":"Idle","paused":False,"country":"US","limit":0}
BULK_STATE=Path("/data/bulk_import_progress.json")
BULK_PAUSE=threading.Event()
BULK_RECOVER=False
def save_bulk():
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
  return {'ok':True,'message':'US importer started. Existing imported URLs will be skipped.'}

def db():
 c=sqlite3.connect(DB)
 c.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS imported (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, payload TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS photos (recipe_id INTEGER PRIMARY KEY, image_url TEXT NOT NULL)')
 return c

def allrecipes():
 with db() as c: imported=[json.loads(x[0]) for x in c.execute('SELECT payload FROM imported ORDER BY id DESC')]
 recipes=RECIPES+imported
 with db() as c:photos=dict(c.execute('SELECT recipe_id,image_url FROM photos'))
 return [dict(r,image=photos.get(r['id'],r.get('image',''))) for r in recipes]

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

def allowed_url(url):
 u=urllib.parse.urlsplit(url)
 host=(u.hostname or '').lower()
 return u.scheme=='https' and (host=='hellofresh.com' or host.startswith('hellofresh.') or host.endswith('.hellofresh.com') or host.endswith('.hellofresh.co.uk') or host.endswith('.hellofresh.com.au') or host.endswith('.hellofresh.co.nz')) and not u.username and not u.password

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
 return {'name':name,'country':urllib.parse.urlsplit(url).hostname,'url':url,'image':str(photo),'ingredients':ingredients,'ingredient_text':items,'source':'HelloFresh page','instructions_url':url}

def discover_sitemap(country,limit):
 roots={'US':'https://www.hellofresh.com/sitemap_recipe_pages.xml','UK':'https://www.hellofresh.co.uk/sitemap.xml','AU':'https://www.hellofresh.com.au/sitemap.xml','CA':'https://www.hellofresh.ca/sitemap.xml','DE':'https://www.hellofresh.de/sitemap.xml','NL':'https://www.hellofresh.nl/sitemap.xml'}
 root=roots[country];host=urllib.parse.urlsplit(root).hostname
 rp=urllib.robotparser.RobotFileParser();rp.set_url('https://'+host+'/robots.txt')
 try:rp.read()
 except Exception:raise ValueError('Could not check robots.txt; import aborted')
 queue=[root];seen=set();recipes=[];sitemaps=0
 while queue and (limit==0 or len(recipes)<limit) and sitemaps<30:
  url=queue.pop(0)
  if url in seen or not allowed_url(url) or not rp.can_fetch('PersonalRecipeImporter',url):continue
  seen.add(url);sitemaps+=1
  try:
   req=urllib.request.Request(url,headers={'User-Agent':'PersonalRecipeImporter/1.0'})
   with urllib.request.urlopen(req,timeout=15) as resp:
    if not allowed_url(resp.geturl()):continue
    content=resp.read(5_000_001)
   if len(content)>5_000_000:continue
   tree=ET.fromstring(content)
   locs=[(e.text or '').strip() for e in tree.iter() if e.tag.endswith('}loc') or e.tag=='loc']
   for loc in locs:
    if not allowed_url(loc) or urllib.parse.urlsplit(loc).hostname!=host:continue
    if loc.endswith('.xml'):
     if len(queue)<100:queue.append(loc)
    elif '/recipes/' in loc and loc not in recipes:recipes.append(loc)
    if limit and len(recipes)>=limit:break
  except Exception:pass
  time.sleep(0.5)
 return recipes,rp

def bulk_worker(country,limit):
 try:
  urls,rp=discover_sitemap(country,limit)
  job['discovered']=len(urls)
  for url in urls:
   if BULK_PAUSE.is_set():job['message']='Paused';job['paused']=True;save_bulk();return
   if not rp.can_fetch('PersonalRecipeImporter',url):job['skipped']+=1;continue
   try:
    with db() as c:
     if c.execute('SELECT 1 FROM imported WHERE url=?',(url,)).fetchone():
      job['skipped']+=1;continue
    recipe=recipe_from_url(url)
    with lock:
     with db() as c:
      cur=c.execute('INSERT OR IGNORE INTO imported(url,payload) VALUES(?,?)',(url,'{}'))
      if cur.rowcount:
       recipe['id']=1000000+cur.lastrowid
       c.execute('UPDATE imported SET payload=? WHERE url=?',(json.dumps(recipe),url))
       job['imported']+=1
      else:job['skipped']+=1
   except Exception:job['errors']+=1
   job['processed']+=1
   save_bulk()
   time.sleep(1.5)
  job['message']='Completed; some sites may not expose importable recipe data'
 except Exception as e:job['message']='Stopped: '+str(e)
 finally:job['running']=False;save_bulk()

def default():return {'selected':{},'list':[],'revision':0}
def read():
 with db() as c:
  row=c.execute('SELECT payload FROM state WHERE id=1').fetchone()
 return json.loads(row[0]) if row else default()
def save(state):
 with db() as c:c.execute('INSERT INTO state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(json.dumps(state),))
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*a,**kw):super().__init__(*a,directory=str(BASE),**kw)
 def sendjson(self,obj,status=200):
  b=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):
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
  if self.path=='/api/recipes':return self.sendjson(allrecipes())
  if self.path=='/api/state':return self.sendjson(read())
  if self.path=='/api/backup':
   with lock:
    with db() as conn:
     data={'version':1,'exported_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'state':read(),'imported':[dict(url=u,payload=json.loads(v)) for u,v in conn.execute('SELECT url,payload FROM imported')],'photos':[dict(recipe_id=i,image_url=u) for i,u in conn.execute('SELECT recipe_id,image_url FROM photos')]}
   raw=json.dumps(data).encode()
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Disposition','attachment; filename="hellogroceries-backup.json"');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  if self.path=='/api/bulk-status':return self.sendjson(dict(job))
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
  return super().do_GET()
 def do_POST(self):
  if self.path!='/api/action':return self.sendjson({'error':'Not found'},404)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if size<1 or size>25000000:return self.sendjson({'error':'Too large (25 MB maximum)'},413)
   p=json.loads(self.rfile.read(size));action=p['action']
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
     job.update(running=True,paused=False,country=country,limit=limit,discovered=0,processed=0,imported=0,skipped=0,errors=0,message='Discovering public recipe pages');save_bulk()
     threading.Thread(target=bulk_worker,args=(country,limit),daemon=True).start()
    return self.sendjson({'ok':True,'message':'Bulk import started'})
   if action=='resume-bulk':
    if job['running']:raise ValueError('Import already running')
    BULK_PAUSE.clear();country=job.get('country','US');limit=int(job.get('limit',0))
    job.update(running=True,paused=False,discovered=0,processed=0,imported=0,skipped=0,errors=0,message='Resuming (already imported recipes will be skipped)');save_bulk()
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
    url=str(p.get('url',''))
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
    if action=='select':
     rid=str(int(p['id']));servings=float(p.get('servings',1))
     if not (0<servings<=20):raise ValueError('Serving multiplier must be 0–20')
     if rid not in {str(r['id']) for r in allrecipes()}:raise ValueError('Unknown recipe')
     if p.get('checked'):state['selected'][rid]=servings
     else:state['selected'].pop(rid,None)
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
     favorites=p.get('favorites',[]);collections=p.get('collections',{});weekly=p.get('weekly',{});pantry=p.get('pantry',[]);manual=p.get('manual',[]);previous_week=p.get('previous_week',{})
     if not isinstance(favorites,list) or len(favorites)>20000 or not isinstance(collections,dict) or not isinstance(weekly,dict) or not isinstance(pantry,list) or not isinstance(manual,list) or not isinstance(previous_week,dict) or len(previous_week)>7:raise ValueError('Invalid preferences')
     if len(collections)>100 or len(weekly)>7 or len(pantry)>500 or len(manual)>500:raise ValueError('Too many items')
     state.update(favorites=favorites,collections=collections,weekly=weekly,pantry=pantry,manual=manual,previous_week=previous_week)
    elif action=='generate':
     totals={}
     for recipe in allrecipes():
      scale=state['selected'].get(str(recipe['id']))
      if scale is None:continue
      for name,qty in recipe['ingredients'].items():totals[name]=totals.get(name,0)+qty*scale
     for item in state.get('manual',[]):
      if isinstance(item,str) and item.strip():totals[item.strip()]=totals.get(item.strip(),0)+1
     pantry={str(x).strip().lower() for x in state.get('pantry',[])}
     state['list']=[{'name':name,'quantity':round(qty,3),'purchased':False} for name,qty in sorted(totals.items()) if qty>0 and name.strip().lower() not in pantry]
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
    state['revision']+=1;save(state)
   return self.sendjson(state)
  except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:return self.sendjson({'error':str(e)},400)
if __name__=='__main__':
 if BULK_RECOVER:
  job.update(running=True,paused=False,processed=0,discovered=0,imported=0,skipped=0,errors=0,message='Recovering interrupted import; saved recipes will be skipped')
  threading.Thread(target=bulk_worker,args=(job.get('country','US'),int(job.get('limit',0))),daemon=True).start()
 ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()