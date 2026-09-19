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
const React = require('react');
const {act} = React;
const {createRoot} = require('react-dom/client');
const PulseStrip = require('../src/components/HighlightPulseStrip.tsx').default;
const CitationBadge = require('../src/components/CitationBadge.tsx').default;
const {playMoment, stopPlayback} = require('../src/playback.ts');
const {correctionText} = require('../src/corrections.ts');
let root;
async function mount(component, props) {
  const container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(component, props)));
  return container;
}
afterEach(async () => {if (root) {await act(async () => root.unmount()); root = null;} document.body.replaceChildren();});

test('every pulse is labelled and passes its exact playback anchor on click', async () => {
  const pulses = ['decision','disagreement','commitment','number'].map((kind,i) => ({id:`p${i}`, kind, start_s:61+i, segment_id:`sg_${i}`, probability:.95, text:`Moment ${i}`}));
  const picked = [];
  const container = await mount(PulseStrip, {pulses, onSelect:p=>picked.push(p)});
  const buttons = [...container.querySelectorAll('button')];
  assert.equal(buttons.length, 4);
  for (const button of buttons) {
    assert.match(button.getAttribute('aria-label'), /at 1:0[1-4]/);
    await act(async () => button.click());
  }
  assert.deepEqual(picked, pulses);
});

test('citation badge is advisory and vanishes when its claim revision changes', async () => {
  const item = {revision:2, citation_check:{status:'unsupported', revision:2}};
  const container = await mount(CitationBadge, {item});
  assert.match(container.textContent, /Check citation/);
  assert.match(container.querySelector('span').title, /does not certify/);
  await act(async () => root.render(React.createElement(CitationBadge, {item:{...item, revision:3}})));
  assert.equal(container.textContent, '');
});

test('playback refreshes live audio and only the latest metadata seek runs', async () => {
  const audio = document.createElement('audio');
  let loads=0, plays=0;
  audio.load = () => {loads++;};
  audio.play = () => {plays++; return Promise.reject(new Error('autoplay blocked'));};
  playMoment(audio, 65, '/audio?revision=1');
  playMoment(audio, 125, '/audio?revision=2');
  audio.dispatchEvent(new dom.window.Event('loadedmetadata'));
  await Promise.resolve();
  assert.equal(loads, 2);
  assert.equal(plays, 1);
  assert.equal(audio.currentTime, 125);
});

test('spoken term correction is reversible and cannot be forged by an agent', () => {
  const note = {author_type:'system', author_id:'voice_command', status:'proposed', data:{kind:'term_correction',source:'voice_command',command:'fix_transcript',selected_text:'Myra',replacement:'Maya'}};
  assert.equal(correctionText([note])('Myra owns it.'), 'Maya owns it.');
  assert.equal(correctionText([{...note,status:'removed'}])('Myra owns it.'), 'Myra owns it.');
  assert.equal(correctionText([{...note,author_type:'agent'}])('Myra owns it.'), 'Myra owns it.');
});


test('history meaning search exposes keyword fallback and ignores stale responses', async () => {
  const {api} = require('../src/api.ts');
  const HistoryPane = require('../src/components/HistoryPane.tsx').default;
  const originalMeetings = api.meetings, originalSearch = api.searchHistory;
  let resolve, mode;
  api.meetings = async () => [];
  api.searchHistory = async (token, query, selectedMode) => {mode=selectedMode; return new Promise(done => {resolve=done;});};
  try {
    const container = await mount(HistoryPane, {token:'demo', onClose:()=>{}});
    const field = container.querySelector('input[type="search"]');
    const type = async value => act(async () => {
      Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(field,value);
      field.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
    });
    const search = [...container.querySelectorAll('button')].find(b=>b.textContent==='Search');
    await type('shipping delay');
    await act(async () => search.click());
    assert.equal(mode, 'semantic');
    assert.equal(search.disabled, true);
    await type('different question');
    await act(async () => resolve({results:[{title:'Old result'}],mode:'semantic',message:'Old scope'}));
    assert.doesNotMatch(container.textContent, /Old result/);
    await act(async () => search.click());
    await act(async () => resolve({results:[],mode:'keyword',message:'Semantic ranking unavailable. Showing keyword results.'}));
    assert.match(container.textContent, /Showing keyword results/);
    assert.equal(search.disabled, false);
  } finally {api.meetings=originalMeetings; api.searchHistory=originalSearch;}
});


test('closing a replay while metadata is loading cancels the pending autoplay', () => {
  const audio = document.createElement('audio');
  let plays = 0, pauses = 0;
  audio.load = () => {};
  audio.play = () => { plays++; return Promise.resolve(); };
  audio.pause = () => { pauses++; };
  playMoment(audio, 60, '/recording');
  stopPlayback(audio);
  audio.dispatchEvent(new dom.window.Event('loadedmetadata'));
  assert.equal(plays, 0);
  assert.equal(pauses, 1);
});

