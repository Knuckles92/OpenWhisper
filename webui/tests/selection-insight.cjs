const assert = require('node:assert/strict');
const { test } = require('node:test');
const ts = require('../node_modules/typescript');
const fs = require('node:fs');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
for (const extension of ['.tsx', '.ts']) {
  require.extensions[extension] = (module, filename) => {
    const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
    });
    module._compile(outputText, filename);
  };
}
require.extensions['.css'] = () => {};
const { insightOp, correctionText, correctedSegments, termRules } = require('../src/corrections.ts');
const SelectionInsight = require('../src/components/SelectionInsight.tsx').default;

const note = (selected, replacement, overrides = {}) => ({
  id: 'it_1', card: 'user_notes', text: 'x', status: 'edited', author_type: 'user', author_id: null,
  pinned: false, revision: 1, evidence: [], created_at: '', updated_at: '',
  data: { kind: 'term_correction', selected_text: selected, replacement }, ...overrides,
});

test('insightOp builds a term correction when a replacement is given, otherwise an insight', () => {
  const correction = insightOp(' Entropic ', 'The AI company.', ' Anthropic ');
  assert.equal(correction.op, 'add_item');
  assert.equal(correction.card, 'user_notes');
  assert.deepEqual(correction.data, { kind: 'term_correction', selected_text: 'Entropic', replacement: 'Anthropic' });
  assert.equal(correction.text, 'Correction: “Entropic” → “Anthropic”. The AI company.');
  assert.equal(insightOp('Entropic', '', 'Anthropic').text, 'Correction: “Entropic” → “Anthropic”.');
  const insight = insightOp('the Q3 plan', 'This refers to the hiring plan.', '');
  assert.equal(insight.data.kind, 'agent_insight');
  assert.equal(insight.text, 'Regarding “the Q3 plan”: This refers to the hiring plan.');
});

test('corrections match whole words case-insensitively, never chain, and escape regex syntax', () => {
  const correct = correctionText([note('entropic', 'Anthropic'), note('Anthropic', 'Acme'), note('c++', 'C++')]);
  assert.equal(correct('Entropic said ENTROPIC is not entropically fine'), 'Anthropic said Anthropic is not entropically fine');
  assert.equal(correct('Anthropic'), 'Acme');
  assert.equal(correct('we use c++ and cpp'), 'we use C++ and cpp');
  const phrase = correctionText([note('open whisper', 'OpenWhisper'), note('whisper', 'Whisper')]);
  assert.equal(phrase('open whisper uses whisper'), 'OpenWhisper uses Whisper');
});

test('only live human term corrections within bounds become rules', () => {
  const long = 'x'.repeat(121);
  const rules = termRules([
    note('a', 'A'),
    note('b', 'B', { status: 'removed' }),
    note('c', 'C', { author_type: 'agent' }),
    note('d', 'D', { data: { kind: 'agent_insight', selected_text: 'd', replacement: 'D' } }),
    note(long, 'E'),
    note('f', ''),
  ]);
  assert.deepEqual([...rules.entries()], [['a', 'A']]);
});

test('segments are corrected from the server-provided raw text and untouched without rules', () => {
  const segments = [{ id: 'sg_1', text: 'Anthropic', original_text: 'Entropic' }, { id: 'sg_2', text: 'plain' }];
  assert.equal(correctedSegments(segments, correctionText([])), segments);
  const corrected = correctedSegments(segments, correctionText([note('entropic', 'Acme')]));
  assert.equal(corrected[0].text, 'Acme');
  assert.equal(corrected[0].original_text, 'Entropic');
  assert.equal(corrected[1].text, 'plain');
});

test('the insight dialog renders with no trigger until text is selected', () => {
  const html = renderToStaticMarkup(React.createElement(SelectionInsight, {
    onSend: async () => true, live: true, online: true,
  }));
  assert.match(html, /Offer insight to agent/);
  assert.match(html, /Term corrections apply to whole words/);
  assert.doesNotMatch(html, /selection-insight-trigger/);
});
