import { fetchLocal, readApiResponse } from './api.js';

export function clipboardCode(text) {
  const value = String(text || '').trim();
  if (/^[0-9]{4,6}$/.test(value)) return value;
  const codes = [...value.matchAll(/(?:验证码|校验码|短信码|verification code|code)[^0-9]{0,12}([0-9]{4,6})(?![0-9])/gi)].map(m => m[1]);
  return codes.length === 1 ? codes[0] : null;
}

export function createManualCode({ token }) {
  const $ = id => document.getElementById(id);
  let jobId, timer, generation = 0, requestId = '', lastPayload = '', targetVersion = 0;
  const tell = text => { $('smsManualStatus').textContent = text; };
  async function post(path, data) {
    return readApiResponse(await fetchLocal(path, { method: 'POST', timeoutMs: 10000,
      headers: { 'Content-Type': 'application/json', 'X-Job-Agent-Token': token() }, body: JSON.stringify(data) }));
  }
  async function targets() {
    const epoch = generation;
    const version = ++targetVersion;
    try {
      const data = await post('/api/fill/sessions', { job_id: Number(jobId) });
      if (epoch !== generation || version !== targetVersion || !$('browserUseDialog').open) return;
      const incoming = ['', ...(data.sessions || []).map(s => s.session_id)];
      if (JSON.stringify(incoming) === JSON.stringify([...$('smsFillTarget').options].map(o => o.value))) return;
      const selected = $('smsFillTarget').value;
      $('smsFillTarget').replaceChildren(new Option('仅存本机，由我手工填写', ''));
      for (const session of data.sessions || []) $('smsFillTarget').add(new Option(`岗位 #${session.job_id} · ${session.origin}`, session.session_id));
      if ([...$('smsFillTarget').options].some(o => o.value === selected)) $('smsFillTarget').value = selected;
      else if (selected) tell('原页面连接已过期，请重新选择目标；不会自动换到其他页面。');
    } catch { if (epoch === generation) tell('无法读取页面连接。可刷新重试，或选择仅存本机。'); }
  }
  $('smsRefreshTargets').addEventListener('click', targets);
  $('smsPasteCode').addEventListener('click', async () => {
    const epoch = generation;
    try {
      const code = clipboardCode(await navigator.clipboard.readText());
      if (epoch !== generation || !$('browserUseDialog').open) return;
      if (!code) { tell('未发现唯一的短信验证码，请手工输入 4–6 位数字。'); return; }
      $('smsManualInput').value = code;
      tell('已粘贴，核对目标页面后点击录入。');
    } catch { tell('剪贴板权限不可用，请在输入框按 Ctrl+V 或使用数字键盘。'); }
  });
  $('smsKeypad').addEventListener('click', event => {
    const button = event.target.closest('[data-code-key]');
    if (!button) return;
    const input = $('smsManualInput');
    const key = button.dataset.codeKey;
    input.value = key === 'backspace' ? input.value.slice(0, -1) : key === 'clear' ? '' : (input.value + key).slice(0, 6);
    input.focus();
  });
  $('smsManualSubmit').addEventListener('click', async () => {
    const code = $('smsManualInput').value.trim();
    if (!/^[0-9]{4,6}$/.test(code)) { tell('请输入 4–6 位数字验证码；图形验证码和 CAPTCHA 请在网站手工完成。'); return; }
    const payload = { code, session_id: $('smsFillTarget').value || undefined };
    const serialized = JSON.stringify(payload);
    if (serialized !== lastPayload) { requestId = crypto.randomUUID(); lastPayload = serialized; }
    const epoch = generation;
    $('smsManualSubmit').disabled = true;
    try {
      const data = await post('/api/sms/manual', { ...payload, request_id: requestId });
      if (epoch !== generation) return;
      $('smsManualInput').value = '';
      tell(data.queued ? '已交给所选页面连接。请回招聘页面检查；入队不等于已填成功。' : '已录入本机。请本人核对并填写；未发送到招聘页面。');
    } catch (error) { if (epoch === generation) tell(`录入失败：${error.message}。输入已保留，可重试。`); }
    finally { if (epoch === generation) $('smsManualSubmit').disabled = false; }
  });
  const stop = () => { generation++; clearTimeout(timer); $('smsManualInput').value = ''; $('smsFillTarget').replaceChildren(new Option('仅存本机，由我手工填写', '')); lastPayload = ''; requestId = ''; };
  $('browserUseDialog').addEventListener('close', stop);
  return { open(id) {
    stop(); jobId = id;
    $('smsManualSubmit').disabled = false;
    tell('在招聘页点插件“接收此页验证码”，再回来选择连接。连接约两分钟后失效。');
    const epoch = generation;
    const poll = async () => { await targets(); if (epoch === generation && $('browserUseDialog').open) timer = setTimeout(poll, 2000); };
    poll();
  } };
}
