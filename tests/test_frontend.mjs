// Run: node --experimental-vm-modules --test tests/test_frontend.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { SourceTextModule, createContext } from 'node:vm';
import { safeArtifactUrl, safeExternalUrl, escapeHtml } from '../src/job_agent/web/js/utils.js';
import { fetchLocal, readApiResponse } from '../src/job_agent/web/js/api.js';
import { coalesceRefresh } from '../src/job_agent/web/js/state.js';
import { resumeText } from '../src/job_agent/web/js/resume-tailor.js';
import { clipboardCode } from '../src/job_agent/web/js/manual-code.js';

test('clipboard SMS extraction requires a unique ASCII code, not a random date', () => {
  assert.equal(clipboardCode('123456'), '123456');
  assert.equal(clipboardCode('验证码：004321，五分钟有效'), '004321');
  for (const text of ['2026年9月19日', 'code 1234 code 5678', '１２３４', '验证码1234567', '', '订单 123456']) {
    assert.equal(clipboardCode(text), null, text);
  }
});

test('live coverage text excludes contact details and unrelated metadata', () => {
  assert.equal(resumeText({ person: { phone: 'private' }, summary: '分析', skills: ['SQL'],
    experience_sections: [{ entries: [{ organization: '示例', role: '研究', bullets: [{ text: '事实' }] }] }] }),
    '分析\nSQL\n示例\n研究\n事实');
  assert.equal(resumeText({}), '');
  assert.equal(resumeText({ self_evaluation: 'Python', education: [{ major: '统计' }],
    experience_sections: [{ entries: [{ context: '研究支持' }] }] }), 'Python\n统计\n研究支持');
});

test('dashboard entry and all imports link without duplicate or missing bindings', async () => {
  const context = createContext({});
  const modules = new Map();
  async function load(url) {
    if (!modules.has(url.href)) {
      modules.set(url.href, new SourceTextModule(await readFile(url, 'utf8'), { context, identifier: url.href }));
    }
    return modules.get(url.href);
  }
  const entry = await load(new URL('../src/job_agent/web/js/main.js', import.meta.url));
  await entry.link((name, parent) => load(new URL(name, parent.identifier)));
  assert.equal(entry.status, 'linked');
  for (const module of ['resume-tailor.js', 'tracking.js', 'mock-interview.js', 'manual-code.js']) {
    assert.ok([...modules.keys()].some(url => url.endsWith('/' + module)), `${module} must be wired into the dashboard`);
  }
});

test('artifact links accept only application routes and reject executable or remote URLs', () => {
  for (const value of ['/resume-draft/23/pdf', '/resume-draft/23/docx', '/preparation/23', '/project-workshop/23/preview']) {
    assert.equal(safeArtifactUrl(value), value);
  }
  for (const value of ['javascript:alert(1)', '//evil.test', '/\\evil.test', 'https://evil.test/a', '/preparation/../settings', '/preparation/1?next=https://evil.test', null]) {
    assert.equal(safeArtifactUrl(value), null);
  }
  assert.equal(safeExternalUrl('https://careers.example.org/jobs/1'), 'https://careers.example.org/jobs/1');
  assert.equal(safeExternalUrl('https://user:password@example.org/'), null);
  assert.equal(safeExternalUrl('javascript:alert(1)'), null);
  assert.equal(escapeHtml('<script>"x"</script>'), '&lt;script&gt;&quot;x&quot;&lt;/script&gt;');
});

test('malformed success responses and structured server errors are not treated as success', async () => {
  await assert.rejects(readApiResponse(new Response('not json')), { code: 'invalid_response' });
  await assert.rejects(readApiResponse(new Response('null')), { code: 'invalid_response' });
  await assert.rejects(readApiResponse(new Response('{"error":"请先确认资料","code":"confirmation_required"}', { status: 400 })), {
    status: 400, code: 'confirmation_required', message: '请先确认资料',
  });
  assert.deepEqual(await readApiResponse(new Response('{"ok":true}')), { ok: true });
});

test('network failure and timeout return actionable errors without retrying writes', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError('Failed to fetch'); };
  await assert.rejects(fetchLocal('/api/dashboard'), { code: 'network_error' });
  globalThis.fetch = (_, { signal }) => new Promise((resolve, reject) => {
    calls++;
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
  });
  await assert.rejects(fetchLocal('/api/copilot/message', { method: 'POST', timeoutMs: 5 }), { code: 'timeout' });
  assert.equal(calls, 2);
});

test('request timeout includes a stalled response body after headers arrive', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let receivedSignal;
  let calls = 0;
  globalThis.fetch = async (_, { signal }) => {
    calls++;
    receivedSignal = signal;
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"ok":'));
        signal.addEventListener('abort', () => controller.error(signal.reason), { once: true });
        // The server never sends the rest of the JSON or closes the body.
      },
    }), { headers: { 'Content-Type': 'application/json' } });
  };
  const result = await Promise.race([
    fetchLocal('/api/copilot/message', { method: 'POST', timeoutMs: 5 })
      .then(readApiResponse).then(() => 'success', error => error.code),
    new Promise(resolve => { const timer = setTimeout(() => resolve('still pending'), 100); t.after(() => clearTimeout(timer)); }),
  ]);
  assert.equal(result, 'timeout');
  assert.equal(receivedSignal.aborted, true);
  assert.equal(calls, 1);
});

test('completed requests retain the original unread Response for consumers', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const response = new Response('{"ok":true}', { status: 201, headers: { 'X-Test': 'retained' } });
  globalThis.fetch = async () => response;
  const received = await fetchLocal('/api/example', { timeoutMs: 1000 });
  assert.equal(received, response);
  assert.equal(received.bodyUsed, false);
  assert.equal(received.status, 201);
  assert.equal(received.headers.get('X-Test'), 'retained');
  assert.equal(await received.clone().text(), '{"ok":true}');
  assert.deepEqual(await readApiResponse(received), { ok: true });
});

test('overlapping refreshes wait for a fresh read after the current one', async () => {
  const releases = [];
  let calls = 0;
  const refresh = coalesceRefresh(() => { calls++; return new Promise(resolve => releases.push(resolve)); });
  const first = refresh();
  await Promise.resolve();
  const afterMutation = refresh();
  assert.equal(first, afterMutation);
  releases.shift()();
  await Promise.resolve();
  assert.equal(calls, 2);
  releases.shift()();
  await afterMutation;
  assert.equal(calls, 2);
});

test('refresh queue recovers after failure', async () => {
  let calls = 0;
  const refresh = coalesceRefresh(async () => { if (++calls === 1) throw new Error('offline'); });
  await assert.rejects(refresh(), /offline/);
  await refresh();
  assert.equal(calls, 2);
});
