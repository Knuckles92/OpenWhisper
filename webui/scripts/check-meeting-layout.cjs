// Run after npm run build. Requires Playwright (or PLAYWRIGHT_MODULE).
// Uses synthetic data only; never connects to a real meeting server.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const title = 'Dispatching unedited prompts: tolerating typos and clarifying only if models fail';
const long = 'MeetingReference'.repeat(12);
const meeting = { id: 'layout-test', title, started_at: '2026-09-07T18:49:10Z', status: 'ended', has_audio: false, insights_pill: 'Saved for later', insights_tone: 'warning' };
const participant = { id: 'me', display_name: long, kind: 'me', name_source: 'human', is_provisional: false };
const segments = Array.from({ length: 12 }, (_, i) => ({ id: `s${i}`, meeting_id: meeting.id, channel: 'mic', start_s: i * 20, end_s: i * 20 + 15, text: `Discussion ${i}: ${long}`, speaker_participant_id: 'me', speaker_source: 'human', speaker_pinned: false }));
const cards = Object.fromEntries(['key_points','decisions','action_items','risks','timeline','live_notes','user_notes'].map(card => [card, [{ id: card, card, text: `A planning point with ${long}`, data: { title: title, owner_id: 'me' }, status: 'confirmed', author_type: 'human', author_id: 'me', pinned: true, revision: 1, evidence: ['s1'], created_at: '2026-09-07T18:49:10Z', updated_at: '2026-09-07T18:49:10Z' }]]));
const state = { meeting_id: meeting.id, seq: 0, status: 'ended', title, cloud_enabled: true, intelligence_online: true, diarization_available: true, topic: { current: title, history: [] }, rolling_summary: `Planning the next release. ${long}`, rolling_summary_evidence: [], capture: { mic_available: true, loopback_available: true, message: '' }, participants: { me: participant }, cards, questions: [], report_views: ['ribbon','brief','signal'] };
const dist = path.resolve(__dirname, '../dist');
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    let result;
    if (url.pathname === '/api/session') result = { role: 'host', state, meeting, urls: {} };
    else if (url.pathname === '/api/meetings') result = Array.from({ length: 30 }, (_, i) => ({ ...meeting, id: i ? `m${i}` : meeting.id, title: i === 1 ? long : title }));
    else if (url.pathname.endsWith('/transcript')) result = { items: segments, next_cursor: null };
    else if (url.pathname === '/api/search') result = [{ meeting_id: meeting.id, segment_id: 's1', text: long }];
    else if (url.pathname.startsWith('/api/meetings/')) result = { meeting, state, segments, transcript_next_cursor: null };
    else result = [];
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(result));
    return;
  }
  const file = url.pathname.startsWith('/assets/') ? path.join(dist, path.basename(url.pathname) ? 'assets/' + path.basename(url.pathname) : '') : path.join(dist, 'index.html');
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL ? { channel: process.env.BROWSER_CHANNEL } : {}) });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = `http://127.0.0.1:${server.address().port}`;
  async function check(label) {
    const failures = await page.evaluate(() => {
      return [...document.querySelectorAll('.app-shell, .app-main, .header-bar, .confirm-card, .panel, .history-detail-grid, .history-item, .history-item-title, .history-content-column, .report-sheet, .segment, .chip, .card-add-row, .report-view-menu, .report-download-menu')].flatMap(el => {
        const rect = el.getBoundingClientRect();
        if (!rect.width || !rect.height || !el.checkVisibility()) return [];
        // The history title intentionally ellipsizes, while other containers must fit.
        if (el.matches('.history-item-title')) {
          const parent = el.parentElement.getBoundingClientRect();
          return rect.right > parent.right ? [el.className + ': title escapes button'] : [];
        }
        return el.scrollWidth > el.clientWidth + 1 || rect.right > innerWidth + 1 || rect.left < -1
          ? [`${el.className}: ${el.clientWidth}/${el.scrollWidth}, left=${rect.left}, right=${rect.right}`] : [];
      });
    });
    if (failures.length) console.log(await page.evaluate(() => [...document.querySelectorAll('*')].filter(el => el.checkVisibility() && el.getBoundingClientRect().width && el.scrollWidth > el.clientWidth + 2 && !['hidden','clip'].includes(getComputedStyle(el).overflowX)).map(el => [el.tagName,el.className,el.clientWidth,el.scrollWidth]).slice(-30)));
    assert.deepEqual(failures, [], label);
    assert.deepEqual(errors, [], 'No runtime errors');
    console.log(`PASS ${label}`);
  }
  try {
    for (const width of [1920, 1280, 1201, 1024, 900, 768, 601, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(base + '/m/test?history=layout-test');
      await page.locator('.rb-title').waitFor();
      await check(`${width}px history / Ribbon`);
      for (const view of ['Brief', 'Signal', 'Ribbon']) {
        await page.locator('.report-view-select summary').click();
        await check(`${width}px view menu`);
        await page.locator('.report-view-menu button').filter({ has: page.getByText(view, { exact: true }) }).click();
        await check(`${width}px history / ${view}`);
      }
      await page.locator('.report-download summary').click();
      await check(`${width}px download menu`);
      await page.locator('.report-download summary').click();
      if (process.env.UI_SCREENSHOTS && [1920,390].includes(width)) {
        fs.mkdirSync(process.env.UI_SCREENSHOTS, { recursive: true });
        await page.evaluate(() => document.querySelectorAll('.app-shell, .app-main, .panel-body').forEach(el => el.scrollTop = 0));
        await page.screenshot({ path: path.join(process.env.UI_SCREENSHOTS, `history-${width}.png`) });
      }
      // Live meeting and its editable controls exercise the narrow side rails.
      state.status = 'active'; meeting.status = 'active';
      await page.goto(base + '/m/test');
      await page.locator('.card-add-row').waitFor();
      await check(`${width}px live meeting`);
      state.status = 'ended'; meeting.status = 'ended';
      await page.goto(base + '/m/test');
      await page.locator('.report-sheet').waitFor();
      await check(`${width}px ended meeting`);
      await page.locator('.report-download summary').click();
      await check(`${width}px header download menu`);
      await page.locator('.report-download summary').click();
      await page.getByRole('button', { name: 'History', exact: true }).click();
      await page.getByRole('searchbox').fill('reference');
      await page.getByRole('button', { name: 'Search', exact: true }).click();
      await page.locator('.search-hit').waitFor();
      await check(`${width}px transcript search results`);
      await page.locator('.search-hit').click();
      await page.getByRole('button', { name: 'Delete', exact: true }).click();
      await page.locator('dialog[open]').waitFor();
      await check(`${width}px delete confirmation`);
      await page.getByRole('button', { name: 'Keep meeting', exact: true }).click();
    }
    for (const [width, height] of [[390, 320], [844, 390]]) {
      await page.setViewportSize({ width, height });
      await page.getByRole('button', { name: 'Delete', exact: true }).click();
      await check(`${width}x${height} short-window dialog`);
      assert.equal(await page.locator('dialog[open] .confirm-card').evaluate(el => {
        const rect = el.getBoundingClientRect();
        return rect.top >= 0 && rect.bottom <= innerHeight;
      }), true, 'Confirmation stays inside the window');
      await page.getByRole('button', { name: 'Keep meeting', exact: true }).click();
    }
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
