const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const src=fs.readFileSync('app/assets/share.js','utf8');
const fake=()=>({style:{},children:[],setAttribute(){},addEventListener(){},append(...x){this.children.push(...x)},appendChild(x){this.children.push(x)},replaceChildren(...x){this.children=x},showModal(){},close(){},parentElement:{insertBefore(){}},querySelector(){return null},querySelectorAll(){return []}});
const doc={body:fake(),createElement:fake,querySelectorAll(){return []},getElementById(){return null}};
const ctx={document:doc,MutationObserver:class{observe(){}},navigator:{},location:{search:'',origin:'https://staging-groceries.greenlee.uk'},URLSearchParams,TextEncoder,TextDecoder,Uint8Array,btoa,atob,alert(){},setInterval(){},setTimeout(){},window:{},recipes:[],state:{},console};
vm.runInNewContext(src,ctx,{filename:'share.js'});
const {encode,decode}=ctx.window.HelloGroceriesShare;
for(const payload of [
 {type:'recipe',ids:[101]},
 {type:'collection',name:'Family dinners',ids:[101,102]},
 {type:'plan',ids:[101,102],servings:[2,4]}
]){
 const decoded=decode(encode(payload));
 assert.equal(decoded.type,payload.type);
 assert.deepEqual(Array.from(decoded.ids),payload.ids);
}
assert.throws(()=>encode({type:'recipe',ids:[]}));
assert.throws(()=>encode({type:'collection',ids:[-1],name:'Bad'}));
assert.throws(()=>decode('not-a-valid-link!'));
assert.throws(()=>decode('a'.repeat(6001)));
console.log('PASS: share links round-trip recipes, collections and meal plans; malformed links rejected');
