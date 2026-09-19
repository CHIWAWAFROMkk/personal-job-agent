(() => {
if (globalThis.__pjaCaptureInstalled) return;
globalThis.__pjaCaptureInstalled = true;
// 针对国内各大招聘网站的页面结构提取规则
function extractJobInfo() {
  const url = window.location.href;
  const host = window.location.hostname;
  let title = "";
  let company = "";
  let location = "";
  let salary = "";
  let jdText = "";
  let platform = "web";

  if (host.includes("zhipin.com")) {
    platform = "boss";
    title = (document.querySelector(".job-name, .name, h1")?.innerText || "").trim();
    company = (document.querySelector(".company-info a, .company-name, .job-company")?.innerText || "").trim();
    salary = (document.querySelector(".job-banner .salary, .salary")?.innerText || "").trim();
    location = (document.querySelector(".text-desc, .job-location, .location")?.innerText || "").trim();
    jdText = (document.querySelector(".job-detail-section, .job-sec-text, .job-detail")?.innerText || "").trim();
  } else if (host.includes("nowcoder.com")) {
    platform = "nowcoder";
    title = (document.querySelector(".job-title, h1")?.innerText || "").trim();
    company = (document.querySelector(".company-name, .com-name")?.innerText || "").trim();
    salary = (document.querySelector(".job-salary, .salary")?.innerText || "").trim();
    jdText = (document.querySelector(".job-detail, .content-box, .detail-box")?.innerText || "").trim();
  } else if (host.includes("shixiseng.com")) {
    platform = "shixiseng";
    title = (document.querySelector(".new_job_name, h1")?.innerText || "").trim();
    company = (document.querySelector(".com_name, .company-name")?.innerText || "").trim();
    salary = (document.querySelector(".job_money, .salary")?.innerText || "").trim();
    location = (document.querySelector(".job_position, .city")?.innerText || "").trim();
    jdText = (document.querySelector(".job_detail, .dec_content")?.innerText || "").trim();
  } else if (host.includes("liepin.com")) {
    platform = "liepin";
    title = (document.querySelector(".job-title, h1")?.innerText || "").trim();
    company = (document.querySelector(".company-info-box a, .company-name")?.innerText || "").trim();
    salary = (document.querySelector(".salary")?.innerText || "").trim();
    location = (document.querySelector(".location")?.innerText || "").trim();
    jdText = (document.querySelector(".job-intro-content, .job-content")?.innerText || "").trim();
  }

  // 通用后备逻辑
  if (!title) {
    title = (document.querySelector("h1, h2")?.innerText || document.title || "").split(/[-_|]/)[0].trim();
  }
  if (!company) {
    const metaAuthor = document.querySelector('meta[name="author"], meta[property="og:site_name"]');
    company = (metaAuthor?.getAttribute("content") || "").trim();
  }
  if (!jdText) {
    // 寻找包含“职责”或“要求”的长文本段落
    const candidates = Array.from(document.querySelectorAll("div, section, article"));
    let bestBlock = "";
    for (const el of candidates) {
      const text = el.innerText || "";
      if ((text.includes("岗位职责") || text.includes("任职要求") || text.includes("工作内容") || text.includes("任职资格")) && text.length > 100) {
        if (text.length > bestBlock.length && text.length < 10000) {
          bestBlock = text;
        }
      }
    }
    jdText = bestBlock.trim() || document.body.innerText.slice(0, 3000);
  }

  return {
    title: title || "未知职位",
    company: company || "未知公司",
    location: location,
    salary: salary,
    jd_text: jdText,
    source_url: url,
    platform: platform
  };
}

// 监听来自 popup 的调用
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (sender.id === chrome.runtime.id && request.action === "GET_JOB_INFO") {
    sendResponse(extractJobInfo());
  }
  return true;
});
})();
