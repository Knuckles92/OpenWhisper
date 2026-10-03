/** Synthetic markup probe; does not measure browser layout, scrolling or paint.
 * Reproduce: node webui/benchmarks/history-render.cjs --baseline=<commit> [--repeats=7]
 * The baseline commit is recorded in the JSON, so it remains reproducible after committing changes.
 */
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { performance } = require('node:perf_hooks');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');
const repo = path.resolve(__dirname, '../..');
const option = (name, fallback) => process.argv.find(arg => arg.startsWith('--' + name + '='))?.split('=').slice(1).join('=') || fallback;
const baseline = option('baseline', execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, encoding: 'utf8' }).trim());
const repeats = Math.max(1, Math.min(50, Number(option('repeats', '7')) || 7));
let mode = 'before';
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, file) => {
  const relative = path.relative(repo, file).split(path.sep).join('/');
  const source = mode === 'before' && relative.startsWith('webui/src/')
    ? execFileSync('git', ['show', baseline + ':' + relative], { cwd: repo, encoding: 'utf8' })
    : fs.readFileSync(file, 'utf8');
  module._compile(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020,
  } }).outputText, file);
};
require.extensions['.css'] = () => {};
const state = {
  meeting_id: 'm1', seq: 0, title: 'Saved meeting', status: 'ended', cloud_enabled: false,
  intelligence_online: false, participants: {}, cards: {}, questions: [],
  topic: { current: '', history: [] }, rolling_summary: 'Saved summary', report_views: ['brief'],
};
const samples = [];
for (mode of ['before', 'after']) {
  for (const key of Object.keys(require.cache)) if (key.startsWith(path.join(repo, 'webui', 'src') + path.sep)) delete require.cache[key];
  const TranscriptPane = require('../src/components/TranscriptPane.tsx').default;
  const ReportTabs = require('../src/components/report/ReportTabs.tsx').default;
  for (const count of [500, 5000]) {
    const segments = Array.from({ length: count }, (_, index) => ({
      id: 's' + index, meeting_id: 'm1', channel: 'mic', start_s: index * 2, end_s: index * 2 + 1,
      text: 'Turn s' + index, speaker_participant_id: null,
    }));
    const render = () => renderToStaticMarkup(React.createElement(React.Fragment, null,
      React.createElement(TranscriptPane, { segments, participants: [], highlightSegmentId: null,
        onHighlightClear() {}, onReassignSpeaker() {}, readOnly: true }),
      React.createElement(ReportTabs, { state, segments, activeView: 'brief' })));
    render(); // Warm imports and React's render path outside timing.
    const times = []; let markup;
    for (let trial = 0; trial < repeats; trial++) {
      const started = performance.now(); markup = render(); times.push(performance.now() - started);
    }
    const sorted = [...times].sort((a, b) => a - b);
    samples.push({ mode, segments: count, transcript_articles: (markup.match(/<article/g) || []).length,
      hidden_full_print_document: markup.includes('full-meeting-document'),
      markup_bytes: Buffer.byteLength(markup, 'utf8'),
      static_render_ms: times.map(value => Number(value.toFixed(3))),
      static_render_median_ms: Number(sorted[Math.floor(sorted.length / 2)].toFixed(3)),
    });
  }
}
process.stdout.write(JSON.stringify({
  scenario: 'Visible read-only TranscriptPane plus ended ReportTabs with Brief view, 500 and 5000 synthetic turns',
  baseline_commit: baseline, after_source: 'Current working tree', node: process.version,
  platform: process.platform, repeats, script: 'webui/benchmarks/history-render.cjs', samples,
  limitations: 'Synthetic React static rendering in one warmed Node process. Article counts and markup bytes describe rendered output; timing excludes imports, browser DOM, layout, scroll, paint, audio, ASR and network, and does not establish production latency or frame rate.',
}, null, 2) + '\n');
