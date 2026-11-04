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
    storageState: process.env.STAGING_AUTH_STATE || undefined,
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
    if (process.env.STAGING_AUTH_STATE) { await page.goto(base+'/',{waitUntil:'domcontentloaded'}); } else {
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
    }
    // Login intentionally lands on Home; navigate to Recipes before testing its controls.
    await page.locator('.page-nav-button[data-page="recipes"]').waitFor({ state: 'attached', timeout: 30000 });
    await page.locator('.page-nav-button[data-page="recipes"]').evaluate(button => button.click());
    // Read-only Plan page regression: navigation, selected meal count and controls.
    await page.locator('.page-nav-button[data-page="planner"]').evaluate(button => button.click());
    await page.waitForFunction(() => document.querySelector('#selected-plan-list') && typeof state !== 'undefined');
    const planSnapshot = await page.evaluate(() => ({
      expected: Object.keys(state.selected || {}).length,
      displayed: document.querySelectorAll('#selected-plan-list .selected-meal').length,
      missingControls: [...document.querySelectorAll('#selected-plan-list .selected-meal')].filter(card => !card.querySelector('[data-plan-scale]') || !card.querySelector('[data-remove-meal]')).length,
      hasWeekGrid: !!document.querySelector('#week-grid')
    }));
    assert.equal(planSnapshot.displayed, planSnapshot.expected, 'Plan selected meals count does not match saved state');
    assert.equal(planSnapshot.missingControls, 0, 'Plan meal serving/remove controls missing');
    assert.ok(planSnapshot.hasWeekGrid, 'Weekly planner missing');
    console.log('PASS: Plan page selected meals, controls and weekly planner (' + planSnapshot.expected + ' selected)');
    await page.locator('.page-nav-button[data-page="recipes"]').evaluate(button => button.click());
    // Verify visible recommendations do not repeat a recipe family.
    await page.locator('#recommend-list').waitFor({ state: 'attached', timeout: 30000 });
    await page.waitForTimeout(2500);
    const recommendationTitles = await page.locator('#recommend-list .recommend-title').allInnerTexts();
    const families = recommendationTitles.map(title => title.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(' ').slice(0, 5).join(' '));
    assert.equal(new Set(families).size, families.length, 'Duplicate recommendation family: ' + JSON.stringify(recommendationTitles));
    console.log('PASS: visible recommendations contain no duplicate title families (' + families.length + ' checked)');
    // Ensure recommendation images invoke the recovery path when the photo API fails.
    const recommendationPhotoWiring = await page.evaluate(() => {
      const img = document.querySelector('#recommend-list .recommend-photo');
      return { count: document.querySelectorAll('#recommend-list .recommend-photo').length,
        wired: !img || img.getAttribute('onerror') === 'recommendationPhotoFailed(this)' };
    });
    assert.ok(recommendationPhotoWiring.wired, 'Recommendation images lack a photo failure handler');
    console.log('PASS: recommendation photo error recovery is wired');
    await page.locator('#fast-browse-toggle').waitFor({ state: 'attached', timeout: 30000 });
    await page.locator('#fast-browse-toggle').evaluate(button => {
      // The legacy pagination panel is intentionally hidden from customers.
      // Reveal it only in this automated regression test.
      button.parentElement.style.display = '';
      document.querySelector('#fast-browse-panel').style.display = '';
      button.click();
    });
    const cards = page.locator('#fast-browse-results .recipe-card');
    await cards.first().waitFor({ timeout: 120000 }).catch(async error=>{
      const panel=(await page.locator('#fast-browse-results').innerText()).slice(0,600);
      throw new Error('Recipe browser failed to render cards; panel='+panel+'; '+error.message);
    });
    assert.ok(await cards.count() <= 24, 'Server returned more than 24 cards');
    const firstPageTitle = await cards.first().locator('.recipe-card-title').innerText();
    assert.ok(firstPageTitle.trim(), 'First recipe has no title');
    const pageInfo = await page.locator('#fast-browse-pageinfo').innerText();
    assert.match(pageInfo, /Page 1 of \d+/);
    console.log('PASS: initial 24-card page and page count');
    const searchRequests = [];
    const isRecipePage = url => url.includes('/api/recipes/page');
    page.on('request', request => {
      if (isRecipePage(request.url())) searchRequests.push({ event: 'started', query: new URL(request.url()).searchParams.get('q') });
    });
    page.on('response', response => {
      if (isRecipePage(response.url())) searchRequests.push({ event: 'response', status: response.status(), query: new URL(response.url()).searchParams.get('q') });
    });
    page.on('requestfailed', request => {
      if (isRecipePage(request.url())) searchRequests.push({ event: 'failed', error: request.failure()?.errorText, query: new URL(request.url()).searchParams.get('q') });
    });
    await page.locator('#fast-browse-search').fill('chicken');
    await page.locator('#fast-browse-search-button').click();
    try {
      await page.waitForFunction(() => {
        const panel = document.querySelector('#fast-browse-results');
        const cards = [...panel.querySelectorAll('.recipe-card')];
        return cards.length > 0 && cards.every(card => card.querySelector('.recipe-card-title')?.textContent?.toLowerCase().includes('chicken'));
      }, null, { timeout: 90000 });
    } catch (error) {
      throw new Error('Search did not render matching cards. API requests=' + JSON.stringify(searchRequests) + '; panel=' + (await page.locator('#fast-browse-results').innerText()).slice(0, 500) + '; ' + error.message);
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
      await page.waitForFunction(() => document.querySelector('#fast-browse-pageinfo')?.textContent?.startsWith('Page 2 of'), null, { timeout: 90000 });
      assert.ok(await cards.count() > 0, 'Page 2 is empty');
      await page.locator('#fast-browse-prev').click();
      try {
        await page.waitForFunction(() => document.querySelector('#fast-browse-pageinfo')?.textContent?.startsWith('Page 1 of'), null, { timeout: 90000 });
      } catch (error) {
        const diagnostics = await page.evaluate(() => ({ pageInfo: document.querySelector('#fast-browse-pageinfo')?.textContent, results: document.querySelector('#fast-browse-results')?.textContent?.slice(0, 300), prevDisabled: document.querySelector('#fast-browse-prev')?.disabled, nextDisabled: document.querySelector('#fast-browse-next')?.disabled }));
        throw new Error('Previous-page navigation stalled: ' + JSON.stringify({ diagnostics, requests: searchRequests.slice(-15) }) + '; ' + error.message);
      }
      console.log('PASS: next and previous pagination');
    }
    const api = await page.request.get(base + '/api/recipes/page?page=1&page_size=24&q=chicken');
    assert.equal(api.status(), 200);
    const json = await api.json();
    assert.ok(Array.isArray(json.items) && Number.isInteger(json.total) && Number.isInteger(json.pages));
    console.log('PASS: paginated API response');
    // Mutations are restricted to the dedicated staging test account and restored in finally.
    assert.equal(new URL(base).hostname, 'staging-groceries.greenlee.uk', 'Persistence tests must never run against PROD');
    const targetId = await cards.first().locator('[data-fast-favorite]').getAttribute('data-fast-favorite');
    const testCollection = '__staging_smoke_test__';
    const original = await page.evaluate(id => ({
      favorites: structuredClone(state.favorites || []),
      collections: structuredClone(state.collections || {}),
      selected: Object.hasOwn(state.selected || {}, String(id))
    }), targetId);
    try {
      // Start from a known state, preserving the original snapshot for cleanup.
      await page.evaluate(async ({ id, name }) => {
        if (Object.hasOwn(state.selected || {}, String(id))) await selectMealAndRefresh(Number(id), false, 2);
        await preferences({ favorites: (state.favorites || []).filter(value => Number(value) !== Number(id)), collections: { ...(state.collections || {}), [name]: [] } });
        await loadFastBrowse();
      }, { id: targetId, name: testCollection });
      const saveEvents = [];
      page.on('dialog', async dialog => { saveEvents.push({ dialog: dialog.message() }); await dialog.dismiss(); });
      page.on('response', response => {
        if (response.url().includes('/api/action')) saveEvents.push({ status: response.status(), endpoint: '/api/action' });
      });
      await page.locator('#fast-browse-results [data-fast-favorite="' + targetId + '"]').click();
      try {
        await page.waitForFunction(id => (state.favorites || []).map(Number).includes(Number(id)), targetId, { timeout: 90000 });
      } catch (error) {
        throw new Error('Favorite not saved: ' + JSON.stringify(saveEvents) + '; ' + error.message);
      }
      // Collection save comes first: selecting a meal can remove its card from
      // the current filtered result set before the collection selector is used.
      await page.locator('#fast-browse-results [data-fast-collection="' + targetId + '"]').selectOption(testCollection);
      await page.waitForFunction(([id, name]) => (state.collections?.[name] || []).map(Number).includes(Number(id)), [targetId, testCollection], { timeout: 30000 });
      await page.waitForFunction(id => !!document.querySelector('#fast-browse-results [data-fast-plan="' + id + '"]'), targetId, { timeout: 90000 });
      await page.locator('#fast-browse-results [data-fast-plan="' + targetId + '"]').evaluate(async button => {
        button.click();
        await mealSelectionQueue;
      });
      await page.waitForFunction(id => Object.hasOwn(state.selected || {}, String(id)), targetId, { timeout: 30000 });
      // Capture server responses during reload to distinguish hydration failure from lost data.
      const reloadResponses = [];
      const trackReloadResponse = response => {
        if (response.url().startsWith(base) && /\/api\//.test(response.url())) {
          reloadResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
        }
      };
      page.on('response', trackReloadResponse);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.locator('.page-nav-button[data-page="recipes"]').waitFor({ state: 'attached', timeout: 30000 });
      await page.locator('.page-nav-button[data-page="recipes"]').evaluate(button => button.click());
      await page.locator('#fast-browse-toggle').evaluate(button => {
      // The legacy pagination panel is intentionally hidden from customers.
      // Reveal it only in this automated regression test.
      button.parentElement.style.display = '';
      document.querySelector('#fast-browse-panel').style.display = '';
      button.click();
    });
      const persistedSnapshot = async () => page.evaluate(([id, name]) => ({
        favorite: typeof state !== 'undefined' && (state.favorites || []).map(Number).includes(Number(id)),
        selected: typeof state !== 'undefined' && Object.hasOwn(state.selected || {}, String(id)),
        collection: typeof state !== 'undefined' && (state.collections?.[name] || []).map(Number).includes(Number(id)),
        favoriteCount: typeof state !== 'undefined' ? (state.favorites || []).length : null,
        selectedCount: typeof state !== 'undefined' ? Object.keys(state.selected || {}).length : null,
        collectionCount: typeof state !== 'undefined' ? (state.collections?.[name] || []).length : null,
        pathname: location.pathname
      }), [targetId, testCollection]);
      try {
        await page.waitForFunction(([id, name]) => {
          return typeof state !== 'undefined'
            && (state.favorites || []).map(Number).includes(Number(id))
            && Object.hasOwn(state.selected || {}, String(id))
            && (state.collections?.[name] || []).map(Number).includes(Number(id));
        }, [targetId, testCollection], { timeout: 30000 });
      } catch (error) {
        const snapshot = await persistedSnapshot().catch(e => ({ diagnosticError: e.message }));
        snapshot.reloadResponses = reloadResponses;
        snapshot.pageErrors = errors.slice(-15);
        throw new Error('Post-reload persistence check failed: ' + JSON.stringify(snapshot) + '; ' + error.message);
      }
      console.log('PASS: favorite, meal plan and collection persisted after reload');
      // Exercise the actual customer-facing recipe cards, not just the hidden fast browser.
      await page.waitForFunction(() => !!document.querySelector('#recipes [data-favorite]'), null, { timeout: 30000 });
      const visibleId = await page.locator('#recipes [data-favorite]').first().getAttribute('data-favorite');
      const visibleBefore = await page.evaluate(id => ({
        favorite: (state.favorites || []).map(Number).includes(Number(id)),
        collections: structuredClone(state.collections || {})
      }), visibleId);
      try {
        await page.locator('#recipes [data-favorite="' + visibleId + '"]').click();
        await page.waitForFunction(([id, was]) => (state.favorites || []).map(Number).includes(Number(id)) !== was,
          [visibleId, visibleBefore.favorite], { timeout: 30000 });
        try {
          await page.waitForFunction(([id, expected]) => {
            const button = document.querySelector('#recipes [data-favorite="' + id + '"]');
            return button && button.textContent.includes('Favorited') === expected;
          }, [visibleId, !visibleBefore.favorite], { timeout: 30000 });
        } catch (error) {
          const snapshot = await page.evaluate(id => ({
            button: document.querySelector('#recipes [data-favorite="' + id + '"]')?.outerHTML,
            savedFavorite: (state.favorites || []).map(Number).includes(Number(id)),
            pageGeneration: typeof pageRequestGeneration !== 'undefined' ? pageRequestGeneration : null,
            visibleCards: document.querySelectorAll('#recipes [data-favorite]').length
          }), visibleId);
          throw new Error('Visible favorite button did not refresh: ' + JSON.stringify(snapshot) + '; ' + error.message);
        }
        await page.evaluate(async ({ name }) => {
          await preferences({ collections: { ...(state.collections || {}), [name]: [] } });
        }, { name: testCollection });
        await page.locator('#recipes [data-collection-add="' + visibleId + '"]').selectOption(testCollection);
        await page.waitForFunction(([id, name]) => (state.collections?.[name] || []).map(Number).includes(Number(id)),
          [visibleId, testCollection], { timeout: 30000 });
        await page.reload({ waitUntil: 'domcontentloaded' });
        await page.waitForFunction(([id, name, was]) =>
          (state.collections?.[name] || []).map(Number).includes(Number(id)) &&
          (state.favorites || []).map(Number).includes(Number(id)) !== was,
          [visibleId, testCollection, visibleBefore.favorite], { timeout: 30000 });
        console.log('PASS: visible recipe card favorite and collection saved and persisted');
      } finally {
        await page.evaluate(async ({ id, before }) => {
          const favorites = new Set((state.favorites || []).map(Number));
          if (before.favorite) favorites.add(Number(id)); else favorites.delete(Number(id));
          await preferences({ favorites: [...favorites], collections: before.collections });
        }, { id: visibleId, before: visibleBefore });
      }
    } finally {
      // Restore the test account even when an assertion fails.
      await page.evaluate(async ({ id, saved }) => {
        const isSelected = Object.hasOwn(state.selected || {}, String(id));
        if (isSelected !== saved.selected) await selectMealAndRefresh(Number(id), saved.selected, 2);
        await preferences({ favorites: saved.favorites, collections: saved.collections });
      }, { id: targetId, saved: original });
    }

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
