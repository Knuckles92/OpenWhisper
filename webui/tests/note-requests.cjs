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

const noteBlock = (id, startS, heading) => ({
  id, card: 'live_notes', text: `${heading} body`, data: {heading, start_s: startS},
  status: 'proposed', author_type: 'system', author_id: 'note_agent', revision: 1,
  evidence: [], created_at: '2026-09-19T19:00:00Z', updated_at: '2026-09-19T19:00:00Z',
});
test('the live pane leads with the newest block, the archived document stays chronological', () => {
  const notes = [noteBlock('first', 12, 'Opening block'), noteBlock('latest', 600, 'Closing block')];
  const live = render({notes, newestFirst: true});
  assert.ok(live.indexOf('Closing block') < live.indexOf('Opening block'),
    'live notes should read newest first, like the Captured rail');
  const archived = render({notes, readOnly: true});
  assert.ok(archived.indexOf('Opening block') < archived.indexOf('Closing block'),
    'the archived document should still read oldest first');
});
