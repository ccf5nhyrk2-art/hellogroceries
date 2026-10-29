from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from pathlib import Path
import json,sqlite3,threading,re,html,urllib.request,urllib.parse,urllib.robotparser,xml.etree.ElementTree as ET,time,subprocess,sys,fcntl,os,ipaddress,socket
BASE=Path(__file__).parent;DB=Path('/data/shopping.db');DB.parent.mkdir(exist_ok=True,parents=True)
RECIPES=json.loads((BASE/'recipes.json').read_text());lock=threading.Lock()
job={"running":False,"discovered":0,"processed":0,"imported":0,"skipped":0,"errors":0,"message":"Idle"}
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

def official_image(recipe):
 url=recipe.get('url','')
 if not url or not allowed_url(url):return ''
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; PersonalRecipePhoto/1.0)'})
  with urllib.request.urlopen(req,timeout=10) as response:
   if not allowed_url(response.geturl()):return ''
   page=response.read(1500000).decode('utf-8','replace')
  candidates=[]
  for block in re.findall(r'<script\b[^>]*type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>',page,re.I|re.S):
   try:data=json.loads(html.unescape(block))
   except (ValueError,TypeError):continue
   def walk(obj):
    if isinstance(obj,list):
     for item in obj:yield from walk(item)
    elif isinstance(obj,dict):
     if '@graph' in obj:yield from walk(obj['@graph'])
     yield obj
   for node in walk(data):
    types=node.get('@type',[])
    if isinstance(types,str):types=[types]
    if 'Recipe' not in types:continue
    images=node.get('image',[])
    if not isinstance(images,list):images=[images]
    for item in images:
     if isinstance(item,dict):item=item.get('url') or item.get('contentUrl') or ''
     candidates.append(item)
   if candidates:break
  for tag in re.findall(r'<meta\b[^>]*>',page,re.I):
   attrs=dict((k.lower(),html.unescape(v)) for k,v in re.findall(r'([\w:-]+)\s*=\s*["\x27]([^"\x27]*)["\x27]',tag))
   if attrs.get('property','').lower() in ('og:image','og:image:secure_url') or attrs.get('name','').lower() in ('twitter:image','twitter:image:src'):
    candidates.append(attrs.get('content',''))
  for img in candidates:
   if not isinstance(img,str):continue
   img=html.unescape(img).strip()
   parsed=urllib.parse.urlsplit(img)
   if parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password and len(img)<2048:return img
 except Exception:pass
 return ''


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
  return host in ('images.ctfassets.net','res.cloudinary.com','img.hellofresh.com','cdn.hellofresh.com') or host.endswith('.hellofresh.com') or host.endswith('.hellofresh.co.uk') or host.endswith('.hellofresh.com.au') or host.endswith('.hellofresh.co.nz')
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
  if existing:urls.append(existing)
  found=official_image(recipe)
  if found and found not in urls:urls.append(found)
  for url in urls:
   try:
    result=download_photo(url,rid)
    if result:
     if url!=existing:
      with db() as c:c.execute('INSERT OR REPLACE INTO photos(recipe_id,image_url) VALUES(?,?)',(rid,url))
     return result
   except (OSError,ValueError,TimeoutError,urllib.error.URLError):continue
  return cached_photo(rid)

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
 while queue and len(recipes)<limit and sitemaps<30:
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
    if len(recipes)>=limit:break
  except Exception:pass
  time.sleep(0.5)
 return recipes,rp

def bulk_worker(country,limit):
 try:
  urls,rp=discover_sitemap(country,limit)
  job['discovered']=len(urls)
  for url in urls:
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
   time.sleep(1.5)
  job['message']='Completed; some sites may not expose importable recipe data'
 except Exception as e:job['message']='Stopped: '+str(e)
 finally:job['running']=False

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
   if size<1 or size>100000:return self.sendjson({'error':'Too large'},413)
   p=json.loads(self.rfile.read(size));action=p['action']
   if action=='start-us-import':return self.sendjson(start_us_import())
   if action=='bulk':
    country=str(p.get('country','US')).upper();limit=int(p.get('limit',50))
    if country not in ('US','UK','AU','CA','DE','NL') or not 1<=limit<=200:raise ValueError('Choose a supported country and a limit of 1–200')
    with lock:
     if job['running']:raise ValueError('An import is already running')
     job.update(running=True,discovered=0,processed=0,imported=0,skipped=0,errors=0,message='Discovering public recipe pages')
     threading.Thread(target=bulk_worker,args=(country,limit),daemon=True).start()
    return self.sendjson({'ok':True,'message':'Bulk import started'})
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
    elif action=='generate':
     totals={}
     for recipe in allrecipes():
      scale=state['selected'].get(str(recipe['id']))
      if scale is None:continue
      for name,qty in recipe['ingredients'].items():totals[name]=totals.get(name,0)+qty*scale
     state['list']=[{'name':name,'quantity':round(qty,3),'purchased':False} for name,qty in sorted(totals.items()) if qty>0]
    elif action=='clear':state['list']=[]
    elif action=='purchase':
     name=p['name'];found=next((i for i in state['list'] if i['name']==name),None)
     if found is None:raise ValueError('Unknown item')
     found['purchased']=bool(p['purchased'])
    else:raise ValueError('Unknown action')
    state['revision']+=1;save(state)
   return self.sendjson(state)
  except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:return self.sendjson({'error':str(e)},400)
if __name__=='__main__':ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()