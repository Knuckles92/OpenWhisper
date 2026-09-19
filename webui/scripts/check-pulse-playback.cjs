// Run after npm run build; uses synthetic meeting data and silent audio only.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const participant = {id:'me',display_name:'Me',kind:'me',name_source:'human'};
const meeting = {id:'replay-test',title:'Budget planning',status:'active',has_audio:true,started_at:'2026-09-19T19:24:00Z'};
const pulses = [
  {id:'budget',kind:'number',start_s:60.874,segment_id:'budget',text:'Add a note that we need to get a thousand dollars for budget A.',probability:.81},
  {id:'decision',kind:'decision',start_s:105,segment_id:'decision',text:'We will review the revised budget on Friday.',probability:.95},
];
const segments = pulses.map(p => ({id:p.segment_id,meeting_id:meeting.id,channel:'mic',start_s:p.start_s,end_s:p.start_s+5,text:p.text,speaker_participant_id:'me',speaker_source:'human',speaker_pinned:false}));
const state = {meeting_id:meeting.id,seq:0,status:'active',title:meeting.title,cloud_enabled:true,intelligence_online:true,diarization_available:true,
  topic:{current:'Budget A',history:[]},rolling_summary:'Budget A needs an additional $1,000.',rolling_summary_evidence:['budget'],
  capture:{mic_available:true,loopback_available:true,message:''},participants:{me:participant},
  cards:Object.fromEntries(['key_points','decisions','action_items','risks','timeline','live_notes','user_notes'].map(k=>[k,[]])),questions:[],live_highlights:pulses,report_views:['ribbon','brief','signal']};
const wav = Buffer.alloc(44 + 16000 * 2 * 150);
wav.write('RIFF'); wav.writeUInt32LE(wav.length-8,4); wav.write('WAVEfmt ',8); wav.writeUInt32LE(16,16);
wav.writeUInt16LE(1,20); wav.writeUInt16LE(1,22); wav.writeUInt32LE(16000,24); wav.writeUInt32LE(32000,28);
wav.writeUInt16LE(2,32); wav.writeUInt16LE(16,34); wav.write('data',36); wav.writeUInt32LE(wav.length-44,40);
let failAudio = false;
const dist = path.resolve(__dirname, '../dist');
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (url.pathname.endsWith('/audio')) {
    if (failAudio) { res.writeHead(503); res.end(); return; }
    const range = /bytes=(\d+)-(\d*)/.exec(req.headers.range || '');
    const start = range ? Number(range[1]) : 0, end = range?.[2] ? Number(range[2]) : wav.length-1;
    res.writeHead(range ? 206 : 200, {'Content-Type':'audio/wav','Accept-Ranges':'bytes','Content-Length':end-start+1,
      ...(range ? {'Content-Range':`bytes ${start}-${end}/${wav.length}`} : {})});
    res.end(wav.subarray(start,end+1)); return;
  }
  if (url.pathname.startsWith('/api/')) {
    let result = [];
    if (url.pathname === '/api/session') result = {role:'host',state,meeting,urls:{}};
    else if (url.pathname === '/api/meetings') result = [meeting, {...meeting,id:'another',title:'Another meeting'}];
    else if (url.pathname.endsWith('/transcript')) result = {items:segments,next_cursor:null};
    else if (url.pathname.startsWith('/api/meetings/')) result = {meeting,state,segments,transcript_next_cursor:null};
    res.setHeader('Content-Type','application/json'); res.end(JSON.stringify(result)); return;
  }
  const file = url.pathname.startsWith('/assets/') ? path.join(dist,'assets',path.basename(url.pathname)) : path.join(dist,'index.html');
  res.setHeader('Content-Type',file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL ? {channel:process.env.BROWSER_CHANNEL} : {})});
  const page = await browser.newPage();
  const errors=[]; page.on('pageerror',error=>errors.push(error.message));
  const base=`http://127.0.0.1:${server.address().port}`;
  try {
    for (const mode of ['active','ended','history']) {
      state.status=meeting.status=mode==='active' ? 'active' : 'ended';
      for (const width of [1280,390,320]) {
        await page.setViewportSize({width,height:800});
        await page.goto(`${base}/m/test${mode==='history' ? '?history=replay-test' : ''}`);
        await page.locator('.pulse-number.pulse-mark').click();
        const player=page.getByRole('region',{name:'Meeting replay',exact:true});
        await player.waitFor();
        await page.waitForFunction(()=>{ const a=document.querySelector('audio'); return a && !a.paused && a.currentTime>=60.874 && a.currentTime<65; });
        assert.equal(await page.locator('audio').count(),1);
        const rect=await player.boundingBox();
        assert.ok(rect.x>=0 && rect.y>=0 && rect.x+rect.width<=width+1 && rect.y+rect.height<=801,`${mode} ${width}: player inside viewport ${JSON.stringify(rect)}`);
        assert.equal(await player.evaluate(el=>el.scrollWidth<=el.clientWidth),true);
        // Exercise the visible transport and keyboard-accessible timeline.
        await player.getByRole('button',{name:'Pause replay',exact:true}).click();
        await page.waitForFunction(()=>document.querySelector('audio').paused);
        await player.getByRole('button',{name:'Skip forward 10 seconds'}).click();
        assert.ok(await page.locator('audio').evaluate(a=>a.currentTime>=70 && a.currentTime<76));
        await player.getByRole('button',{name:'Skip back 10 seconds'}).click();
        const beforeSeek=await page.locator('audio').evaluate(a=>a.currentTime);
        await player.getByRole('slider',{name:'Replay position'}).press('ArrowRight');
        assert.ok(await page.locator('audio').evaluate((a,before)=>a.currentTime>before,beforeSeek));
        await player.getByRole('button',{name:'Play replay',exact:true}).press('Space');
        await page.waitForFunction(()=>!document.querySelector('audio').paused);
        await player.getByRole('button',{name:'Pause replay',exact:true}).waitFor();
        if (process.env.UI_SCREENSHOTS && width!==320) {
          fs.mkdirSync(process.env.UI_SCREENSHOTS,{recursive:true});
          await page.screenshot({path:path.join(process.env.UI_SCREENSHOTS,`replay-${mode}-${width}.png`)});
        }
        await player.getByRole('button',{name:'Close replay and stop audio'}).click();
        assert.equal(await page.locator('.is-floating').count(),0);
        assert.equal(await page.locator('audio').evaluate(a=>a.paused),true);
        await page.locator('.pulse-decision.pulse-mark').click();
        await page.waitForFunction(()=>{const a=document.querySelector('audio'); return a && !a.paused && a.currentTime>=105;});
        await player.getByRole('button',{name:'Close replay and stop audio'}).focus();
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('.is-floating').count(),0);
        console.log(`PASS ${mode} ${width}px: visible replay, pause/play and scrubber, skip, close, new moment, Escape`);
      }
    }
    state.status=meeting.status='active'; failAudio=true;
    await page.goto(`${base}/m/test`);
    await page.locator('.pulse-number.pulse-mark').click();
    await page.getByText('Recording unavailable. Please try again.').waitFor();
    failAudio=false;
    await page.getByRole('button',{name:'Retry',exact:true}).click();
    await page.waitForFunction(()=>{const a=document.querySelector('audio'); return a && !a.paused && a.currentTime>=60.874;});
    assert.deepEqual(errors,[]);
    console.log('PASS recording failure and retry');
  } finally { await browser.close(); await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
