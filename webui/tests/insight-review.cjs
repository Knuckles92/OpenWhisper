const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const key of ['window','document','HTMLElement','HTMLInputElement','HTMLTextAreaElement','Event','MouseEvent']) {
  global[key] = key === 'window' ? dom.window : dom.window[key];
}
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const ts = require('typescript');
const fs = require('node:fs');
for (const ext of ['.ts','.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020},
}).outputText, filename);
const InsightReview = require('../src/components/InsightReview.tsx').default;
const { ReviewCorrections } = require('../src/components/InsightReview.tsx');
const { applyEffect } = require('../src/state.ts');
const { liveItems } = require('../src/report.ts');
let root, container;
const item = {id:'it1', card:'action_items', text:'Send paper links', revision:1, status:'proposed', data:{}, evidence:['s1'], review:{state:'provisional'}};
const q = {id:'q1', item_id:'it1', revision:1, field:'acceptance', text:'Was this agreed, or only proposed?', choices:{accepted:'Agreed', offered:'Only proposed', edit:'Correct it', incorrect:'Incorrect'}, status:'open', reason:'Agreement needs clarification.', evidence:['s1'], sources:[{id:'s1', start_s:12, text:'I could send the paper links.'}], owners:[]};
const state = () => ({meeting_id:'m1',status:'ended',cloud_enabled:true,seq:1,participants:{},cards:{action_items:[structuredClone(item)]},insight_review:{enabled:true,status:'ready',message:'Review these details.',questions:[structuredClone(q)]}});
const props = onSendOp => ({state:state(),onSendOp,onRetry:async()=>{},onEvidenceClick:()=>{}});
async function mount(Component, data) {container=document.createElement('div'); document.body.append(container);root=createRoot(container);await act(async()=>root.render(React.createElement(Component,data)));}
afterEach(async()=>{if(root) await act(async()=>root.unmount());document.body.replaceChildren();});
function button(text) {return [...container.querySelectorAll('button')].find(b=>b.textContent===text && !b.closest('[hidden]'));}
async function click(el) {await act(async()=>el.click());}
async function input(el,value) {await act(async()=>{Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));});}

test('review sends a linked answer once and waits for acknowledgement',async()=>{
  let resolve; const pending=new Promise(r=>resolve=r); const calls=[];
  await mount(InsightReview,props(op=>{calls.push(op);return pending;}));
  await click(button('Only proposed'));
  assert.equal(button('Only proposed').disabled,true);
  await click(button('Only proposed'));
  assert.equal(calls.length,1);
  assert.equal(calls[0].op,'review_answer');
  assert.equal(calls[0].question_id,'q1');
  assert.equal(calls[0].answer,'offered');
  await act(async()=>resolve(false));
  assert.match(container.textContent,/Could not save/);
  assert.equal(button('Only proposed').disabled,false);
});

test('failed correction preserves wording for retry and sources stay accessible',async()=>{
  const calls=[], evidence=[];
  await mount(InsightReview,{...props(async op=>{calls.push(op);return false;}),onEvidenceClick:id=>evidence.push(id)});
  await click(button('Correct it'));
  await input(container.querySelector('textarea'),'Paper links were offered, but no task was agreed.');
  await click(button('Save correction'));
  assert.equal(calls[0].text,'Paper links were offered, but no task was agreed.');
  assert.equal(container.querySelector('textarea').value,calls[0].text);
  await click(button('0:12'));
  assert.deepEqual(evidence,['s1']);
});

test('review later does not resolve anything and skipped questions can reopen',async()=>{
  const calls=[]; const p=props(async op=>{calls.push(op);return true;});
  await mount(InsightReview,p);
  await click(button('Review later'));
  assert.equal(button('Agreed'),undefined);
  assert.equal(calls.length,0);
  await click(button('Continue review'));
  await click(button('Skip'));
  assert.equal(calls[0].op,'review_skip');
  p.state.insight_review.questions[0].status='skipped';
  await act(async()=>root.render(React.createElement(InsightReview,p)));
  await click(button('Reopen'));
  assert.equal(calls[1].op,'review_reopen');
});

test('stale question cannot overwrite a newer insight',async()=>{
  const p=props(async()=>{throw new Error('must not send');});
  p.state.cards.action_items[0].revision=2;
  await mount(InsightReview,p);
  assert.equal(button('Agreed'),undefined);
  assert.match(container.textContent,/insight changed/);
});

