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
global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions:{module:ts.ModuleKind.CommonJS, jsx:ts.JsxEmit.ReactJSX, target:ts.ScriptTarget.ES2020},
}).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react');
const {act} = React;
const {createRoot} = require('react-dom/client');
const RoomDisplay = require('../src/components/RoomDisplay.tsx').default;
const {PocketTabs, pocketTabs, usePocketTab} = require('../src/components/PocketShell.tsx');

let root;
async function mount(component, props) {
  const container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(component, props)));
  return container;
}
afterEach(async () => {if (root) {await act(async () => root.unmount()); root = null;} document.body.replaceChildren();});

const item = (id, card, text, extra = {}) => ({id, card, text, data:{}, status:'confirmed', author_type:'agent', author_id:null,
  pinned:false, revision:1, evidence:[], created_at:'2026-09-22T09:00:00Z', updated_at:'2026-09-22T09:0' + id.length + ':00Z', ...extra});
const people = [
  {id:'me', display_name:'Dana Park', kind:'me', name_source:'human', is_provisional:false, created_at:'1', updated_at:'1'},
  {id:'luis', display_name:'Luis Ortega', kind:'others_cluster', name_source:'human', is_provisional:false, created_at:'2', updated_at:'2'},
];
const state = {
  meeting_id:'m', seq:1, status:'active', title:'Pump station review', cloud_enabled:true, intelligence_online:true,
  diarization_available:true, topic:{current:'Whether Pump 3 can wait', history:[]}, rolling_summary:'', rolling_summary_evidence:[],
  capture:{mic_available:true, loopback_available:true, message:''}, participants:{},
  cards:{decisions:[item('d1','decisions','Replace Pump 2 motor in Phase 1'), item('d2','decisions','Dropped idea', {status:'removed'})],
    action_items:[item('a1','action_items','Pull flow logs', {data:{owner_participant_id:'luis'}})],
    key_points:[], risks:[], timeline:[], live_notes:[], user_notes:[]},
  questions:[{id:'q', text:'Does the bypass carry full demand?', status:'open', evidence:[]}],
};
const segments = [
  {id:'s1', meeting_id:'m', channel:'mic', start_s:10, end_s:14, text:'First thing said', speaker_participant_id:'me'},
  {id:'s2', meeting_id:'m', channel:'loopback', start_s:20, end_s:24, text:'Second thing said', speaker_participant_id:'luis'},
  {id:'s3', meeting_id:'m', channel:'mic', start_s:30, end_s:34, text:'Latest thing said', speaker_participant_id:'me'},
];

test('room display shows the topic, the last two turns, and what the room decided and owes', async () => {
  const container = await mount(RoomDisplay, {state, segments, participants:people,
    chapters:[{label:'Goals', start_s:0}, {label:'Budget', start_s:20}], elapsedS:65, onExit(){}});
  const dialog = container.querySelector('[role=dialog]');
  assert.equal(dialog.getAttribute('aria-label'), 'Room display');
  assert.match(container.querySelector('.room-topic').textContent, /Whether Pump 3 can wait/);
  const lines = [...container.querySelectorAll('.room-line')].map(l => l.textContent);
  assert.deepEqual(lines, ['Luis OrtegaSecond thing said', 'Dana ParkLatest thing said']);
  assert.match(container.textContent, /Replace Pump 2 motor/);
  assert.doesNotMatch(container.textContent, /Dropped idea/, 'removed decisions stay off the wall');
  assert.match(container.textContent, /Luis Ortega Pull flow logs/);
  assert.match(container.textContent, /Still open.*bypass/);
  assert.match(container.querySelector('.room-clock').textContent, /^1:05$/);
  assert.equal(container.querySelector('.room-chapters li.current').textContent, 'Budget');
});

test('room display exits from its button and from Escape', async () => {
  let exits = 0;
  const container = await mount(RoomDisplay, {state, segments, participants:people, chapters:[], elapsedS:0, onExit:() => { exits++; }});
  const exit = container.querySelector('.room-exit');
  assert.equal(document.activeElement, exit, 'focus lands on the exit control');
  await act(async () => exit.click());
  await act(async () => document.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key:'Escape'})));
  assert.equal(exits, 2);
});

test('an ended meeting offers a transcript tab in place of live notes', () => {
  assert.deepEqual(pocketTabs(false), ['live', 'notes', 'captured', 'people']);
  assert.deepEqual(pocketTabs(true), ['live', 'transcript', 'captured', 'people']);
});

test('pocket tabs mark the current section, badge the captured count, and report picks', async () => {
  const picked = [];
  const container = await mount(PocketTabs, {tabs:pocketTabs(false), active:'live', onSelect:t => picked.push(t), capturedCount:3});
  const buttons = [...container.querySelectorAll('button.pocket-tab')];
  assert.equal(buttons.length, 4);
  assert.equal(buttons[0].getAttribute('aria-current'), 'page');
  assert.equal(buttons[1].getAttribute('aria-current'), null);
  assert.equal(container.querySelector('.pocket-badge').textContent, '3');
  await act(async () => buttons[2].click());
  assert.deepEqual(picked, ['captured']);
});

test('the chosen pocket tab is remembered for the session and falls back when unavailable', async () => {
  sessionStorage.clear();
  let api;
  function Probe({ended}) { api = usePocketTab(ended); return null; }
  await mount(Probe, {ended:false});
  assert.equal(api[0], 'live');
  await act(async () => api[1]('notes'));
  assert.equal(api[0], 'notes');
  assert.equal(sessionStorage.getItem('openwhisper.pocketTab'), 'notes');
  await act(async () => root.render(React.createElement(Probe, {ended:true})));
  assert.equal(api[0], 'live', 'ended meetings have no notes tab');
});
