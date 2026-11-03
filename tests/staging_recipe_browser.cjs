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
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  fs.mkdirSync('test-results', { recursive: true });
  try {
    await page.goto(base + '/auth.html', { waitUntil: 'domcontentloaded' });
    await page.locator('#form').waitFor({ state: 'visible', timeout: 20000 });
    await page.locator('#email').fill(email);
    await page.locator('#password').fill(password);
    await page.locator('#submit').click();
    await page.waitForURL(url => !url.pathname.endsWith('/auth.html'), { timeout: 20000 });
    await page.locator('#fast-browse-toggle').waitFor({ timeout: 30000 });
    await page.locator('#fast-browse-toggle').click();
    const cards = page.locator('#fast-browse-results .recipe-card');
    await cards.first().waitFor({ timeout: 30000 });
    assert.ok(await cards.count() <= 24, 'Server returned more than 24 cards');
    const firstPageTitle = await cards.first().locator('.recipe-card-title').innerText();
    assert.ok(firstPageTitle.trim(), 'First recipe has no title');
    const pageInfo = await page.locator('#fast-browse-pageinfo').innerText();
    assert.match(pageInfo, /Page 1 of \d+/);
    console.log('PASS: initial 24-card page and page count');
    await page.locator('#fast-browse-search').fill('chicken');
    await page.locator('#fast-browse-search-button').click();
    await page.waitForFunction(() => document.querySelector('#fast-browse-pageinfo')?.textContent?.includes('Page 1 of') && document.querySelector('#fast-browse-results .recipe-card'));
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
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
