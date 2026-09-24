let currentJob = null, boundTab = null, fillData = null, profileContext = null;
const $ = id => document.getElementById(id);
const names = {name:"姓名",phone:"电话",email:"邮箱",school:"学校",major:"专业",degree:"学历",experience:"经历"};
function status(message) { $("fillStatus").textContent = message; }
async function page() {
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  if (!tab?.id) throw new Error("无法读取当前页面");
  PjaSafety.pageUrl(tab.url);
  return tab;
}
async function unchanged(tab) {
  const current = await page();
  if (current.id !== tab.id || current.url !== tab.url) throw new Error("页面已改变，请重新读取档案");
}
async function request(path, body) {
  const base = PjaSafety.localBase($("serverUrl").value.trim());
  const token = $("agentToken").value.trim();
  if (!token) throw new Error("请在本地连接设置中填写 Agent Token");
  if (body !== undefined && !profileContext) throw new Error("请重新打开扩展弹窗，以确认当前用户资料后再保存。");
  const headers = {"Content-Type":"application/json","X-Agent-Token":token};
  if (body !== undefined) headers["X-Profile-Context"] = profileContext;
  const res = await fetch(base + path, {method:body === undefined ? "GET":"POST", redirect:"error", credentials:"omit", cache:"no-store", signal:AbortSignal.timeout(8000), headers, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}
async function bindProfileContext() {
  // Bind this popup once. Fetching a fresh context at write time would allow
  // an old, still-open popup to write its previous user's data after a switch.
  const state = await request("/api/agent/state");
  if (!state.profile_context) throw new Error("本地服务尚未提供用户资料校验，请更新程序后重试。");
  profileContext = state.profile_context;
}
async function helper(tab, action, fields) {
  await unchanged(tab);
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:["fill.js"]});
  await unchanged(tab);
  const result = await chrome.scripting.executeScript({target:{tabId:tab.id},func:(op, data, url) => {
    if (location.href !== url) throw new Error("页面已变化");
    return globalThis.PjaFill[op](data);
  },args:[action,fields || null,tab.url]});
  await unchanged(tab);
  if (!result[0]) throw new Error("网页未响应");
  return result[0];
}
function sourceMatches(data, tab) {
  if (!data.job_url || new URL(PjaSafety.recruitmentUrl(data.job_url)).hostname !== new URL(PjaSafety.recruitmentUrl(tab.url)).hostname) {
    throw new Error("当前网站与已保存岗位链接不一致。请先在工作台更新并核实岗位链接；跨域 ATS 跳转暂不自动信任。");
  }
}
async function loadFields() {
  fillData = null; $("fillReview").hidden = true;
  const id = PjaSafety.jobId($("jobId").value.trim());
  const tab = await page(); status("正在读取本地档案…");
  const data = await request(`/api/jobs/${id}/fill-data`);
  await unchanged(tab); sourceMatches(data,tab);
  fillData = {job_id:id,fields:Object.fromEntries(Object.keys(names).filter(k => typeof data.fields?.[k] === "string").map(k => [k,data.fields[k]]))};
  boundTab = tab;
  $("fillFields").replaceChildren();
  for (const [key,value] of Object.entries(fillData.fields)) {
    if (!value.trim()) continue;
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = names[key]; dd.textContent = value; $("fillFields").append(dt,dd);
  }
  $("attachmentNote").textContent = data.attachment?.note || "附件请在网页手动上传。";
  $("fillReview").hidden = false;
  status(`岗位 #${id} → ${new URL(tab.url).hostname}。核对以上信息后再预填。`);
}
async function confirmFill() {
  if (!fillData || !boundTab) throw new Error("请先读取并核对档案");
  const data = fillData; fillData = null; $("fillReview").hidden = true;
  const result = await helper(boundTab,"fill",data.fields);
  const filled = result.result.filled.map(k => names[k]).join("、") || "无";
  status(`已静默预填：${filled}。已有内容、重复或不明确的字段已跳过。部分网站需手动重输；请核对后手动上传和提交。`);
}
async function sendToAgent() {
  if (!currentJob) throw new Error("尚未识别岗位");
  const data = await request("/api/jobs/import-parsed",{company:currentJob.company,title:currentJob.title,jd_text:currentJob.jd_text,location:currentJob.location,source_url:currentJob.source_url,source:`web_${currentJob.platform || "extension"}`});
  $("statusMsg").textContent = `已收录岗位 #${data.job_id}`;
  if (data.job_id) $("jobId").value = data.job_id;
}
async function run(button, fn) {
  button.disabled = true;
  try { await fn(); } catch(err) { status(err.message || "操作失败，请重试"); }
  finally { button.disabled = false; }
}
async function init() {
  await chrome.storage.local.setAccessLevel?.({accessLevel:"TRUSTED_CONTEXTS"});
  const stored = await chrome.storage.local.get(["serverUrl","agentToken"]);
  if (stored.serverUrl) $("serverUrl").value = stored.serverUrl;
  if (stored.agentToken) $("agentToken").value = stored.agentToken;
  for (const id of ["serverUrl","agentToken"]) $(id).addEventListener("change",async () => {
    fillData = null; profileContext = null; $("fillReview").hidden = true;
    try { PjaSafety.localBase($("serverUrl").value.trim()); await chrome.storage.local.set({[id]:$(id).value.trim()}); await bindProfileContext(); }
    catch(err) { status(err.message); }
  });
  try { await bindProfileContext(); } catch(err) { status(err.message); }
  $("jobId").addEventListener("input",() => {fillData=null;$("fillReview").hidden=true;});
  for (const [id,fn] of [["loadFillBtn",loadFields],["confirmFillBtn",confirmFill],["sendBtn",sendToAgent]]) $(id).addEventListener("click",() => run($(id),fn));
  try {
    const tab = await page(); $("pageHost").textContent = new URL(tab.url).hostname;
    await chrome.scripting.executeScript({target:{tabId:tab.id},files:["content.js"]});
    currentJob = await chrome.tabs.sendMessage(tab.id,{action:"GET_JOB_INFO"});
    $("jobTitle").textContent = currentJob.title || "未识别岗位";
    $("jobMeta").textContent = [currentJob.company,currentJob.location,currentJob.salary].filter(Boolean).join(" · ");
    $("jobJd").textContent = currentJob.jd_text || "未读取到 JD";
    $("platformBadge").textContent = currentJob.platform || "网页";
    $("sendBtn").disabled = !(currentJob.title && currentJob.company && currentJob.jd_text);
  } catch(err) { status(err.message); }
}
document.addEventListener("DOMContentLoaded",() => init().catch(err => status(err.message)));
