import {fetchLocal, readApiResponse} from './api.js';
import {escapeHtml as esc} from './utils.js';
const starLabels={S:'S · 情境',T:'T · 任务',A:'A · 行动',R:'R · 结果'};

export function timerText(seconds) {
  const n=Math.max(0,Math.floor(Number(seconds)||0));
  return `${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;
}

export function createMockInterview({token}) {
  const $=id=>document.getElementById(id);
  const dialog=document.createElement('dialog'); dialog.id='mockDialog'; dialog.setAttribute('aria-labelledby','mockTitle');
  dialog.innerHTML=`<header class="mock-head"><div><h2 id="mockTitle">把经历，讲清楚。</h2><p id="mockJob"></p></div><button type="button" class="action-button secondary" id="mockClose">关闭</button></header><div class="mock-body"><section class="mock-setup" aria-label="练习设置"><div class="mock-controls"><label>练习方式<select id="mockEngine"><option value="local">本地规则 · 不联网</option><option value="cloud">已配置 AI · 辅助选题</option></select></label><button type="button" class="action-button mock-primary" id="mockStart">开始新练习</button><button type="button" class="action-button secondary" id="mockCard">查看提词卡</button></div><label id="mockConsentRow" class="mock-check" hidden><input id="mockConsent" type="checkbox">允许向当前配置的 AI 服务发送岗位 JD、已确认经历与本次回答。请勿输入敏感信息。</label><p>练习记录保存在本机。新回答不是档案事实；不录音，可将口述内容自行转为文字。</p><div class="mock-controls"><label>本岗位练习记录<select id="mockHistory"><option value="">暂无记录</option></select></label><button type="button" class="action-button secondary" id="mockResume">读取记录</button><button type="button" class="action-button secondary" id="mockRefresh">刷新记录</button></div></section><p id="mockNotice" role="status"></p><section id="mockSession" hidden><div class="mock-session-bar"><span id="mockRound"></span><span>本次打开 <time id="mockTimer">00:00</time></span><button id="mockPause" type="button" class="action-button secondary">暂停计时</button></div><details class="mock-plan"><summary>核心追问与事实依据</summary><ol id="mockQuestions"></ol></details><div id="mockMessages" class="mock-conversation" role="log" aria-label="对练记录" aria-live="polite"></div><form id="mockReplyForm" class="mock-composer"><label for="mockAnswer">你的回答</label><textarea id="mockAnswer" rows="4" maxlength="5000" required placeholder="说清情境、任务、自己的行动和可核验结果。没有做过的内容请如实说明。"></textarea><div class="mock-controls"><button id="mockReply" class="action-button mock-primary" type="submit">发送回答</button><button id="mockScore" class="action-button secondary" type="button">结束并复盘</button></div></form><section id="mockDiagnosis" aria-label="练习复盘" hidden></section></section></div>`;
  document.body.append(dialog);
  const card=document.createElement('aside');card.id='mockTeleprompter';card.hidden=true;card.setAttribute('aria-label','面试参考提词卡');
  card.innerHTML=`<header><strong>面试参考卡</strong><div class="mock-controls"><button id="mockCardPosition" class="action-button secondary" type="button">移到左侧</button><button id="mockCardClose" class="action-button secondary" type="button">关闭</button></div></header><div id="mockCardContent"></div>`;
  document.body.append(card);
  let jobId,jobLabel='',session=null,busy=false,epoch=0,started=0,elapsed=0,paused=false,timer=null,startKey='',retryKey='',retryBody='';
  const tell=text=>{$('mockNotice').textContent=text;};
  const api=async(path,payload)=>readApiResponse(await fetchLocal(path,{method:payload===undefined?'GET':'POST',cache:'no-store',timeoutMs:30000,headers:{'Content-Type':'application/json','X-Job-Agent-Token':token()},...(payload===undefined?{}:{body:JSON.stringify(payload)})}));
  function lock(value) {busy=value; for(const id of ['mockStart','mockResume','mockRefresh','mockReply','mockScore','mockEngine','mockHistory']) $(id).disabled=value; if(!value) { $('mockScore').disabled=!session || !session.turn_count || session.status==='completed'; $('mockReply').disabled=!session || session.status!=='active'; } }
  function clock(){ $('mockTimer').textContent=timerText(elapsed+(paused?0:(performance.now()-started)/1000)); }
  function stopClock(){clearInterval(timer);timer=null;}
  function startClock(){stopClock();elapsed=0;started=performance.now();paused=false;$('mockPause').textContent='暂停计时';clock();timer=setInterval(clock,1000);}
  function render() {
    if(!session)return;
    $('mockSession').hidden=false;
    $('mockRound').textContent=`${session.engine==='cloud'?'AI 辅助':'本地规则'} · 已回答 ${session.turn_count} / 5 轮${session.notice ? ` · ${session.notice}` : ''}`;
    $('mockQuestions').innerHTML=session.questions.map(q=>`<li>${esc(q.text)}${q.fact_ids?.length?`<small>事实依据：${esc(q.fact_ids.join('、'))}</small>`:''}</li>`).join('');
    $('mockMessages').innerHTML=session.messages.map(m=>`<article class="mock-message ${m.role==='user'?'is-user':'is-interviewer'}"><strong>${m.role==='user'?'你的练习回答（未核验）':'模拟面试官'}</strong><p>${esc(m.content)}</p></article>`).join('');
    $('mockReplyForm').hidden=session.status!=='active';
    $('mockDiagnosis').hidden=!session.score;
    $('mockScore').disabled=busy || !session.turn_count || session.status==='completed';
    if(session.status==='ready_to_score') {$('mockReplyForm').hidden=false;$('mockAnswer').disabled=true;$('mockReply').disabled=true;}
    else {$('mockAnswer').disabled=false;}
    if(session.score){
      const s=session.score;
      $('mockDiagnosis').innerHTML=`<h3>复盘：下一次怎么讲</h3><p class="mock-score">表达结构参考 ${esc(s.total)} / 100</p><p>${esc(s.disclaimer)}</p><dl class="mock-dimensions">${Object.entries(s.dimensions).map(([key,value])=>`<div><dt>${esc(starLabels[key]||key)}</dt><dd>${esc(value)}<small> / 25</small></dd></div>`).join('')}</dl><ul>${(s.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('')}</ul><h4>基于已确认事实的重写提纲</h4>${(s.rewrites||[]).map(r=>`<details><summary>${esc(r.question)}</summary><p class="mock-script">${esc(r.script)}</p></details>`).join('')}`;
      stopClock();
    }
  }
  async function history(){
    const current=epoch;const result=await api(`/api/interview/jobs/${jobId}/sessions`); if(current!==epoch)return;
    const selected=session?.session_id || $('mockHistory').value;
    $('mockHistory').replaceChildren(new Option('选择练习记录',''));
    for(const item of result.sessions) $('mockHistory').add(new Option(`${new Date(item.created_at).toLocaleString('zh-CN')} · ${item.status==='completed'?'已复盘':'进行中'} · ${item.turn_count || 0} 轮`,item.session_id));
    if([...$('mockHistory').options].some(o=>o.value===selected))$('mockHistory').value=selected;
  }
  $('mockEngine').onchange=()=>{$('mockConsentRow').hidden=$('mockEngine').value!=='cloud';$('mockConsent').checked=false;startKey='';};
  $('mockStart').onclick=async()=>{
    if(busy)return;
    const engine=$('mockEngine').value;
    if(engine==='cloud'&&!$('mockConsent').checked){tell('请先确认向已配置 AI 服务发送必要内容，或使用本地规则。');return;}
    if(session && !window.confirm('开始新的练习？现有记录会保留，尚未发送的回答不会带入。'))return;
    lock(true);tell('正在准备基于事实的追问…');const current=epoch;startKey ||= crypto.randomUUID();
    try {const result=await api('/api/interview/session/start',{job_id:jobId,engine,cloud_consent:engine==='cloud'&&$('mockConsent').checked,request_id:startKey});if(current!==epoch)return;session=result;startKey='';$('mockAnswer').value='';render();startClock();tell('可以开始。缺少的信息请明确说“待补充”，不用编造结果。');await history();}
    catch(error){if(current===epoch)tell(`开始失败：${error.message}。可重试。`);}finally{lock(false);}
  };
  $('mockReplyForm').onsubmit=async event=>{
    event.preventDefault();if(busy||!session||session.status!=='active')return;
    const answer=$('mockAnswer').value.trim();if(!answer){tell('请输入真实回答。');return;}
    const body={session_id:session.session_id,answer,expected_revision:session.revision};
    const serialized=JSON.stringify(body);if(serialized!==retryBody){retryBody=serialized;retryKey=crypto.randomUUID();}
    lock(true);const current=epoch;tell('正在核对表达结构与事实边界…');
    try {const result=await api('/api/interview/session/reply',{...body,request_id:retryKey});if(current!==epoch)return;session=result;$('mockAnswer').value='';render();tell(session.status==='ready_to_score'?'本轮练习完成，可以结束并复盘。':'已保存回答。继续追问，或结束并复盘。');$('mockMessages').lastElementChild?.scrollIntoView({block:'nearest'});}
    catch(error){if(current===epoch)tell(`回答未确认保存：${error.message}。输入已保留；超时可原样重试，版本冲突请读取最新记录。`);}finally{lock(false);if(session?.status==='ready_to_score')$('mockReply').disabled=true;}
  };
  $('mockScore').onclick=async()=>{
    if(busy||!session)return;
    if($('mockAnswer').value.trim()&&!window.confirm('输入框还有未发送的回答。仍结束并只复盘已保存内容？'))return;
    lock(true);const current=epoch;tell('正在生成结构复盘…');
    try {const result=await api('/api/interview/session/score',{session_id:session.session_id,expected_revision:session.revision});if(current!==epoch)return;session=result;render();tell('复盘已保存。评分只反映表达结构，不是面试通过率。');$('mockDiagnosis').scrollIntoView({block:'start'});await history();}
    catch(error){if(current===epoch)tell(`复盘失败：${error.message}。可以重试或读取最新记录。`);}finally{lock(false);}
  };
  $('mockResume').onclick=async()=>{
    const id=$('mockHistory').value;if(!id||busy)return;
    if($('mockAnswer').value.trim()&&!window.confirm('读取记录会替换未发送的回答，继续？'))return;
    lock(true);const current=epoch;
    try{const result=await api(`/api/interview/session/${encodeURIComponent(id)}`);if(current!==epoch)return;session=result;$('mockAnswer').value='';render();startClock();if(session.status==='completed')stopClock();tell('已读取本地记录；它使用练习开始时的事实快照。');}
    catch(error){if(current===epoch)tell(error.message);}finally{lock(false);}
  };
  $('mockRefresh').onclick=()=>history().catch(e=>tell(e.message));
  $('mockPause').onclick=()=>{if(!paused){elapsed+=(performance.now()-started)/1000;paused=true;}else{started=performance.now();paused=false;}$('mockPause').textContent=paused?'继续计时':'暂停计时';clock();};
  $('mockClose').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>{epoch++;stopClock();$('mockAnswer').value='';$('mockConsent').checked=false;});
  async function teleprompter(id,current){
    const result=await api('/api/interview/teleprompter',{job_id:id});
    if(current!==epoch || !dialog.open) return false;
    card.hidden=false;
    $('mockCardContent').innerHTML=`<p class="mock-card-boundary">仅供允许参考资料的场景使用。不监听、不作答；切换其他应用后不保证置顶。</p><h3>自我介绍</h3><p>${esc(result.intro)}</p><h3>已确认的关键数字</h3>${result.metrics.length?result.metrics.map(f=>`<p>${esc(f.statement)}<small>依据 ${esc(f.id)}</small></p>`).join(''):'<p>暂无可核验数字，不自行补写。</p>'}<details><summary>事实与 STAR 提纲</summary>${result.facts.map(f=>`<p>${esc(f.statement)}<small>依据 ${esc(f.id)}</small></p>`).join('')}<ul>${result.star.map(s=>`<li>${esc(s)}</li>`).join('')}</ul></details><h3>可以反问</h3><ul>${result.questions.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>`;
    return true;
  }
  $('mockCard').onclick=async()=>{const id=jobId,current=epoch;$('mockCard').disabled=true;try{if(await teleprompter(id,current)){dialog.close();$('mockCardClose').focus();}}catch(error){if(current===epoch)tell(`提词卡未生成：${error.message}`);}finally{$('mockCard').disabled=false;}};
  $('mockCardClose').onclick=()=>{card.hidden=true;};
  $('mockCardPosition').onclick=()=>{card.classList.toggle('is-left');$('mockCardPosition').textContent=card.classList.contains('is-left')?'移到右侧':'移到左侧';};
  return {async open(id,label=''){
    epoch++;jobId=Number(id);jobLabel=label;session=null;startKey='';retryKey='';retryBody='';$('mockSession').hidden=true;$('mockJob').textContent=jobLabel;$('mockEngine').value='local';$('mockEngine').onchange();$('mockAnswer').value='';tell('选择本地练习，或读取上次记录继续。');dialog.showModal();
    try{await history();}catch(error){tell(`历史记录暂不可读：${error.message}`);}
  }};
}
