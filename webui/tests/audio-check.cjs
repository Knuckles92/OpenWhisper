const assert = require('node:assert/strict');
const {test, afterEach} = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/'});
global.window = dom.window;
global.document = dom.window.document;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions:{module:ts.ModuleKind.CommonJS, jsx:ts.JsxEmit.ReactJSX, target:ts.ScriptTarget.ES2020},
}).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react');
const {act} = React;
const {createRoot} = require('react-dom/client');
const AudioCheck = require('../src/components/AudioCheck.tsx').default;

let root, container, tick;
const realNow = Date.now;
const realInterval = global.setInterval;
const realClear = global.clearInterval;
async function mount(capture, extra = {}) {
  container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(AudioCheck, {capture, channel:'mic', ...extra})));
  return container;
}
async function render(capture, extra = {}) {
  await act(async () => root.render(React.createElement(AudioCheck, {capture, channel:'mic', ...extra})));
}
afterEach(async () => {
  if (root) { await act(async () => root.unmount()); root = null; }
  document.body.replaceChildren();
  Date.now = realNow;
  global.setInterval = realInterval;
  global.clearInterval = realClear;
  tick = null;
});

test('guided check passes on a new signal window and does not claim source identity', async () => {
  const capture = {mic_available:true, loopback_available:true, mic_receiving:true,
    mic_signal_windows:4, loopback_signal_windows:0, message:''};
  await mount(capture);
  assert.match(container.textContent, /Speak for five seconds/);
  await act(async () => container.querySelector('button').click());
  assert.match(container.textContent, /Speak into the microphone now/);
  await render({...capture, mic_signal_windows:5});
  assert.match(container.textContent, /Audio signal detected.*cannot identify the source/);
  assert.equal(container.querySelector('button').disabled, false);
});

test('quiet callbacks do not warn unless a five-second guided check fails', async () => {
  let now = 1000;
  Date.now = () => now;
  global.setInterval = fn => { tick = fn; return 1; };
  global.clearInterval = () => {};
  const capture = {mic_available:true, loopback_available:true, mic_receiving:true,
    mic_signal_windows:0, loopback_signal_windows:0, message:''};
  await mount(capture);
  assert.doesNotMatch(container.textContent, /No signal detected/);
  await act(async () => container.querySelector('button').click());
  now += 5001;
  await act(async () => tick());
  assert.match(container.textContent, /Speak into the microphone now/);
  now += 2000;
  await act(async () => tick());
  assert.match(container.textContent, /No signal detected.*selected microphone/);
  assert.equal(container.querySelector('button').disabled, false);
});

test('unavailable and paused sources cannot start a check', async () => {
  const capture = {mic_available:false, loopback_available:true, message:''};
  await mount(capture);
  assert.equal(container.querySelector('button').disabled, true);
  await render({...capture, mic_available:true}, {paused:true});
  assert.equal(container.querySelector('button').disabled, true);
  assert.match(container.textContent, /Resume capture to test/);
});

test('pausing an in-progress check cancels it without a failure warning', async () => {
  const capture = {mic_available:true, loopback_available:false,
    mic_signal_windows:0, message:''};
  await mount(capture);
  await act(async () => container.querySelector('button').click());
  await render(capture, {paused:true});
  assert.match(container.textContent, /Resume capture to test/);
  assert.doesNotMatch(container.textContent, /No signal detected/);
});

test('failed check distinguishes absent callbacks from quiet callbacks', async () => {
  let now = 1000;
  Date.now = () => now;
  global.setInterval = fn => { tick = fn; return 1; };
  global.clearInterval = () => {};
  const capture = {mic_available:true, loopback_available:false,
    mic_receiving:false, mic_signal_windows:0, message:''};
  await mount(capture);
  await act(async () => container.querySelector('button').click());
  now += 7001;
  await act(async () => tick());
  assert.match(container.textContent, /No audio blocks arrived.*permissions/);
});

test('a source restart clears a check and any previous success', async () => {
  const capture = {mic_available:true, loopback_available:false,
    mic_receiving:true, mic_signal_windows:4, mic_source_generation:1, message:''};
  await mount(capture);
  await act(async () => container.querySelector('button').click());
  await render({...capture, mic_source_generation:2, mic_signal_windows:5});
  assert.doesNotMatch(container.textContent, /Audio signal detected/);
  assert.match(container.textContent, /Speak for five seconds/);
  await act(async () => container.querySelector('button').click());
  await render({...capture, mic_source_generation:2, mic_signal_windows:6});
  assert.match(container.textContent, /Audio signal detected/);
  await render({...capture, mic_source_generation:3, mic_signal_windows:6});
  assert.doesNotMatch(container.textContent, /Audio signal detected/);
});
