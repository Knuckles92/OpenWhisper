const assert = require('node:assert/strict');
const {test, afterEach} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {url: 'http://localhost/'});
for (const key of ['window', 'document', 'navigator', 'location', 'HTMLElement', 'HTMLInputElement', 'Event', 'KeyboardEvent']) {
  global[key] = key === 'window' ? dom.window : dom.window[key];
}
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020},
}).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react');
const {act} = React;
const {createRoot} = require('react-dom/client');
const {MAX_TITLE_LENGTH, titleError} = require('../src/titles.ts');
const {socketStatusForCloseCode} = require('../src/ws.ts');
const {api} = require('../src/api.ts');
const HeaderBar = require('../src/components/HeaderBar.tsx').default;
const HistoryPane = require('../src/components/HistoryPane.tsx').default;

let root, container;
const originalApi = {...api};
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  root = null;
  document.body.replaceChildren();
  Object.assign(api, originalApi);
});
async function mount(Component, props) {
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(Component, props)));
}
async function type(value) {
  const input = container.querySelector('input[aria-label="Meeting title"]');
  assert.ok(input);
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new Event('input', {bubbles: true}));
  });
  return input;
}
const state = {
  meeting_id: 'm_title', seq: 1, status: 'ended', title: 'Original', cloud_enabled: false,
  intelligence_online: false, diarization_available: false, topic: {current: '', history: []},
  rolling_summary: '', rolling_summary_evidence: [], participants: {}, cards: {}, questions: [],
  report_views: ['ribbon'],
};

const valid = ['x', 'x'.repeat(120), 'x'.repeat(121), 'x'.repeat(200), '📝'.repeat(200), '  New title  '];
const invalid = ['', '   ', 'x'.repeat(201), '📝'.repeat(201), 'bad\nname', 'bad\x00name', 'bad\x7fname'];

test('dashboard title contract matches the Python boundary, including Unicode and controls', () => {
  const python = fs.readFileSync(path.join(__dirname, '../../services/titles.py'), 'utf8');
  assert.equal(MAX_TITLE_LENGTH, Number(python.match(/^MAX_TITLE_LENGTH = (\d+)$/m)[1]));
  for (const value of valid) assert.equal(titleError(value), null);
  for (const value of invalid) assert.match(titleError(value), /1–200/);
  assert.equal(socketStatusForCloseCode(4409), null, 'title resyncs must allow automatic reconnect');
  assert.notEqual(socketStatusForCloseCode(4401), null, 'expired tokens remain terminal');
});

for (const surface of ['header', 'history']) {
  test(`${surface} accepts 121–200 character titles, rejects overlong drafts, and allows correcting them`, async () => {
    const sent = [], errors = [];
    if (surface === 'header') {
      await mount(HeaderBar, {
        token: 'test', isHost: true, state, meeting: null, guestUrl: null, socketStatus: 'open',
        meetingEnded: true, lastError: null, showHistory: false, showActivity: false,
        transcriptLoadError: null, onRetryTranscript() {}, onClearError() {}, onToggleHistory() {},
        onToggleActivity() {}, onClientError: error => errors.push(error),
        onSendOp: async op => {sent.push(op.text); return true;},
      });
    } else {
      const row = {id: 'm_title', title: 'Original', status: 'ended', started_at: '2026-10-01T12:00:00Z', has_audio: false};
      api.meetings = async () => [row];
      api.meeting = async () => ({meeting: row, state, segments: [], transcript_next_cursor: null});
      api.renameMeeting = async (_token, _id, title) => {sent.push(title); return {ok: true, title};};
      await mount(HistoryPane, {token: 'test', selectedId: 'm_title', focused: true, onClose() {}});
    }
    const submit = async value => {
      const input = await type(value);
      await act(async () => {
        if (surface === 'header') input.dispatchEvent(new dom.window.FocusEvent('focusout', {bubbles: true}));
        else input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      });
    };
    await submit('x'.repeat(201));
    assert.deepEqual(sent, []);
    if (surface === 'header') assert.match(errors.at(-1), /1–200/);
    else assert.match(container.textContent, /1–200/);
    assert.equal(container.querySelector('input[aria-label="Meeting title"]').value, 'x'.repeat(201));
    for (const value of valid) await submit(value);
    assert.deepEqual(sent, valid.map(value => value.trim()));
  });
}
