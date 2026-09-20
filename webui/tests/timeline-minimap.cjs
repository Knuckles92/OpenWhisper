const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'KeyboardEvent', 'MouseEvent']) {
  global[key] = key === 'window' ? dom.window : dom.window[key];
}
global.IS_REACT_ACT_ENVIRONMENT = true;
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
const TimelineMinimap = require('../src/components/report/TimelineMinimap.tsx').default;
const TranscriptPane = require('../src/components/TranscriptPane.tsx').default;

let root, container;
async function mount(Component, props) {
  container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(Component, props)));
}
afterEach(async () => { if (root) await act(async () => root.unmount()); document.body.replaceChildren(); });

const segment = (id, start, end, text, speaker = null) => ({
  id, meeting_id: 'm1', chunk_id: null, channel: 'loopback', start_s: start, end_s: end, text,
  speaker_participant_id: speaker, speaker_source: 'channel', speaker_pinned: false,
});

const SEGMENTS = [
  segment('a', 0, 10, 'Opening the review', 'p1'),
  segment('b', 12, 24, 'Splitting the key review into four', 'p2'),
  segment('c', 30, 44, 'Two month rotation it is'),
];
const PARTICIPANTS = {
  p1: { id: 'p1', display_name: 'Lily' },
  p2: { id: 'p2', display_name: 'Eric Johnson' },
};
const MARKERS = [{ id: 'd1', card: 'decisions', time: 18, text: 'Split the review' }];

/** A stand-in for the dashboard's <audio>: jsdom has no media pipeline. */
function fakeAudio() {
  const node = document.createElement('audio');
  const state = { time: 0, paused: true, ended: false, duration: 44 };
  for (const [key, read] of [['currentTime', () => state.time], ['paused', () => state.paused],
    ['ended', () => state.ended], ['duration', () => state.duration]]) {
    Object.defineProperty(node, key, { get: read, configurable: true });
  }
  document.body.append(node);
  return {
    ref: { current: node },
    async move(time, { paused = false } = {}) {
      state.time = time; state.paused = paused;
      await act(async () => node.dispatchEvent(new Event('timeupdate')));
    },
  };
}

const props = (overrides = {}) => ({
  segments: SEGMENTS, markers: MARKERS, participants: PARTICIPANTS, duration: 44, ...overrides,
});

async function clickAt(element, clientX) {
  await act(async () => element.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, clientX })));
}

test('clicking a turn seeks to its start; clicking the gap seeks by position', async () => {
  const seeks = [];
  await mount(TimelineMinimap, props({ onSeek: (s) => seeks.push(s) }));
  const track = container.querySelector('.mm-track');
  const bars = container.querySelectorAll('.mm-seg');
  assert.equal(bars.length, 3);
  assert.equal(track.getAttribute('role'), 'slider');
  assert.equal(track.getAttribute('aria-valuemax'), '44');

  await clickAt(bars[1], 5);
  assert.deepEqual(seeks, [12]);

  // jsdom reports a zero-width box, so give the track a real one to hit.
  track.getBoundingClientRect = () => ({ left: 0, width: 200, top: 0, height: 78, right: 200, bottom: 78 });
  await clickAt(track, 100);
  assert.equal(seeks.length, 2);
  assert.ok(Math.abs(seeks[1] - 22) < 0.001, `expected mid-track seek, got ${seeks[1]}`);

  // Past the end of the track still lands inside the meeting.
  await clickAt(track, 400);
  assert.equal(seeks[2], 44);
});

test('marker clicks seek once, without also firing the track behind them', async () => {
  const seeks = [];
  await mount(TimelineMinimap, props({ onSeek: (s) => seeks.push(s) }));
  const marker = container.querySelector('.mm-marker');
  assert.equal(marker.tagName, 'BUTTON');
  assert.equal(marker.getAttribute('type'), 'button');
  assert.match(marker.getAttribute('aria-label'), /Decision at 0:18: Split the review/);
  await clickAt(marker, 60);
  assert.deepEqual(seeks, [18]);
});

test('the playhead and the turn highlight follow the recording', async () => {
  const audio = fakeAudio();
  await mount(TimelineMinimap, props({ onSeek() {}, audioRef: audio.ref, audioKey: 'm1' }));
  assert.equal(container.querySelector('.mm-playhead'), null, 'no playhead before playback starts');

  await audio.move(18);
  const head = container.querySelector('.mm-playhead');
  assert.ok(head, 'playhead appears once the recording moves');
  assert.ok(head.className.includes('is-playing'));
  assert.equal(head.style.left, `${(18 / 44) * 100}%`);
  assert.equal(container.querySelector('.mm-played').style.width, `${(18 / 44) * 100}%`);
  assert.equal(container.querySelector('.mm-track').getAttribute('aria-valuenow'), '18');

  const playing = container.querySelectorAll('.mm-seg.is-playing');
  assert.equal(playing.length, 1);
  assert.equal(playing[0].dataset.start, '12');
  assert.match(container.querySelector('.mm-now').textContent, /0:18.*Eric Johnson.*Splitting the key review/);

  // A gap between turns keeps the playhead but has nothing to highlight.
  await audio.move(27, { paused: true });
  assert.equal(container.querySelectorAll('.mm-seg.is-playing').length, 0);
  assert.equal(container.querySelector('.mm-now'), null);
  assert.equal(container.querySelector('.mm-playhead').className.includes('is-playing'), false);
});

