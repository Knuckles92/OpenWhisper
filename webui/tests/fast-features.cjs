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

test('the inline player pops out where it sits and docks back without stopping', async () => {
  const RecordingPlayer = require('../src/components/RecordingPlayer.tsx').default;
  const audioRef = {current: null};
  let popped = null, closed = 0, pauses = 0;
  const props = {audioRef, src: '/recording', label: 'Meeting audio', moment: null,
    onClose: () => { closed++; }, onPopOut: moment => { popped = moment; }};
  const container = await mount(RecordingPlayer, props);
  const audio = audioRef.current;
  audio.load = () => {};
  audio.play = () => Promise.resolve();
  audio.pause = () => { pauses++; };
  Object.defineProperty(audio, 'duration', {value: 150});
  const popOut = () => container.querySelector('.recording-popout');
  assert.equal(popOut().disabled, true); // Nothing to pop out until the recording reports a length.
  await act(async () => audio.dispatchEvent(new dom.window.Event('loadedmetadata')));
  assert.equal(popOut().disabled, false);
  audio.currentTime = 42;
  await act(async () => popOut().click());
  assert.equal(popped.start_s, 42); // The panel opens where the audio already is, no seek.
  await act(async () => root.render(React.createElement(RecordingPlayer, {...props, moment: popped})));
  assert.equal(popOut(), null); // One player at a time.
  assert.equal(audio.hidden, true);
  assert.match(document.querySelector('.replay-heading-copy').textContent, /Full recording/);
  await act(async () => document.querySelector('[aria-label="Return to the small player and keep playing"]').click());
  assert.equal(closed, 1);
  assert.equal(pauses, 0); // Docking hands the same playback back to the inline controls.
  await act(async () => root.render(React.createElement(RecordingPlayer, {...props, moment: null})));
  assert.equal(audio.hidden, false);
  assert.equal(container.querySelectorAll('audio').length, 1);
});

test('highlights without a recording explain why playback is disabled', async () => {
  let picked = false;
  const container = await mount(PulseStrip, {pulses: [{id:'p',kind:'number',start_s:60,text:'Budget'}],
    playbackAvailable: false, onSelect: () => { picked = true; }});
  assert.match(container.textContent, /No recording available/);
  const button = container.querySelector('button');
  assert.equal(button.getAttribute('aria-disabled'), 'true');
  await act(async () => button.focus());
  assert.match(document.querySelector('[role=dialog]').textContent, /Detection probabilityUnavailable/);
  assert.match(document.querySelector('[role=dialog]').textContent, /No recording available for this moment/);
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

test('legacy pulse previews show individual saved scores, follow keyboard focus, and dismiss with Escape', async () => {
  const pulses = ['decision','disagreement','commitment','number'].map((kind,i) => ({
    id:`preview-${i}`, kind, start_s:61+i, segment_id:`source-${i}`, probability:.81 + i * .04, text:`Quoted passage ${i}`,
  }));
  const props = {pulses, onSelect:() => {}};
  const container = await mount(PulseStrip, props);
  const buttons = [...container.querySelectorAll('.pulse-mark')];
  assert.equal(document.querySelector('[role=dialog]'), null);
  for (const [i, button] of buttons.entries()) {
    await act(async () => button.focus());
    const tooltip = document.querySelector('[role=dialog]');
    assert.equal(document.querySelectorAll('[role=dialog]').length, 1);
    assert.equal(button.hasAttribute('title'), false);
    assert.equal(button.getAttribute('aria-controls'), tooltip.id);
    assert.equal(tooltip.querySelector('.pulse-preview-body').firstElementChild, tooltip.querySelector('blockquote'));
    assert.equal(tooltip.querySelector('blockquote').textContent, pulses[i].text);
    assert.match(tooltip.textContent, new RegExp(`1:0${i+1}`));
    assert.match(tooltip.querySelector('.pulse-preview-score').textContent, new RegExp(`${81 + i * 4}%`));
    assert.match(tooltip.textContent, /Additional scoring details were not saved/);
    assert.doesNotMatch(tooltip.textContent, /Flagged|Cutoff|Category scores|Source rank/);
    assert.match(tooltip.textContent, /Click pulse to replay/);
  }
  await act(async () => document.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key:'Escape',bubbles:true})));
  assert.equal(document.querySelector('[role=dialog]'), null);
  assert.equal(buttons[3].hasAttribute('aria-controls'), false);
  assert.equal(document.activeElement, buttons[3]);
  await act(async () => buttons[0].focus());
  await act(async () => root.render(React.createElement(PulseStrip, {...props, pulses:[]})));
  assert.equal(document.querySelector('[role=dialog]'), null);
});

test('hover preview can be read under the pointer and dismisses on leaving or meeting scroll', async () => {
  const pulse = {id:'hover',kind:'number',start_s:53,text:'We need a thousand dollars.'};
  let picked = null;
  const container = await mount(PulseStrip, {pulses:[pulse], onSelect:p => {picked=p;}});
  const button = container.querySelector('.pulse-mark');
  const pointer = (target, type, relatedTarget=null) => target.dispatchEvent(new dom.window.MouseEvent(type, {bubbles:true, relatedTarget}));
  const settle = () => new Promise(resolve => setTimeout(resolve, 200));
  await act(async () => pointer(button, 'pointerover'));
  let tooltip = document.querySelector('[role=dialog]');
  assert.ok(tooltip);
  await act(async () => {
    pointer(button, 'pointerout', tooltip);
    pointer(tooltip, 'pointerover', button);
    await settle();
  });
  assert.equal(document.querySelector('[role=dialog]'), tooltip);
  await act(async () => tooltip.querySelector('.pulse-preview-body').dispatchEvent(new dom.window.Event('scroll')));
  assert.equal(document.querySelector('[role=dialog]'), tooltip);
  await act(async () => {pointer(tooltip, 'pointerout', document.body); await settle();});
  assert.equal(document.querySelector('[role=dialog]'), null);
  await act(async () => pointer(button, 'pointerover'));
  await act(async () => container.dispatchEvent(new dom.window.Event('scroll')));
  assert.equal(document.querySelector('[role=dialog]'), null);
  await act(async () => pointer(button, 'pointerover'));
  await act(async () => button.click());
  assert.equal(document.querySelector('[role=dialog]'), null);
  assert.equal(picked, pulse);
});

