// Staging-only read-only coverage for shopping, pantry scanner and admin access.
// Never upload images, generate lists, modify household state or change admin settings.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const base = (process.env.STAGING_URL || '').replace(/\/$/, '');
const email = process.env.STAGING_TEST_EMAIL;
const password = process.env.STAGING_TEST_PASSWORD;
async function main() {
  assert.equal(new URL(base).hostname, 'staging-groceries.greenlee.uk', 'Never run against production');
  assert.ok(email && password, 'Dedicated staging test account secrets required');
  const browser = await chromium.launch({headless:true});
  const headers = process.env.CF_ACCESS_CLIENT_ID && process.env.CF_ACCESS_CLIENT_SECRET ? {
    'CF-Access-Client-Id':process.env.CF_ACCESS_CLIENT_ID,
    'CF-Access-Client-Secret':process.env.CF_ACCESS_CLIENT_SECRET
  } : {};
  const page = await browser.newPage({viewport:{width:1280,height:900},extraHTTPHeaders:headers});
  try {
    await page.goto(base+'/auth.html',{waitUntil:'domcontentloaded'});
    await page.locator('#form').waitFor({state:'visible',timeout:30000});
    await page.locator('#email').fill(email);
    await page.locator('#password').fill(password);
    await page.locator('#submit').click();
    await page.waitForURL(url=>!url.pathname.endsWith('/auth.html'),{timeout:30000});
    await page.locator('#generate-grocery').waitFor({state:'attached',timeout:30000});
    // Shopping controls are present and wired; avoid generating or clearing user data.
    assert.ok(await page.locator('#shop-generate-tour').count());
    assert.ok(await page.locator('#generate-grocery').count());
    assert.ok(await page.locator('#items').count());
    assert.ok(await page.locator('#summary').count());
    await page.locator('.page-nav-button[data-page="shopping"]').first().evaluate(el=>el.click());
    assert.ok(await page.locator('#generate-grocery').isVisible(),'Shopping list generator is hidden');
    console.log('PASS: shopping list navigation and generation controls');
    // Pantry camera/upload must be opt-in. Inspect buttons, input and review controls only.
    await page.locator('.page-nav-button[data-page="pantry"]').first().evaluate(el=>el.click());
    for(const id of ['#pantry-input','#pantry-add','#pantry-scan-start','#pantry-scan-receipt','#pantry-barcode-open','#pantry-scan-file','#pantry-scan-results']){
      assert.ok(await page.locator(id).count(), 'Missing pantry element: '+id);
    }
    assert.ok(await page.locator('#pantry-scan-start').isVisible(),'Grocery scan button hidden');
    assert.ok(await page.locator('#pantry-scan-receipt').isVisible(),'Receipt scan button hidden');
    assert.ok(await page.locator('#pantry-barcode-open').isVisible(),'Barcode scan button hidden');
    console.log('PASS: pantry scanner, receipt and barcode UI controls');
    // Role-sensitive admin access. Do not assume the staging account is an administrator.
    const adminResponse=await page.request.get(base+'/admin.html',{maxRedirects:0});
    assert.ok([200,302,303,401,403].includes(adminResponse.status()),'Unexpected admin response '+adminResponse.status());
    const adminLink=page.locator('a[href="/admin.html"][data-admin-only]');
    assert.ok(await adminLink.count(),'Admin link missing from application markup');
    if(adminResponse.status()===200){
      const content=await adminResponse.text();
      assert.ok(content.includes('admin') || content.includes('Admin'),'Admin page missing expected content');
      console.log('PASS: admin page accessible for staging account (admin-only authorization requires separate non-admin test)');
    }else{
      console.log('PASS: admin route not accessible to staging account, HTTP '+adminResponse.status());
    }
    console.log('PASS: read-only shopping/pantry/admin smoke suite completed');
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
