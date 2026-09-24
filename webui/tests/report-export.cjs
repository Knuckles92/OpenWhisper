const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {JSDOM} = require('jsdom');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020},
}).outputText, filename);
require.extensions['.css'] = () => {};
const FullMeetingDocument =require('../src/components/report/FullMeetingDocument.tsx').default;
const ReportTabs = require('../src/components/report/ReportTabs.tsx').default;
const ReportDownload = require('../src/components/report/ReportDownload.tsx').default;
const {EvidenceProvider} = require('../src/evidence.tsx');
const {CARD_KEYS} = require('../src/types.ts');
const {liveItems} = require('../src/report.ts');

function item(card, text, extra = {}) {
  return {id:card, card, text, status:'proposed', data:{}, evidence:[],
    revision:2, pinned:false, author_type:'agent', author_id:null,
    created_at:'2026-09-19T18:00:00Z', updated_at:'2026-09-19T18:00:00Z', ...extra};
}
function fixture() {
  const cards = Object.fromEntries(CARD_KEYS.map(key => [key, []]));
  cards.action_items = [item('action_items', 'Draft the RFC', {
    data:{owner_participant_id:'sam', deadline:'Friday'},
    review:{state:'provisional'},
    citation_check:{status:'contradicted', revision:2},
  })];
  cards.risks = [item('risks', 'Vendor delay', {data:{severity:'high'}})];
  cards.timeline = [item('timeline', 'Kickoff', {data:{start_s:95}})];
  cards.live_notes = [item('live_notes', 'Keep the budget', {
    author_type:'system', author_id:'voice_command', data:{start_s:90, heading:'Budget'},
  })];
  cards.user_notes = [item('user_notes', 'REMOVED NOTE', {status:'removed'})];
  return {
    meeting_id:'m1', seq:1, title:'Planning', status:'ended',
    cloud_enabled:true, intelligence_online:true, participants:{
      sam:{id:'sam', display_name:'Sam', kind:'guest'},
    }, cards, questions:[], topic:{current:'Roadmap', history:[]},
    rolling_summary:'We discussed delivery.', report_views:['ribbon','brief','signal'],
    insight_review:{questions:[{id:'r1', correction:'Budget was only proposed.'}]},
    live_highlights:[
      {id:'late', kind:'number', text:'5000 budget', start_s:90, probability:0.9, segment_id:'s1'},
      {id:'lesson', kind:'takeaway', text:'Early feedback prevents rework', start_s:45, probability:0.93, segment_id:'s1'},
      {id:'early', kind:'decision', text:'Choose June', start_s:5, probability:0.95, segment_id:'s1'},
    ],
  };
}
const segments = [{id:'s1', meeting_id:'m1', channel:'loopback', start_s:5, end_s:100,
  speaker_participant_id:'sam', text:'Maya will send the RFC', original_text:'Myra will send the RFC'}];
function dom(markup) { return new JSDOM(markup).window.document; }

test('full meeting retains pulses, structured details, spoken notes and citation warnings', () => {
  const state = fixture();
  const before = structuredClone(state);
  const html = renderToStaticMarkup(React.createElement(EvidenceProvider, {
    segments, participants:Object.values(state.participants),
  }, React.createElement(FullMeetingDocument, {state, segments})));
  const document = dom(html);
  const text = document.body.textContent;
  for (const value of ['We discussed delivery.','Meeting pulses','Takeaways:','Early feedback prevents rework','0:45','1:30','Number:','5000 budget','Spoken note',
    'Owner: Sam','Due: Friday','Severity: high','1:35','Citation conflicts with claim',
    'Provisional','Budget was only proposed.','Maya will send the RFC']) assert.ok(text.includes(value), value);
  assert.ok(text.indexOf('Choose June') < text.indexOf('Early feedback prevents rework'));
  assert.ok(text.indexOf('Early feedback prevents rework') < text.indexOf('5000 budget'));
  assert.ok(!text.includes('REMOVED NOTE'));
  assert.ok(!text.includes('Myra will send the RFC'));
  assert.equal(document.querySelectorAll('textarea,input,select').length, 0);
  assert.deepEqual(state, before);
});

test('every summary report retains deadlines and advisory citation labels', () => {
  const state = fixture();
  for (const activeView of state.report_views) {
    const document = dom(renderToStaticMarkup(React.createElement(ReportTabs, {
      state, segments, activeView, transcriptComplete:true,
    })));
    const text = document.querySelector('.report-sheet').textContent;
    assert.ok(text.includes('Due: Friday'), activeView);
    assert.ok(text.includes('Citation conflicts with claim'), activeView);
    assert.ok(text.includes('Provisional'), activeView);
  }
});

test('report annotations ignore stale citation assessments without changing saved text', () => {
  const state = fixture();
  const action = state.cards.action_items[0];
  action.revision = 3;
  action.data = {due_date:'Monday'};
  const rendered = liveItems([action])[0];
  assert.match(rendered.text, /Due: Monday/);
  assert.doesNotMatch(rendered.text, /Citation/);
  assert.equal(action.text, 'Draft the RFC');
});

test('full export remains disabled until the complete transcript is loaded', () => {
  for (const complete of [false, true]) {
    const document = dom(renderToStaticMarkup(React.createElement(ReportDownload, {
      state:fixture(), transcriptComplete:complete,
    })));
    const buttons = document.querySelectorAll('button');
    assert.equal(buttons[0].disabled, false);
    assert.equal(buttons[1].disabled, !complete);
  }
});

test('Ribbon includes untimed and early insights once, even without timeline beats', () => {
  const state = fixture();
  state.cards.decisions = [
    item('decisions', 'Before the first beat', {id:'early-insight', data:{start_s:3}}),
    item('decisions', 'After the first beat', {id:'later-insight', data:{start_s:98}}),
  ];
  for (const timeline of [state.cards.timeline, []]) {
    state.cards.timeline = timeline;
    const document = dom(renderToStaticMarkup(React.createElement(ReportTabs, {
      state, segments, activeView:'ribbon',
    })));
    const body = document.querySelector('.report-sheet .rb-flow').textContent;
    for (const text of ['Before the first beat', 'After the first beat', 'Draft the RFC']) {
      assert.equal(body.split(text).length - 1, 1, text);
    }
  }
});
