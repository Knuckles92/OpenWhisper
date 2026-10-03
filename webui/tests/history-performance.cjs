const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<html><body></body></html>', { url: 'http://localhost/' });
for (const key of ['window', 'document', 'navigator', 'location', 'HTMLElement', 'Event', 'KeyboardEvent', 'MouseEvent', 'DOMException']) global[key] = key === 'window' ? dom.window : dom.window[key];
global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React;
const { createRoot } = require('react-dom/client');
const { renderToStaticMarkup } = require('react-dom/server');
const fs = require('fs'), ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 } }).outputText, f);
require.extensions['.css'] = () => {};
const TranscriptPane = require('../src/components/TranscriptPane.tsx').default;
const ReportTabs = require('../src/components/report/ReportTabs.tsx').default;
const ReportDownload = require('../src/components/report/ReportDownload.tsx').default;
const FullMeetingDocument = require('../src/components/report/FullMeetingDocument.tsx').default;
const HistoryPane = require('../src/components/HistoryPane.tsx').default;
const { api } = require('../src/api.ts');
const { printMeeting } = require('../src/print.ts');
let root, container;
async function mount(Component, props) { container = document.createElement('div'); document.body.append(container); root = createRoot(container); await act(async () => root.render(React.createElement(Component, props))); }
async function rerender(Component, props) { await act(async () => root.render(React.createElement(Component, props))); }
afterEach(async () => { if (root) await act(async () => root.unmount()); root = null; document.body.replaceChildren(); });
const state = id => ({ meeting_id: id, seq: 0, title: id, status: 'ended', cloud_enabled: false, intelligence_online: false, participants: {}, cards: {}, questions: [], topic: { current: '', history: [] }, rolling_summary: 'Saved summary', report_views: ['brief'] });
const segment = (id, index) => ({ id, meeting_id: 'm1', channel: 'mic', start_s: index * 2, end_s: index * 2 + 1, text: 'Turn ' + id, speaker_participant_id: null });
const turns = Array.from({ length: 5000 }, (_, index) => segment('s' + index, index));
const transcriptProps = extra => ({ segments: turns, participants: [], highlightSegmentId: null, onHighlightClear() {}, onReassignSpeaker() {}, readOnly: true, ...extra });
const button = text => [...container.querySelectorAll('button')].find(node => node.textContent.trim() === text);
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail; }); return { promise, resolve, reject }; };
const rows = [{ id: 'm1', title: 'First', status: 'ended', has_audio: false, started_at: '2026-09-01' }, { id: 'm2', title: 'Second', status: 'ended', has_audio: false, started_at: '2026-08-01' }];


test('large screen transcripts mount a bounded viewport and print retains every turn', () => {
  const visible = renderToStaticMarkup(React.createElement(React.Fragment, null,
    React.createElement(TranscriptPane, transcriptProps()), React.createElement(ReportTabs, { state: state('m1'), segments: turns, activeView: 'brief' })));
  assert.equal((visible.match(/<article/g) || []).length, 30);
  assert.ok(!visible.includes('full-meeting-document'));
  const printed = renderToStaticMarkup(React.createElement(FullMeetingDocument, { state: state('m1'), segments: turns }));
  assert.equal((printed.match(/<article/g) || []).length, 5000);
  assert.match(printed, /Turn s4999/);
});


test('unmounted citation targets, keyboard endpoints and playback remain accessible', async () => {
  await mount(TranscriptPane, transcriptProps({ highlightSegmentId: 's4990' }));
  assert.equal(document.activeElement.id, 'seg-s4990');
  assert.ok(container.querySelectorAll('article').length < 80);
  await rerender(TranscriptPane, transcriptProps());
  const far = container.querySelector('#seg-s4990');
  await act(async () => far.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })));
  assert.equal(document.activeElement.id, 'seg-s0');
  await act(async () => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })));
  assert.equal(document.activeElement.id, 'seg-s4999');
  const audio = document.createElement('audio');
  Object.defineProperties(audio, { currentTime: { value: 9980, configurable: true }, paused: { value: false, configurable: true }, ended: { value: false, configurable: true } });
  await rerender(TranscriptPane, transcriptProps({ audioRef: { current: audio }, audioKey: 'recording' }));
  await act(async () => audio.dispatchEvent(new Event('timeupdate')));
  assert.equal(container.querySelector('[aria-current="true"]').id, 'seg-s4990');
  assert.ok(container.querySelectorAll('article').length < 80);
});


