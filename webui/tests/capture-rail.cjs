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
  // Insights lead; timeline navigation stays in the ordinary stream.
  assert.deepEqual(top.map((i) => i.id), ['kp_new', 'dc_1', 'kp_old']);
  const restIds = rest.map((entry) => entry.item.id);
  assert.deepEqual(restIds, ['tl_1']);
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
  assert.match(container.querySelector('.capture-lead-ghost').textContent, /Turn on AI insights/);
  assert.equal(container.querySelector('.capture-rest-title'), null);
});


test('legacy repair snippets are not displayed as captured insights', async () => {
  const doc = cards();
  const legacy = {author_type: 'system', author_id: 'state_repair'};
  doc.key_points.push(item('raw_transition', 'key_points', "Let's take a look at this.", legacy));
  doc.timeline.push(item('raw_label', 'timeline', 'Seasoning.', legacy));
  const feed = capturedRailFeed(doc, [], 3);
  assert.ok(!feed.top.some((i) => i.id.startsWith('raw_')));
  assert.ok(!feed.rest.some((e) => e.item.id.startsWith('raw_')));
  const container = await mount(CardsPane, {
    cards: doc, questions: [], onEvidenceClick() {}, lastSeqByTarget: {},
    newestFirst: true, embedded: true, highlightTop: 3,
  });
  assert.ok(!container.textContent.includes('Seasoning.'));
  assert.ok(!container.textContent.includes("Let's take a look at this."));
});

test('a human can retain or feature a repair item or timeline beat', () => {
  for (const change of [{pinned: true}, {status: 'edited'}, {status: 'confirmed'}]) {
    const doc = cards();
    doc.timeline.push(item('human_choice', 'timeline', 'Seasoning.', {
      author_type: 'system', author_id: 'state_repair', ...change,
    }));
    assert.equal(capturedRailFeed(doc).top[0].id, 'human_choice');
  }
});

test('timeline-only speech leaves top insights empty', () => {
  const doc = {timeline: [item('transition', 'timeline', "Let's take a look at this.")]};
  assert.deepEqual(capturedRailFeed(doc).top, []);
});


test('a synthesized replacement of a legacy sample can become a capture', () => {
  const doc = {key_points: [item('rewritten', 'key_points', 'The meal exceeded the budget by 13 cents.', {
    author_type: 'system', author_id: 'state_repair', data: {insight_synthesized: true},
  })]};
  assert.equal(capturedRailFeed(doc).top[0].id, 'rewritten');
});

const {EvidenceProvider} = require('../src/evidence.tsx');
const {dueTone, ledgerCounts, ledgerTabOf} = require('../src/components/CardsPane.tsx');

const ledgerCards = () => ({
  ...cards(),
  action_items: [item('ac_1', 'action_items', 'Pull 18 months of flow logs', {
    data: {owner_participant_id: 'p_luis', deadline: '2026-10-02'}, created_at: at(30), updated_at: at(30),
  })],
  risks: [item('rk_1', 'risks', 'Motor lead time is 14 weeks', {created_at: at(25), updated_at: at(25)})],
});
const people = [{id: 'p_luis', display_name: 'Luis Ortega', kind: 'others_cluster', created_at: at(0)}];
const mountLedger = (extra = {}) => mount((props) => React.createElement(EvidenceProvider,
  {segments: [], participants: people}, React.createElement(CardsPane, props)), {
  cards: ledgerCards(), questions: [], onEvidenceClick() {}, lastSeqByTarget: {},
  newestFirst: true, embedded: true, highlightTop: 3, status: 'active',
  cloudEnabled: true, intelligenceOnline: true, ...extra,
});
const tabButtons = (container) => [...container.querySelectorAll('[role=tab]')];

test('ledger tabs count each kind and hide empty ones', async () => {
  const container = await mountLedger();
  const labels = tabButtons(container).map((b) => b.textContent);
  assert.deepEqual(labels, ['All6', 'Decisions1', 'Actions1', 'Risks1', 'Other3']);
  assert.equal(container.querySelector('[role=tab][aria-selected=true]').dataset.tab, 'all');
  assert.ok(container.querySelector('.capture-lead'), 'All keeps the highlighted lead');
});

