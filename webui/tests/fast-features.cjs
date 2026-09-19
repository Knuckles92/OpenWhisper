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
const {playMoment} = require('../src/playback.ts');
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
