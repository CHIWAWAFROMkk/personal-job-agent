/* Isolated-world helper. No token, network, clipboard, submit or button-click capabilities. */
(() => {
  if (globalThis.PjaFill) return;
  let target = null, timer = null, expires = 0, armedUrl = "";
  const allowedTypes = new Set(["text", "email", "tel", "search", ""]);
  const blocked = /验证码|校验码|captcha|verification|one.time|otp|薪资|工资|salary|期望|expected|地址|住址|address|地点|城市|city|location|调剂|性别|gender|政治|身份证|id.card|密码|password|紧急|emergency|推荐人|referr|导师|父亲|母亲|监护|guardian/i;
  const aliases = {
    name: /^(?:姓名|中文姓名|真实姓名|申请人姓名|候选人姓名|full\s*name|your\s*name|name)$/i,
    phone: /^(?:手机|手机号|手机号码|联系电话|电话|mobile|mobile\s*(?:number|phone)|phone(?:\s*number)?)$/i,
    email: /^(?:邮箱|电子邮箱|电子邮件|email|e-mail|email\s*address)$/i,
    school: /^(?:学校|毕业院校|院校|学校名称|school|university|institution)$/i,
    major: /^(?:专业|所学专业|专业名称|major|field\s*of\s*study)$/i,
    degree: /^(?:学历|学位|最高学历|degree|education\s*level)$/i,
    experience: /^(?:经历概述|经历汇总|工作经历概述|experience\s*summary|career\s*summary)$/i,
  };
  function labels(el) {
    const texts = Array.from(el.labels || [], x => x.textContent || "");
    const aria = el.getAttribute("aria-label");
    if (aria) texts.push(aria);
    for (const id of (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)) {
      texts.push(document.getElementById(id)?.textContent || "");
    }
    return texts.map(x => x.replace(/[＊*：:]/g, "").trim()).filter(Boolean);
  }
  function visible(el) {
    if (!el?.isConnected || el.disabled || el.readOnly || el.hidden || el.closest("[hidden],[inert],[aria-hidden='true']")) return false;
    const style = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility === "visible" && Number(style.opacity) > 0 && r.width >= 20 && r.height >= 16 && el.getClientRects().length > 0;
  }
  function editable(el, allowPopulated = false) {
    return visible(el) && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && allowedTypes.has(el.type.toLowerCase()))) && (allowPopulated || !String(el.value || "").trim());
  }
  function fieldKey(el, allowPopulated = false) {
    if (!editable(el, allowPopulated)) return null;
    const names = labels(el);
    const all = [...names, el.name || "", el.id || "", el.getAttribute("placeholder") || "", el.getAttribute("autocomplete") || ""].join(" ");
    if (blocked.test(all.replace(/email\s+address/gi, "email"))) return null;
    const auto = el.getAttribute("autocomplete") || "";
    const autoKeys = { name: "name", tel: "phone", email: "email" };
    const keys = new Set(names.flatMap(label => Object.entries(aliases).filter(([, re]) => re.test(label)).map(([key]) => key)));
    if (autoKeys[auto]) keys.add(autoKeys[auto]);
    const key = keys.size === 1 ? [...keys][0] : null;
    return key === "experience" && el.tagName !== "TEXTAREA" ? null : key;
  }
  function plan(fields) {
    const candidates = {};
    for (const el of document.querySelectorAll("input,textarea")) {
      const key = fieldKey(el, true);
      if (key && typeof fields[key] === "string" && fields[key].trim()) (candidates[key] ||= []).push(el);
    }
    return Object.entries(candidates).map(([key, elements]) => ({key, elements, ambiguous: elements.length !== 1}));
  }
  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.style.outline = "2px solid #4475c7";
    el.style.outlineOffset = "2px";
  }
  function fill(fields) {
    const filled = [], skipped = [];
    for (const item of plan(fields)) {
      if (item.ambiguous) { skipped.push(item.key); continue; }
      const el = item.elements[0];
      // Recheck each node before writing; never synthesize website events.
      if (fieldKey(el) !== item.key || fields[item.key].length > 6000 || (el.maxLength > 0 && fields[item.key].length > el.maxLength)) {
        skipped.push(item.key); continue;
      }
      setValue(el, fields[item.key]);
      filled.push(item.key);
    }
    return {filled, skipped, message: "静默预填已结束。请本人核对、手动上传附件与提交；部分网站需手动重输才会识别。"};
  }
  function otpEligible(el) {
    if (!editable(el) || el.tagName !== "INPUT") return false;
    const names = labels(el).join(" ");
    const hints = [names, el.name || "", el.id || "", el.getAttribute("placeholder") || ""].join(" ");
    if (/图形|图片|滑块|拼图|captcha/i.test(hints)) return false;
    return (el.getAttribute("autocomplete") === "one-time-code" || /短信验证码|手机验证码|sms\s*(?:verification\s*)?code/i.test(names)) && (el.maxLength < 0 || el.maxLength >= 4);
  }
  function stop() {
    if (timer) clearInterval(timer);
    timer = null; target = null;
  }
  function arm() {
    stop();
    const el = document.activeElement;
    if (!otpEligible(el)) throw new Error("先在网页聚焦一个空的短信验证码输入框，再打开扩展；不支持图形验证或分格输入");
    target = el; armedUrl = location.href;
    return {ready: true};
  }
  async function tick() {
    if (!target || location.href !== armedUrl || Date.now() >= expires || !otpEligible(target)) {
      stop(); await chrome.runtime.sendMessage({action: "CLOSE_BOUND_CODE"}).catch(() => {}); return;
    }
    try {
      const result = await chrome.runtime.sendMessage({action: "POLL_BOUND_CODE"});
      if (result?.error || result?.closed) { stop(); return; }
      if (result?.code) {
        if (!/^\d{4,6}$/.test(result.code) || location.href !== armedUrl || !otpEligible(target) || (target.maxLength > 0 && result.code.length > target.maxLength)) { stop(); return; }
        // No events: frameworks may require the user to type/edit once before proceeding.
        setValue(target, result.code);
        target.setAttribute("title", "验证码已填入。请本人核对；未触发输入事件或提交。必要时手动重新输入。");
        stop();
      }
    } catch { stop(); }
  }
  function start() {
    if (!target || !otpEligible(target) || location.href !== armedUrl) throw new Error("验证码输入框已变化，请重新选择");
    expires = Date.now() + 120000;
    let pending = false;
    timer = setInterval(async () => { if (pending) return; pending = true; try { await tick(); } finally { pending = false; } }, 1000);
    return {waiting: true};
  }
  globalThis.PjaFill = Object.freeze({labels, visible, editable, fieldKey, plan, fill, otpEligible, arm, start, stop, tick});
})();
