export const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

export const numberText = (value) => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(Number(value || 0));
export const joinValues = (values) => Array.isArray(values) ? values.join("、") : "";
export const splitValues = (value) => String(value || "").split(/[,，、;；\n]+/).map((item) => item.trim()).filter(Boolean);

export const optionalPositiveInteger = (element) => {
  const raw = String(element.value || "").trim();
  return raw ? Number(raw) : null;
};

export function formatDateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
    hour12: false, timeZone: "Asia/Shanghai"
  }).format(parsed);
}

export function relativeFreshness(value) {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "更新时间未知";
  const minutes = Math.max(0, Math.round((Date.now() - timestamp) / 60000));
  if (minutes < 1) return "数据刚刚更新";
  if (minutes < 60) return `${minutes} 分钟前更新`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前更新`;
  return `${Math.round(hours / 24)} 天前更新`;
}

export function safeExternalUrl(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password) return null;
    return url.href;
  } catch (_) {
    return null;
  }
}

// Artifacts are served by this application. Escaping HTML alone does not make
// javascript:, protocol-relative, or external download URLs safe to navigate.
export function safeArtifactUrl(raw) {
  const value = String(raw || '');
  return /^\/(?:resume-draft\/\d+\/(?:pdf|docx)|preparation\/\d+|project-workshop\/\d+\/preview)$/.test(value) ? value : null;
}