test('replay keeps one controllable audio element and supports skipping, closing, and load failures', async () => {
  const RecordingPlayer = require('../src/components/RecordingPlayer.tsx').default;
  const audioRef = {current: null};
  let closed = 0, plays = 0;
  const props = {audioRef, src: '/recording', label: 'Meeting audio', moment: null, onClose: () => { closed++; }};
  const container = await mount(RecordingPlayer, props);
  const audio = audioRef.current;
  audio.load = () => {};
  audio.pause = () => {};
  audio.play = () => { plays++; return Promise.resolve(); };
  Object.defineProperty(audio, 'duration', {value: 150});
  const moment = {kind: 'number', start_s: 60.874, text: 'Budget A needs a thousand dollars.'};
  await act(async () => root.render(React.createElement(RecordingPlayer, {...props, moment})));
  assert.equal(container.querySelector('audio'), audio);
  assert.equal(audio.controls, true); // The original player is reused when the floating controls close.
  assert.equal(container.querySelectorAll('audio').length, 1);
  assert.match(document.body.textContent, /Budget A needs a thousand dollars/);
  assert.ok(document.querySelector('.is-floating'));
  const back = document.querySelector('[aria-label="Skip back 10 seconds"]');
  const forward = document.querySelector('[aria-label="Skip forward 10 seconds"]');
  assert.equal(back.disabled, true);
  await act(async () => audio.dispatchEvent(new dom.window.Event('loadedmetadata')));
  audio.currentTime = 60;
  await act(async () => forward.click());
  assert.equal(audio.currentTime, 70);
  await act(async () => back.click());
  assert.equal(audio.currentTime, 60);
  audio.currentTime = 4;
  await act(async () => back.click());
  assert.equal(audio.currentTime, 0);
  audio.currentTime = 149;
  await act(async () => forward.click());
  assert.equal(audio.currentTime, 150);
  await act(async () => {
    playMoment(audio, 60, '/fresh-recording');
    document.querySelector('[aria-label="Close replay and stop audio"]').click();
    audio.dispatchEvent(new dom.window.Event('loadedmetadata'));
  });
  assert.equal(closed, 1);
  assert.equal(plays, 0);
  await act(async () => audio.dispatchEvent(new dom.window.Event('error')));
  assert.match(document.body.textContent, /Recording unavailable/);
  assert.equal(forward.disabled, true);
  await act(async () => [...document.querySelectorAll('button')].find(b => b.textContent === 'Retry').click());
  await act(async () => audio.dispatchEvent(new dom.window.Event('loadedmetadata')));
  assert.equal(plays, 1);
  playMoment(audio, 80, '/another-recording');
  await act(async () => root.unmount()); root = null;
  audio.dispatchEvent(new dom.window.Event('loadedmetadata'));
  assert.equal(plays, 1);
});

test('highlights without a recording explain why playback is disabled', async () => {
  let picked = false;
  const container = await mount(PulseStrip, {pulses: [{id:'p',kind:'number',start_s:60,text:'Budget'}],
    playbackAvailable: false, onSelect: () => { picked = true; }});
  assert.match(container.textContent, /No recording available/);
  const button = container.querySelector('button');
  assert.equal(button.disabled, true);
  await act(async () => button.click());
  assert.equal(picked, false);
});


test('highlight status from hello and live updates replaces the empty message without losing pulses', async () => {
  const {initialUiState, meetingReducer} = require('../src/state.ts');
  const pulse = {id:'p', kind:'number', start_s:60, text:'Budget'};
  let ui = meetingReducer(initialUiState, {type:'server_message', msg:{
    type:'hello', role:'host', participant_id:null, segments:[], urls:{},
    state:{status:'active', cloud_enabled:true, live_highlights_status:'off', live_highlights:[], participants:{}, cards:{}},
  }});
  const props = () => ({pulses:ui.state.live_highlights, highlightStatus:ui.state.live_highlights_status,
    meetingStatus:ui.state.status, cloudEnabled:ui.state.cloud_enabled, isHost:true, onSelect:() => {}});
  const container = await mount(PulseStrip, props());
  assert.match(container.querySelector('[role=status]').textContent, /Live highlights are off/);
  assert.doesNotMatch(container.textContent, /Click a moment/);
  const update = async msg => {
    ui = meetingReducer(ui, {type:'server_message', msg});
    await act(async () => root.render(React.createElement(PulseStrip, props())));
  };
  await update({type:'status', live_highlights_status:'on'});
  assert.match(container.textContent, /Live highlights are on.*once a minute/);
  await update({type:'status', intelligence_online:false});
  assert.match(container.textContent, /Live highlights are on/);
  await update({type:'patch', results:[{seq:1, effect:{entity:'live_highlights', pulses:[pulse]}}]});
  await update({type:'status', live_highlights_status:'off'});
  assert.match(container.textContent, /Live highlights are off/);
  assert.equal(container.querySelectorAll('.pulse-mark').length, 1);
  await update({type:'status', live_highlights_status:'on'});
  await update({type:'patch', results:[{seq:2, effect:{entity:'cloud_enabled', enabled:false}}]});
  assert.match(container.textContent, /Live highlights are on, but Cloud insights are off/);
});

for (const [props, expected] of [
  [{highlightStatus:'off', isHost:false}, /Live highlights are off.*host can enable/],
  [{highlightStatus:'unavailable', isHost:true}, /Live highlights are on, but unavailable.*Meeting settings/],
  [{highlightStatus:'on', cloudEnabled:false, isHost:false}, /host has Cloud insights turned off/],
  [{highlightStatus:'on', meetingStatus:'paused'}, /Live highlights are on.*Resume the meeting/],
  [{}, /Live highlight status is unavailable/],
  [{highlightStatus:'on', meetingStatus:'ended'}, /No highlights were captured for this meeting/],
]) {
  test('highlights explain current availability: ' + expected, async () => {
    const container = await mount(PulseStrip, {pulses:[], meetingStatus:'active', onSelect:() => {}, ...props});
    assert.match(container.textContent, expected);
    assert.doesNotMatch(container.textContent, /when live highlights are enabled/);
  });
}
