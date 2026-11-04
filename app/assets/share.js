/* Explicit, account-to-account sharing. Links contain only chosen recipe IDs and optional
   collection name/servings; never household IDs, pantry data or private credentials. */
(function(){
'use strict';
const MAX=100, MAX_LINK=6000;
function encode(payload){
 const ids=payload.ids;
 if(!['recipe','collection','plan'].includes(payload.type)||!Array.isArray(ids)||!ids.length||ids.length>MAX||!ids.every(id=>Number.isSafeInteger(Number(id))&&Number(id)>0))throw Error('Invalid shared recipes');
 const clean={v:1,type:payload.type,ids:[...new Set(ids.map(Number))]};
 if(payload.type==='collection')clean.name=String(payload.name||'Shared recipes').slice(0,80);
 if(payload.type==='plan')clean.servings=clean.ids.map((id,i)=>Math.max(.5,Math.min(12,Number(payload.servings?.[i])||2)));
 const bytes=new TextEncoder().encode(JSON.stringify(clean));
 let binary='';for(const b of bytes)binary+=String.fromCharCode(b);
 return btoa(binary).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
}
function decode(encoded){
 if(!encoded||encoded.length>MAX_LINK||!/^[A-Za-z0-9_-]+$/.test(encoded))throw Error('Invalid sharing link');
 const raw=atob(encoded.replace(/-/g,'+').replace(/_/g,'/'));
 const data=JSON.parse(new TextDecoder().decode(Uint8Array.from(raw,c=>c.charCodeAt(0))));
 if(data.v!==1)throw Error('Unsupported sharing link');
 const valid=encode(data);if(valid!==encoded.replace(/=+$/,''))throw Error('Invalid sharing link');
 return data;
}
function node(tag,attrs={},text=''){const el=document.createElement(tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text)el.textContent=text;return el;}
const dialog=node('dialog',{id:'hg-share-dialog','aria-label':'Share HelloGroceries content'});
dialog.style.cssText='border:1px solid #23875e;border-radius:16px;padding:22px;max-width:min(92vw,480px);background:#1b2420;color:#f2f7f3;box-shadow:0 16px 60px #0008';
const title=node('h3',{},'Share with HelloGroceries');const message=node('p');
const actions=node('div');actions.style.cssText='display:flex;flex-wrap:wrap;gap:8px';
const close=node('button',{type:'button'},'Close');close.onclick=()=>dialog.close();
dialog.append(title,message,actions,close);document.body.append(dialog);
async function share(payload){
 let url;try{url=location.origin+'/?share='+encode(payload)}catch(e){alert(e.message);return}
 const label=payload.type==='recipe'?'recipe':payload.type==='plan'?'meal plan':'collection';
 message.textContent='Anyone with this link can view the shared '+label+' after signing in. Only the selected recipe IDs are included; your household and pantry stay private.';
 actions.replaceChildren();
 const copy=node('button',{type:'button'},'Copy link');copy.onclick=async()=>{try{await navigator.clipboard.writeText(url);copy.textContent='Copied!'}catch(e){prompt('Copy this link:',url)}};
 actions.append(copy);
 if(navigator.share){const native=node('button',{type:'button'},'Share…');native.onclick=async()=>{try{await navigator.share({title:'HelloGroceries '+label,url})}catch(e){if(e.name!=='AbortError')alert('Sharing unavailable: '+e.message)}};actions.append(native)}
 dialog.showModal();
}
function available(ids){const known=new Set(recipes.map(r=>Number(r.id)));return ids.filter(id=>known.has(Number(id)))}
function addButton(container,text,click){const b=node('button',{type:'button','data-hg-share-button':'1'},text);b.style.cssText='margin:5px;padding:8px 12px;border:1px solid #23875e;border-radius:10px;background:transparent;color:inherit';b.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();click()});container.append(b);return b}
function enhance(){
 document.querySelectorAll('.recipe-card').forEach(card=>{
  if(card.querySelector('[data-hg-share-button]'))return;
  const id=card.querySelector('[data-fast-detail],[data-detail]')?.getAttribute('data-fast-detail')||card.querySelector('[data-detail]')?.getAttribute('data-detail');
  if(id)addButton(card,'Share',()=>share({type:'recipe',ids:[Number(id)]}));
 });
 const collections=document.getElementById('my-collection-cards');
 if(collections&&!document.getElementById('hg-share-collection')){
  const wrap=node('div',{id:'hg-share-collection'});wrap.style.cssText='display:flex;gap:8px;flex-wrap:wrap;margin:10px 0';
  const select=node('select',{'aria-label':'Collection to share'});select.style.cssText='max-width:220px';
  const fill=()=>{const current=select.value;select.replaceChildren(node('option',{value:''},'Choose a collection'));Object.keys(state.collections||{}).forEach(name=>select.append(node('option',{value:name},name)));select.value=current};
  fill();select.addEventListener('focus',fill);wrap.append(select);
  addButton(wrap,'Share collection',()=>{const name=select.value;if(!name)return alert('Choose a collection first');const ids=state.collections?.[name]||[];if(!ids.length)return alert('This collection is empty');share({type:'collection',name,ids})});
  collections.parentElement.insertBefore(wrap,collections);
 }
 const selected=document.getElementById('selected-plan-list');
 if(selected&&!document.getElementById('hg-share-plan')){
  const wrap=node('div',{id:'hg-share-plan'});
  addButton(wrap,'Share meal plan',()=>{const entries=Object.entries(state.selected||{});if(!entries.length)return alert('Choose some meals first');share({type:'plan',ids:entries.map(([id])=>Number(id)),servings:entries.map(([,n])=>Number(n)||2)})});
  selected.parentElement.insertBefore(wrap,selected);
 }
}
let busy=false;
async function incoming(){
 const params=new URLSearchParams(location.search),token=params.get('share');
 if(!token||busy)return;
 busy=true;
 try{
  const data=decode(token),ids=available(data.ids);
  if(!ids.length)throw Error('These recipes are not available in this catalog.');
  const missing=data.ids.length-ids.length;
  message.textContent=(data.type==='recipe'?'Shared recipe':data.type==='collection'?'Shared collection: '+data.name:'Shared meal plan')+' — '+ids.length+' available recipe(s)'+(missing?' ('+missing+' unavailable)':'')+'. Save a copy to your account?';
  actions.replaceChildren();
  const save=node('button',{type:'button'},'Save to my account');
  save.onclick=async()=>{
   save.disabled=true;
   try{
    if(data.type==='recipe'){await preferences({favorites:[...new Set([...(state.favorites||[]).map(Number),...ids])]})}
    if(data.type==='collection'){
     const collections={...(state.collections||{})};let name=data.name||'Shared recipes',n=2;
     while(Object.hasOwn(collections,name))name=(data.name||'Shared recipes')+' ('+(n++)+')';
     collections[name]=ids;await preferences({collections});
    }
    if(data.type==='plan'){
     for(const id of ids){const index=data.ids.indexOf(id);await selectMealAndRefresh(id,true,data.servings?.[index]||2)}
     const current=state.selected||{};if(ids.some(id=>!Object.hasOwn(current,String(id))))throw Error('Some meals could not be saved');
    }
    history.replaceState(null,'',location.pathname);
    message.textContent='Saved to your HelloGroceries account.';
    actions.replaceChildren();
   }catch(e){alert('Unable to save shared content: '+e.message);save.disabled=false}
  };
  actions.append(save);dialog.showModal();
 }catch(e){message.textContent='This share link cannot be opened: '+e.message;actions.replaceChildren();dialog.showModal()}
 finally{busy=false}
}
let scheduled=false;
const observer=new MutationObserver(()=>{if(scheduled)return;scheduled=true;setTimeout(()=>{scheduled=false;enhance()},150)});
observer.observe(document.body,{childList:true,subtree:true});
enhance();
if(new URLSearchParams(location.search).has('share')){
 let attempts=0;const timer=setInterval(()=>{if(recipes.length&&state&&Object.keys(state).length){clearInterval(timer);incoming()}else if(++attempts>90){clearInterval(timer);message.textContent='Could not load the recipe catalog. Refresh to try again.';dialog.showModal()}},500);
}
window.HelloGroceriesShare={encode,decode,share};
})();
