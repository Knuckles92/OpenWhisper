// The Captured rail is now the only home for ranked insights: the top picks
// are highlighted at its head instead of being duplicated into a centre row.
const assert = require('node:assert/strict');
const {test, afterEach} = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/'});
global.window = dom.window;
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.location = dom.window.location;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions:{module:ts.ModuleKind.CommonJS, jsx:ts.JsxEmit.ReactJSX, target:ts.ScriptTarget.ES2020},
}).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react');
const {act} = React;
const {createRoot} = require('react-dom/client');
const CardsPane = require('../src/components/CardsPane.tsx').default;
const {capturedRailFeed} = require('../src/state.ts');

let root;
async function mount(component, props) {
  const container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(component, props)));
  return container;
}
afterEach(async () => {if (root) {await act(async () => root.unmount()); root = null;} document.body.replaceChildren();});

const at = (minute) => `2026-09-19T10:${String(minute).padStart(2, '0')}:00Z`;
const item = (id, card, text, extra = {}) => ({
  id, card, text, status: 'proposed', pinned: false, evidence: [], data: {},
  created_at: at(0), updated_at: at(0), ...extra,
});

const cards = () => ({
  key_points: [
    item('kp_old', 'key_points', 'Fuel burn is the main constraint'),
    item('kp_new', 'key_points', 'A lot of people help with the effort', {created_at: at(20), updated_at: at(20)}),
  ],
  decisions: [item('dc_1', 'decisions', 'Hold the video until a million subscribers', {created_at: at(10), updated_at: at(10)})],
  timeline: [item('tl_1', 'timeline', 'No cell service for miles', {created_at: at(15), updated_at: at(15)})],
  action_items: [],
  risks: [],
  live_notes: [item('nb_1', 'live_notes', 'Note block that belongs to NotesPane')],
  user_notes: [],
});

test('the rail lifts the ranked picks and never repeats them in the stream', () => {
  const {top, rest} = capturedRailFeed(cards(), [], 3);
  assert.equal(top.length, 3);
  // One per category, newest first, and note-taker blocks stay out.
  assert.deepEqual(top.map((i) => i.id), ['kp_new', 'tl_1', 'dc_1']);
  const restIds = rest.map((entry) => entry.item.id);
  assert.deepEqual(restIds, ['kp_old']);
  assert.ok(!restIds.includes('nb_1'));
});

test('a pinned capture outranks newer ones and keeps the lead order', () => {
  const doc = cards();
  doc.key_points[0].pinned = true;
  const {top} = capturedRailFeed(doc, [], 3);
  assert.equal(top[0].id, 'kp_old');
});

test('highlightTop 0 leaves the rail as one flat feed', () => {
  const {top, rest} = capturedRailFeed(cards(), [], 0);
  assert.deepEqual(top, []);
  assert.equal(rest.length, 4);
});

test('the rail renders one highlighted lead above the rest of the stream', async () => {
  const container = await mount(CardsPane, {
    cards: cards(), questions: [], onEvidenceClick() {}, lastSeqByTarget: {},
    newestFirst: true, embedded: true, highlightTop: 3, status: 'active',
    cloudEnabled: true, intelligenceOnline: true,
  });
  const lead = container.querySelector('.capture-lead');
  assert.ok(lead, 'lead group renders');
  assert.match(lead.querySelector('.card-section-title').textContent, /Top insights/);
  assert.equal(lead.querySelectorAll('.card-item.featured').length, 3);
  // Every item appears exactly once across the whole rail.
  assert.equal(container.querySelectorAll('.card-item').length, 4);
  assert.ok(container.querySelector('.capture-rest-title'), 'the remainder is labelled');
});

test('with nothing captured the lead explains why instead of leaving a gap', async () => {
  const empty = {key_points: [], decisions: [], action_items: [], risks: [], timeline: [], live_notes: [], user_notes: []};
  const container = await mount(CardsPane, {
    cards: empty, questions: [], onEvidenceClick() {}, lastSeqByTarget: {},
    newestFirst: true, embedded: true, highlightTop: 3, status: 'active',
    cloudEnabled: false, intelligenceOnline: false,
  });
  assert.match(container.querySelector('.capture-lead-ghost').textContent, /Enable cloud insights/);
  assert.equal(container.querySelector('.capture-rest-title'), null);
});