test('measured row heights keep scrolling bounded and retain the viewport anchor on resize', async () => {
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const originalObserver = global.ResizeObserver;
  const observers = new Set(); let width = 600;
  global.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.add(this); }
    observe() {}
    disconnect() { observers.delete(this); }
  };
  const heightOf = node => node.dataset.transcriptId ? (Number(node.dataset.transcriptId.slice(1)) % 2 ? 60 : 120) * (width === 600 ? 1 : 0.75) : Number.parseFloat(node.style.height) || 0;
  HTMLElement.prototype.getBoundingClientRect = function () {
    const scroller = document.getElementById('measured-scroll');
    let top = 0, height = 600;
    if (this.classList.contains('segment-list')) top = 100 - (scroller?.scrollTop || 0);
    if (this.dataset.transcriptId) {
      top = 100 - (scroller?.scrollTop || 0);
      for (let node = this.previousElementSibling; node; node = node.previousElementSibling) top += heightOf(node);
      height = heightOf(this);
    }
    return { top, bottom: top + height, left: 0, right: width, width, height, x: 0, y: top, toJSON() {} };
  };
  try {
    await mount(() => React.createElement('div', { id: 'measured-scroll', style: { overflowY: 'auto', height: 600 } }, React.createElement(TranscriptPane, transcriptProps())), {});
    const scroller = document.getElementById('measured-scroll');
    Object.defineProperty(scroller, 'clientHeight', { value: 600 });
    scroller.scrollTo = ({ top }) => { scroller.scrollTop = top; scroller.dispatchEvent(new Event('scroll')); };
    await act(async () => scroller.scrollTo({ top: 40000 }));
    assert.ok(container.querySelectorAll('article').length < 80);
    const firstVisible = () => [...container.querySelectorAll('article')].find(node => node.getBoundingClientRect().bottom > 0)?.id;
    const before = firstVisible();
    assert.ok(Number(before.slice(5)) > 300, before);
    await act(async () => { width = 800; for (const observer of [...observers]) observer.callback([]); window.dispatchEvent(new Event('resize')); });
    assert.equal(firstVisible(), before);
    assert.ok(container.querySelectorAll('article').length < 80);
  } finally { HTMLElement.prototype.getBoundingClientRect = originalRect; global.ResizeObserver = originalObserver; }
});


test('preview omits transcript hydration and changing selection aborts a late page', async () => {
  const originals = { meetings: api.meetings, meeting: api.meeting, page: api.meetingTranscriptPage };
  const requests = [], pages = [];
  api.meetings = async () => ({ meetings: rows, next_cursor: null });
  api.meeting = async (_token, id, options) => { requests.push({ id, options }); return { meeting: rows.find(row => row.id === id), state: state(id), segments: [], transcript_next_cursor: null, transcript_included: false }; };
  api.meetingTranscriptPage = (_token, id, cursor, _limit, signal) => { const pending = deferred(); pages.push({ id, cursor, signal, pending }); return pending.promise; };
  const props = { token: 'host', selectedId: 'm1', focused: false, onClose() {} };
  try {
    await mount(HistoryPane, props);
    assert.equal(pages.length, 0);
    assert.equal(requests[0].options.includeTranscript, false);
    await rerender(HistoryPane, { ...props, focused: true });
    assert.equal(pages.length, 1);
    await rerender(HistoryPane, { ...props, selectedId: 'm2', focused: true });
    assert.equal(pages[0].signal.aborted, true);
    assert.equal(pages.length, 2);
    await act(async () => { pages[1].pending.resolve({ items: [segment('current', 0)], next_cursor: 'more' }); pages[0].pending.resolve({ items: [segment('stale', 0)], next_cursor: 'unwanted' }); });
    assert.match(container.textContent, /Turn current/);
    assert.doesNotMatch(container.textContent, /Turn stale/);
    assert.equal(pages.length, 2, 'continuation is requested only when needed');
    await act(async () => button('Load more transcript').click());
    assert.equal(pages[2].cursor, 'more');
    await rerender(HistoryPane, { ...props, selectedId: 'm2', focused: false });
    assert.equal(pages[2].signal.aborted, true, 'leaving full view stops its download');
    await act(async () => pages[2].pending.resolve({ items: [], next_cursor: null }));
  } finally { api.meetings = originals.meetings; api.meeting = originals.meeting; api.meetingTranscriptPage = originals.page; }
});


