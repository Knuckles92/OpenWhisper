const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'KeyboardEvent', 'MouseEvent']) {
  global[key] = key === 'window' ? dom.window : dom.window[key];
}
global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
global.IS_REACT_ACT_ENVIRONMENT = true;
// jsdom has no dialog rendering; exercise component effects through this browser API seam.
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
const NotesPane = require('../src/components/NotesPane.tsx').default;
const SelectionInsight = require('../src/components/SelectionInsight.tsx').default;
let root, container;
async function mount(Component, props) {
  container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(Component, props)));
}
afterEach(async () => { if (root) await act(async () => root.unmount()); document.body.replaceChildren(); });
async function input(element, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value').set.call(element, value);
    element.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function click(element) { await act(async () => element.click()); }
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; };
const noteProps = request => ({
  notes: [], status: 'active', cloudEnabled: true, intelligenceOnline: true,
  onRequestAdjustment: request, onEvidenceClick() {}, lastSeqByTarget: {},
});

test('note request trims, suppresses duplicate submission, preserves failed draft and clears on retry', async () => {
  const pending = deferred(), calls = [];
  await mount(NotesPane, noteProps(text => { calls.push(text); return calls.length === 1 ? pending.promise : Promise.resolve({ok: true, applied: 2, rejected: 1}); }));
  const field = container.querySelector('textarea'), button = container.querySelector('button');
  await input(field, '  Keep Maia and the Friday deadline  ');
  await click(button);
  assert.equal(field.disabled, true);
  await act(async () => field.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', ctrlKey:true, bubbles:true})));
  assert.deepEqual(calls, ['Keep Maia and the Friday deadline']);
  await act(async () => pending.resolve({ok:false, applied:1, rejected:0, error:'provider offline'}));
  assert.match(container.textContent, /1 changes applied before the request stopped/);
  assert.match(container.textContent, /provider offline/);
  assert.match(field.value, /Maia/);
  await click(button);
  assert.equal(calls.length, 2);
  assert.equal(field.value, '');
  assert.match(container.textContent, /2 changes applied.*Some changes were rejected/);
});

test('note request no-op and exception preserve draft; rerender offline prevents keyboard submission', async () => {
  let calls = 0;
  const request = async () => { if (++calls === 1) return {ok:true, applied:0,rejected:0}; throw new Error('network lost'); };
  await mount(NotesPane, noteProps(request));
  const field = container.querySelector('textarea');
  await input(field, 'shorten notes');
  await click(container.querySelector('button'));
  assert.match(container.textContent, /No changes were applied/);
  await click(container.querySelector('button'));
  assert.match(container.textContent, /network lost/);
  assert.equal(field.value, 'shorten notes');
  await act(async () => root.render(React.createElement(NotesPane, {...noteProps(request), intelligenceOnline:false})));
  await act(async () => field.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',ctrlKey:true,bubbles:true})));
  assert.equal(calls, 2);
  assert.equal(field.disabled, true);
});

test('selection effects open dialog; failed save retains correction; retry saves exact op and restores focus', async () => {
  const calls = [], pending = deferred();
  await mount(SelectionInsight, {live:true, online:true, onSend: op => { calls.push(op); return calls.length === 1 ? pending.promise : Promise.resolve(true); }});
  const source = document.createElement('textarea');
  source.value = 'Entropic builds models'; document.body.prepend(source);
  source.focus(); source.setSelectionRange(0, 8);
  await act(async () => document.dispatchEvent(new Event('pointerup', {bubbles:true})));
  await click(container.querySelector('.selection-insight-trigger'));
  const dialog = container.querySelector('dialog');
  assert.equal(dialog.open, true);
  assert.equal(dialog.querySelector('blockquote').textContent, 'Entropic');
  await input(dialog.querySelector('input'), 'Anthropic');
  await input(dialog.querySelector('textarea'), 'The AI company.');
  const send = dialog.querySelector('[type=submit]');
  await click(send);
  assert.equal(send.disabled, true);
  await act(async () => pending.resolve(false));
  assert.equal(dialog.open, true);
  assert.equal(dialog.querySelector('input').value, 'Anthropic');
  assert.match(dialog.textContent, /please retry/);
  await click(send);
  assert.equal(dialog.open, false);
  assert.equal(document.activeElement, source);
  assert.deepEqual(calls[0].data, {kind:'term_correction',selected_text:'Entropic',replacement:'Anthropic'});
  assert.deepEqual(calls[1], calls[0]);
  assert.match(container.textContent, /Saved. Agent review requested/);
});
