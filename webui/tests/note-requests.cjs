const assert = require('node:assert/strict');
const { test } = require('node:test');
const ts = require('../node_modules/typescript');
const fs = require('node:fs');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
for (const extension of ['.tsx', '.ts']) {
  require.extensions[extension] = (module, filename) => {
    const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    });
    module._compile(outputText, filename);
  };
}
const NotesPane = require('../src/components/NotesPane.tsx').default;
const render = overrides => renderToStaticMarkup(React.createElement(NotesPane, {
  notes: [], status: 'active', cloudEnabled: true, intelligenceOnline: true,
  onRequestAdjustment: async () => ({ok: true, applied: 1, rejected: 0}),
  onEvidenceClick() {}, lastSeqByTarget: {}, ...overrides,
}));
test('request input is available in active and paused meetings', () => {
  for (const status of ['active', 'paused']) {
    const html = render({status});
    assert.match(html, /Ask the note agent/);
    assert.match(html, /maxlength="4000"/i);
    assert.doesNotMatch(html.match(/<textarea[^>]*>/)[0], /disabled/);
  }
});
test('request input disables when cloud is off, offline, or meeting is over', () => {
  for (const overrides of [{cloudEnabled: false}, {intelligenceOnline: false}, {status: 'ending'}, {status: 'ended'}]) {
    assert.match(render(overrides).match(/<textarea[^>]*>/)[0], /disabled/);
  }
});
test('archive and print views omit the note request composer', () => {
  assert.doesNotMatch(render({readOnly: true}), /Ask the note agent/);
});
