const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const key of ['window', 'document', 'navigator', 'Node', 'Event', 'KeyboardEvent']) global[key] = key === 'window' ? dom.window : dom.window[key];
global.localStorage = dom.window.localStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
}).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { DESIGN_THEMES, readTheme, saveTheme, readDesignTheme, applyDesignTheme, saveDesignTheme } = require('../src/theme.ts');
const ThemePicker = require('../src/components/ThemePicker.tsx').default;
let root;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  root = null;
  global.localStorage = dom.window.localStorage;
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-design');
  document.body.replaceChildren();
});

test('design selection preserves an existing color-mode preference and survives a reload', () => {
  localStorage.setItem('openwhisper.meeting.theme', 'dark');
  assert.equal(readTheme(), 'dark');
  for (const {id} of DESIGN_THEMES) {
    saveDesignTheme(id);
    assert.equal(readDesignTheme(), id);
    assert.equal(document.documentElement.dataset.design, id);
    assert.equal(readTheme(), 'dark');
  }
  saveTheme('light');
  assert.equal(readDesignTheme(), 'original');
  saveTheme('system');
  assert.equal(document.documentElement.hasAttribute('data-theme'), false);
  assert.equal(readDesignTheme(), 'original');
});

test('missing or unrecognized designs fall back to Studio without altering color mode', () => {
  assert.equal(readDesignTheme(), 'studio');
  localStorage.setItem('openwhisper.meeting.design', 'retired-design');
  assert.equal(readDesignTheme(), 'studio');
  applyDesignTheme(readDesignTheme());
  assert.equal(document.documentElement.dataset.design, 'studio');
  assert.equal(readTheme(), 'system');
});

test('blocked browser storage still applies the selected appearance for the current visit', () => {
  global.localStorage = { getItem() { throw new Error('Blocked'); }, setItem() { throw new Error('Blocked'); }, removeItem() { throw new Error('Blocked'); } };
  assert.equal(readDesignTheme(), 'studio');
  assert.equal(readTheme(), 'system');
  assert.doesNotThrow(() => saveDesignTheme('sunlit'));
  assert.doesNotThrow(() => saveTheme('dark'));
  assert.equal(document.documentElement.dataset.design, 'sunlit');
  assert.equal(document.documentElement.dataset.theme, 'dark');
});

test('picker applies all five designs and color modes, closes with Escape, and returns focus', async () => {
  applyDesignTheme(readDesignTheme());
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  await act(async () => root.render(React.createElement(ThemePicker)));
  const details = container.querySelector('details'); const summary = container.querySelector('summary');
  assert.equal(summary.getAttribute('aria-label'), 'Appearance');
  details.open = true;
  for (const {id} of DESIGN_THEMES) {
    const radio = container.querySelector(`input[value="${id}"]`);
    await act(async () => radio.click());
    assert.equal(radio.checked, true);
    assert.equal(document.documentElement.dataset.design, id);
    assert.equal(details.open, true, 'previewing a theme leaves the picker open');
  }
  for (const mode of ['dark', 'light', 'system']) {
    await act(async () => container.querySelector(`input[value="${mode}"]`).click());
    assert.equal(readTheme(), mode);
    assert.equal(readDesignTheme(), 'original');
  }
  await act(async () => details.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  assert.equal(details.open, false);
  assert.equal(document.activeElement, summary);
  details.open = true;
  await act(async () => document.body.dispatchEvent(new Event('pointerdown', { bubbles: true })));
  assert.equal(details.open, false);
});
