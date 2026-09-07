const assert = require('node:assert/strict');
const { test } = require('node:test');
const ts = require('../node_modules/typescript');
const fs = require('node:fs');
require.extensions['.ts'] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  module._compile(outputText, filename);
};
const { hydrateTranscript, sendDashboardAction } = require('../src/dashboardActions.ts');
const { initialUiState, meetingReducer } = require('../src/state.ts');

function hydrationHarness() {
  let ui = initialUiState;
  const states = [];
  const onSegments = (segments) => {
    ui = meetingReducer(ui, { type: 'hydrate_segments', segments });
  };
  return {
    states,
    segments: () => ui.segments,
    run: (fetchPage, cancelled = () => false) =>
      hydrateTranscript(fetchPage, onSegments, (state) => states.push(state), cancelled),
  };
}

test('first-page failure leaves exports incomplete and exposes a retryable error', async () => {
  const h = hydrationHarness();
  await h.run(async () => { throw new Error('network'); });
  assert.equal(h.states.at(-1).status, 'failed');
  assert.match(h.states.at(-1).error, /could not be loaded/);
  assert.deepEqual(h.segments(), []);
  await h.run(async () => ({ items: [{ id: 'a', start_s: 0 }], next_cursor: null }));
  assert.equal(h.states.at(-1).status, 'complete');
  assert.equal(h.segments().length, 1);
});

test('later-page failure preserves partial content and retry merges without duplicates', async () => {
  const h = hydrationHarness();
  const first = { id: 'a', start_s: 0 };
  await h.run(async (cursor) => {
    if (cursor) throw new Error('network');
    return { items: [first], next_cursor: 'next' };
  });
  assert.equal(h.states.at(-1).status, 'failed');
  assert.deepEqual(h.segments(), [first]);
  const cursors = [];
  await h.run(async (cursor) => {
    cursors.push(cursor);
    return cursor
      ? { items: [{ id: 'b', start_s: 1 }], next_cursor: null }
      : { items: [first], next_cursor: 'next' };
  });
  assert.deepEqual(cursors, [undefined, 'next']);
  assert.deepEqual(h.segments().map((s) => s.id), ['a', 'b']);
  assert.equal(h.states.at(-1).status, 'complete');
});

test('empty successful transcript is complete', async () => {
  const h = hydrationHarness();
  await h.run(async () => ({ items: [], next_cursor: null }));
  assert.equal(h.states.at(-1).status, 'complete');
});

for (const rejects of [false, true]) {
  test(`cancellation ignores a late ${rejects ? 'failure' : 'page'}`, async () => {
    const h = hydrationHarness();
    let cancelled = false;
    let settle;
    const request = h.run(() => new Promise((resolve, reject) => {
      settle = rejects ? () => reject(new Error('late')) : () => resolve({ items: [{ id: 'late' }] });
    }), () => cancelled);
    cancelled = true;
    settle();
    await request;
    assert.deepEqual(h.states, [{ status: 'loading' }]);
    assert.deepEqual(h.segments(), []);
  });
}

for (const action of ['change', 'undo']) {
  test(`${action} handles offline, missing ack, rejection and transport failure`, async () => {
    const errors = [];
    const report = (message) => errors.push(message);
    assert.equal(await sendDashboardAction(null, action, report), false);
    assert.match(errors.pop(), /offline/);
    assert.equal(await sendDashboardAction(async () => [], action, report), false);
    assert.match(errors.pop(), /did not acknowledge/);
    assert.equal(await sendDashboardAction(async () => [{ ok: true }, { ok: false, reason: 'not_allowed' }], action, report), false);
    assert.equal(errors.pop(), 'not allowed');
    assert.equal(await sendDashboardAction(async () => [{ ok: false }], action, report), false);
    assert.equal(errors.pop(), action === 'undo' ? 'Undo was rejected' : 'Action was rejected');
    assert.equal(await sendDashboardAction(async () => { throw new Error('connection lost'); }, action, report), false);
    assert.equal(errors.pop(), 'connection lost');
    assert.equal(await sendDashboardAction(async () => { throw null; }, action, report), false);
    assert.equal(errors.pop(), action === 'undo' ? 'Undo could not be sent.' : 'Your change could not be sent.');
    assert.equal(await sendDashboardAction(async () => [{ ok: true }], action, report), true);
    assert.deepEqual(errors, []);
  });
}
