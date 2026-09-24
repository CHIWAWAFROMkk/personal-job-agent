// Run: node --experimental-vm-modules --test tests/test_frontend.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { SourceTextModule, createContext } from 'node:vm';
import { safeArtifactUrl, safeExternalUrl, escapeHtml, localDateTimeInput, localDateTimeIso } from '../src/job_agent/web/js/utils.js';
import { installGlobalFeedback } from '../src/job_agent/web/js/global-feedback.js';
import { fetchLocal, readApiResponse } from '../src/job_agent/web/js/api.js';
import { coalesceRefresh } from '../src/job_agent/web/js/state.js';
import { resumeText } from '../src/job_agent/web/js/resume-tailor.js';
import { isVersionComplete, createReviewDialog } from '../src/job_agent/web/js/review-dialog.js';
import { createPrepareController } from '../src/job_agent/web/js/prepare-controller.js';


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
  for (const module of ['resume-tailor.js', 'tracking.js', 'mock-interview.js', 'review-dialog.js', 'prepare-controller.js']) {
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

test('unhandled page failures give one actionable recovery path without exposing exception details', () => {
  const listeners = new Map();
  const target = { addEventListener: (type, listener) => listeners.set(type, listener) };
  const messages = [];
  installGlobalFeedback(target, message => messages.push(message));
  listeners.get('unhandledrejection')({ reason: new Error('private path and credentials') });
  listeners.get('unhandledrejection')({ reason: { name: 'AbortError' } });
  listeners.get('error')({ target, error: new Error('internal stack') });
  listeners.get('error')({ target: {}, error: null });
  assert.equal(messages.length, 2);
  assert.match(messages[0], /刷新后核对结果.*不要重复提交/);
  assert.equal(messages[0], messages[1]);
  assert.doesNotMatch(messages[0], /private path|credentials|internal stack/);
});

test('paid interview preparation uses the authorized POST helper', async () => {
  const source = await readFile(new URL('../src/job_agent/web/js/main.js', import.meta.url), 'utf8');
  const start = source.indexOf('if (action === "interview-prep")');
  const end = source.indexOf('if (action === "browser-use-assist")', start);
  assert.ok(start >= 0 && end > start);
  const branch = source.slice(start, end);
  assert.match(branch, /postLocalJson\(`\/api\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/interview-prep`, \{\}\)/);
  assert.doesNotMatch(branch, /fetch\s*\(/);
});

test('calendar input and displayed timestamps follow the device timezone', () => {
  assert.equal(localDateTimeInput('not-a-date'), '');
  assert.equal(localDateTimeInput(null), '');
  assert.equal(localDateTimeIso('not-a-date'), null);
  const moduleUrl = new URL('../src/job_agent/web/js/utils.js', import.meta.url).href;
  const script = `import {formatDateTime,localDateTimeInput,localDateTimeIso} from ${JSON.stringify(moduleUrl)}; process.stdout.write(JSON.stringify({display:formatDateTime('2026-09-23T00:00:00Z'), input:localDateTimeInput('2026-09-23T00:00:00Z'), instant:localDateTimeIso(localDateTimeInput('2026-09-23T00:00:00Z')), nonexistent:localDateTimeIso('2026-03-08T02:30'), repeated:localDateTimeIso('2026-11-01T01:30')}));`;
  function inZone(zone) {
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
      encoding: 'utf8', timeout: 5000, env: { ...process.env, TZ: zone },
    });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  }
  const china = inZone('Asia/Shanghai');
  const pacific = inZone('America/Los_Angeles');
  assert.equal(china.input, '2026-09-23T08:00');
  assert.equal(pacific.input, '2026-09-22T17:00');
  assert.match(china.display, /8:00/);
  assert.match(pacific.display, /17:00/);
  assert.equal(china.instant, '2026-09-23T00:00:00.000Z');
  assert.equal(pacific.instant, china.instant);
  assert.equal(pacific.nonexistent, null);
  assert.equal(pacific.repeated, null);
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

function setupMockReviewDialog() {
  if (!globalThis.document) {
    globalThis.document = {
      createElement(tag) {
        return {
          tagName: tag.toUpperCase(),
          className: '',
          style: {},
          innerHTML: '',
          remove() { this._removed = true; },
        };
      },
    };
  }
  const elements = new Map();
  function getElement(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        disabled: false,
        textContent: '',
        innerHTML: '',
        src: '',
        onclick: null,
        listeners: {},
        addEventListener(evt, fn) {
          this.listeners[evt] = this.listeners[evt] || [];
          this.listeners[evt].push(fn);
        },
        click() {
          if (this.onclick) return this.onclick();
          if (this.listeners['click']) {
            return Promise.all(this.listeners['click'].map(fn => fn()));
          }
        },
        querySelectorAll(sel) { return []; },
        querySelector(sel) {
          if (this._children) {
            return this._children.find(c => sel.includes(c.className) && !c._removed) || null;
          }
          return null;
        },
        prepend(el) {
          this._children = this._children || [];
          this._children.unshift(el);
          this.innerHTML = (el.innerHTML || '') + this.innerHTML;
        },
        close() { this.open = false; },
        showModal() { this.open = true; },
        setAttribute(k, v) { this[k] = v; },
        removeAttribute(k) { delete this[k]; },
      });
    }
    return elements.get(id);
  }
  return { $: (id) => getElement(id), elements };
}

