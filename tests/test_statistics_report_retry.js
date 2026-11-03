const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../bot/statistics_cards_report.js'), 'utf8');
const helpers = source.slice(source.indexOf('function waitForRetry('), source.indexOf('async function run('));
function setup(responses, onFetch) {
  let calls = 0;
  const delays = [];
  const context = {
    DOMException, Date, AbortController,
    setTimeout(fn, ms) { delays.push(ms); return setTimeout(fn, 0); }, clearTimeout,
    fetch: async (...args) => { calls += 1; if (onFetch) onFetch(calls, ...args); return responses.shift(); }
  };
  vm.createContext(context); vm.runInContext(helpers, context);
  return { context, delays, calls: () => calls };
}
function reply(status, body, headers = {}) {
  return { ok: status === 200, status, text: async () => body,
    json: async () => JSON.parse(body), headers: { get: name => headers[name] || null } };
}
test('503 then success preserves URL, shows retry and reads JSON delay', async () => {
  const notices = [];
  const url = '/statistics/api/card-report?code=1';
  const s = setup([reply(503, '{"error":"busy","retry_after":3}'), reply(200, '{"rows":[]}')],
    (_, actual, options) => { assert.equal(actual, url); assert.equal(options.credentials, 'same-origin'); });
  const result = await s.context.fetchReport(url, new AbortController().signal, (...v) => notices.push(v));
  assert.equal(result.rows.length, 0); assert.equal(s.calls(), 2);
  assert.deepEqual(s.delays, [3000]); assert.deepEqual(notices, [[3, 1]]);
});
test('persistent busy stops after three retries and preserves plain text reason', async () => {
  const s = setup(Array.from({ length: 4 }, () => reply(503, '统计正在计算', { 'Content-Type': 'text/plain; charset=utf-8', 'Retry-After': '2' })));
  await assert.rejects(s.context.fetchReport('/q', new AbortController().signal, () => {}), /统计正在计算/);
  assert.equal(s.calls(), 4); assert.deepEqual(s.delays, [2000, 2000, 2000]);
});
test('authorization and invalid conditions do not retry', async () => {
  for (const status of [400, 403, 410]) {
    const s = setup([reply(status, '{"error":"条件或权限错误"}')]);
    await assert.rejects(s.context.fetchReport('/q', new AbortController().signal, () => {}), /条件或权限错误/);
    assert.equal(s.calls(), 1); assert.equal(s.delays.length, 0);
  }
});
test('reset or replacement query cancels retry before another request', async () => {
  const controller = new AbortController();
  const s = setup([reply(503, 'busy')]);
  await assert.rejects(s.context.fetchReport('/q', controller.signal, () => controller.abort()), { name: 'AbortError' });
  assert.equal(s.calls(), 1);
});
