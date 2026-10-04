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
const { insightOp, correctionText, correctedSegments, termRules, segmentFingerprint, stableOccurrenceIndex } = require('../src/corrections.ts');
const { default: SelectionInsight, transcriptTarget } = require('../src/components/SelectionInsight.tsx');

const note = (selected, replacement, overrides = {}) => ({
  id: 'it_1', card: 'user_notes', text: 'x', status: 'edited', author_type: 'user', author_id: null,
  pinned: false, revision: 1, evidence: [], created_at: '', updated_at: '',
  data: { kind: 'term_correction', selected_text: selected, replacement }, ...overrides,
});

test('base fingerprint matches the Python UTF-8 implementation', () => {
  assert.equal(segmentFingerprint('alpha βeta 😀'), 'fnv1a64:8ea367c29aed5110');
});

test('insightOp builds a term correction when a replacement is given, otherwise an insight', () => {
  const correction = insightOp(' Entropic ', 'The AI company.', ' Anthropic ', 'meeting');
  assert.equal(correction.op, 'add_item');
  assert.equal(correction.card, 'user_notes');
  assert.deepEqual(correction.data, { kind: 'term_correction', selected_text: 'Entropic', replacement: 'Anthropic' });
  assert.equal(correction.text, 'Correction throughout this meeting: “Entropic” → “Anthropic”. The AI company.');
  assert.equal(insightOp('Entropic', '', 'Anthropic', 'meeting').text, 'Correction throughout this meeting: “Entropic” → “Anthropic”.');
  const insight = insightOp('the Q3 plan', 'This refers to the hiring plan.', '', 'meeting');
  assert.equal(insight.data.kind, 'agent_insight');
  assert.equal(insight.text, 'Regarding “the Q3 plan”: This refers to the hiring plan.');
});

