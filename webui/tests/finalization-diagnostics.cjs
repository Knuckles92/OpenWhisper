const assert = require('node:assert/strict');
const { test } = require('node:test');
const ts = require('../node_modules/typescript');
const fs = require('node:fs');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
require.extensions['.tsx'] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  });
  module._compile(outputText, filename);
};
const Diagnostics = require('../src/components/FinalizationDiagnostics.tsx').default;
const render = finalization => renderToStaticMarkup(React.createElement(Diagnostics, { finalization, meetingId: 'meeting-123' }));
test('timeout shows stage outcomes, recovery guidance and correlation details', () => {
  const html = render({status: 'failed', message: 'Cleanup failed', steps: [
    {id:'polish', name:'Transcript Cleanup', status:'failed', detail:'Timed out after 60s. Request ID: req-123.'},
    {id:'finalize', name:'State Finalization', status:'completed', detail:'Saved'},
  ]});
  for (const text of ['60s', 'req-123', 'meeting-123', 'State Finalization: completed', 'faster meeting intelligence model', 'Retry failed steps', 'openwhisper.log']) assert.ok(html.includes(text), text);
});
test('legacy unavailable errors show actionable authentication guidance without steps', () => {
  const html = render({status:'unavailable', message:'Invalid API key'});
  assert.match(html, /Check the API key and model access/);
});
test('completed and absent finalization show no failure panel', () => {
  assert.equal(render({status:'completed', message:'Done'}), '');
  assert.equal(render(null), '');
});
