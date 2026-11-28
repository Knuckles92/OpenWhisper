const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const key of ['window', 'document', 'navigator', 'location', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Node', 'Event', 'KeyboardEvent', 'MouseEvent']) {
  global[key] = key === 'window' ? dom.window : dom.window[key];
}
global.PointerEvent = dom.window.MouseEvent;
global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { renderToStaticMarkup } = require('react-dom/server');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) {
  require.extensions[ext] = (module, filename) => {
    module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
    }).outputText, filename);
  };
}
require.extensions['.css'] = () => {};

const ActivityPane = require('../src/components/ActivityPane.tsx').default;
const NotesPane = require('../src/components/NotesPane.tsx').default;
const CardsPane = require('../src/components/CardsPane.tsx').default;
const CustomReports = require('../src/components/report/CustomReports.tsx').default;
const ReportViewSelect = require('../src/components/report/ReportViewSelect.tsx').default;
const ReportDownload = require('../src/components/report/ReportDownload.tsx').default;
const { speakerColorMap, speakerColor } = require('../src/people.ts');
const { mergeSegments } = require('../src/state.ts');
const { api } = require('../src/api.ts');

let root, container;
async function mount(Component, props) {
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(Component, props)));
}
afterEach(async () => {
  if (root) {
    await act(async () => root.unmount());
    root = null;
  }
  document.body.replaceChildren();
});

test('speakerColorMap matches speakerColor for all participants', () => {
  const participants = [
    { id: 'p2', display_name: 'Bob', kind: 'guest', created_at: '2026-01-02' },
    { id: 'me', display_name: 'Host', kind: 'me', created_at: '2026-01-01' },
    { id: 'p1', display_name: 'Alice', kind: 'others_cluster', created_at: '2026-01-01' },
  ];
  const map = speakerColorMap(participants);
  for (const p of participants) {
    assert.equal(map.get(p.id), speakerColor(p.id, participants));
  }
});

test('ActivityPane defers /api/events until expanded and aborts on collapse', async () => {
  const originalEvents = api.events;
  const calls = [];
  api.events = (_token, _beforeSeq, signal) => {
    calls.push({ signal });
    return Promise.resolve([]);
  };
  try {
    await mount(ActivityPane, {
      token: 'tok',
      onUndo: async () => true,
      onHide() {},
      refreshKey: 5,
      cloudEnabled: true,
      intelligenceOnline: true,
      meetingStatus: 'active',
    });
    assert.equal(calls.length, 0, 'collapsed ActivityPane must not fetch /api/events');

    const toggle = container.querySelector('.agent-activity-toggle');
    await act(async () => toggle.click());
    assert.equal(calls.length, 1, 'expanding fetches once (no duplicate refreshKey fetch)');
    assert.equal(calls[0].signal.aborted, false);

    await act(async () => toggle.click());
    assert.equal(calls[0].signal.aborted, true, 'collapsing aborts active request controller');
  } finally {
    api.events = originalEvents;
  }
});

test('NotesPane keeps editor open when moving focus to heading input and supports Save/Cancel', async () => {
  const sent = [];
  const note = {
    id: 'n1', card: 'live_notes', text: 'Original body',
    data: { heading: 'Old heading' }, status: 'proposed', author_type: 'agent', author_id: null,
    pinned: false, revision: 1, evidence: [], created_at: '2026-09-20T10:00:00Z', updated_at: '2026-09-20T10:00:00Z',
  };
  await mount(NotesPane, {
    notes: [note], status: 'active', cloudEnabled: true, intelligenceOnline: true,
    onSendOp: async (op) => { sent.push(op); return true; },
    onEvidenceClick() {}, lastSeqByTarget: {},
  });
  const editBtn = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Edit');
  await act(async () => editBtn.click());

  const headingInput = container.querySelector('.note-heading-edit');
  const bodyArea = container.querySelector('.note-body-edit');
  assert.ok(headingInput && bodyArea, 'both heading input and body textarea are mounted');

  // Focusing heading input from textarea must NOT close the editor.
  await act(async () => {
    bodyArea.dispatchEvent(new dom.window.FocusEvent('blur', { bubbles: true, relatedTarget: headingInput }));
  });
  assert.ok(container.querySelector('.note-heading-edit'), 'editor stays open on internal focus move');

  // Change heading and click Save.
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(headingInput, 'New heading');
    headingInput.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const saveBtn = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Save');
  await act(async () => saveBtn.click());
  assert.equal(sent.length, 1);
  assert.equal(sent[0].set.data.heading, 'New heading');
  assert.equal(container.querySelector('.note-heading-edit'), null);
});

