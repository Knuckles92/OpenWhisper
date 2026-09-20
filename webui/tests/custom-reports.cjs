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
const CustomReports = require('../src/components/report/CustomReports.tsx').default;
const MarkdownView = require('../src/components/report/MarkdownView.tsx').default;
const { parseMarkdown, parseInline } = require('../src/markdown.ts');
const { applyEffect } = require('../src/state.ts');

const report = (overrides = {}) => ({
  id: 'rep_1',
  request: 'every commitment we made',
  status: 'ready',
  title: 'Commitments',
  markdown: '# Commitments\n\n- Hold at 12% [0:09]',
  message: '',
  run_id: 'run_1',
  sources: { transcript_lines: 42, model: 'gpt-4o-mini' },
  requested_by: null,
  created_at: '2026-09-20T10:00:00Z',
  updated_at: '2026-09-20T10:01:00Z',
  ...overrides,
});

const state = (reports) => ({
  meeting_id: 'm_1', seq: 1, status: 'ended', cloud_enabled: true,
  intelligence_online: false, diarization_available: false, title: 'Vendor sync',
  topic: { current: '', history: [] }, rolling_summary: '', rolling_summary_evidence: [],
  capture: { mic_available: true, loopback_available: true, message: '' },
  participants: {}, cards: {}, questions: [], custom_reports: reports,
});

const render = (reports, overrides = {}) => renderToStaticMarkup(
  React.createElement(CustomReports, {
    state: state(reports), token: 'tok', meetingId: 'm_1', ...overrides,
  }),
);

// Parser

test('parses the block constructs a report actually uses', () => {
  const blocks = parseMarkdown([
    '# Title',
    '',
    'A paragraph that',
    'wraps across lines.',
    '',
    '- first',
    '- second',
    '',
    '1. step one',
    '2. step two',
    '',
    '> a quoted ask',
    '',
    '| Owner | Due |',
    '| --- | --- |',
    '| Priya | Friday |',
    '',
    '```js',
    '# not a heading',
    '```',
    '',
    '---',
  ].join('\n'));
  assert.deepEqual(blocks.map((b) => b.kind), [
    'heading', 'paragraph', 'list', 'list', 'quote', 'table', 'code', 'rule',
  ]);
  assert.equal(blocks[1].text, 'A paragraph that wraps across lines.');
  assert.equal(blocks[3].ordered, true);
  assert.deepEqual(blocks[5].header, ['Owner', 'Due']);
  assert.deepEqual(blocks[5].rows, [['Priya', 'Friday']]);
  assert.equal(blocks[6].text, '# not a heading');
});

test('ragged table rows are padded to the header width', () => {
  const [table] = parseMarkdown('| A | B | C |\n| - | - | - |\n| 1 | 2 |\n| 1 | 2 | 3 | 4 |');
  assert.deepEqual(table.rows, [['1', '2', ''], ['1', '2', '3']]);
});

test('inline emphasis, code, and safe links are recognized', () => {
  assert.deepEqual(parseInline('a **b** _c_ `d`').map((n) => n.kind),
    ['text', 'strong', 'text', 'em', 'text', 'code']);
  const [link] = parseInline('[docs](https://example.com)');
  assert.deepEqual(link, { kind: 'link', text: 'docs', href: 'https://example.com' });
});

test('unsafe link schemes degrade to plain text', () => {
  for (const href of ['javascript:alert(1)', 'data:text/html,x', 'file:///etc/passwd']) {
    const nodes = parseInline(`[click](${href})`);
    assert.ok(!nodes.some((node) => node.kind === 'link'), href);
    assert.ok(!nodes.some((node) => 'href' in node), href);
  }
});

// Renderer

test('report markdown is rendered as elements, never as HTML', () => {
  const html = renderToStaticMarkup(React.createElement(MarkdownView, {
    source: '## Risks\n\n<img src=x onerror="alert(1)">\n\n<script>alert(1)</script>',
  }));
  assert.match(html, /<h4>Risks<\/h4>/);
  assert.doesNotMatch(html, /<script/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;script&gt;/);
});

