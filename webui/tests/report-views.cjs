const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {url: 'http://localhost/'});
global.localStorage = dom.window.localStorage;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020},
}).outputText, filename);
require.extensions['.css'] = () => {};
const ReportTabs = require('../src/components/report/ReportTabs.tsx').default;
const {CARD_KEYS} = require('../src/types.ts');
const {enabledReportViews, readStoredReportView, writeStoredReportView} = require('../src/report.ts');
const {pickPullQuote} = require('../src/components/report/EditorialReport.tsx');
const {
  actionGroups, recapEmail, mailtoHref, ownerEmailText, MAILTO_BODY_LIMIT, UNASSIGNED,
} = require('../src/components/report/followThrough.ts');

function item(card, text, extra = {}) {
  return {id: `${card}-${text}`, card, text, status: 'proposed', data: {}, evidence: [],
    revision: 2, pinned: false, author_type: 'agent', author_id: null,
    created_at: '2026-09-19T18:00:00Z', updated_at: '2026-09-19T18:00:00Z', ...extra};
}

function fixture() {
  const cards = Object.fromEntries(CARD_KEYS.map(key => [key, []]));
  cards.decisions = [item('decisions', 'Ship in June', {status: 'confirmed', evidence: ['s1']})];
  cards.action_items = [
    item('action_items', 'Draft the RFC', {
      data: {owner_participant_id: 'sam', deadline: 'Friday'},
      review: {state: 'provisional'},
      citation_check: {status: 'contradicted', revision: 2},
      evidence: ['s1'],
    }),
    item('action_items', 'Book the room'),
    item('action_items', 'Review the budget', {data: {owner_participant_id: 'ana'}}),
    item('action_items', 'Old idea', {status: 'removed', data: {owner_participant_id: 'sam'}}),
  ];
  return {
    meeting_id: 'm1', seq: 1, title: 'Planning', status: 'ended',
    cloud_enabled: true, intelligence_online: true,
    participants: {
      sam: {id: 'sam', display_name: 'Sam Lee', kind: 'guest', created_at: '2026-09-19T18:00:00Z'},
      ana: {id: 'ana', display_name: 'Ana Ruiz', kind: 'me', created_at: '2026-09-19T17:59:00Z'},
    },
    cards,
    questions: [
      {id: 'q1', text: 'Does the vendor support SSO?', status: 'resolved', answer: 'Yes, via SAML.', evidence: ['s1']},
      {id: 'q2', text: 'Who signs off on the budget?', status: 'open', answer: null, evidence: []},
    ],
    topic: {current: 'Budget', history: [
      {text: 'Scope', ts: '2026-09-19T18:00:10Z', evidence: [], actor_type: 'agent'},
      {text: 'Budget', ts: '2026-09-19T18:01:00Z', evidence: [], actor_type: 'agent'},
    ]},
    rolling_summary: 'We agreed to ship in June. The RFC comes first. Budget review follows next week.',
    report_views: ['ribbon', 'brief', 'signal'],
    live_highlights: [
      {id: 'n', kind: 'number', text: '5000 budget', start_s: 90, probability: 0.99, segment_id: 's1'},
      {id: 'd', kind: 'decision', text: 'We ship in June', start_s: 5, probability: 0.8, segment_id: 's1'},
    ],
  };
}
const segments = [{id: 's1', meeting_id: 'm1', channel: 'loopback', start_s: 5, end_s: 120,
  speaker_participant_id: 'sam', text: 'We ship in June'}];
const meeting = {id: 'm1', title: 'Planning', started_at: '2026-09-19T18:00:00Z', status: 'ended', duration_s: 120};
const render = (activeView, state = fixture()) => new JSDOM(renderToStaticMarkup(React.createElement(ReportTabs, {
  state, segments, meeting, activeView, transcriptComplete: true,
}))).window.document;

test('derived views follow the configured ones and survive odd server lists', () => {
  assert.deepEqual(enabledReportViews(fixture()), ['ribbon', 'brief', 'signal', 'editorial', 'handoff']);
  assert.deepEqual(enabledReportViews({...fixture(), report_views: ['signal', 'bogus', 'signal']}),
    ['signal', 'editorial', 'handoff']);
  assert.deepEqual(enabledReportViews({...fixture(), report_views: []}), ['ribbon', 'editorial', 'handoff']);
  assert.deepEqual(enabledReportViews({...fixture(), report_views: ['editorial']}), ['ribbon', 'editorial', 'handoff']);
});

