// 通用前端工具：格式化、转义与 URL 规范化。

export function normalizeUrl(url) {
  if (!url) return "";
  try {
    return new URL(url, location.origin).href;
  } catch {
    return String(url);
  }
}

export function toMediaPath(url) {
  if (!url) return "";
  try {
    const parsed = new URL(url, location.origin);
    return parsed.pathname.startsWith("/media/") ? parsed.pathname : "";
  } catch {
    return String(url).startsWith("/media/") ? String(url) : "";
  }
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)}GB`;
}