test('CardsPane CardItemRow cancels editing on Escape', async () => {
  const item = {
    id: 'd1', card: 'decisions', text: 'Ship Friday',
    data: {}, status: 'proposed', author_type: 'agent', author_id: null,
    pinned: false, revision: 1, evidence: [], created_at: '2026-09-20T10:00:00Z', updated_at: '2026-09-20T10:00:00Z',
  };
  await mount(CardsPane, {
    cards: { decisions: [item] },
    onSendOp: async () => true,
    onEvidenceClick() {},
    lastSeqByTarget: {},
  });
  const editBtn = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Edit');
  await act(async () => editBtn.click());
  const textarea = container.querySelector('textarea');
  assert.ok(textarea);
  await act(async () => textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  assert.equal(container.querySelector('textarea'), null);
});

test('CustomReports disables composer when AI insights are off or live meeting is offline', () => {
  const base = {
    meeting_id: 'm1', seq: 1, status: 'ended', cloud_enabled: false,
    intelligence_online: false, diarization_available: false, title: 'Sync',
    topic: { current: '', history: [] }, rolling_summary: '', rolling_summary_evidence: [],
    capture: { mic_available: true, loopback_available: true, message: '' },
    participants: {}, cards: {}, questions: [], custom_reports: [],
  };
  const offHtml = renderToStaticMarkup(React.createElement(CustomReports, { state: base, token: 't', meetingId: 'm1' }));
  assert.match(offHtml.match(/<textarea[^>]*>/)[0], /disabled/);
  assert.match(offHtml, /Turn on AI insights to request a custom report/);

  const liveOfflineHtml = renderToStaticMarkup(React.createElement(CustomReports, {
    state: { ...base, status: 'active', cloud_enabled: true, intelligence_online: false },
    token: 't', meetingId: 'm1',
  }));
  assert.match(liveOfflineHtml.match(/<textarea[^>]*>/)[0], /disabled/);
  assert.match(liveOfflineHtml, /AI insights are offline/);
});

test('ReportViewSelect and ReportDownload dismiss on outside pointerdown and Escape', async () => {
  await mount(ReportViewSelect, { views: ['ribbon', 'brief'], active: 'ribbon', onSelect() {} });
  const details = container.querySelector('details');
  details.setAttribute('open', '');
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  assert.equal(details.open, false);

  details.setAttribute('open', '');
  await act(async () => document.body.dispatchEvent(new dom.window.MouseEvent('pointerdown', { bubbles: true })));
  assert.equal(details.open, false);

  await act(async () => root.unmount());
  root = null;
  document.body.replaceChildren();

  await mount(ReportDownload, {
    state: {
      meeting_id: 'm1', seq: 1, status: 'ended', cloud_enabled: true, intelligence_online: false,
      diarization_available: false, title: 'Sync', topic: { current: '', history: [] },
      rolling_summary: '', rolling_summary_evidence: [], participants: {}, cards: {}, questions: [],
    },
    transcriptComplete: true,
  });
  const dlDetails = container.querySelector('details');
  dlDetails.setAttribute('open', '');
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  assert.equal(dlDetails.open, false);
});

test('mergeSegments preserves reference on empty incoming and handles fast-path append, in-place update, and out-of-order merge', () => {
  const seg = (id, start_s, text) => ({
    id, meeting_id: 'm1', chunk_id: null, channel: 'mic', start_s, end_s: start_s + 2, text,
    speaker_participant_id: null, speaker_source: 'channel', speaker_pinned: false,
  });
  const base = [seg('s1', 0, 'first'), seg('s2', 5, 'second')];
  assert.equal(mergeSegments(base, []), base, 'empty incoming preserves exact array reference');

  const appended = mergeSegments(base, [seg('s3', 10, 'third')]);
  assert.deepEqual(appended.map((s) => s.id), ['s1', 's2', 's3']);

  const batchAppended = mergeSegments(appended, [seg('s4', 12, 'fourth'), seg('s5', 15, 'fifth')]);
  assert.deepEqual(batchAppended.map((s) => s.id), ['s1', 's2', 's3', 's4', 's5']);

  const updatedInPlace = mergeSegments(batchAppended, [seg('s2', 5, 'second revised')]);
  assert.deepEqual(updatedInPlace.map((s) => s.id), ['s1', 's2', 's3', 's4', 's5']);
  assert.equal(updatedInPlace[1].text, 'second revised');

  const outOfOrder = mergeSegments(updatedInPlace, [seg('s1b', 3, 'inserted'), seg('s4', 12, 'fourth revised')]);
  assert.deepEqual(outOfOrder.map((s) => s.id), ['s1', 's1b', 's2', 's3', 's4', 's5']);
  assert.equal(outOfOrder[4].text, 'fourth revised');
});

