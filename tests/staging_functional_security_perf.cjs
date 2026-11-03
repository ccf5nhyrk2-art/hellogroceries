// Non-destructive functional/API and latency checks against the dedicated staging test account.
// Optional STAGING_NONADMIN_EMAIL/PASSWORD enable a real role-boundary assertion.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const base = (process.env.STAGING_URL || '').replace(/\/$/, '');
assert.equal(new URL(base).hostname, 'staging-groceries.greenlee.uk', 'Never test production');
const headers = process.env.CF_ACCESS_CLIENT_ID && process.env.CF_ACCESS_CLIENT_SECRET
  ? {'CF-Access-Client-Id':process.env.CF_ACCESS_CLIENT_ID,'CF-Access-Client-Secret':process.env.CF_ACCESS_CLIENT_SECRET} : {};
const budget = Number(process.env.STAGING_RECIPE_LATENCY_WARN_MS || 10000);
async function login(browser, email, password) {
  const context = await browser.newContext({extraHTTPHeaders:headers});
  const page = await context.newPage();
  await page.goto(base+'/auth.html', {waitUntil:'domcontentloaded'});
  await page.locator('#form').waitFor({state:'visible',timeout:30000});
  await page.locator('#email').fill(email);
  await page.locator('#password').fill(password);
  await page.locator('#submit').click();
  await page.waitForURL(u=>!u.pathname.endsWith('/auth.html'),{timeout:30000});
  return {context,page};
}
async function probe(page, path) {
  const start = Date.now();
  const response = await page.request.get(base+path,{timeout:90000});
  const elapsed = Date.now()-start;
  console.log('LATENCY '+path.split('?')[0]+' '+elapsed+'ms status='+response.status());
  return {response,elapsed};
}
async function main() {
  assert.ok(process.env.STAGING_TEST_EMAIL && process.env.STAGING_TEST_PASSWORD,'Staging test account required');
  const browser = await chromium.launch({headless:true});
  try {
    const {context,page} = await login(browser,process.env.STAGING_TEST_EMAIL,process.env.STAGING_TEST_PASSWORD);
    try {
      const {response,elapsed} = await probe(page,'/api/recipes/page?page=1&page_size=24&q=chicken');
      assert.equal(response.status(),200,'Recipe pagination API');
      const data=await response.json();
      assert.ok(Array.isArray(data.items) && Number.isInteger(data.total) && Number.isInteger(data.pages),'Invalid recipe API response');
      if(elapsed>budget)console.warn('PERFORMANCE WARNING: recipe API exceeded '+budget+'ms; investigate backend scan/indexing');
      console.log('PASS: real recipe API response and timing');
      // Preview computes the shopping list without overwriting the existing list.
      const preview = await page.evaluate(async () => {
        const response = await fetch('/api/action', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'preview_grocery'})});
        return {status:response.status,body:await response.text()};
      });
      assert.equal(preview.status,200,'Shopping list preview API: '+preview.body.slice(0,300));
      const result=JSON.parse(preview.body);
      assert.ok(Array.isArray(result.items),'Shopping preview missing items array');
      console.log('PASS: shopping list generation preview returns structured ingredients (no list mutation)');
      // Bad barcode input is rejected before any paid or external product lookup.
      const invalid = await page.request.get(base+'/api/pantry/barcode?code=invalid');
      assert.equal(invalid.status(),400,'Malformed barcode should be rejected');
      console.log('PASS: pantry barcode input validation');
      const admin = await page.request.get(base+'/api/admin/recipe-source-status');
      assert.ok([200,403].includes(admin.status()),'Unexpected admin API status '+admin.status());
      console.log('PASS: current staging account admin API response '+admin.status()+' (not a non-admin authorization test)');
    } finally {await context.close();}
    const nonadminEmail=process.env.STAGING_NONADMIN_EMAIL;
    const nonadminPassword=process.env.STAGING_NONADMIN_PASSWORD;
    if(Boolean(nonadminEmail)!==Boolean(nonadminPassword))throw Error('Provide both staging non-admin credentials');
    if(nonadminEmail){
      const {context,page}=await login(browser,nonadminEmail,nonadminPassword);
      try{
        const r=await page.request.get(base+'/api/admin/recipe-source-status');
        assert.equal(r.status(),403,'Non-admin account must not access admin API');
        const link=page.locator('a[href="/admin.html"][data-admin-only]');
        if(await link.count())assert.equal(await link.isVisible(),false,'Non-admin should not see admin navigation');
        console.log('PASS: non-admin blocked from admin API and navigation');
      }finally{await context.close();}
    }else{
      console.log('SKIP: non-admin role test requires STAGING_NONADMIN_EMAIL and STAGING_NONADMIN_PASSWORD GitHub secrets');
    }
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
