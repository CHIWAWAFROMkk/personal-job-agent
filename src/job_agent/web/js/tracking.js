import { fetchLocal, readApiResponse } from './api.js';
import { escapeHtml as esc, safeExternalUrl } from './utils.js';
import { statusLabels } from './labels.js';

export function countdown(at, now = Date.now()) {
  const delta = new Date(at).getTime() - now;
  if (!Number.isFinite(delta)) return '时间待核对';
  if (delta <= 0) return '已到期 · 请核对结果';
  const minutes = Math.ceil(delta / 60000);
  return minutes < 60 ? `剩余 ${minutes} 分钟` : `剩余 ${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
}
const groups = [
  ['准备', ['discovered', 'matched', 'saved', 'ready_to_apply']], ['等待反馈', ['applied', 'hr_read', 'no_response']],
  ['推进中', ['resume_requested', 'screening', 'assessment', 'written_test']],
  ['面试', ['interview_1', 'interview_2', 'final_interview']], ['已结束', ['offer', 'rejected', 'withdrawn']],
];
const statuses = [['oa_pending', '测评'], ['written_test', '笔试'], ['interview_scheduled', '一面邀请'], ['interview_2', '二面邀请'], ['final_interview', '终面邀请'], ['rejected', '明确拒绝'], ['offered', '收到 Offer']];
const shanghaiTime = at => new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(at));
function inputTime(at) {
  if (!at || !Number.isFinite(Date.parse(at))) return '';
  return new Date(Date.parse(at) + 8 * 3600000).toISOString().slice(0, 16);
}

export function createTracking({ token, openJob, changed }) {
  const $ = id => document.getElementById(id);
  const host = document.createElement('section');
  host.id = 'trackingHub'; host.className = 'tracking-hub';
  host.innerHTML = `<div class="tracking-toolbar"><div><h2>求职看板</h2><p>核实通知，再推进一步。</p></div><div class="tracking-actions"><button type="button" class="action-button secondary" id="trackingExport">导出日历</button><button type="button" class="action-button" id="trackingOpen">登记通知</button><button type="button" class="action-button secondary" id="trackingRefresh">刷新</button></div></div><p id="trackingNotice" role="status"></p><section id="trackingUrgent" aria-label="48 小时内待办"></section><div id="trackingBoard" class="tracking-board" aria-label="求职状态看板"></div><details class="tracking-agenda"><summary>全部未完成日程 <span id="trackingEventCount"></span></summary><div id="trackingEvents"></div></details>`;
  $('overviewPanel').querySelector('.overview-head').after(host);
  const dialog = document.createElement('dialog');
  dialog.id = 'trackingDrawer'; dialog.className = 'tracking-drawer'; dialog.setAttribute('aria-labelledby','trackingTitle');
  dialog.innerHTML = `<form id="trackingForm"><header><div><h2 id="trackingTitle">粘贴通知快速登记</h2><p>仅在本机识别，不上传邮件或短信。</p></div><button type="button" class="action-button secondary" id="trackingClose">关闭</button></header><div class="tracking-drawer-body"><label for="trackingMessage">通知原文</label><textarea id="trackingMessage" maxlength="20000" rows="5" placeholder="粘贴测评或面试邀请；原文不会被保存。"></textarea><button type="button" class="action-button secondary" id="trackingParse">本地识别</button><p id="trackingParseStatus" role="status"></p><section id="trackingReview" hidden><p id="trackingCompany" class="tracking-detected"></p><ul id="trackingWarnings"></ul><label for="trackingJob">归属岗位（必须本人选择）</label><select id="trackingJob" required></select><label for="trackingStatus">核对状态</label><select id="trackingStatus" required><option value="">请选择状态</option>${statuses.map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select><label class="tracking-check"><input id="trackingSchedule" type="checkbox">同时登记日程</label><div id="trackingScheduleFields" hidden><label for="trackingKind">日程类型</label><select id="trackingKind"><option value="oa">测评截止</option><option value="interview">面试开始</option></select><label for="trackingAt">日期与时间（北京时间 UTC+8）</label><input type="datetime-local" id="trackingAt"><label for="trackingUrl">测评 / 会议链接（可选）</label><input id="trackingUrl" type="url" maxlength="2048" placeholder="https://"><p>链接未经真实性验证；打开前请核对发件人及域名。</p></div><label class="tracking-check"><input id="trackingConfirmed" type="checkbox" required>我已核对岗位、状态与时间，确认写入记录。</label><button id="trackingApply" type="submit" class="action-button">确认登记</button></section></div></form>`;
  document.body.append(dialog);
  let board = {jobs:[],reminders:[]}, generation = 0, readVersion = 0, requestId = '', lastPayload = '', busy = false;
  const notice = text => { $('trackingNotice').textContent = text; };
  const headers = () => ({'Content-Type':'application/json','X-Job-Agent-Token':token()});
  const api = async (path, data) => readApiResponse(await fetchLocal(path, {method:data === undefined ? 'GET' : 'POST', headers:headers(), cache:'no-store', timeoutMs:15000, ...(data === undefined ? {} : {body:JSON.stringify(data)})}));
  function reminder(item) {
    const url = safeExternalUrl(item.url);
    return `<article class="tracking-event"><div><strong>${esc(item.company)} · ${item.kind === 'oa' ? '测评截止' : '面试开始'}</strong><p>${esc(item.title)}</p><time datetime="${esc(item.at)}">${esc(shanghaiTime(item.at))} 北京时间</time><span class="tracking-pill">${esc(countdown(item.at))}</span></div><div class="tracking-actions">${url ? `<a class="action-button secondary" href="${esc(url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">打开${item.kind === 'oa' ? '测评' : '会议'}</a>` : ''}<button type="button" class="action-button secondary" data-complete="${Number(item.id)}">标记完成</button></div></article>`;
  }
  function render() {
    const active = board.reminders.filter(r=>!r.completed);
    const urgent = active.filter(r=>Date.parse(r.at)-Date.now() <= 48*3600000);
    $('trackingUrgent').innerHTML = `<h3>优先处理 <span class="tracking-pill">${urgent.length}</span></h3>${urgent.length ? urgent.map(reminder).join('') : '<p class="tracking-empty">未来 48 小时暂无已登记的紧急事项。</p>'}`;
    $('trackingEventCount').textContent = `（${active.length}）`;
    $('trackingEvents').innerHTML = active.length ? active.map(reminder).join('') : '<p class="tracking-empty">登记通知并确认时间后，日程会出现在这里。</p>';
    $('trackingBoard').innerHTML = groups.map(([name, values]) => {
      const jobs = board.jobs.filter(j=>values.includes(j.status) || (name === '准备' && !groups.some(([,v])=>v.includes(j.status))));
      return `<section class="tracking-column"><h3>${name}<span>${jobs.length}</span></h3>${jobs.length ? jobs.map(j=>`<button type="button" class="tracking-job" data-tracking-job="${Number(j.job_id)}"><strong>${esc(j.company)}</strong><span>${esc(j.title)}</span><small class="tracking-pill">${esc(statusLabels[j.status] || j.status)}</small><span class="tracking-job-action">查看岗位 →</span></button>`).join('') : '<p class="tracking-empty">暂无岗位</p>'}</section>`;
    }).join('');
  }
  async function refresh() {
    if (!token()) return;
    const version = ++readVersion;
    try { const result = await api('/api/tracking/board'); if(version !== readVersion) return; board=result; render(); notice('状态以本人核实记录为准；不会自动读取邮箱或短信。'); }
    catch(error) { if(version === readVersion) notice(`读取失败：${error.message}`); }
  }
  $('trackingRefresh').onclick = refresh;
  $('trackingOpen').onclick = async () => {
    dialog.showModal(); $('trackingMessage').focus(); await refresh();
  };
  $('trackingClose').onclick = () => dialog.close();
  dialog.addEventListener('close',()=>{generation++; $('trackingForm').reset(); $('trackingReview').hidden=true; $('trackingParseStatus').textContent=''; requestId=''; lastPayload='';});
  $('trackingMessage').oninput = () => {generation++; $('trackingReview').hidden=true; $('trackingConfirmed').checked=false; $('trackingParseStatus').textContent='原文已修改，请重新识别。';};
  $('trackingReview').addEventListener('input',e=>{if(e.target.id !== 'trackingConfirmed') $('trackingConfirmed').checked=false;});
  $('trackingSchedule').onchange = () => { $('trackingScheduleFields').hidden = !$('trackingSchedule').checked; $('trackingAt').required = $('trackingSchedule').checked; };
  $('trackingParse').onclick = async () => {
    const text=$('trackingMessage').value.trim(); if(!text) { $('trackingParseStatus').textContent='请先粘贴通知原文。'; return; }
    const epoch=++generation; $('trackingParse').disabled=true; $('trackingReview').hidden=true; $('trackingConfirmed').checked=false;
    try {
      const result=await api('/api/tracking/parse-message',{text});
      if(epoch!==generation || !dialog.open) return;
      $('trackingCompany').textContent=result.company ? `识别企业：${result.company}` : '企业未确定，请对照通知选择岗位。';
      $('trackingWarnings').innerHTML=(result.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('');
      $('trackingJob').replaceChildren(new Option('请选择具体岗位',''));
      for(const j of board.jobs) $('trackingJob').add(new Option(`${j.company} · ${j.title} (#${j.job_id})`,j.job_id));
      $('trackingStatus').value=result.status || '';
      $('trackingKind').value=result.event?.kind || 'oa';
      $('trackingAt').value=inputTime(result.event?.at);
      $('trackingUrl').value=result.event?.url || '';
      $('trackingSchedule').checked=Boolean(result.event);
      $('trackingSchedule').onchange();
      $('trackingReview').hidden=false;
      $('trackingParseStatus').textContent='识别完成。没有把握的字段留空，请本人补充。';
    } catch(error) {if(epoch===generation) $('trackingParseStatus').textContent=`识别失败：${error.message}`;}
    finally {$('trackingParse').disabled=false;}
  };
  $('trackingForm').onsubmit=async event=>{
    event.preventDefault(); if(busy || $('trackingReview').hidden) return;
    const payload={confirmed:$('trackingConfirmed').checked,job_id:Number($('trackingJob').value),status:$('trackingStatus').value};
    if($('trackingSchedule').checked) payload.event={kind:$('trackingKind').value,at:`${$('trackingAt').value}:00+08:00`,url:$('trackingUrl').value.trim() || null};
    const serialized=JSON.stringify(payload); if(lastPayload!==serialized) {lastPayload=serialized;requestId=crypto.randomUUID();}
    busy=true; $('trackingApply').disabled=true; const epoch=generation;
    try {
      await api('/api/tracking/apply-update',{...payload,request_id:requestId});
      if(epoch===generation) {dialog.close();notice('通知已登记。');}
      await refresh(); await changed();
    } catch(error) {if(epoch===generation) $('trackingParseStatus').textContent=`登记失败：${error.message}。保留输入，可核对后重试。`;}
    finally {busy=false;$('trackingApply').disabled=false;}
  };
  host.addEventListener('click',async event=>{
    const job=event.target.closest('[data-tracking-job]'); if(job) {openJob(Number(job.dataset.trackingJob));return;}
    const button=event.target.closest('[data-complete]'); if(!button) return;
    if(!window.confirm('确认这项测评或面试已经完成？岗位状态不会因此自动改变。')) return;
    button.disabled=true;
    try {await api(`/api/tracking/reminders/${Number(button.dataset.complete)}/complete`,{confirmed:true});await refresh();}
    catch(error){notice(error.message);button.disabled=false;}
  });
  $('trackingExport').onclick=async()=>{
    $('trackingExport').disabled=true;
    try {
      const response=await fetchLocal('/api/tracking/calendar.ics',{headers:headers(),cache:'no-store',timeoutMs:15000});
      if(!response.ok) {await readApiResponse(response);return;}
      const url=URL.createObjectURL(await response.blob()); const a=document.createElement('a');a.href=url;a.download='求职日程.ics';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      notice('已导出日历快照；后续变更需重新导出。');
    } catch(error){notice(`导出失败：${error.message}`);} finally {$('trackingExport').disabled=false;}
  };
  setInterval(()=>{if(!host.hidden && !$('overviewPanel').hidden && $('overviewPanel').dataset.mode==='progress' && !document.hidden) render();},60000);
  return {refresh};
}
