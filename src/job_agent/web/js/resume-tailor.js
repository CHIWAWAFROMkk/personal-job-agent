import { fetchLocal, readApiResponse } from './api.js';

// Extends the existing saved draft editor; never changes the Profile or approval.
export function resumeText(content) {
  return [content.summary, content.self_evaluation, ...(content.skills || []),
    ...(content.education || []).flatMap(e => [e.institution, e.major, e.degree, e.highlights]),
    ...(content.experience_sections || []).flatMap(s => (s.entries || []).flatMap(e =>
      [e.organization, e.role, e.context, ...(e.bullets || []).map(b => b.text)]))].filter(Boolean).join('\n');
}

export function createResumeTailor({ getState, sync, token }) {
  const dialog = document.getElementById('resumeEditorDialog');
  const form = document.getElementById('resumeEditorForm');
  const byId = id => document.getElementById(id);
  let selected = null, timer, controller, revision = 0, openEpoch = 0, jobId, jd = '';
  let applied = null;
  let previewController, previewURL = '', previewRevision = 0;
  const status = text => { byId('tailorStatus').textContent = text; };
  function invalidate() {
    revision++;
    clearTimeout(timer);
    controller?.abort();
    byId('tailorSuggestions').replaceChildren();
  }
  function bulletFor(target) {
    if (!target?.isConnected) return null;
    const section = target.closest('[data-section-index]');
    const entry = target.closest('[data-entry-index]');
    const bullet = target.closest('[data-bullet-index]');
    return getState()?.content?.experience_sections?.[Number(section?.dataset.sectionIndex)]
      ?.entries?.[Number(entry?.dataset.entryIndex)]?.bullets?.[Number(bullet?.dataset.bulletIndex)];
  }
  function selectedFactIds() { return bulletFor(selected)?.fact_ids || []; }
  function resetPreview(message = '内容已修改，请重新预览排版。') {
    previewRevision++;
    previewController?.abort();
    byId('tailorPreviewFrame').removeAttribute('src');
    byId('tailorPreviewDownload').removeAttribute('href');
    byId('tailorPreviewDownload').hidden = true;
    if (previewURL) URL.revokeObjectURL(previewURL);
    previewURL = '';
    byId('tailorPreviewButton').disabled = false;
    byId('tailorPreviewStatus').textContent = message;
  }
  function showPreview(visible) {
    byId('tailorEditPanel').hidden = visible;
    byId('tailorPreviewPanel').hidden = !visible;
    byId('tailorEditView').setAttribute('aria-pressed', String(!visible));
    byId('tailorPreviewButton').setAttribute('aria-pressed', String(visible));
  }
  function list(target, values, render) {
    target.replaceChildren(...values.map(render));
  }
  function node(tag, text, cls) {
    const el = document.createElement(tag);
    el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  async function refresh() {
    invalidate();
    const seq = revision;
    if (!jd || !dialog.open) return;
    sync();
    const target = selected?.isConnected ? selected : null;
    const original = target?.value || '';
    controller = new AbortController();
    status('正在对照本地事实…');
    try {
      const response = await fetchLocal('/api/resume/tailor-live', {
        method: 'POST', signal: controller.signal, timeoutMs: 20000,
        headers: { 'Content-Type': 'application/json', 'X-Job-Agent-Token': token() },
        body: JSON.stringify({ job_id: jobId, jd_text: jd, paragraph: original,
          fact_ids: selectedFactIds(), resume_text: resumeText(getState().content) }),
      });
      const data = await readApiResponse(response);
      if (seq !== revision || !dialog.open) return;
      const coverage = data.coverage || {};
      byId('tailorCoverage').textContent = Number.isFinite(coverage.score) ? `${coverage.score}%` : '暂无';
      const matched = new Set(coverage.matched || []);
      list(byId('tailorKeywords'), coverage.keywords || [], word =>
        node('span', `${matched.has(word) ? '已覆盖 · ' : '未覆盖 · '}${word}`, matched.has(word) ? 'tailor-word covered' : 'tailor-word'));
      list(byId('tailorGates'), data.hard_gates || [], gate =>
        node('li', [gate.requirement, gate.explanation || gate.status].filter(Boolean).join('：')));
      if (!data.hard_gates?.length) byId('tailorGates').append(node('li', '未提取到明确硬门槛，请核对 JD 原文。'));
      list(byId('tailorSuggestions'), target ? data.suggestions || [] : [], suggestion => {
        const item = node('section', '', 'tailor-suggestion');
        const apply = node('button', '采用此建议', 'action-button secondary');
        apply.type = 'button';
        apply.disabled = suggestion.text.length > target.maxLength && target.maxLength > 0;
        apply.addEventListener('click', () => {
          if (seq !== revision || !target.isConnected || target.value !== original) return;
          showPreview(false);
          const bullet = bulletFor(target);
          applied = { target, before: original, after: suggestion.text, factIds: [...(bullet?.fact_ids || [])] };
          if (bullet) bullet.fact_ids = [...(suggestion.fact_ids || [])];
          target.value = suggestion.text;
          target.dispatchEvent(new Event('input', { bubbles: true }));
          byId('tailorUndo').disabled = false;
          status('已替换选中段落，尚未保存。请检查后保存并审阅 PDF。');
        });
        const compare = document.createElement('details');
        compare.append(node('summary', '查看改动与依据'), node('p', `当前段落：${original}`, 'tailor-diff-before'),
          node('p', `已确认依据：${suggestion.source_text || suggestion.text}`));
        for (const change of suggestion.changes || []) compare.append(node('p', change));
        const questions = suggestion.questions || [];
        if (questions.length) {
          compare.append(node('h4', '需要你补充的证据（不会自动写入）'));
          const ul = document.createElement('ul');
          for (const question of questions) ul.append(node('li', question));
          compare.append(ul);
        }
        item.append(node('h4', suggestion.title), node('p', suggestion.text), node('small', suggestion.reason || '依据已确认事实整理'), compare, apply);
        if (apply.disabled) item.append(node('small', '建议超过段落字数限制，请手工精简；不会截断后强行替换。'));
        return item;
      });
      list(byId('tailorWarnings'), data.warnings || [], warning => node('li', warning));
      status(!target ? '点击中间的经历要点，查看定向建议。' : data.suggestions?.length
        ? '建议已就绪。采用前可展开查看改动与依据。'
        : '当前段落缺少可用的已确认依据；请查看分析说明，仍可手工编辑。');
    } catch (error) {
      if (seq === revision && dialog.open) {
        byId('tailorCoverage').textContent = '暂无';
        status(`暂时无法分析：${error.message}。可点击“重新分析”。`);
      }
    }
  }
  form.addEventListener('focusin', event => {
    if (!event.target.matches('[data-editor-field="bullet-text"]') || selected === event.target) return;
    selected?.classList.remove('tailor-selected');
    selected = event.target;
    selected.classList.add('tailor-selected');
    refresh();
  });
  form.addEventListener('input', () => {
    resetPreview();
    invalidate();
    byId('tailorCoverage').textContent = '更新中';
    status('内容已修改，正在重新对照…');
    timer = setTimeout(refresh, 450);
  });
  new MutationObserver(() => {
    if (selected && !selected.isConnected) selected = null;
    if (!dialog.open) return;
    resetPreview();
    invalidate();
    timer = setTimeout(refresh, 0);
  }).observe(byId('editorSections'), { childList: true, subtree: true });
  byId('tailorRetry').addEventListener('click', refresh);
  byId('tailorUndo').addEventListener('click', () => {
    if (applied?.target.isConnected && applied.target.value === applied.after) {
      showPreview(false);
      const bullet = bulletFor(applied.target);
      if (bullet) bullet.fact_ids = applied.factIds;
      applied.target.value = applied.before;
      applied.target.dispatchEvent(new Event('input', { bubbles: true }));
    } else status('段落已再次修改，不能自动撤销以免覆盖新内容。');
    applied = null;
    byId('tailorUndo').disabled = true;
  });
  byId('tailorEditView').addEventListener('click', () => showPreview(false));
  byId('tailorPreviewButton').addEventListener('click', async () => {
    if (!getState()) return;
    showPreview(false);
    for (const input of form.querySelectorAll(':invalid')) {
      for (let el = input.parentElement; el && el !== form; el = el.parentElement) {
        if (el.tagName === 'DETAILS') el.open = true;
      }
    }
    if (!form.reportValidity()) return;
    sync();
    resetPreview('正在本机生成临时 PDF，不会保存为新版本…');
    const seq = previewRevision;
    previewController = new AbortController();
    byId('tailorPreviewButton').disabled = true;
    try {
      const response = await fetchLocal('/api/resume/preview', {
        method: 'POST', signal: previewController.signal, timeoutMs: 60000,
        headers: { 'Content-Type': 'application/json', 'X-Job-Agent-Token': token() },
        body: JSON.stringify({ job_id: jobId, content: getState().content }),
      });
      if (!response.ok) await readApiResponse(response);
      if (!response.headers.get('Content-Type')?.startsWith('application/pdf')) throw new Error('预览返回的不是 PDF');
      const blob = await response.blob();
      if (seq !== previewRevision || !dialog.open) return;
      previewURL = URL.createObjectURL(blob);
      byId('tailorPreviewFrame').src = previewURL;
      byId('tailorPreviewDownload').href = previewURL;
      byId('tailorPreviewDownload').hidden = false;
      showPreview(true);
      const pages = Number(response.headers.get('X-Resume-Preview-Pages'));
      byId('tailorPreviewStatus').textContent = `临时排版${pages > 0 ? ` · ${pages} 页` : ''} · 尚未保存或批准。${pages > 1 ? '内容超过一页，请检查分页。' : ''}`;
    } catch (error) {
      if (seq === previewRevision && dialog.open) byId('tailorPreviewStatus').textContent = `预览失败：${error.message}。编辑内容已保留，可修改后重试。`;
    } finally {
      if (seq === previewRevision) byId('tailorPreviewButton').disabled = false;
    }
  });
  dialog.addEventListener('close', () => { openEpoch++; invalidate(); resetPreview(); selected = null; });
  return {
    async open(id) {
      invalidate();
      resetPreview('预览不保存、不批准；使用当前模板在本机生成。');
      showPreview(false);
      jobId = Number(id); jd = ''; selected = null; applied = null;
      byId('tailorUndo').disabled = true;
      byId('tailorKeywords').replaceChildren();
      byId('tailorWarnings').replaceChildren();
      byId('tailorGates').replaceChildren();
      byId('tailorCoverage').textContent = '暂无';
      byId('tailorJD').textContent = '读取岗位要求…';
      const epoch = ++openEpoch;
      status('正在读取当前岗位…');
      try {
        const data = await readApiResponse(await fetchLocal(`/api/jobs/${encodeURIComponent(id)}/detail`));
        if (epoch !== openEpoch || !dialog.open) return;
        jd = data.job?.jd_text || '';
        byId('tailorJD').textContent = jd || '此岗位尚无 JD，无法计算关键词覆盖度。';
        if (jd) refresh();
        else status('请先补充岗位 JD；仍可手动编辑并保存简历。');
      } catch (error) { if (epoch === openEpoch) status(`岗位读取失败：${error.message}。请关闭后重新打开。`); }
    },
  };
}
