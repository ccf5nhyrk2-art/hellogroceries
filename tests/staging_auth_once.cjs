// One login per workflow run. The state file stays on the runner and is never uploaded.
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const base=(process.env.STAGING_URL||'').replace(/\/$/,'');
const output=process.env.STAGING_AUTH_STATE||'test-results/staging-auth-state.json';
async function main(){
 if(new URL(base).hostname!=='staging-groceries.greenlee.uk')throw Error('Staging only');
 if(!process.env.STAGING_TEST_EMAIL||!process.env.STAGING_TEST_PASSWORD)throw Error('Staging credentials missing');
 const browser=await chromium.launch({headless:true});
 try{
  const headers=process.env.CF_ACCESS_CLIENT_ID&&process.env.CF_ACCESS_CLIENT_SECRET?{'CF-Access-Client-Id':process.env.CF_ACCESS_CLIENT_ID,'CF-Access-Client-Secret':process.env.CF_ACCESS_CLIENT_SECRET}:{};
  const context=await browser.newContext({extraHTTPHeaders:headers});
  const page=await context.newPage();
  await page.goto(base+'/auth.html',{waitUntil:'domcontentloaded'});
  await page.locator('#form').waitFor({state:'visible',timeout:30000});
  await page.locator('#email').fill(process.env.STAGING_TEST_EMAIL);
  await page.locator('#password').fill(process.env.STAGING_TEST_PASSWORD);
  await page.locator('#submit').click();
  await page.waitForURL(url=>!url.pathname.endsWith('/auth.html'),{timeout:30000,waitUntil:'domcontentloaded'}).catch(async error=>{
   throw Error('Staging login failed: '+(await page.locator('#error').textContent().catch(()=>''))+'; '+error.message);
  });
  await page.locator('.page-nav-button[data-page="recipes"]').waitFor({state:'attached',timeout:30000});
  fs.mkdirSync(path.dirname(output),{recursive:true});
  await context.storageState({path:output});
  console.log('PASS: authenticated staging test session saved for reuse across suites');
  await context.close();
 }finally{await browser.close()}
}
main().catch(error=>{console.error(error);process.exitCode=1});
