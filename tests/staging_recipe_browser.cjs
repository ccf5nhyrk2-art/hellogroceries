// Read-only end-to-end tests: never mutate a real household.
const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const base = (process.env.STAGING_URL || 'https://staging-groceries.greenlee.uk').replace(/\/$/, '');
const email = process.env.STAGING_TEST_EMAIL;
const password = process.env.STAGING_TEST_PASSWORD;
async function main() {
  if (!email || !password) {
    throw new Error('Set STAGING_TEST_EMAIL and STAGING_TEST_PASSWORD secrets for a dedicated staging-only test account.');
  }
  const browser = await chromium.launch({ headless: true });
  const accessId = process.env.CF_ACCESS_CLIENT_ID;
  const accessSecret = process.env.CF_ACCESS_CLIENT_SECRET;
  if (Boolean(accessId) !== Boolean(accessSecret)) throw new Error('Configure both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET.');
  const page = await browser.newPage({
    viewport: { width: 1280, height: 900 },
    extraHTTPHeaders: accessId ? {
      'CF-Access-Client-Id': accessId,
      'CF-Access-Client-Secret': accessSecret
    } : {}
  });
  fs.mkdirSync('test-results', { recursive: true });
  const errors = [];
  page.on('pageerror', e => errors.push('page: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400 && r.url().startsWith(base)) errors.push('HTTP ' + r.status() + ' ' + r.url()); });
  try {
    const initial = await page.goto(base + '/auth.html', { waitUntil: 'domcontentloaded' });
    console.log('Login landing: status=' + (initial?.status() ?? 'unknown') + ' origin=' + new URL(page.url()).origin);
    if (new URL(page.url()).hostname.endsWith('.cloudflareaccess.com')) {
      throw new Error('Cloudflare Access blocked the test. Configure a staging-scoped Cloudflare Access service token and GitHub secrets CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET; allow Service Auth for the staging Access application. Do not disable Cloudflare Access.');
    }
    try {
      await page.locator('#form').waitFor({ state: 'visible', timeout: 20000 });
    } catch (error) {
      const status = await page.locator('#mode').textContent().catch(() => 'missing');
      const message = await page.locator('#error').textContent().catch(() => 'missing');
      const title = await page.title().catch(() => 'unknown');
      const diagnosis = { status: initial?.status(), url: page.url(), title, authMode: status, authError: message, consoleErrors: errors };
      fs.writeFileSync('test-results/diagnostics.json', JSON.stringify(diagnosis, null, 2));
      throw new Error('Staging login form not visible: ' + JSON.stringify(diagnosis) + '; ' + error.message);
    }
    await page.locator('#email').fill(email);
    await page.locator('#password').fill(password);
    await page.locator('#submit').click();
    await page.waitForURL(url => !url.pathname.endsWith('/auth.html'), { timeout: 20000 });
    // Login intentionally lands on Home; navigate to Recipes before testing its controls.
    await page.locator('.page-nav-button[data-page="recipes"]').waitFor({ state: 'attached', timeout: 30000 });
    await page.locator('.page-nav-button[data-page="recipes"]').evaluate(button => button.click());
    await page.locator('#fast-browse-toggle').waitFor({ state: 'visible', timeout: 30000 });
    await page.locator('#fast-browse-toggle').click();
    const cards = page.locator('#fast-browse-results .recipe-card');
    await cards.first().waitFor({ timeout: 30000 });
    assert.ok(await cards.count() <= 24, 'Server returned more than 24 cards');
    const firstPageTitle = await cards.first().locator('.recipe-card-title').innerText();
    assert.ok(firstPageTitle.trim(), 'First recipe has no title');
    const pageInfo = await page.locator('#fast-browse-pageinfo').innerText();
    assert.match(pageInfo, /Page 1 of \d+/);
    console.log('PASS: initial 24-card page and page count');
    const searchResponses = [];
    page.on('response', response => {
      if (response.url().includes('/api/recipes/page')) searchResponses.push({ status: response.status(), url: response.url().split('?')[0], query: new URL(response.url()).searchParams.get('q') });
    });
    await page.locator('#fast-browse-search').fill('chicken');
    await page.locator('#fast-browse-search-button').click();
    try {
      await page.waitForFunction(() => {
        const panel = document.querySelector('#fast-browse-results');
        const cards = [...panel.querySelectorAll('.recipe-card')];
        return cards.length > 0 && cards.every(card => card.querySelector('.recipe-card-title')?.textContent?.toLowerCase().includes('chicken'));
      }, null, { timeout: 30000 });
    } catch (error) {
      throw new Error('Search did not render matching cards. API responses=' + JSON.stringify(searchResponses) + '; panel=' + (await page.locator('#fast-browse-results').innerText()).slice(0, 500) + '; ' + error.message);
    }
    const searchTitles = await cards.locator('.recipe-card-title').allInnerTexts();
    assert.ok(searchTitles.length > 0 && searchTitles.every(t => t.toLowerCase().includes('chicken')), 'Search results do not match chicken');
    console.log('PASS: server-side chicken search');
    const detail = cards.first().locator('[data-fast-detail]');
    assert.ok(await detail.isVisible(), 'Missing cooking details button');
    assert.ok(await cards.first().locator('[data-fast-favorite]').isVisible(), 'Missing favorite button');
    assert.ok(await cards.first().locator('[data-fast-plan]').isVisible(), 'Missing meal planning button');
    assert.ok(await cards.first().locator('[data-fast-collection]').isVisible(), 'Missing collection selector');
    console.log('PASS: cooking details, favorite, plan and collection controls exist (read-only)');
    if (await page.locator('#fast-browse-next').isEnabled()) {
      await page.locator('#fast-browse-next').click();
      await page.waitForFunction(() => document.querySelector('#fast-browse-pageinfo')?.textContent?.startsWith('Page 2 of'));
      assert.ok(await cards.count() > 0, 'Page 2 is empty');
      await page.locator('#fast-browse-prev').click();
      await page.waitForFunction(() => document.querySelector('#fast-browse-pageinfo')?.textContent?.startsWith('Page 1 of'));
      console.log('PASS: next and previous pagination');
    }
    const api = await page.request.get(base + '/api/recipes/page?page=1&page_size=24&q=chicken');
    assert.equal(api.status(), 200);
    const json = await api.json();
    assert.ok(Array.isArray(json.items) && Number.isInteger(json.total) && Number.isInteger(json.pages));
    console.log('PASS: paginated API response');
  } catch (error) {
    await page.screenshot({ path: 'test-results/failure.png', fullPage: true }).catch(() => {});
    fs.writeFileSync('test-results/page.html', await page.content().catch(() => 'Unavailable'));
    if (!fs.existsSync('test-results/diagnostics.json')) fs.writeFileSync('test-results/diagnostics.json', JSON.stringify({ url: page.url(), consoleErrors: errors }, null, 2));
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