test('arrow and Home/End keys scrub the track from the current position', async () => {
  const audio = fakeAudio();
  const seeks = [];
  await mount(TimelineMinimap, props({ onSeek: (s) => seeks.push(s), audioRef: audio.ref, audioKey: 'm1' }));
  await audio.move(18);
  const track = container.querySelector('.mm-track');
  const press = async (key) => act(async () =>
    track.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })));
  await press('ArrowRight');
  await press('ArrowLeft');
  await press('PageUp');
  await press('Home');
  await press('End');
  await press('Enter');
  assert.deepEqual(seeks, [23, 13, 44, 0, 44]);
});

test('without a seek handler the track is inert and markers stay legible', async () => {
  await mount(TimelineMinimap, props());
  const track = container.querySelector('.mm-track');
  assert.equal(track.getAttribute('role'), null);
  assert.equal(track.getAttribute('tabindex'), null);
  assert.equal(container.querySelector('.mm-marker').disabled, true);
  assert.match(container.querySelector('.mm-legend').textContent, /colour = speaker/);
});

test('the conversation title and its player share one stickable head block', async () => {
  await mount(TranscriptPane, {
    segments: SEGMENTS, participants: [], highlightSegmentId: null,
    onHighlightClear() {}, onReassignSpeaker() {}, readOnly: true,
    headerExtra: React.createElement('div', { className: 'recording-inline' }, 'player'),
  });
  const head = container.querySelector('.panel-head');
  assert.ok(head, 'transcript renders a head block');
  assert.ok(head.querySelector('.panel-header'), 'title lives in the head');
  assert.ok(head.querySelector('.recording-inline'), 'player lives in the head');
  assert.equal(head.parentElement.className, 'panel');
  assert.equal(head.nextElementSibling.className, 'panel-body');
});

const transcript = (overrides = {}) => ({
  segments: SEGMENTS, participants: [], highlightSegmentId: null,
  onHighlightClear() {}, onReassignSpeaker() {}, readOnly: true, ...overrides,
});

test('clicking a turn, or its timestamp, plays the recording from that turn', async () => {
  const played = [];
  await mount(TranscriptPane, transcript({ onPlaySegment: (s, id) => played.push([s, id]) }));
  const turns = container.querySelectorAll('.segment');
  assert.equal(turns.length, 3);
  assert.ok(turns[1].className.includes('is-seekable'));

  await clickAt(turns[1], 0);
  assert.deepEqual(played, [[12, 'b']]);

  // The timestamp is the keyboard-reachable control; clicking it must not also
  // fire the row handler it bubbles through.
  const seek = turns[2].querySelector('.segment-seek');
  assert.equal(seek.tagName, 'BUTTON');
  assert.equal(seek.getAttribute('aria-label'), 'Play the recording from 0:30');
  await clickAt(seek, 0);
  assert.deepEqual(played, [[12, 'b'], [30, 'c']]);
});

test('an in-progress text selection is not hijacked into a seek', async () => {
  const played = [];
  await mount(TranscriptPane, transcript({ onPlaySegment: (s, id) => played.push([s, id]) }));
  const selection = window.getSelection();
  const original = Object.getOwnPropertyDescriptor(selection.constructor.prototype, 'isCollapsed');
  Object.defineProperty(selection, 'isCollapsed', { get: () => false, configurable: true });
  try {
    await clickAt(container.querySelectorAll('.segment')[1], 0);
    assert.deepEqual(played, [], 'releasing a selection inside a turn should not seek');
  } finally {
    if (original) Object.defineProperty(selection, 'isCollapsed', original);
  }
});

test('the turn being played marks itself and follows the recording', async () => {
  const audio = fakeAudio();
  await mount(TranscriptPane, transcript({
    onPlaySegment() {}, audioRef: audio.ref, audioKey: 'm1',
  }));
  assert.equal(container.querySelector('.segment.is-playing'), null, 'nothing is marked before playback');

  await audio.move(18);
  const playing = container.querySelectorAll('.segment.is-playing');
  assert.equal(playing.length, 1);
  assert.match(playing[0].textContent, /Splitting the key review/);
  assert.equal(playing[0].getAttribute('aria-current'), 'true');

  await audio.move(35);
  assert.match(container.querySelector('.segment.is-playing').textContent, /Two month rotation/);

  // A gap between turns leaves nothing to mark.
  await audio.move(27, { paused: true });
  assert.equal(container.querySelector('.segment.is-playing'), null);
});

test('without a play handler the transcript stays inert for print', async () => {
  await mount(TranscriptPane, transcript());
  assert.equal(container.querySelector('.segment-seek'), null);
  assert.equal(container.querySelector('.segment.is-seekable'), null);
  assert.equal(container.querySelector('.segment-time').tagName, 'TIME');
});