test('meeting list loads a continuation on demand and deduplicates overlapping rows', async () => {
  const original = api.meetings, cursors = [];
  api.meetings = async (_token, options) => { cursors.push(options.cursor); return options.cursor ? { meetings: [rows[0], rows[1]], next_cursor: null } : { meetings: [rows[0]], next_cursor: 'older' }; };
  try {
    await mount(HistoryPane, { token: 'host', onClose() {} });
    assert.equal(container.querySelectorAll('.history-item').length, 1);
    await act(async () => button('Load more meetings').click());
    assert.deepEqual(cursors, [undefined, 'older']);
    assert.equal(container.querySelectorAll('.history-item').length, 2);
  } finally { api.meetings = original; }
});


test('report polling uses state only and schedules the next poll after completion', async () => {
  const originals = { meetings: api.meetings, meeting: api.meeting, status: api.meetingState, setTimeout, clearTimeout };
  const timers = new Map(); let id = 10000;
  global.setTimeout = (callback, delay, ...args) => { if (delay !== 2000) return originals.setTimeout(callback, delay, ...args); const key = ++id; timers.set(key, callback); return key; };
  global.clearTimeout = key => { if (timers.has(key)) timers.delete(key); else originals.clearTimeout(key); };
  const pending = deferred(), calls = [];
  let details = 0;
  api.meetings = async () => ({ meetings: rows, next_cursor: null });
  api.meeting = async (_token, meetingId) => { details++; return { meeting: rows[0], state: { ...state(meetingId), insight_review: { status: 'running' } }, segments: [], transcript_next_cursor: null, transcript_included: false }; };
  api.meetingState = (_token, meetingId, signal) => { calls.push({ meetingId, signal }); return pending.promise; };
  try {
    await mount(HistoryPane, { token: 'host', selectedId: 'm1', focused: false, onClose() {} });
    assert.equal(timers.size, 1);
    const [key, callback] = [...timers][0]; timers.delete(key);
    await act(async () => { void callback(); });
    assert.equal(calls.length, 1); assert.equal(timers.size, 0, 'slow request has no competing timer');
    await act(async () => pending.resolve({ state: { ...state('m1'), insight_review: { status: 'running' } } }));
    assert.equal(timers.size, 1); assert.equal(details, 1);
    await act(async () => root.unmount()); root = null;
    assert.equal(calls[0].signal.aborted, true); assert.equal(timers.size, 0);
  } finally { api.meetings = originals.meetings; api.meeting = originals.meeting; api.meetingState = originals.status; global.setTimeout = originals.setTimeout; global.clearTimeout = originals.clearTimeout; }
});