test('atomic review effect updates notes and corrections together',()=>{
  const doc=state(); const corrected={...item,revision:2,status:'edited',text:'Only offered',review:{state:'human'}};
  const next=applyEffect(doc,{entity:'review',review:{...doc.insight_review,questions:[{...q,status:'answered',correction:'Only offered'}]},items:[corrected,{id:'n1',card:'user_notes',text:'Clarification',status:'edited'}]});
  assert.equal(next.cards.action_items[0].text,'Only offered');
  assert.equal(next.cards.user_notes[0].text,'Clarification');
  assert.equal(next.insight_review.questions[0].status,'answered');
  assert.equal(doc.cards.action_items[0].text,'Send paper links');
});

test('reports preserve uncertainty labels and show authoritative corrections',async()=>{
  assert.match(liveItems([item])[0].text,/Provisional:/);
  const doc=state();doc.insight_review.questions[0].correction='Only offered; no commitment.';
  await mount(ReviewCorrections,{state:doc});
  assert.match(container.textContent,/supersede earlier wording/);
  assert.match(container.textContent,/Only offered; no commitment/);
});

function reviewSet(count) {
  const doc=state();
  doc.cards.action_items=Array.from({length:count},(_,i)=>({...structuredClone(item),id:`it${i}`,text:`Priority ${i+1}`}));
  doc.insight_review.questions=doc.cards.action_items.map((it,i)=>({...structuredClone(q),id:`q${i}`,item_id:it.id}));
  return doc;
}
function openRows() {
  return [...container.querySelectorAll('.review-question')].filter(el=>!el.closest('[hidden], details'));
}

test('initial review shows the first three and reveals every remaining question without retrying',async()=>{
  const p={...props(async()=>true),state:reviewSet(8),onRetry:async()=>assert.fail('expansion must not recheck insights')};
  await mount(InsightReview,p);
  assert.match(container.textContent,/8 to review/);
  assert.deepEqual(openRows().map(el=>el.querySelector('blockquote').textContent),['Priority 1','Priority 2','Priority 3']);
  await click(button('Review more (5 remaining)'));
  assert.deepEqual(openRows().map(el=>el.querySelector('blockquote').textContent),Array.from({length:8},(_,i)=>`Priority ${i+1}`));
  assert.equal(button('Review more (5 remaining)'),undefined);
  await click(button('Review later'));
  assert.equal(openRows().length,0);
  await click(button('Continue review'));
  assert.equal(openRows().length,8);
});

test('answering or skipping the first three does not automatically reveal more',async()=>{
  const p={...props(async()=>true),state:reviewSet(8)};
  p.onSendOp=async op=>{
    p.state=structuredClone(p.state);
    p.state.insight_review.questions.find(question=>question.id===op.question_id).status=op.op==='review_skip'?'skipped':'answered';
    root.render(React.createElement(InsightReview,p));
    return true;
  };
  await mount(InsightReview,p);
  await click(button('Agreed'));
  assert.equal(openRows().length,2);
  await click(button('Skip'));
  await click(button('Agreed'));
  assert.equal(openRows().length,0);
  assert.match(container.textContent,/5 to review/);
  assert.ok(button('Review more (5 remaining)'));
  await click(button('Review more (5 remaining)'));
  assert.equal(openRows().length,5);
  assert.equal(openRows()[0].querySelector('blockquote').textContent,'Priority 4');
});

test('a new question set resets the preview and a reopened hidden question becomes visible',async()=>{
  const p={...props(async()=>true),state:reviewSet(8)};
  p.state.insight_review.questions[7].status='skipped';
  p.onSendOp=async op=>{
    p.state=structuredClone(p.state);
    p.state.insight_review.questions.find(question=>question.id===op.question_id).status='open';
    root.render(React.createElement(InsightReview,p));
    return true;
  };
  await mount(InsightReview,p);
  await click(button('Reopen'));
  assert.equal(openRows().length,4);
  assert.equal(openRows()[3].querySelector('blockquote').textContent,'Priority 8');
  await click(button('Review more (4 remaining)'));
  assert.equal(openRows().length,8);
  p.state=reviewSet(6);
  p.state.insight_review.questions.forEach(question=>question.id=`new-${question.id}`);
  await act(async()=>root.render(React.createElement(InsightReview,p)));
  assert.equal(openRows().length,3);
  assert.ok(button('Review more (3 remaining)'));
});

test('small reviews show all questions and hiding review preserves a draft',async()=>{
  await mount(InsightReview,{...props(async()=>true),state:reviewSet(2)});
  assert.equal(openRows().length,2);
  assert.equal([...container.querySelectorAll('button')].some(b=>b.textContent.includes('Review more')),false);
  await click(button('Correct it'));
  await input(container.querySelector('textarea'),'Clarified wording');
  await click(button('Review later'));
  await click(button('Continue review'));
  assert.equal(container.querySelector('textarea').value,'Clarified wording');
});
