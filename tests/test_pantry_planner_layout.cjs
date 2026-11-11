// Guard the desktop/mobile page structure before adding pantry matching features.
// Read-only source checks: no household data or live staging credentials required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../app/index.html'), 'utf8');
for (const id of ['recipes', 'selected-plan', 'week-grid', 'pantry-recipe-find', 'pantry-recipe-results']) {
  assert.match(html, new RegExp('id="' + id + '"'), 'Missing UI element: ' + id);
}
assert.match(html, /page-nav-button/, 'Desktop navigation must remain present');
assert.match(html, /mobile-bottom-nav/, 'Mobile navigation must remain present');
assert.match(html, /data-pantry-recipe-detail/, 'Pantry recommendations must open recipe details');
assert.match(html, /function pantryMatchKey\(/, 'Pantry matching function missing');
assert.match(html, /state\.pantry_inventory/, 'Pantry matching must use saved inventory');
assert.match(html, /\$\('week-grid'\)\.addEventListener\('change'/, 'Weekly planner persistence handler missing');
console.log('Pantry/planner layout source regression checks passed');
