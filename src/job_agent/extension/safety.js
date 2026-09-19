/* Shared by trusted extension contexts only. Never forward credentials to a page. */
globalThis.PjaSafety = Object.freeze({
  localBase(raw) {
    const url = new URL(raw || "http://127.0.0.1:8787");
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname) ||
        url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      throw new Error("本地地址仅支持 http://127.0.0.1:端口 或 http://localhost:端口");
    }
    return url.origin;
  },
  pageUrl(raw) {
    const url = new URL(raw);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      throw new Error("请在普通招聘网页使用此功能");
    }
    return url.href;
  },
  recruitmentUrl(raw) {
    const url = new URL(raw);
    if (url.protocol !== "https:" || url.port || url.username || url.password) {
      throw new Error("预填和验证码仅支持标准 HTTPS 招聘页面，请核实岗位链接");
    }
    return url.href;
  },
  jobId(raw) {
    if (!/^[1-9]\d{0,9}$/.test(String(raw))) throw new Error("请输入工作台中的岗位编号");
    return Number(raw);
  },
});