test('a stored derived view is remembered', () => {
  writeStoredReportView('handoff');
  assert.equal(readStoredReportView(), 'handoff');
  localStorage.setItem('ow_report_view', 'nonsense');
  assert.equal(readStoredReportView(), null);
});

test('both derived views keep deadlines and advisory labels like the other summaries', () => {
  for (const view of ['editorial', 'handoff']) {
    const text = render(view).querySelector('.report-sheet').textContent;
    assert.ok(text.includes('Due: Friday'), view);
    assert.ok(text.includes('Citation conflicts with claim'), view);
    assert.ok(text.includes('Provisional'), view);
    assert.ok(!text.includes('Old idea'), `${view} hides removed items`);
  }
});

test('editorial sets the lead, decisions, action table, answers, quote and chapters', () => {
  const sheet = render('editorial').querySelector('.report-sheet');
  const text = sheet.textContent;
  assert.equal(sheet.querySelector('.ed-title').textContent, 'Planning');
  assert.match(text, /2 min · 2 people/);
  assert.equal(sheet.querySelector('.ed-lede').textContent.replace(/\s+/g, ' '), 'We agreed to ship in June. The RFC comes first.');
  assert.match(sheet.querySelector('.ed-decided').textContent, /Ship in June/);
  const rows = [...sheet.querySelectorAll('.ed-table tbody tr')].map(row => row.querySelector('.ed-owner').textContent);
  assert.deepEqual(rows.sort(), ['Ana Ruiz', 'Sam Lee', 'Unassigned']);
  assert.match(text, /Does the vendor support SSO\?Yes, via SAML\./);
  assert.match(sheet.querySelector('.ed-open').textContent, /Who signs off/);
  assert.equal(sheet.querySelector('.ed-quote blockquote').textContent, 'We ship in June');
  const chapters = [...sheet.querySelectorAll('.ed-chapters li')].map(li => li.textContent);
  assert.deepEqual(chapters, ['Scope1 min', 'Budget1 min']);
});

test('the pull quote prefers a decision, then the surest highlight', () => {
  assert.equal(pickPullQuote([]), null);
  assert.equal(pickPullQuote(fixture().live_highlights).id, 'd');
  assert.equal(pickPullQuote([
    {id: 'a', kind: 'number', text: 'x', start_s: 1, probability: 0.7},
    {id: 'b', kind: 'takeaway', text: 'y', start_s: 2, probability: 0.9},
  ]).id, 'b');
});

test('handoff groups work by owner with unassigned last', () => {
  const groups = actionGroups(fixture());
  assert.deepEqual(groups.map(group => [group.name, group.items.length]),
    [['Sam Lee', 1], ['Ana Ruiz', 1], [UNASSIGNED, 1]]);
  const sheet = render('handoff').querySelector('.report-sheet');
  assert.match(sheet.querySelector('.ho-summary').textContent, /1 decision.*3 actions.*1 open question/);
  assert.deepEqual([...sheet.querySelectorAll('.ho-owner')].map(el => el.textContent),
    ['Sam Lee', 'Ana Ruiz', UNASSIGNED, 'Still open']);
  const segs = new Map(segments.map(segment => [segment.id, segment]));
  assert.equal(ownerEmailText(groups[0], 'Planning', segs),
    'Sam Lee — your follow-ups from "Planning":\n- Draft the RFC (due Friday, at 0:05)');
});

test('the recap email is built from captured content and fits a mailto link', () => {
  const email = recapEmail(fixture(), 'Planning');
  assert.equal(email.subject, 'Recap: Planning');
  for (const part of ['Hi all,', 'We agreed to ship in June.', 'Decisions\n- Ship in June',
    'Actions\nSam Lee:\n- Draft the RFC (due Friday)', 'Unassigned:\n- Book the room',
    'Still open\n- Who signs off on the budget?']) {
    assert.ok(email.body.includes(part), part);
  }
  assert.ok(!email.body.includes('Old idea'));
  const href = mailtoHref(email);
  assert.ok(href.startsWith('mailto:?subject=Recap%3A%20Planning&body='));
  assert.equal(decodeURIComponent(href.split('&body=')[1]), email.body);
  const long = {subject: 'Recap: x', body: 'a'.repeat(MAILTO_BODY_LIMIT * 3)};
  const body = decodeURIComponent(mailtoHref(long).split('&body=')[1]);
  assert.ok(body.length < MAILTO_BODY_LIMIT + 120);
  assert.match(body, /Copy email/);
});