test('picking a tab shows only that kind, and arrow keys move between tabs', async () => {
  const container = await mountLedger();
  const actions = tabButtons(container).find((b) => b.dataset.tab === 'actions');
  await act(async () => actions.click());
  const rows = [...container.querySelectorAll('#ledger-panel .card-item')];
  assert.deepEqual(rows.map((r) => r.dataset.card), ['action_items']);
  assert.equal(container.querySelector('.capture-lead'), null, 'the lead belongs to All only');
  await act(async () => actions.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true})));
  assert.equal(container.querySelector('[role=tab][aria-selected=true]').dataset.tab, 'risks');
  assert.equal(document.activeElement.dataset.tab, 'risks');
});

test('an action row shows its owner and due date, and the box confirms it', async () => {
  const sent = [];
  const container = await mountLedger({onSendOp: async (op) => { sent.push(op); return true; }});
  const row = container.querySelector('.card-item[data-card=action_items]');
  assert.match(row.querySelector('.ledger-owner').textContent, /LOLuis Ortega/);
  assert.ok(row.querySelector('.ledger-due'));
  const box = row.querySelector('input[type=checkbox]');
  assert.equal(box.checked, false);
  await act(async () => box.click());
  assert.deepEqual(sent, [{op: 'confirm_item', id: 'ac_1'}]);
});

test('a proposed decision offers a confirm step; a confirmed one does not', async () => {
  const sent = [];
  const container = await mountLedger({onSendOp: async (op) => { sent.push(op); return true; }});
  const state = container.querySelector('.card-item[data-card=decisions] .ledger-state');
  await act(async () => state.querySelector('.ledger-confirm').click());
  assert.deepEqual(sent, [{op: 'confirm_item', id: 'dc_1'}]);
  const doc = ledgerCards();
  doc.decisions[0].status = 'confirmed';
  const again = await mountLedger({cards: doc});
  assert.equal(again.querySelector('.ledger-state .ledger-confirm'), null);
  assert.ok(again.querySelector('.ledger-state.confirmed'));
});

test('quick-add sends the picked kind', async () => {
  const sent = [];
  const container = await mountLedger({onSendOp: async (op) => { sent.push(op); return true; }});
  const risk = [...container.querySelectorAll('.ledger-kinds [role=radio]')].find((b) => b.textContent === 'Risk');
  await act(async () => risk.click());
  const input = container.querySelector('.ledger-input input');
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;
  await act(async () => { setter.call(input, 'Bypass capacity unconfirmed'); input.dispatchEvent(new dom.window.Event('input', {bubbles: true})); });
  await act(async () => container.querySelector('.ledger-input button').click());
  assert.deepEqual(sent, [{op: 'add_item', card: 'risks', text: 'Bypass capacity unconfirmed'}]);
});

test('print and archive keep the plain feed without ledger chrome', async () => {
  const container = await mountLedger({readOnly: true});
  assert.equal(container.querySelector('[role=tablist]'), null);
  assert.equal(container.querySelector('.ledger-action'), null);
  assert.equal(container.querySelector('.ledger-composer'), null);
});

test('ledger helpers classify entries and judge only ISO due dates', () => {
  const entries = [
    {kind: 'item', item: item('a', 'action_items', 'x')},
    {kind: 'item', item: item('b', 'timeline', 'y', {status: 'removed'})},
    {kind: 'question', question: {id: 'q'}},
  ];
  assert.equal(ledgerTabOf(entries[0]), 'actions');
  assert.equal(ledgerTabOf(entries[2]), 'questions');
  const counts = ledgerCounts(entries);
  assert.equal(counts.all, 2);
  assert.equal(counts.other, 0, 'removed items never count');
  const now = new Date(2026, 8, 30);
  assert.equal(dueTone('2026-09-29', now), 'overdue');
  assert.equal(dueTone('2026-10-01', now), 'soon');
  assert.equal(dueTone('2026-10-20', now), 'neutral');
  assert.equal(dueTone('next Tuesday', now), 'neutral');
});
