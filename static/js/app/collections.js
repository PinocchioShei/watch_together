// Collection helpers are shared by video and audio playback.
export function collectionTracks(item, mode) {
  if (!item) return [];
  const urls = mode === "video" ? item.videoTracks : item.audioTracks;
  const fallback = mode === "video" ? item.videoUrl : item.audioUrl;
  return [...new Set((Array.isArray(urls) && urls.length ? urls : [fallback]).filter(Boolean))];
}

export function collectionMode(value) {
  return value === "next" || value === "repeat" ? value : "stop";
}

export function collectionLabel(url, index, originalNames = {}) {
  if (originalNames?.[url]) return `${index + 1}. ${originalNames[url]}`;
  const filename = String(url || "").split("/").pop() || `Item ${index + 1}`;
  try {
    return `${index + 1}. ${decodeURIComponent(filename)}`;
  } catch {
    return `${index + 1}. ${filename}`;
  }
}
