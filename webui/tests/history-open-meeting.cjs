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
const HistoryPane = require('../src/components/HistoryPane.tsx').default;

let root, container;
async function mount(Component, props) {
  container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(Component, props)));
}
async function rerender(Component, props) {
  await act(async () => root.render(React.createElement(Component, props)));
}
afterEach(async () => { if (root) await act(async () => root.unmount()); root = null; document.body.replaceChildren(); });
const click = async element => act(async () => element.click());
const button = label => [...container.querySelectorAll('button')].find(b => b.textContent.trim() === label);

const meetingState = (id, title) => ({
  meeting_id: id, seq: 1, status: 'ended', title, cloud_enabled: true, intelligence_online: true,
  diarization_available: true, topic: { current: title, history: [] }, rolling_summary: title,
  rolling_summary_evidence: [], capture: { mic_available: true, loopback_available: true, message: '' },
  participants: {}, cards: {}, questions: [], report_views: ['ribbon'],
});
const headerProps = extra => ({
  token: 'demo', isHost: true, state: meetingState('m_current', 'Current meeting'), meeting: null,
  guestUrl: null, socketStatus: 'open', meetingEnded: true, lastError: null,
  onSendOp: async () => true, onClientError() {}, onClearError() {},
  onToggleHistory() {}, showHistory: true, onToggleActivity() {}, showActivity: false,
  transcriptLoadError: null, onRetryTranscript() {}, ...extra,
});

test('the history header opens the selected meeting instead of leaving for this dashboard', async () => {
  const pressed = [];
  await mount(HeaderBar, headerProps({
    onToggleHistory: () => pressed.push('leave'),
    onOpenHistoryMeeting: () => pressed.push('open'),
    onExitHistoryMeeting: () => pressed.push('list'),
  }));
  // Nothing picked yet: the only exit is back to this dashboard's own meeting.
  assert.ok(button('Back to meeting'));
  assert.equal(button('Open meeting'), undefined);

  await rerender(HeaderBar, headerProps({
    historySelectionId: 'm_older',
    onToggleHistory: () => pressed.push('leave'),
    onOpenHistoryMeeting: () => pressed.push('open'),
    onExitHistoryMeeting: () => pressed.push('list'),
  }));
  assert.equal(button('Back to meeting'), undefined, 'the mislabelled exit is gone once a meeting is picked');
  await click(button('Open meeting'));

  await rerender(HeaderBar, headerProps({
    historySelectionId: 'm_older', historyFocused: true,
    onToggleHistory: () => pressed.push('leave'),
    onOpenHistoryMeeting: () => pressed.push('open'),
    onExitHistoryMeeting: () => pressed.push('list'),
  }));
  await click(button('All meetings'));
  assert.deepEqual(pressed, ['open', 'list'], 'neither press falls through to leaving history');
});

test('a live meeting keeps its own way back while a past meeting can still be opened', async () => {
  const pressed = [];
  await mount(HeaderBar, headerProps({
    state: { ...meetingState('m_current', 'Current meeting'), status: 'active' },
    meetingEnded: false, historySelectionId: 'm_older',
    onToggleHistory: () => pressed.push('leave'),
    onOpenHistoryMeeting: () => pressed.push('open'),
  }));
  assert.ok(button('Open meeting'));
  await click(button('Back to live'));
  assert.deepEqual(pressed, ['leave']);
});

test('the full view fills the pane with the picked meeting and steps back to the list', async () => {
  const rows = [
    { id: 'm_older', title: 'Budget review', started_at: '2026-09-18T18:00:00Z', status: 'ended', has_audio: false },
    { id: 'm_latest', title: 'Standup', started_at: '2026-09-19T18:00:00Z', status: 'ended', has_audio: false },
  ];
  const original = { meetings: api.meetings, meeting: api.meeting };
  api.meetings = async () => rows;
  api.meeting = async (token, id) => ({
    meeting: rows.find(r => r.id === id), state: meetingState(id, id === 'm_older' ? 'Budget review' : 'Standup'),
    segments: [], transcript_next_cursor: null,
  });
  try {
    const selections = [], focusChanges = [];
    let props = {
      token: 'demo', selectedId: null, focused: false, onClose() {},
      onSelectMeeting: id => { selections.push(id); props = { ...props, selectedId: id }; },
      onFocusChange: next => { focusChanges.push(next); props = { ...props, focused: next } },
    };
    await mount(HistoryPane, props);
    await click([...container.querySelectorAll('.history-item')].find(b => b.textContent.includes('Budget review')));
    assert.deepEqual(selections, ['m_older'], 'picking a row reports it up to the dashboard');

    await rerender(HistoryPane, props);
    assert.match(container.textContent, /Budget review/);
    assert.ok(container.querySelector('.history-sidebar-column'), 'the list is still beside the preview');

    // What the header's "Open meeting" does: focus the meeting that was picked.
    await rerender(HistoryPane, { ...props, focused: true });
    assert.equal(container.querySelector('.history-sidebar-column'), null, 'the list steps aside');
    assert.ok(container.querySelector('.history-detail-grid.focused'));
    assert.match(container.textContent, /Budget review/);
    assert.doesNotMatch(container.textContent, /Standup/, 'the newest meeting is not what is shown');

    await click(button('← All meetings'));
    assert.deepEqual(focusChanges, [false]);

    await act(async () => window.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape' })));
    assert.deepEqual(focusChanges, [false, false], 'Escape leaves the full view before it leaves history');
  } finally {
    api.meetings = original.meetings;
    api.meeting = original.meeting;
  }
});