test('pulse evidence shows detection probability without source selection', async () => {
  const pulse = {id:'scored',kind:'number',start_s:63,segment_id:'budget',probability:.81,text:'Budget is a thousand dollars.',
    assessment:{threshold:.8,window_start_s:60,window_end_s:120,
      scores:{number:.81,decision:.95,commitment:.81,disagreement:0},
      source_probability:.65,source_confidence:.4,source_rank:1,source_option_count:3}};
  const props = {pulses:[pulse], onSelect:() => {}};
  const container = await mount(PulseStrip, props);
  await act(async () => container.querySelector('button').focus());
  const tooltip = document.querySelector('[role=dialog]');
  assert.match(tooltip.querySelector('.pulse-preview-score').textContent, /Detection probability81%Cutoff 80%\+1 pt above/);
  assert.doesNotMatch(tooltip.textContent, /Source selection|Selected passage|Source rank|Selection confidence|Category scores|This pulse|Independent probabilities/);
  assert.equal(tooltip.querySelector('details'), null);
  const updated = {...pulse,probability:.9,assessment:{...pulse.assessment,threshold:.9,scores:{number:.9},source_confidence:0}};
  await act(async () => root.render(React.createElement(PulseStrip, {...props,pulses:[updated]})));
  assert.match(tooltip.querySelector('.pulse-preview-score').textContent, /90%Cutoff 90%At cutoff/);
});

test('unavailable pulse scores never become zero or fabricated rankings', async () => {
  const pulse = {id:'missing',kind:'number',start_s:1,text:'A saved passage',probability:null};
  const container = await mount(PulseStrip, {pulses:[pulse],onSelect:() => {}});
  await act(async () => container.querySelector('button').focus());
  const tooltip = document.querySelector('[role=dialog]');
  assert.match(tooltip.textContent, /Detection probabilityUnavailable/);
  assert.doesNotMatch(tooltip.textContent, /0%|NaN|Cutoff|Source rank/);
});


test('pulse preview keyboard access reaches Close without showing source options', async () => {
  const pulse = {id:'options', kind:'number', start_s:63, segment_id:'budget', probability:.94, text:'Budget excerpt',
    assessment:{threshold:.8, window_start_s:60, window_end_s:120, scores:{number:.94},
      source_probability:.4998, source_rank:1, source_option_count:4, source_confidence:.2,
      source_options:[
        {segment_id:'tiny', probability:.0004, start_s:65, text:'A very unlikely passage'},
        {segment_id:null, probability:0},
        {segment_id:'other', probability:.4998, start_s:80, text:'Another amount'},
        {segment_id:'budget', probability:.4998, start_s:63, text:'The full budget passage saved during evaluation.'},
      ]}};
  const picked = [];
  const container = await mount(PulseStrip, {pulses:[pulse], onSelect:p => picked.push(p)});
  const mark = container.querySelector('.pulse-mark');
  await act(async () => mark.focus());
  const preview = document.querySelector('[role=dialog]');
  assert.equal(preview.querySelector('details'), null);
  assert.doesNotMatch(preview.textContent, /Source selection|View all|No matching passage|Other source options/);
  assert.equal(mark.getAttribute('aria-haspopup'), 'dialog');
  await act(async () => mark.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key:'Tab', bubbles:true, cancelable:true})));
  assert.equal(document.activeElement, preview.querySelector('.pulse-preview-close'));
  await act(async () => {
    preview.dispatchEvent(new dom.window.MouseEvent('pointerout', {bubbles:true,relatedTarget:document.body}));
    await new Promise(resolve => setTimeout(resolve, 200));
  });
  assert.equal(document.querySelector('[role=dialog]'), preview, 'keyboard focus keeps the preview open');
  await act(async () => document.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key:'Escape',bubbles:true})));
  assert.equal(document.querySelector('[role=dialog]'), null);
  assert.equal(document.activeElement, mark);
  await act(async () => mark.click());
  assert.deepEqual(picked, [pulse], 'original pulse playback is unchanged');
});

test('saved source fields stay off the preview when a recording is unavailable', async () => {
  const pulse = {id:'partial', kind:'number', start_s:10, segment_id:'budget', probability:.9, text:'Budget',
    assessment:{source_probability:.9, source_option_count:3, source_rank:1,
      source_options:[{segment_id:'budget', start_s:10, text:'Budget', probability:.9}]}};
  const container = await mount(PulseStrip, {pulses:[pulse], playbackAvailable:false, onSelect:() => assert.fail('playback')});
  await act(async () => container.querySelector('.pulse-mark').click());
  const preview = document.querySelector('[role=dialog]');
  assert.equal(preview.querySelector('details'), null);
  assert.doesNotMatch(preview.textContent, /Source selection|Other source options|Source rank/);
  assert.match(preview.textContent, /No recording available for this moment/);
});