// Panel

test('the composer offers example asks and stays enabled with no reports', () => {
  const html = render([]);
  assert.match(html, /Describe the report you want/);
  assert.match(html, /maxlength="2000"/i);
  assert.doesNotMatch(html.match(/<textarea[^>]*>/)[0], /disabled/);
  assert.match(html, /A one-page brief for someone who missed the call/);
  assert.match(html, /No reports yet for this meeting/);
});

test('a running report disables the composer and explains why', () => {
  const html = render([report({ status: 'running', markdown: '', message: 'Reading…' })]);
  assert.match(html.match(/<textarea[^>]*>/)[0], /disabled/);
  assert.match(html, /One report is written at a time/);
  assert.match(html, /Reading…/);
  // A stranded run must stay discardable, which is the only way to unblock.
  assert.match(html, /Stop<\/button>/);
});

test('a finished report shows its body, its ask, and where it came from', () => {
  const html = render([report()]);
  assert.match(html, /Commitments/);
  assert.match(html, /every commitment we made/);
  assert.match(html, /Hold at 12% \[0:09\]/);
  assert.match(html, /Written from 42 transcript lines · gpt-4o-mini/);
  assert.match(html, /report-1\/download\?token=tok|reports\/rep_1\/download/);
});

test('a failed report surfaces its message and keeps the composer usable', () => {
  const html = render([report({ status: 'failed', markdown: '', message: 'No API key.' })]);
  assert.match(html, /No API key\./);
  assert.doesNotMatch(html.match(/<textarea[^>]*>/)[0], /disabled/);
});

test('the print copy drops the composer, actions, and unfinished reports', () => {
  const html = render(
    [report(), report({ id: 'rep_2', status: 'failed', markdown: '', message: 'nope' })],
    { readOnly: true },
  );
  assert.match(html, /Requested Reports/);
  assert.match(html, /Hold at 12%/);
  assert.doesNotMatch(html, /Describe the report you want/);
  assert.doesNotMatch(html, /Download<\/a>/);
  assert.doesNotMatch(html, /nope/);
});

test('the print copy renders nothing when no report is finished', () => {
  assert.equal(render([report({ status: 'running', markdown: '' })], { readOnly: true }), '');
  assert.equal(render([], { readOnly: true }), '');
});

// Reducer

test('custom_report effects insert, replace, and remove by id', () => {
  const base = state([]);
  const running = applyEffect(base, {
    entity: 'custom_report', report: report({ status: 'running' }), removed: false,
  });
  assert.equal(running.custom_reports.length, 1);
  const ready = applyEffect(running, {
    entity: 'custom_report', report: report(), removed: false,
  });
  assert.equal(ready.custom_reports.length, 1);
  assert.equal(ready.custom_reports[0].status, 'ready');
  const gone = applyEffect(ready, {
    entity: 'custom_report', report: report(), removed: true,
  });
  assert.deepEqual(gone.custom_reports, []);
  // The reducer must not mutate the document it was handed.
  assert.equal(base.custom_reports.length, 0);
});

test('a report body drops its own title line when the card already shows it', () => {
  const { stripLeadingTitle } = require('../src/markdown.ts');
  assert.equal(stripLeadingTitle('# Commitments\n\n- Hold at 12%', 'Commitments'), '- Hold at 12%');
  // A different title, or no leading heading, is left completely alone.
  assert.equal(stripLeadingTitle('# Other\n\nbody', 'Commitments'), '# Other\n\nbody');
  assert.equal(stripLeadingTitle('body only', 'Commitments'), 'body only');
  assert.equal(stripLeadingTitle('## Commitments\n\nbody', 'Commitments'), '## Commitments\n\nbody');
});

test('the rendered card shows its title exactly once', () => {
  const html = render([report()]);
  assert.equal(html.split('Commitments').length - 1, 1);
});
