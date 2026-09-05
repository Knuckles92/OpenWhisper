const assert = require('node:assert/strict');
const ts = require('../node_modules/typescript');
const fs = require('node:fs');
require.extensions['.ts'] = (module, filename) => {
  const source = fs.readFileSync(filename, 'utf8');
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  module._compile(outputText, filename);
};
const { initialUiState, meetingReducer } = require('../src/state.ts');
const receive = (state, msg) => meetingReducer(state, { type: 'server_message', msg });
const partial = { type: 'speech_preview', channel: 'mic', text: 'draft', start_s: 1, end_s: 3, final: false };
let state = receive(initialUiState, partial);
assert.equal(state.speechPreviews.mic.text, 'draft');
assert.deepEqual(state.segments, []);
state = receive(state, { ...partial, text: '', final: true, end_s: 4 });
state = receive(state, { ...partial, text: 'late result' });
assert.equal(state.speechPreviews.mic.text, '');
state = receive(state, { ...partial, end_s: 4, text: 'late equal-time result' });
assert.equal(state.speechPreviews.mic.text, '');
state = receive(state, { ...partial, end_s: 5, text: 'new speech' });
assert.equal(state.speechPreviews.mic.text, 'new speech');
state = meetingReducer(state, { type: 'socket_status', status: 'closed' });
assert.deepEqual(state.speechPreviews, {});
state = receive({ ...state, meetingEnded: true }, partial);
assert.deepEqual(state.speechPreviews, {});
console.log('Live preview reducer: stale events, commit clearing, reconnect, and ended meetings passed.');

