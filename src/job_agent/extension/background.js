importScripts("safety.js");
const BINDING = "otpBinding";
const trusted = async () => {
  await chrome.storage.local.setAccessLevel?.({accessLevel: "TRUSTED_CONTEXTS"});
  await chrome.storage.session.setAccessLevel?.({accessLevel: "TRUSTED_CONTEXTS"});
};
async function config() {
  await trusted();
  const stored = await chrome.storage.local.get(["serverUrl", "agentToken"]);
  const base = PjaSafety.localBase(stored.serverUrl);
  if (!stored.agentToken) throw new Error("请在扩展设置中填写 Agent Token");
  return {base, token: stored.agentToken};
}
async function api(base, token, path, data) {
  const res = await fetch(`${PjaSafety.localBase(base)}${path}`, {method: "POST", redirect: "error", cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(5000), headers: {"Content-Type": "application/json", "X-Agent-Token": token}, body: JSON.stringify(data)});
  const result = await res.json();
  if (!res.ok) throw new Error(result.error || "本地连接失败");
  return result;
}
async function close(binding) {
  await chrome.storage.session.remove(BINDING);
  if (!binding) return;
  try {
    const c = await config();
    if (c.base === binding.base) await api(c.base, c.token, "/api/fill/close", {session_id: binding.session_id});
  } catch { /* Server TTL is the final cleanup boundary. */ }
}
async function handle(request, sender) {
  if (sender.id !== chrome.runtime.id) throw new Error("非法扩展来源");
  await trusted();
  const binding = (await chrome.storage.session.get(BINDING))[BINDING];
  if (request.action === "START_BOUND_CODE") {
    if (sender.tab || sender.url !== chrome.runtime.getURL("popup.html")) throw new Error("只能由扩展面板启动");
    const tab = await chrome.tabs.get(request.tabId);
    const url = PjaSafety.recruitmentUrl(request.url);
    if (tab.url !== url || !request.documentId) throw new Error("页面已变化，请重试");
    await close(binding);
    const c = await config();
    const result = await api(c.base, c.token, "/api/fill/session", {job_id: PjaSafety.jobId(request.jobId), url});
    if (!result.session_id || result.origin !== new URL(url).origin) throw new Error("验证码会话页面不匹配");
    await chrome.storage.session.set({[BINDING]: {tabId: tab.id, url, documentId: request.documentId, base: c.base, session_id: result.session_id, expires: Date.now() + Math.min(120, Number(result.expires_in) || 120) * 1000}});
    return {ready: true};
  }
  if (!["POLL_BOUND_CODE", "CLOSE_BOUND_CODE"].includes(request.action)) return null;
  if (!binding || sender.tab?.id !== binding.tabId || sender.frameId !== 0 || sender.documentId !== binding.documentId || sender.url !== binding.url) return {closed: true};
  const tab = await chrome.tabs.get(binding.tabId).catch(() => null);
  if (request.action === "CLOSE_BOUND_CODE" || binding.expires <= Date.now() || tab?.url !== binding.url) { await close(binding); return {closed: true}; }
  const c = await config();
  if (c.base !== binding.base) { await close(binding); return {closed: true}; }
  const result = await api(c.base, c.token, "/api/fill/poll", {session_id: binding.session_id});
  if (result.closed) { await close(binding); return {closed: true}; }
  const latest = (await chrome.storage.session.get(BINDING))[BINDING];
  const after = await chrome.tabs.get(binding.tabId).catch(() => null);
  if (latest?.session_id !== binding.session_id || after?.url !== binding.url || binding.expires <= Date.now()) return {closed: true};
  if (result.code && /^\d{4,6}$/.test(result.code)) { await close(binding); return {code: result.code}; }
  return {code: null};
}
let queue = Promise.resolve();
chrome.runtime.onMessage.addListener((request, sender, respond) => {
  if (!["START_BOUND_CODE", "POLL_BOUND_CODE", "CLOSE_BOUND_CODE"].includes(request?.action)) return false;
  queue = queue.then(() => handle(request, sender)).then(respond, () => respond({error: "验证码会话已停止，请重新连接"}));
  return true;
});
trusted().catch(() => {});