test('full print constructs its complete tree synchronously and removes it after print', async () => {
  const original = window.print;
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  let count = 0;
  window.print = () => { window.dispatchEvent(new Event('beforeprint')); count = container.querySelectorAll('.full-meeting-document article').length; window.dispatchEvent(new Event('afterprint')); };
  try {
    await mount(ReportTabs, { state: state('m1'), segments: turns, activeView: 'brief', transcriptComplete: true });
    assert.equal(container.querySelector('.full-meeting-document'), null);
    await act(async () => printMeeting('full', 'Saved meeting'));
    assert.equal(count, 5000);
    assert.equal(container.querySelector('.full-meeting-document'), null);
    await act(async () => printMeeting('summary', 'Summary'));
    assert.equal(count, 0);
  } finally { window.print = original; }
});


test('full download commits its last transcript page before the print snapshot', async () => {
  const originals = { meetings: api.meetings, meeting: api.meeting, page: api.meetingTranscriptPage, print: window.print };
  const finalPage = deferred(), cursors = [];
  let printed = [];
  api.meetings = async () => ({ meetings: rows, next_cursor: null });
  api.meeting = async () => ({ meeting: rows[0], state: state('m1'), segments: [], transcript_next_cursor: null, transcript_included: false });
  api.meetingTranscriptPage = async (_token, _id, cursor) => {
    cursors.push(cursor);
    return cursor ? finalPage.promise : { items: turns.slice(0, 3), next_cursor: 'final-page' };
  };
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  window.print = () => {
    window.dispatchEvent(new Event('beforeprint'));
    printed = [...container.querySelectorAll('.full-meeting-document article')].map(node => node.dataset.transcriptId);
    window.dispatchEvent(new Event('afterprint'));
  };
  try {
    await mount(HistoryPane, { token: 'host', selectedId: 'm1', focused: true, onClose() {} });
    assert.equal(button('Full meeting').disabled, false);
    assert.equal(cursors.length, 1, 'opening the meeting requests one page');
    await act(async () => button('Full meeting').click());
    assert.ok(button('Preparing full meeting…'));
    assert.deepEqual(cursors, [undefined, 'final-page']);
    await act(async () => finalPage.resolve({ items: turns.slice(3, 5), next_cursor: null }));
    assert.deepEqual(printed, ['s0', 's1', 's2', 's3', 's4']);
    assert.ok(button('Full meeting'));
    assert.equal(container.querySelector('.full-meeting-document'), null);
  } finally { api.meetings = originals.meetings; api.meeting = originals.meeting; api.meetingTranscriptPage = originals.page; window.print = originals.print; }
});


test('print preparation ignores stale failures, cancellation and unmounted completion', async () => {
  const original = window.print;
  const old = deferred(), next = deferred(), canceled = deferred(), unmounted = deferred();
  let prints = 0;
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  window.print = () => { prints++; window.dispatchEvent(new Event('afterprint')); };
  const props = pending => ({ state: state('m2'), transcriptComplete: false, onPrepareFull: () => pending.promise });
  try {
    await mount(ReportDownload, { ...props(old), state: state('m1') });
    assert.ok(button('Full meeting'));
    await act(async () => button('Full meeting').click());
    await rerender(ReportDownload, props(next));
    assert.ok(button('Full meeting'));
    await act(async () => button('Full meeting').click());
    await act(async () => old.reject(new Error('Stale meeting failed')));
    assert.equal(container.querySelector('[role="alert"]'), null);
    assert.ok(button('Preparing full meeting…'), 'stale finally does not clear a newer preparation');
    await act(async () => next.resolve());
    assert.equal(prints, 1);
    await rerender(ReportDownload, props(canceled));
    await act(async () => button('Full meeting').click());
    await act(async () => canceled.reject(new DOMException('Meeting closed', 'AbortError')));
    assert.equal(container.querySelector('[role="alert"]'), null);
    assert.ok(button('Full meeting')); assert.equal(prints, 1);
    await rerender(ReportDownload, props(unmounted));
    await act(async () => button('Full meeting').click());
    await act(async () => root.unmount()); root = null;
    await act(async () => unmounted.reject(new Error('Late failure')));
    assert.equal(prints, 1);
  } finally { window.print = original; }
});
