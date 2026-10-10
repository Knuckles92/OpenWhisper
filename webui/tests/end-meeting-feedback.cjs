const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const key of ['window', 'document', 'navigator', 'location', 'HTMLElement', 'HTMLInputElement', 'Event', 'KeyboardEvent', 'MouseEvent']) {
  global[key] = key === 'window' ? dom.window : dom.window[key];
}
global.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const ts = require('typescript');
const fs = require('node:fs');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
const { api } = require('../src/api.ts');
const HeaderBar = require('../src/components/HeaderBar.tsx').default;

let root, container;
const originalEnd = api.endMeeting;
async function mount(props) {
  container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(HeaderBar, props)));
}
async function rerender(props) {
  await act(async () => root.render(React.createElement(HeaderBar, props)));
}
afterEach(async () => {
  api.endMeeting = originalEnd;
  if (root) await act(async () => root.unmount());
  root = null; document.body.replaceChildren();
});
const click = async element => act(async () => element.click());
const button = label => [...container.querySelectorAll('button')].find(b => b.textContent.trim() === label);
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const statusText = () => container.querySelector('.cb-status-label').textContent;

const liveState = (status = 'active') => ({
  meeting_id: 'm_live', seq: 1, status, title: 'Planning', cloud_enabled: false, intelligence_online: true,
  diarization_available: true, topic: { current: 'Planning', history: [] }, rolling_summary: '',
  rolling_summary_evidence: [], capture: { mic_available: true, loopback_available: true, message: '' },
  participants: {}, cards: {}, questions: [], report_views: ['ribbon'],
});
const props = (state, extra) => ({
  token: 'host', isHost: true, state, meeting: null, guestUrl: null, socketStatus: 'open',
  meetingEnded: state.status === 'ended', lastError: null,
  onSendOp: async () => true, onClientError() {}, onClearError() {},
  onToggleHistory() {}, showHistory: false, onToggleActivity() {}, showActivity: false,
  transcriptLoadError: null, onRetryTranscript() {}, ...extra,
});

async function confirmEnd() {
  await click(button('End'));
  await click(button('End meeting'));
}

test('confirming End shows the ending state at once, while the request is still pending', async () => {
  const pending = deferred();
  const calls = [];
  api.endMeeting = token => { calls.push(token); return pending.promise; };
  await mount(props(liveState()));
  assert.equal(statusText(), 'Live');

  await confirmEnd();
  assert.deepEqual(calls, ['host']);
  // Nothing from the socket yet, and the request has not answered.
  assert.equal(statusText(), 'Ending');
  assert.match(container.textContent, /Ending meeting — finishing transcription/);
  assert.equal(button('End'), undefined, 'End is not offered twice');
  assert.equal(container.querySelector('[aria-label="Pause"]'), null);

  // The request answering is not the end; the socket still has to say so.
  await act(async () => pending.resolve({ ok: true }));
  assert.equal(statusText(), 'Ending');
  await rerender(props(liveState('ending')));
  assert.equal(statusText(), 'Ending');
  await rerender(props(liveState('ended')));
  assert.equal(statusText(), 'Ended');
  assert.doesNotMatch(container.textContent, /Ending meeting/);
});

test('a failed End request restores the live controls and reports the error', async () => {
  const pending = deferred();
  const errors = [];
  api.endMeeting = () => pending.promise;
  await mount(props(liveState(), { onClientError: message => errors.push(message) }));
  await confirmEnd();
  assert.equal(statusText(), 'Ending');

  await act(async () => pending.reject(new Error('The meeting could not be ended.')));
  assert.deepEqual(errors, ['The meeting could not be ended.']);
  assert.equal(statusText(), 'Live');
  assert.ok(button('End'), 'End is offered again');
  assert.doesNotMatch(container.textContent, /Ending meeting/);
});

test('ending a paused meeting also shows the ending state at once', async () => {
  api.endMeeting = () => deferred().promise;
  await mount(props(liveState('paused')));
  assert.ok(button('Resume'));
  await confirmEnd();
  assert.equal(statusText(), 'Ending');
  assert.equal(button('Resume'), undefined);
});

test('keeping the meeting changes nothing', async () => {
  const calls = [];
  api.endMeeting = () => { calls.push(1); return deferred().promise; };
  await mount(props(liveState()));
  await click(button('End'));
  await click(button('Keep meeting'));
  assert.deepEqual(calls, []);
  assert.equal(statusText(), 'Live');
});