test('review version validator requires 64-char hex sha256 and manifest_path', () => {
  assert.equal(isVersionComplete(null), false);
  assert.equal(isVersionComplete({}), false);
  assert.equal(isVersionComplete({ pdf_sha256: "" }), false);
  assert.equal(isVersionComplete({ pdf_sha256: "12345" }), false);
  assert.equal(isVersionComplete({ pdf_sha256: "Z".repeat(64), manifest_path: "/apps/1/m.json" }), false);
  assert.equal(isVersionComplete({ pdf_sha256: "a".repeat(64) }), false); // missing manifest_path
  assert.equal(isVersionComplete({ pdf_sha256: "A".repeat(64), manifest_path: "/apps/1/m.json" }), true);
  assert.equal(isVersionComplete({ pdf_sha256: "0123456789abcdef0123456789ABCDEF0123456789abcdef0123456789ABCDEF", manifest_path: "/apps/1/m.json" }), true);
});

test('review dialog isolates out-of-order responses and binds reveal to dedicated endpoint', async () => {
  const elements = {
    reviewDialog: {
      open: false,
      showModal() { this.open = true; },
      close() { this.open = false; },
      setAttribute(k, v) { this[k] = v; }
    },
    reviewDialogTitle: { textContent: '' },
    reviewGreetingsList: { innerHTML: '', querySelectorAll: () => [] },
    reviewOpenPageBtn: { disabled: false, onclick: null },
    reviewRevealBtn: { disabled: false, onclick: null },
    reviewMarkAppliedBtn: { disabled: false, onclick: null },
    reviewPdfFrame: { src: '' },
    reviewPdfLink: { href: '', hidden: true, removeAttribute(k) { delete this[k]; } },
    closeReviewButton: { onclick: null },
  };

  const $ = (id) => elements[id] || null;
  const posted = [];
  const postLocalJson = async (url, body) => {
    posted.push({ url, body });
    return { ok: true, revealed: true };
  };

  let resolveJob1 = null;
  let resolveJob2 = null;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url.includes('/api/jobs/1/detail')) {
      return new Promise((res) => {
        resolveJob1 = () => res(new Response(JSON.stringify({
          resume_version: {
            pdf_sha256: '1'.repeat(64),
            manifest_path: '/path/1.json'
          },
          greetings: []
        }), { headers: { 'Content-Type': 'application/json' } }));
      });
    }
    if (url.includes('/api/jobs/2/detail')) {
      return new Promise((res) => {
        resolveJob2 = () => res(new Response(JSON.stringify({
          resume_version: {
            pdf_sha256: '2'.repeat(64),
            manifest_path: '/path/2.json'
          },
          greetings: []
        }), { headers: { 'Content-Type': 'application/json' } }));
      });
    }
    return new Response('{}', { headers: { 'Content-Type': 'application/json' } });
  };

  try {
    const { openReviewPanel } = createReviewDialog({
      $,
      getJobs: () => [{ job_id: 1, title: 'Job 1' }, { job_id: 2, title: 'Job 2' }],
      postLocalJson,
      showError: () => {},
      showSuccess: () => {},
      loadDashboard: async () => {},
    });

    // 1. User opens Job 1
    const p1 = openReviewPanel(1);

    // 2. User quickly switches to Job 2 before Job 1 returns
    const p2 = openReviewPanel(2);

    // 3. Job 2 returns FIRST
    resolveJob2();
    await p2;

    // Verify UI is now showing Job 2
    assert.match(elements.reviewDialogTitle.textContent, /Job 2/);
    assert.equal(elements.reviewPdfFrame.src, `/resume-draft/2/pdf?v=${'2'.repeat(64)}`);
    assert.equal(elements.reviewPdfLink.href, elements.reviewPdfFrame.src);
    assert.equal(elements.reviewPdfLink.hidden, false);

    // 4. Job 1 returns LATER (out-of-order network response)
    resolveJob1();
    await p1;

    // Verify Job 1's delayed response DID NOT overwrite Job 2!
    assert.match(elements.reviewDialogTitle.textContent, /Job 2/);
    assert.equal(elements.reviewPdfFrame.src, `/resume-draft/2/pdf?v=${'2'.repeat(64)}`);
    assert.equal(elements.reviewPdfLink.href, elements.reviewPdfFrame.src);

    // 5. Test reveal button: must call /api/jobs/2/reveal-resume, NEVER review-and-open, and NEVER Job 1!
    await elements.reviewRevealBtn.onclick();
    assert.equal(posted.length, 1);
    assert.equal(posted[0].url, '/api/jobs/2/reveal-resume');
    assert.notEqual(posted[0].url, '/api/jobs/1/reveal-resume');
    assert.ok(!posted[0].url.includes('review-and-open'), 'revealBtn must NEVER call review-and-open');

    // 6. Test mark applied button: must mark Job 2, NEVER Job 1!
    globalThis.window = { confirm: () => true };
    await elements.reviewMarkAppliedBtn.onclick();
    assert.equal(posted.length, 2);
    assert.equal(posted[1].url, '/api/applications/2/status');
    assert.notEqual(posted[1].url, '/api/applications/1/status');

  } finally {
    globalThis.fetch = originalFetch;
    delete globalThis.window;
  }
});

