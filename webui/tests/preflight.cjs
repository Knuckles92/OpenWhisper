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
const PreFlight = require('../src/components/PreFlight.tsx').default;

let root;
async function mount(props) {
  const container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(PreFlight, props)));
  return container;
}
afterEach(async () => {if (root) {await act(async () => root.unmount()); root = null;} document.body.replaceChildren();});

const state = (extra = {}) => ({
  meeting_id: 'm', seq: 1, status: 'active', title: 'Sync', cloud_enabled: true, intelligence_online: true,
  diarization_available: false, topic: {current: '', history: []}, rolling_summary: '', rolling_summary_evidence: [],
  capture: {mic_available: true, loopback_available: false, message: ''}, participants: {}, cards: {}, questions: [],
  ...extra,
});
const me = {id: 'me', display_name: 'Dylan Fiori', kind: 'me', name_source: 'human', is_provisional: false, created_at: '', updated_at: ''};

test('readiness reflects real capture state and asks for a brief when none is set', async () => {
  const container = await mount({state: state(), isHost: true, onSendOp: async () => true, participants: [me]});
  assert.match(container.querySelector('.pf-headline').textContent, /What should this meeting capture\?/);
  const checks = [...container.querySelectorAll('.pf-check')].map((row) => row.textContent);
  assert.ok(checks.some((t) => /Microphone.*Detected/.test(t)));
  assert.ok(checks.some((t) => /System audio.*Not captured/.test(t)));
  assert.ok(checks.some((t) => /AI insights.*Online/.test(t)));
  assert.ok(checks.some((t) => /Speaker separation.*Me \/ Others only/.test(t)));
  assert.match(container.textContent, /Share the guest link to invite others/);
  assert.match(container.textContent, /Waiting for the first words/);
});

test('the brief becomes the headline and the level moves only while speech is heard', async () => {
  const container = await mount({
    state: state({intent: {text: 'Agree Phase 1 scope', updated_at: '', author_id: 'me'}, cloud_enabled: false}),
    isHost: false, onSendOp: async () => true, listening: true,
  });
  assert.equal(container.querySelector('.pf-headline').textContent, 'Agree Phase 1 scope');
  assert.ok(container.querySelector('.pf-level.hearing'));
  assert.match(container.textContent, /Hearing speech/);
  assert.match(container.textContent, /Off — transcript only/);
  assert.equal(container.querySelector('.pf-copy'), null, 'guests get no guest-link row');
});

test('copying the guest link builds an absolute URL and reports clipboard failures', async () => {
  const writes = [];
  Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async (text) => { writes.push(text); }}});
  const errors = [];
  const container = await mount({state: state(), isHost: true, guestUrl: '/m/guest', onSendOp: async () => true, onClientError: (m) => errors.push(m)});
  await act(async () => container.querySelector('.pf-copy').click());
  assert.deepEqual(writes, ['http://localhost/m/guest']);
  assert.equal(container.querySelector('.pf-copy').textContent, 'Copied');

  Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async () => { throw new Error('denied'); }}});
  await act(async () => root.unmount()); root = null; document.body.replaceChildren();
  const again = await mount({state: state(), isHost: true, guestUrl: 'https://x/m/g', onSendOp: async () => true, onClientError: (m) => errors.push(m)});
  await act(async () => again.querySelector('.pf-copy').click());
  assert.deepEqual(errors, ['denied']);
});
