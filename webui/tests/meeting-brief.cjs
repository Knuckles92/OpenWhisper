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
const MeetingBrief = require('../src/components/MeetingBrief.tsx').default;

const BRIEF = 'Capture who objected to each bid, and why.';
const intent = text => ({text, updated_at:'2026-09-20T10:00:00Z', author_id:'p_host'});

let root;
async function mount(props) {
  const container = document.createElement('div'); document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(React.createElement(MeetingBrief, {
    isHost:true, status:'active', onSendOp:async () => true, ...props,
  })));
  return container;
}
const click = async (container, label) => {
  const button = [...container.querySelectorAll('button')].find(b => b.textContent === label);
  assert.ok(button, `no "${label}" button`);
  await act(async () => button.click());
};
// React tracks the last value it wrote, so a plain assignment is swallowed.
const type = async (container, value) => {
  const textarea = container.querySelector('textarea');
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, 'value')
      .set.call(textarea, value);
    textarea.dispatchEvent(new dom.window.Event('input', {bubbles:true}));
  });
  return textarea;
};
afterEach(async () => {if (root) {await act(async () => root.unmount()); root = null;} document.body.replaceChildren();});

test('a guest sees the brief the host wrote but cannot touch it', async () => {
  const container = await mount({isHost:false, intent:intent(BRIEF)});
  assert.match(container.textContent, /Capture who objected/);
  assert.equal(container.querySelectorAll('button').length, 0);
});

test('an unwritten brief is invisible to guests and an invitation to the host', async () => {
  assert.equal((await mount({isHost:false})).textContent, '');
  await act(async () => root.unmount());
  root = null;
  const host = await mount({});
  assert.match(host.textContent, /Add a brief/);
});

test('the host writes a brief and it is sent trimmed as a host-only op', async () => {
  const sent = [];
  const container = await mount({onSendOp:async op => {sent.push(op); return true;}});
  await click(container, 'Add a brief');
  assert.equal(container.querySelector('textarea').getAttribute('maxlength'), '2000');
  await type(container, `   ${BRIEF}   `);
  await click(container, 'Save brief');
  assert.deepEqual(sent, [{op:'set_meeting_intent', text:BRIEF}]);
});

test('a rejected save keeps the draft on screen instead of losing the words', async () => {
  const container = await mount({intent:intent(BRIEF), onSendOp:async () => false});
  await click(container, 'Edit');
  await type(container, `${BRIEF} And the delivery dates.`);
  await click(container, 'Save brief');
  assert.match(container.textContent, /was not saved/);
  assert.equal(container.querySelector('textarea').value,
    `${BRIEF} And the delivery dates.`);
});

test('re-saving an unchanged brief closes the editor without an op', async () => {
  const sent = [];
  const container = await mount({intent:intent(BRIEF), onSendOp:async op => {sent.push(op); return true;}});
  await click(container, 'Edit');
  await click(container, 'Save brief');
  assert.deepEqual(sent, []);
  assert.equal(container.querySelector('textarea'), null);
});

test('cancelling restores the saved brief and leaves the draft unsent', async () => {
  const sent = [];
  const container = await mount({intent:intent(BRIEF), onSendOp:async op => {sent.push(op); return true;}});
  await click(container, 'Edit');
  await type(container, 'scratch that');
  await click(container, 'Cancel');
  assert.deepEqual(sent, []);
  assert.match(container.textContent, /Capture who objected/);
});

test('an ended meeting shows the brief as a record, with nothing left to steer', async () => {
  const container = await mount({intent:intent(BRIEF), status:'ended'});
  assert.match(container.textContent, /Capture who objected/);
  assert.equal(container.querySelectorAll('button').length, 0);
});