test('review dialog reveal button calls dedicated reveal-resume endpoint, never review-and-open', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const { $, elements } = setupMockReviewDialog();
  const postedCalls = [];

  const postLocalJson = async (url, body) => {
    postedCalls.push({ url, body });
    return { ok: true, revealed: true };
  };

  globalThis.fetch = async (url) => {
    if (String(url).includes('/api/jobs/1/detail')) {
      return new Response(JSON.stringify({
        job_id: 1,
        title: '测试岗位',
        resume_version: {
          pdf_sha256: "A".repeat(64),
          manifest_path: "/apps/1/manifest.json",
          version_id: "v1",
        },
        greetings: [{ content: "打招呼" }],
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  const { openReviewPanel } = createReviewDialog({
    $,
    getJobs: () => [{ job_id: 1, title: '测试岗位' }],
    postLocalJson,
    showError: () => {},
    showSuccess: () => {},
    loadDashboard: async () => {},
  });

  await openReviewPanel(1);

  const revealBtn = elements.get('reviewRevealBtn');
  assert.equal(revealBtn.disabled, false);
  await revealBtn.click();

  assert.equal(postedCalls.length, 1);
  assert.equal(postedCalls[0].url, '/api/jobs/1/reveal-resume');
  assert.deepEqual(postedCalls[0].body, {});
  assert.ok(!postedCalls.some(c => c.url.includes('review-and-open')));
});

test('cloud resume requires a separate fact comparison checkbox bound to PDF and content hashes', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const { $, elements } = setupMockReviewDialog();
  const posted = [];
  globalThis.fetch = async () => new Response(JSON.stringify({
    resume_version: {
      pdf_sha256: 'A'.repeat(64), content_sha256: 'B'.repeat(64),
      manifest_path: '/apps/1/manifest.json',
      fact_review_required: true,
      fact_comparisons: [{ label: '经历要点', source: '形成调研报告。', draft: '获得国家级一等奖。' }],
    }, greetings: [],
  }), { headers: { 'Content-Type': 'application/json' } });
  const { openReviewPanel } = createReviewDialog({
    $, getJobs: () => [{ job_id: 1, title: '合成岗位' }],
    postLocalJson: async (url, body) => {
      posted.push({ url, body });
      return { ok: true, pdf_opened: true, page_opened: true, revealed: true };
    },
    showError: () => {}, showSuccess: () => {}, loadDashboard: async () => {},
  });
  await openReviewPanel(1);
  const openBtn = elements.get('reviewOpenPageBtn');
  const factConfirm = elements.get('reviewFactConfirm');
  assert.equal(elements.get('reviewFactSection').hidden, false);
  assert.match(elements.get('reviewFactList').innerHTML, /形成调研报告/);
  assert.match(elements.get('reviewFactList').innerHTML, /国家级一等奖/);
  assert.equal(openBtn.disabled, true);
  await openBtn.click();
  assert.equal(posted.length, 0);
  factConfirm.checked = true;
  factConfirm.onchange();
  assert.equal(openBtn.disabled, false);
  await openBtn.click();
  assert.equal(posted.length, 1);
  assert.equal(posted[0].body.confirmed_fact_review, true);
  assert.equal(posted[0].body.fact_review_sha256, 'A'.repeat(64));
  assert.equal(posted[0].body.fact_review_content_sha256, 'B'.repeat(64));
});

test('review dialog drops stale async responses and does not bind buttons across jobs', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const { $, elements } = setupMockReviewDialog();
  const postedCalls = [];
  const postLocalJson = async (url, body) => {
    postedCalls.push({ url, body });
    return { ok: true, pdf_opened: true, page_opened: true, revealed: true };
  };

  let resolveJob1;
  const job1Promise = new Promise(resolve => { resolveJob1 = resolve; });

  globalThis.fetch = async (url) => {
    const urlStr = String(url);
    if (urlStr.includes('/api/jobs/1/detail')) {
      await job1Promise;
      return new Response(JSON.stringify({
        job_id: 1,
        title: '旧岗位1',
        resume_version: {
          pdf_sha256: "1".repeat(64),
          manifest_path: "/apps/1/manifest.json",
        },
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    if (urlStr.includes('/api/jobs/2/detail')) {
      return new Response(JSON.stringify({
        job_id: 2,
        title: '新岗位2',
        resume_version: {
          pdf_sha256: "2".repeat(64),
          manifest_path: "/apps/2/manifest.json",
        },
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  const { openReviewPanel } = createReviewDialog({
    $,
    getJobs: () => [
      { job_id: 1, title: '旧岗位1' },
      { job_id: 2, title: '新岗位2' },
    ],
    postLocalJson,
    showError: () => {},
    showSuccess: () => {},
    loadDashboard: async () => {},
  });

  const p1 = openReviewPanel(1);
  const p2 = openReviewPanel(2);
  await p2;

  const openBtn = elements.get('reviewOpenPageBtn');
  const pdfFrame = elements.get('reviewPdfFrame');
  assert.equal(pdfFrame.src, `/resume-draft/2/pdf?v=${"2".repeat(64)}`);

  resolveJob1();
  await p1;

  assert.equal(pdfFrame.src, `/resume-draft/2/pdf?v=${"2".repeat(64)}`);

  await openBtn.click();
  assert.equal(postedCalls.length, 1);
  assert.equal(postedCalls[0].url, '/api/jobs/2/review-and-open');
  assert.equal(postedCalls[0].body.expected_sha256, "2".repeat(64));
  assert.equal(postedCalls[0].body.manifest_path, '/apps/2/manifest.json');
});

test('review dialog disables confirm button and offers retry when version info is incomplete', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const { $, elements } = setupMockReviewDialog();
  let fetchCount = 0;

  globalThis.fetch = async (url) => {
    fetchCount++;
    if (fetchCount === 1) {
      return new Response(JSON.stringify({
        job_id: 1,
        resume_version: { pdf_sha256: "short" },
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(JSON.stringify({
      job_id: 1,
      resume_version: {
        pdf_sha256: "E".repeat(64),
        manifest_path: "/apps/1/manifest.json",
      },
    }), { headers: { 'Content-Type': 'application/json' } });
  };

  const { openReviewPanel } = createReviewDialog({
    $,
    getJobs: () => [{ job_id: 1, title: '测试岗位' }],
    postLocalJson: async () => ({}),
    showError: () => {},
    showSuccess: () => {},
    loadDashboard: async () => {},
  });

  await openReviewPanel(1);

  const openBtn = elements.get('reviewOpenPageBtn');
  const revealBtn = elements.get('reviewRevealBtn');
  const greetingsList = elements.get('reviewGreetingsList');
  const pdfFrame = elements.get('reviewPdfFrame');

  assert.equal(openBtn.disabled, true);
  assert.equal(revealBtn.disabled, true);
  assert.equal(pdfFrame.src, 'about:blank');
  assert.equal(elements.get('reviewPdfLink').hidden, true);
  assert.equal(elements.get('reviewPdfLink').href, undefined);
  assert.ok(greetingsList.innerHTML.includes('reviewRetryBtn'));

  const retryBtn = elements.get('reviewRetryBtn');
  assert.ok(retryBtn);
  await retryBtn.click();

  assert.equal(fetchCount, 2);
  assert.equal(openBtn.disabled, false);
  assert.equal(revealBtn.disabled, false);
  assert.equal(pdfFrame.src, `/resume-draft/1/pdf?v=${"E".repeat(64)}`);
  assert.equal(elements.get('reviewPdfLink').href, pdfFrame.src);
  assert.equal(elements.get('reviewPdfLink').hidden, false);
});

test('review without a job URL describes approval accurately and preserves literal titles', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const { $, elements } = setupMockReviewDialog();
  const successes = [], errors = [], posts = [];
  globalThis.fetch = async () => new Response(JSON.stringify({
    resume_version: { pdf_sha256: 'a'.repeat(64), manifest_path: '/synthetic/manifest.json' }, greetings: [],
  }), { headers: { 'Content-Type': 'application/json' } });
  const { openReviewPanel } = createReviewDialog({
    $, getJobs: () => [{ job_id: 1, title: 'R&D <实习>' }],
    postLocalJson: async (url, body) => { posts.push({url, body}); return {pdf_opened: true, revealed: true, page_opened: false}; },
    showSuccess: message => successes.push(message), showError: message => errors.push(message),
    loadDashboard: async () => {},
  });
  await openReviewPanel(1);
  assert.equal(elements.get('reviewDialogTitle').textContent, '审阅投递材料 — R&D <实习>');
  assert.equal(elements.get('reviewOpenPageBtn').textContent, '已核对版面，批准投递材料');
  assert.match(elements.get('reviewDialogCopy').textContent, /没有招聘链接/);
  await elements.get('reviewOpenPageBtn').click();
  assert.equal(errors.length, 0);
  assert.match(successes[0], /尚未记录为已投递/);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/jobs/1/review-and-open');
  assert.equal(posts[0].body.expected_sha256, 'a'.repeat(64));
});

test('review dialog displays step status banner and changes button to retry on partial failure', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const { $, elements } = setupMockReviewDialog();
  let reportedError = null;
  let dashboardRefreshedWith = null;

  globalThis.fetch = async (url) => {
    return new Response(JSON.stringify({
      job_id: 1,
      title: '测试岗位',
      resume_version: {
        pdf_sha256: "F".repeat(64),
        manifest_path: "/apps/1/manifest.json",
        version_id: "v1",
      },
      greetings: [],
    }), { headers: { 'Content-Type': 'application/json' } });
  };

  const postLocalJson = async () => {
    const error = new Error('材料包同步写入受限');
    error.data = {
      visual_review_approved: true,
      error: '材料包同步写入受限（磁盘权限或占用）',
    };
    throw error;
  };

  const { openReviewPanel } = createReviewDialog({
    $,
    getJobs: () => [{ job_id: 1, title: '测试岗位' }],
    postLocalJson,
    showError: (msg) => { reportedError = msg; },
    showSuccess: () => {},
    loadDashboard: async (opts) => { dashboardRefreshedWith = opts; },
  });

  await openReviewPanel(1);

  const openBtn = elements.get('reviewOpenPageBtn');
  const greetingsList = elements.get('reviewGreetingsList');

  // Trigger review and open -> throws partial failure
  await openBtn.click();

  // 1. Button text changes to retry
  assert.equal(openBtn.textContent, '🔄 重试材料包同步与打开');
  assert.equal(openBtn.disabled, false);

  // 2. Error message explicitly informs user of partial completion
  assert.match(reportedError, /部分完成（审批已落盘）：/);

  // 3. Step status banner is rendered inside greetingsList
  assert.ok(greetingsList.innerHTML.includes('步骤执行状态：部分完成（审批已落盘）'));
  assert.ok(greetingsList.innerHTML.includes('第一步：简历版面人工审阅'));
  assert.ok(greetingsList.innerHTML.includes('第二步：投递材料包同步'));
  assert.ok(greetingsList.innerHTML.includes('第三步：打开外部应用与文件'));

  // 4. Dashboard refresh preserves error alert
  assert.deepEqual(dashboardRefreshedWith, { preserveAlert: true });
});

test('review dialog initial load displays retry button if resume is already approved but pack is not ready', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const { $, elements } = setupMockReviewDialog();

  globalThis.fetch = async (url) => {
    return new Response(JSON.stringify({
      job_id: 1,
      title: '测试岗位',
      application_pack_ready: false,
      resume_version: {
        pdf_sha256: "C".repeat(64),
        manifest_path: "/apps/1/manifest.json",
        version_id: "v1",
        qa: {
          pdf_visual_review: "passed",
        },
      },
      greetings: [],
    }), { headers: { 'Content-Type': 'application/json' } });
  };

  const { openReviewPanel } = createReviewDialog({
    $,
    getJobs: () => [{ job_id: 1, title: '测试岗位' }],
    postLocalJson: async () => ({}),
    showError: () => {},
    showSuccess: () => {},
    loadDashboard: async () => {},
  });

  await openReviewPanel(1);

  const openBtn = elements.get('reviewOpenPageBtn');
  assert.equal(openBtn.textContent, '🔄 重试材料包同步与打开');
  assert.equal(openBtn.disabled, false);
});

test('review dialog: A operation waiting for refresh does not close or overwrite B when A completes', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const { $, elements } = setupMockReviewDialog();
  let resolveDashboardRefreshA;
  const refreshPromiseA = new Promise(resolve => { resolveDashboardRefreshA = resolve; });

  const postLocalJson = async (url, body) => {
    return { ok: true, pdf_opened: true, page_opened: true, revealed: true };
  };

  globalThis.fetch = async (url) => {
    const urlStr = String(url);
    if (urlStr.includes('/api/jobs/1/detail')) {
      return new Response(JSON.stringify({
        job_id: 1,
        title: 'Job 1',
        resume_version: { pdf_sha256: "1".repeat(64), manifest_path: "/apps/1/manifest.json" },
        greetings: [],
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    if (urlStr.includes('/api/jobs/2/detail')) {
      return new Response(JSON.stringify({
        job_id: 2,
        title: 'Job 2',
        resume_version: { pdf_sha256: "2".repeat(64), manifest_path: "/apps/2/manifest.json" },
        greetings: [],
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  let dashboardCallCount = 0;
  const { openReviewPanel } = createReviewDialog({
    $,
    getJobs: () => [{ job_id: 1, title: 'Job 1', source_url: 'https://example.com/1' }, { job_id: 2, title: 'Job 2', source_url: 'https://example.com/2' }],
    postLocalJson,
    showError: () => {},
    showSuccess: () => {},
    loadDashboard: async () => {
      dashboardCallCount++;
      if (dashboardCallCount === 1) {
        // Job 1's refresh is delayed
        await refreshPromiseA;
      }
    },
  });

  // 1. User opens Job 1
  await openReviewPanel(1);
  const openBtn = elements.get('reviewOpenPageBtn');
  const dialog = elements.get('reviewDialog');
  const title = elements.get('reviewDialogTitle');
  const pdfFrame = elements.get('reviewPdfFrame');

  assert.equal(dialog.open, true);
  assert.equal(title.textContent, '审阅并打开招聘页 — Job 1');

  // 2. User clicks approve on Job 1 -> initiates review-and-open, which awaits loadDashboard()
  const reviewClickPromiseA = openBtn.click();

  // Give microtasks a tick to reach loadDashboard()
  await new Promise(r => setTimeout(r, 10));

  // 3. While Job 1 is waiting on loadDashboard(), user opens Job 2!
  await openReviewPanel(2);
  assert.equal(dialog.open, true);
  assert.equal(title.textContent, '审阅并打开招聘页 — Job 2');
  assert.equal(pdfFrame.src, `/resume-draft/2/pdf?v=${"2".repeat(64)}`);

  // 4. Now Job 1's loadDashboard() completes!
  resolveDashboardRefreshA();
  await reviewClickPromiseA;

  // 5. CRITICAL VERIFICATION: Job 2 dialog MUST STILL BE OPEN, not closed by Job 1!
  assert.equal(dialog.open, true, 'Job 1 刷新完成不得关闭处于打开状态的 Job 2 弹窗');
  assert.equal(title.textContent, '审阅并打开招聘页 — Job 2', 'Job 1 刷新完成不得修改 Job 2 的标题');
  assert.equal(pdfFrame.src, `/resume-draft/2/pdf?v=${"2".repeat(64)}`, 'Job 1 刷新完成不得修改 Job 2 的预览 PDF');
});

test('prepare controller: DOM re-render during prepare failure writes error to live element and clears busy state', async (t) => {
  let resolvePost;
  let rejectPost;
  const postPromise = new Promise((resolve, reject) => {
    resolvePost = resolve;
    rejectPost = reject;
  });

  const activePreparingJobIds = new Set();
  let reportedError = null;

  // Initial DOM elements
  const oldProgressEl = {
    id: "prepareProgress",
    hidden: true,
    innerHTML: "",
    isConnected: true,
    querySelector: () => null,
  };
  const oldPrimaryBtn = {
    disabled: false,
    textContent: "准备投递",
    isConnected: true,
  };

  // Live replacement DOM elements (simulating re-render during async wait)
  let retryHandlerRegistered = null;
  const newProgressEl = {
    id: "prepareProgress",
    hidden: true,
    innerHTML: "",
    isConnected: true,
    querySelector(sel) {
      if (sel === ".prepare-retry") {
        return {
          addEventListener(evt, fn) {
            if (evt === "click") retryHandlerRegistered = fn;
          },
        };
      }
      return null;
    },
  };
  const newPrimaryBtn = {
    disabled: true,
    textContent: "准备中…",
    isConnected: true,
  };

  let currentDoc = {
    getElementById(id) {
      if (id === "prepareProgress") return oldProgressEl;
      return null;
    },
    querySelector(sel) {
      if (sel === '[data-detail-action="prepare"]') return oldPrimaryBtn;
      return null;
    },
  };

  let prepareCallCount = 0;
  const { startPrepare } = createPrepareController({
    getActiveProfile: () => ({ confirmed_fact_count: 5 }),
    activePreparingJobIds,
    postLocalJson: async () => {
      prepareCallCount++;
      return postPromise;
    },
    loadDashboard: async () => {},
    showError: (msg) => { reportedError = msg; },
    showSuccess: () => {},
    openProfile: () => {},
    getDocument: () => currentDoc,
  });

  // 1. Trigger prepare
  const preparePromise = startPrepare(42);
  assert.equal(activePreparingJobIds.has(42), true);
  assert.equal(oldPrimaryBtn.disabled, true);
  assert.equal(oldPrimaryBtn.textContent, '准备中…');
  assert.equal(oldProgressEl.hidden, false);
  assert.ok(oldProgressEl.innerHTML.includes('正在准备投递材料'));

  // 2. Simulate re-render occurring in background while postLocalJson is pending:
  // Old elements are detached; new elements take their place in document
  oldProgressEl.isConnected = false;
  oldPrimaryBtn.isConnected = false;
  currentDoc = {
    getElementById(id) {
      if (id === "prepareProgress") return newProgressEl;
      return null;
    },
    querySelector(sel) {
      if (sel === '[data-detail-action="prepare"]') return newPrimaryBtn;
      return null;
    },
  };

  // 3. Now the prepare network call fails
  const error = new Error('AI 生成额度用尽');
  error.data = { failed_stage_name: 'resume_generation' };
  rejectPost(error);

  await preparePromise;

  // 4. Assert: Active busy state is cleared
  assert.equal(activePreparingJobIds.has(42), false);

  // 5. Old detached progress element must NOT have received the error
  assert.ok(!oldProgressEl.innerHTML.includes('准备材料未完成'), '已卸载的旧节点不得被写入错误信息');

  // 6. NEW live progress element MUST receive the error and the retry button!
  assert.equal(newProgressEl.hidden, false, '新活跃节点必须显示');
  assert.ok(newProgressEl.innerHTML.includes('准备材料未完成（失败环节：resume_generation）：AI 生成额度用尽'));
  assert.ok(newProgressEl.innerHTML.includes('重试'));

  // 7. NEW live primary button MUST be re-enabled and reset to "准备投递"
  assert.equal(newPrimaryBtn.disabled, false);
  assert.equal(newPrimaryBtn.textContent, '准备投递');

  // 8. Global alert was displayed
  assert.match(reportedError, /准备材料未完成（失败环节：resume_generation）：AI 生成额度用尽/);

  // 9. Retry button on new element triggers prepare again
  assert.ok(typeof retryHandlerRegistered === 'function', '重试按钮必须绑定点击事件');
  retryHandlerRegistered();
  assert.equal(prepareCallCount, 2, '点击重试必须再次触发准备流程');
});

test('prepare controller: onPrepared callback is triggered with jobId upon successful preparation', async () => {
  const activePreparingJobIds = new Set();
  let onPreparedCalledWith = null;
  let dashboardLoaded = false;

  const controller = createPrepareController({
    getActiveProfile: () => ({ confirmed_fact_count: 5 }),
    activePreparingJobIds,
    postLocalJson: async (path, body) => ({ resume_reused: false }),
    loadDashboard: async () => { dashboardLoaded = true; },
    showError: () => {},
    showSuccess: () => {},
    openProfile: () => {},
    onPrepared: async (jobId) => {
      onPreparedCalledWith = jobId;
    },
    getDocument: () => ({
      getElementById: () => ({ hidden: true }),
      querySelector: () => ({ disabled: false }),
    }),
  });

  await controller.startPrepare(88);

  assert.equal(dashboardLoaded, true, 'dashboard 必须在材料就绪后刷新');
  assert.equal(onPreparedCalledWith, 88, 'onPrepared 必须接收正确的 jobId');
  assert.equal(activePreparingJobIds.has(88), false, 'busy 状态必须被清除');
});

test('prepare controller: finishing another job cannot change the visible job or open its review', async () => {
  let resolvePost;
  const pendingPost = new Promise(resolve => { resolvePost = resolve; });
  let selectedJobId = 1;
  let openedJobId = null;
  const firstProgress = { hidden: true, innerHTML: '' };
  const firstButton = { disabled: false, textContent: '准备投递' };
  const secondProgress = { hidden: true, innerHTML: '岗位 2 的进度' };
  const secondButton = { disabled: true, textContent: '岗位 2 准备中…' };
  let currentDoc = {
    getElementById: () => firstProgress,
    querySelector: () => firstButton,
  };
  const activePreparingJobIds = new Set();
  const controller = createPrepareController({
    getActiveProfile: () => ({ confirmed_fact_count: 5 }),
    getSelectedJobId: () => selectedJobId,
    activePreparingJobIds,
    postLocalJson: () => pendingPost,
    loadDashboard: async () => {},
    showError: () => {},
    showSuccess: () => {},
    onPrepared: async jobId => { openedJobId = jobId; },
    getDocument: () => currentDoc,
  });

  const pending = controller.startPrepare(1);
  selectedJobId = 2;
  currentDoc = {
    getElementById: () => secondProgress,
    querySelector: () => secondButton,
  };
  resolvePost({ resume_reused: true });
  await pending;

  assert.equal(secondProgress.hidden, true);
  assert.equal(secondProgress.innerHTML, '岗位 2 的进度');
  assert.equal(secondButton.disabled, true);
  assert.equal(secondButton.textContent, '岗位 2 准备中…');
  assert.equal(openedJobId, null);
  assert.equal(activePreparingJobIds.has(1), false);
});

test('prepare controller: refresh failure after successful preparation does not offer a second preparation', async () => {
  let errorMessage = '';
  let opened = false;
  const progress = { hidden: true, innerHTML: '' };
  const button = { disabled: false, textContent: '准备投递' };
  const controller = createPrepareController({
    getActiveProfile: () => ({ confirmed_fact_count: 5 }),
    getSelectedJobId: () => 7,
    activePreparingJobIds: new Set(),
    postLocalJson: async () => ({ resume_reused: true }),
    loadDashboard: async () => { throw new Error('暂时无法读取'); },
    showError: message => { errorMessage = message; },
    showSuccess: () => {},
    onPrepared: async () => { opened = true; },
    getDocument: () => ({ getElementById: () => progress, querySelector: () => button }),
  });

  await controller.startPrepare(7);
  assert.match(errorMessage, /材料已准备好.*刷新失败.*不要重复准备/);
  assert.equal(progress.hidden, true);
  assert.equal(progress.innerHTML.includes('prepare-retry'), false);
  assert.equal(button.disabled, false);
  assert.equal(opened, false);
});
