const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const base=(process.env.STAGING_URL||'').replace(/\/$/,'');
async function main(){
 assert.equal(new URL(base).hostname,'staging-groceries.greenlee.uk','Staging only');
 const browser=await chromium.launch({headless:true});
 try{
  const headers=process.env.CF_ACCESS_CLIENT_ID&&process.env.CF_ACCESS_CLIENT_SECRET?{'CF-Access-Client-Id':process.env.CF_ACCESS_CLIENT_ID,'CF-Access-Client-Secret':process.env.CF_ACCESS_CLIENT_SECRET}:{};
  const page=await browser.newPage({extraHTTPHeaders:headers});
  await page.goto(base+'/auth.html',{waitUntil:'domcontentloaded'});
  await page.locator('#form').waitFor({state:'visible',timeout:30000});
  await page.locator('#email').fill(process.env.STAGING_TEST_EMAIL);
  await page.locator('#password').fill(process.env.STAGING_TEST_PASSWORD);
  await page.locator('#submit').click();
  await page.waitForURL(u=>!u.pathname.endsWith('/auth.html'),{timeout:30000});
  await page.locator('.page-nav-button[data-page="recipes"]').waitFor({state:'attached',timeout:30000});
  const installed=await page.evaluate(()=>Boolean(window.HelloGroceriesShare));
  if(!installed){console.log('SKIP: share UI is not deployed to staging yet; update staging container before E2E validation');return}
  const result=await page.evaluate(()=>{
   const share=window.HelloGroceriesShare;
   const payload={type:'collection',name:'Shared Test',ids:[101,102]};
   return share.decode(share.encode(payload));
  });
  assert.equal(result.type,'collection');assert.equal(result.name,'Shared Test');
  assert.deepEqual(result.ids,[101,102]);
  await page.locator('.page-nav-button[data-page="recipes"]').first().evaluate(el=>el.click());
  await page.locator('#fast-browse-toggle').click();
  await page.locator('#fast-browse-results [data-hg-share-button]').first().waitFor({timeout:90000});
  await page.locator('#fast-browse-results [data-hg-share-button]').first().click();
  assert.ok(await page.locator('#hg-share-dialog').isVisible(),'Share dialog did not open');
  assert.ok(await page.locator('#hg-share-dialog button').allTextContents().then(x=>x.some(v=>v.includes('Copy link'))),'Copy link action missing');
  console.log('PASS: deployed staging sharing UI and recipe share dialog');
 }finally{await browser.close()}
}
main().catch(e=>{console.error(e);process.exitCode=1});