test('one-occurrence note has a segment anchor; meeting-wide correction is explicit', () => {
  const scoped = insightOp(' Entropic ', 'Company name', ' Anthropic ',
    { segmentId: 'sg_1', occurrenceIndex: 2, baseFingerprint: segmentFingerprint('Entropic Entropic Entropic') });
  assert.deepEqual(scoped.data, { kind: 'occurrence_correction', selected_text: 'Entropic',
    replacement: 'Anthropic', occurrence_index: 2,
    base_fingerprint: segmentFingerprint('Entropic Entropic Entropic') });
  assert.deepEqual(scoped.evidence, ['sg_1']);
  assert.match(scoped.text, /^Correction in this passage:/);
  assert.deepEqual(insightOp('Entropic', '', 'Anthropic', 'meeting').evidence, []);
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

test('segments are corrected from server-provided raw text and undo restores hydrated rows', () => {
  const segments = [{ id: 'sg_1', text: 'Anthropic', original_text: 'Entropic' }, { id: 'sg_2', text: 'plain' }];
  assert.equal(correctedSegments(segments, correctionText([]))[0].text, 'Entropic');
  const alreadyRaw = [{ id: 'sg_1', text: 'Entropic', original_text: 'Entropic' }];
  assert.equal(correctedSegments(alreadyRaw, correctionText([])), alreadyRaw);
  const corrected = correctedSegments(segments, correctionText([note('entropic', 'Acme')]));
  assert.equal(corrected[0].text, 'Acme');
  assert.equal(corrected[0].original_text, 'Entropic');
  assert.equal(corrected[1].text, 'plain');
  const hydratedWhileCorrected = [{ id: 'sg_1', text: 'first word', original_text: 'word word' }];
  const afterUndo = correctedSegments(hydratedWhileCorrected, correctionText([]), []);
  assert.equal(afterUndo[0].text, 'word word');
  assert.equal(hydratedWhileCorrected[0].text, 'first word');
});

test('one-off corrections update live rows and preserve other occurrences through out-of-order edits and undo', () => {
  const segments = [
    { id: 'sg_1', text: 'word word word', original_text: 'word word word' },
    { id: 'sg_2', text: 'word', original_text: 'word' },
  ];
  const last = note('word', 'third', { id: 'last', evidence: ['sg_1'],
    data: { kind: 'occurrence_correction', selected_text: 'word', replacement: 'third', occurrence_index: 2,
      base_fingerprint: segmentFingerprint('word word word') } });
  const first = note('word', 'first', { id: 'first', evidence: ['sg_1'],
    data: { kind: 'occurrence_correction', selected_text: 'word', replacement: 'first', occurrence_index: 0,
      base_fingerprint: segmentFingerprint('word word word') } });
  const correct = correctionText([last, first]);
  assert.equal(correctedSegments(segments, correct, [last])[0].text, 'word word third');
  assert.deepEqual(correctedSegments(segments, correct, [last, first]).map((row) => row.text),
    ['first word third', 'word']);
  assert.deepEqual(correctedSegments(segments, correct, [{ ...last, status: 'removed' }, first]).map((row) => row.text),
    ['first word word', 'word']);
  assert.deepEqual(correctedSegments(segments, correct, [last, { ...first, status: 'removed' }]).map((row) => row.text),
    ['word word third', 'word']);
  assert.equal(stableOccurrenceIndex('word word word', 'word word third', [last], 'sg_1', 'word', 5), 1);
  assert.equal(stableOccurrenceIndex('word word word', 'first word word', [first], 'sg_1', 'word', 11), 2);
  assert.equal(stableOccurrenceIndex('word word word', 'word word third', [last], 'sg_1', 'third', 10), null);
  assert.equal(stableOccurrenceIndex('word word word', 'mismatched', [last], 'sg_1', 'word', 0), null);
  assert.equal(correctedSegments(segments, correctionText([note('word', 'all')]), [note('word', 'all')])[0].text,
    'all all all');
});

test('later global rules or segment revisions cannot retarget a one-off correction', () => {
  const raw = 'alpha beta alpha';
  const scoped = note('alpha', 'fixed', { evidence: ['sg_1'], data: {
    kind: 'occurrence_correction', selected_text: 'alpha', replacement: 'fixed',
    occurrence_index: 1, base_fingerprint: segmentFingerprint(raw),
  } });
  const segment = { id: 'sg_1', text: raw, original_text: raw };
  const render = (seg, notes) => correctedSegments([seg], correctionText(notes), notes)[0].text;
  assert.equal(render(segment, [scoped]), 'alpha beta fixed');
  const global = note('beta', 'alpha', { id: 'global' });
  assert.equal(render(segment, [scoped, global]), 'alpha alpha alpha');
  assert.equal(render(segment, [scoped, { ...global, status: 'removed' }]), 'alpha beta fixed');
  assert.equal(render({ ...segment, original_text: 'alpha new beta alpha' }, [scoped]), 'alpha new beta alpha');
  assert.equal(render(segment, [scoped]), 'alpha beta fixed');
});

test('selection must be wholly inside one transcript passage and match a base occurrence', () => {
  const { JSDOM } = require('jsdom');
  const dom = new JSDOM('<article data-transcript-id="sg_1"><p class="segment-text">word word</p></article><article data-transcript-id="sg_2"><p class="segment-text">word</p></article><p id="other">word</p>');
  const oldElement = global.Element;
  global.Element = dom.window.Element;
  try {
    const [first, second] = dom.window.document.querySelectorAll('.segment-text');
    const range = dom.window.document.createRange();
    range.setStart(first.firstChild, 5); range.setEnd(first.firstChild, 9);
    const selection = dom.window.getSelection();
    selection.removeAllRanges(); selection.addRange(range);
    const resolve = (id, source, offset, displayed) => {
      const occurrenceIndex = stableOccurrenceIndex('word word', displayed, [], id, source, offset);
      return occurrenceIndex === null ? null : { occurrenceIndex, baseFingerprint: segmentFingerprint('word word') };
    };
    assert.deepEqual(transcriptTarget(selection, 'word', resolve), { segmentId: 'sg_1', occurrenceIndex: 1,
      baseFingerprint: segmentFingerprint('word word') });
    range.setEnd(second.firstChild, 4);
    selection.removeAllRanges(); selection.addRange(range);
    assert.equal(transcriptTarget(selection, 'word word', resolve), null);
    const other = dom.window.document.querySelector('#other');
    range.selectNodeContents(other);
    selection.removeAllRanges(); selection.addRange(range);
    assert.equal(transcriptTarget(selection, 'word', resolve), null);
  } finally {
    global.Element = oldElement;
    dom.window.close();
  }
});

test('the insight dialog renders with no trigger until text is selected', () => {
  const html = renderToStaticMarkup(React.createElement(SelectionInsight, {
    onSend: async () => true, live: true, online: true,
  }));
  assert.match(html, /Offer insight to agent/);
  assert.match(html, /Correct this term/);
  assert.doesNotMatch(html, /selection-insight-trigger/);
});
