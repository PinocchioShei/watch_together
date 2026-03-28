import { normalizeUrl, toMediaPath } from "./js/shared/format.js";
import { clearRoomSession, createTabLock, readRoomSession, saveRoomSession } from "./js/app/session.js";
import { createApiClient } from "./js/app/api-client.js";
import { createAuthModule } from "./js/app/auth-module.js";
import { createLobbyModule } from "./js/app/lobby-module.js";
import { createChatModule } from "./js/app/chat-module.js";

const state = {
  token: sessionStorage.getItem("wt_token") || "",
  me: null,
  roomId: null,
  ws: null,
  suppressVideoEvents: false,
  syncTimer: null,
  controllerUserId: null,
  forceTakeover: false,
  roomDisplayNo: null,
  localOverrideUntil: 0,
  roomMeta: new Map(),
  mediaLibrary: [],
  activeMediaUrl: "",
  activeMediaId: null,
  activeTrackIndex: 0,
  lastSyncedMediaPath: "",
  playMode: "video",
  localActionCounter: 0,
  lastLocalActionId: 0,
  lastServerUpdatedAt: 0,
  wsReconnectTimer: null,
  wsReconnectAttempts: 0,
  wsShouldReconnect: false,
  wsPingTimer: null,
  wsPongTimeout: null,
  roomHealthTimer: null,
  authHealthTimer: null,
};

const tabId = `${Date.now()}_${Math.random().toString(16).slice(2)}`;
const { startTabLock, stopTabLock } = createTabLock(tabId);
const THEME_KEY = "wt_theme";

const authPanel = document.getElementById("authPanel");
const appPanel = document.getElementById("appPanel");
const page = document.querySelector(".page");
const tabLogin = document.getElementById("tabLogin");
const tabRegister = document.getElementById("tabRegister");
const loginForm = document.getElementById("loginForm");
const registerForm = document.getElementById("registerForm");
const authMsg = document.getElementById("authMsg");
const themeToggleBtn = document.getElementById("themeToggleBtn");
const roomList = document.getElementById("roomList");
const createRoomForm = document.getElementById("createRoomForm");
const refreshRoomsBtn = document.getElementById("refreshRooms");
const lobbyStatus = document.getElementById("lobbyStatus");
const lobbyPanel = document.getElementById("lobbyPanel");
const roomPanel = document.getElementById("roomPanel");
const currentRoomTitle = document.getElementById("currentRoomTitle");
const videoPlayer = document.getElementById("videoPlayer");
const audioPlayer = document.getElementById("audioPlayer");
const audioTrackRow = document.getElementById("audioTrackRow");
const audioTrackSelect = document.getElementById("audioTrackSelect");
const modeVideoBtn = document.getElementById("modeVideoBtn");
const modeAudioBtn = document.getElementById("modeAudioBtn");
const refreshMediaBtn = document.getElementById("refreshMediaBtn");
const mediaList = document.getElementById("mediaList");
const mediaStatus = document.getElementById("mediaStatus");
const roomMediaTypeFilter = document.getElementById("roomMediaTypeFilter");
const importForm = document.getElementById("importForm");
const videoFileInput = document.getElementById("videoFileInput");
const importTypeSelect = document.getElementById("importTypeSelect");
const coverFileInput = document.getElementById("coverFileInput");
const importSubmitBtn = importForm ? importForm.querySelector("button[type='submit']") : null;
const importProgressWrap = document.getElementById("importProgressWrap");
const importProgressBar = document.getElementById("importProgressBar");
const importProgressText = document.getElementById("importProgressText");
const statusBar = document.getElementById("statusBar");
const onlineCount = document.getElementById("onlineCount");
const memberList = document.getElementById("memberList");
const userBadge = document.getElementById("userBadge");
const logoutBtn = document.getElementById("logoutBtn");
const leaveRoomBtn = document.getElementById("leaveRoomBtn");
const chatBox = document.getElementById("chatBox");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");

let chatModule = null;
let loadRooms = async () => [];
let tryRestoreRoom = async () => {};
let importInFlight = false;
let guestAuthOpen = false;
let importProcessingTimer = null;
let selectedImportFileStamp = "";
const DEBUG_SYNC = true;

function syncDebug(event, fields = {}) {
  if (!DEBUG_SYNC) return;
  const payload = {
    event,
    ts: new Date().toISOString(),
    roomId: state.roomId,
    user: state.me?.username || "guest",
    mode: state.playMode,
    activeMediaId: state.activeMediaId,
    activeMediaUrl: toMediaPath(state.activeMediaUrl) || state.activeMediaUrl || "",
    ...fields,
  };
  try {
    console.info("[WT_SYNC]", payload);
  } catch {
  }
}

function canonicalMediaPath(url) {
  const path = toMediaPath(url);
  if (!path) return "";
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}

window.addEventListener("beforeunload", stopTabLock);

function applyTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  if (themeToggleBtn) {
    themeToggleBtn.textContent = next === "dark" ? "Light" : "Dark";
  }
}

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  applyTheme(saved === "dark" ? "dark" : "light");
}

if (themeToggleBtn) {
  themeToggleBtn.onclick = () => {
    const now = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    const next = now === "dark" ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  };
}

initTheme();

function setMessage(msg, isError = false) {
  authMsg.textContent = msg || "";
  authMsg.style.color = isError ? "#fca5a5" : "#94a3b8";
}

function setLobbyStatus(msg, isError = false) {
  lobbyStatus.textContent = msg || "";
  lobbyStatus.style.color = isError ? "#fca5a5" : "#94a3b8";
}

function setMediaStatus(msg, isError = false) {
  mediaStatus.textContent = msg || "";
  mediaStatus.style.color = isError ? "#fca5a5" : "#94a3b8";
}

function setImportProgress(percent, text = "") {
  if (!importProgressWrap || !importProgressBar || !importProgressText) return;
  const clamped = Math.max(0, Math.min(100, Number(percent) || 0));
  importProgressWrap.classList.remove("processing");
  importProgressWrap.classList.remove("hidden");
  importProgressBar.style.width = `${clamped}%`;
  importProgressText.textContent = text || `${Math.round(clamped)}%`;
}

function estimateServerProcessingSeconds(file) {
  const sizeMb = Math.max(1, Math.round((Number(file?.size || 0) / 1024 / 1024)));
  const mime = String(file?.type || "").toLowerCase();
  const name = String(file?.name || "").toLowerCase();
  const isAudio = mime.startsWith("audio/") || /\.(mp3|m4a|aac|wav|ogg)$/.test(name);
  if (isAudio) {
    return Math.max(10, Math.min(240, Math.round(sizeMb * 0.35 + 8)));
  }
  return Math.max(20, Math.min(900, Math.round(sizeMb * 0.85 + 18)));
}

function importFileStamp(file) {
  if (!file) return "";
  return `${file.name}::${file.size}::${file.lastModified}`;
}

function expectedStageFromProfile(profile = {}, transcoded = false) {
  const hasVideo = String(profile.videoCodec || "none") !== "none";
  if (!hasVideo) return "Saving";
  return transcoded ? "Transcoding" : "Saving";
}

function startImportProcessingTicker(estimatedSeconds = 0) {
  if (!importProgressWrap || !importProgressBar || !importProgressText) return;
  if (importProcessingTimer) return;
  importProgressWrap.classList.add("processing");
  importProgressWrap.classList.remove("hidden");
  importProgressBar.style.width = "100%";
  const startedAt = Date.now();
  const labels = ["Analyzing", "Transcoding", "Generating cover", "Saving"];
  const phaseSpan = estimatedSeconds > 0
    ? Math.max(8, Math.round(estimatedSeconds / labels.length))
    : 12;
  importProcessingTimer = setInterval(() => {
    const sec = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    const phaseIndex = Math.min(labels.length - 1, Math.floor(sec / phaseSpan));
    const phase = labels[phaseIndex];
    if (estimatedSeconds > 0 && sec <= estimatedSeconds) {
      const left = Math.max(0, estimatedSeconds - sec);
      importProgressText.textContent = `${phase} on server... ${sec}s elapsed, ~${left}s remaining`;
    } else if (estimatedSeconds > 0) {
      importProgressText.textContent = `${phase} on server... ${sec}s elapsed (taking longer than estimate)`;
    } else {
      importProgressText.textContent = `${phase} on server... ${sec}s`;
    }
  }, 900);
}

function stopImportProcessingTicker() {
  if (!importProcessingTimer) return;
  clearInterval(importProcessingTimer);
  importProcessingTimer = null;
}

if (videoFileInput) {
  videoFileInput.addEventListener("change", () => {
    const file = videoFileInput.files && videoFileInput.files[0] ? videoFileInput.files[0] : null;
    selectedImportFileStamp = importFileStamp(file);
  });
}

function hideImportProgress() {
  if (!importProgressWrap || !importProgressBar || !importProgressText) return;
  stopImportProcessingTicker();
  importProgressWrap.classList.add("hidden");
  importProgressWrap.classList.remove("processing");
  importProgressBar.style.width = "0%";
  importProgressText.textContent = "0%";
}

function xhrUpload(url, formData, onServerProcessingStart = null) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let processingStarted = false;
    const uploadStartedAt = Date.now();
    xhr.open("POST", url, true);
    if (state.token) {
      xhr.setRequestHeader("Authorization", `Bearer ${state.token}`);
    }
    xhr.timeout = 15 * 60 * 1000;
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        const p = (ev.loaded / ev.total) * 100;
        const elapsedSec = Math.max(1, (Date.now() - uploadStartedAt) / 1000);
        const speedBps = ev.loaded / elapsedSec;
        const remainBytes = Math.max(0, ev.total - ev.loaded);
        const remainSec = speedBps > 0 ? Math.max(0, Math.round(remainBytes / speedBps)) : 0;
        const etaText = ev.loaded > 1024 * 1024 ? `, ~${remainSec}s remaining` : ", estimating...";
        setImportProgress(
          p,
          `Uploading ${Math.round(p)}% (${Math.round(ev.loaded / 1024 / 1024)} / ${Math.round(ev.total / 1024 / 1024)} MB${etaText})`,
        );
        if (!processingStarted && p >= 100) {
          processingStarted = true;
          if (typeof onServerProcessingStart === "function") onServerProcessingStart();
        }
      } else {
        setImportProgress(0, "Uploading...");
      }
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText || "{}");
      } catch {}
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data || {});
      } else if (xhr.status === 401) {
        reject(new Error("Session expired. Please login again."));
      } else {
        reject(new Error((data && data.detail) || `HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("Network unstable, please retry."));
    xhr.ontimeout = () => reject(new Error("Upload timed out. Network is too slow for this file, please retry on stable Wi-Fi."));
    xhr.send(formData);
  });
}

function formatImportError(detail) {
  const raw = String(detail || "").trim();
  const msg = raw || "Request failed";
  const lower = msg.toLowerCase();
  if (lower.includes("invalid media type")) {
    return "Import failed: invalid type. Choose movie/RJ/ASMR/music/shot.";
  }
  if (lower.includes("only supported video/audio formats")) {
    return "Import failed: unsupported media format. Use mp4/webm/ogg/mov/mp3/aac/wav/m4a.";
  }
  if (lower.includes("file too large")) {
    return "Import failed: file too large (max 1.5GB).";
  }
  if (lower.includes("upload timed out") || lower.includes("network unstable")) {
    return "Import failed: upload connection timed out. Large files need a stable network (prefer Wi-Fi).";
  }
  if (lower.includes("cannot parse uploaded media") || lower.includes("invalid media metadata")) {
    return `Import failed: cannot parse media file. ${msg}`;
  }
  if (lower.includes("uploaded file has no audio or video stream")) {
    return "Import failed: file has no playable audio/video stream.";
  }
  if (lower.includes("invalid cover image")) {
    return "Import failed: cover image is invalid. Use jpg/png.";
  }
  if (lower.includes("default cover") || lower.includes("failed to create default cover")) {
    return `Import failed: server cover fallback error. ${msg}`;
  }
  if (lower.includes("transcode failed") || lower.includes("audio import failed")) {
    return `Import failed during ffmpeg processing. ${msg}`;
  }
  return `Import failed: ${msg}`;
}

function renderMembers(payload) {
  const members = Array.isArray(payload?.members) ? payload.members : [];
  onlineCount.textContent = String(payload?.onlineCount ?? members.length);
  memberList.innerHTML = "";
  if (!members.length) {
    const li = document.createElement("li");
    li.textContent = "No one online.";
    memberList.appendChild(li);
    return;
  }
  members.forEach((member) => {
    const li = document.createElement("li");
    if (member.isOwner) li.classList.add("owner");
    const ownerTag = member.isOwner ? " (owner)" : "";
    const meTag = state.me && member.id === state.me.id ? " (you)" : "";
    li.textContent = `${member.username}${ownerTag}${meTag}`;
    memberList.appendChild(li);
  });
}

function stopRoomHealthCheck() {
  if (state.roomHealthTimer) {
    clearInterval(state.roomHealthTimer);
    state.roomHealthTimer = null;
  }
}

function stopAuthHealthCheck() {
  if (state.authHealthTimer) {
    clearInterval(state.authHealthTimer);
    state.authHealthTimer = null;
  }
}

function startAuthHealthCheck() {
  stopAuthHealthCheck();
  state.authHealthTimer = setInterval(async () => {
    if (!state.token) return;
    try {
      await api("/api/me", { method: "GET" });
    } catch {
      // 401 path is handled inside api client and will force logout + popup.
    }
  }, 8000);
}

function startRoomHealthCheck() {
  stopRoomHealthCheck();
  state.roomHealthTimer = setInterval(async () => {
    if (!state.roomId) return;
    try {
      await api(`/api/rooms/${state.roomId}/state`, { method: "GET" });
    } catch (err) {
      const msg = String(err?.message || "").toLowerCase();
      if (msg.includes("room not found") || msg.includes("join room first") || msg.includes("not in room")) {
        alert("This room is no longer available. Returning to room list.");
        safeCloseWs();
        enterLobbyView();
        setLobbyStatus("Room no longer exists.", true);
        loadRooms().catch(() => {});
      }
    }
  }, 10000);
}

function getActivePlayer() {
  return state.playMode === "audio" ? audioPlayer : videoPlayer;
}

function getInactivePlayer() {
  return state.playMode === "audio" ? videoPlayer : audioPlayer;
}

function mediaUrlsMatch(left, right) {
  const leftPath = canonicalMediaPath(left || "");
  const rightPath = canonicalMediaPath(right || "");
  if (leftPath && rightPath) {
    return leftPath === rightPath;
  }
  return normalizeUrl(left || "") === normalizeUrl(right || "");
}

function getPureAudioTracks(item) {
  if (!item || item.videoUrl || !Array.isArray(item.audioTracks)) {
    return [];
  }
  return item.audioTracks.filter((trackUrl) => !!trackUrl);
}

function getTrackIndexForItem(item, url = "", fallbackIndex = null) {
  const tracks = getPureAudioTracks(item);
  if (!tracks.length) {
    return 0;
  }
  const matchedIndex = tracks.findIndex((trackUrl) => mediaUrlsMatch(trackUrl, url));
  if (matchedIndex >= 0) {
    return matchedIndex;
  }
  const numeric = Number(fallbackIndex);
  if (Number.isInteger(numeric)) {
    return Math.max(0, Math.min(tracks.length - 1, numeric));
  }
  return 0;
}

function resolveMediaUrlForMode(item, mode, options = {}) {
  if (!item) return "";
  if (mode === "audio") {
    const tracks = getPureAudioTracks(item);
    if (tracks.length) {
      return tracks[getTrackIndexForItem(item, "", options.trackIndex ?? 0)] || "";
    }
    return item.audioUrl || "";
  }
  return item.videoUrl || "";
}

function mediaItemMatchesModeUrl(item, url, mode) {
  if (!item || !url) {
    return false;
  }
  if (mode === "audio") {
    if (mediaUrlsMatch(item.audioUrl || "", url)) {
      return true;
    }
    return getPureAudioTracks(item).some((trackUrl) => mediaUrlsMatch(trackUrl, url));
  }
  return mediaUrlsMatch(item.videoUrl || "", url);
}

function findMediaItemByUrl(url, mode = state.playMode) {
  return state.mediaLibrary.find((item) => mediaItemMatchesModeUrl(item, url, mode)) || null;
}

function getActiveMediaItem() {
  if (state.activeMediaId != null) {
    return state.mediaLibrary.find((item) => item.id === state.activeMediaId) || null;
  }
  if (!state.activeMediaUrl) {
    return null;
  }
  return findMediaItemByUrl(state.activeMediaUrl, state.playMode);
}

function getSyncSourceUrl(data, mode) {
  if (mode === "audio") {
    return data.videoUrl || data.audioUrl || "";
  }
  return data.videoUrl || "";
}

function getTrackLabel(trackUrl, index) {
  const fallback = `Track ${index + 1}`;
  const filename = String(trackUrl || "").split("/").pop() || fallback;
  try {
    return `${index + 1}. ${decodeURIComponent(filename)}`;
  } catch {
    return `${index + 1}. ${filename}`;
  }
}

function renderAudioTrackSelector() {
  if (!audioTrackRow || !audioTrackSelect) {
    return;
  }
  const activeItem = state.playMode === "audio" ? getActiveMediaItem() : null;
  const tracks = getPureAudioTracks(activeItem);
  const shouldShow = tracks.length > 1;
  audioTrackRow.classList.toggle("hidden", !shouldShow);
  audioTrackSelect.disabled = !shouldShow;
  audioTrackSelect.innerHTML = "";
  if (!shouldShow) {
    return;
  }
  const selectedIndex = getTrackIndexForItem(activeItem, state.activeMediaUrl, state.activeTrackIndex);
  state.activeTrackIndex = selectedIndex;
  tracks.forEach((trackUrl, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = getTrackLabel(trackUrl, index);
    option.selected = index === selectedIndex;
    audioTrackSelect.appendChild(option);
  });
}

function normalizeMediaType(value) {
  const raw = String(value || "").trim();
  if (!raw) return "movie";
  const lower = raw.toLowerCase();
  if (lower === "rj") return "RJ";
  if (lower === "asmr") return "ASMR";
  if (lower === "music") return "music";
  if (lower === "shot") return "shot";
  if (lower === "movie") return "movie";
  return raw;
}

function getRoomMediaFilterType() {
  return roomMediaTypeFilter ? (roomMediaTypeFilter.value || "movie") : "movie";
}

function getVisibleMediaItems() {
  const selectedType = getRoomMediaFilterType();
  if (selectedType === "all") return state.mediaLibrary;
  return state.mediaLibrary.filter((item) => normalizeMediaType(item.type) === selectedType);
}

function ensureFilterShowsActiveItem(source = "") {
  if (!roomMediaTypeFilter) return;
  const selectedType = getRoomMediaFilterType();
  if (selectedType === "all") return;
  const active = getActiveMediaItem();
  if (!active) return;
  const activeType = String(active.type || "movie");
  if (normalizeMediaType(activeType) !== selectedType) {
    roomMediaTypeFilter.value = "all";
    if (source === "remote") {
      setMediaStatus("Synced media is outside current filter; switched filter to all.");
    }
  }
}

function setPlayMode(mode, { silent = false } = {}) {
  const nextMode = mode === "audio" ? "audio" : "video";
  if (state.playMode === nextMode) {
    return;
  }
  const activeItem = getActiveMediaItem();
  if (nextMode === "video" && activeItem && !activeItem.videoUrl) {
    setMediaStatus("Current work has no video track.", true);
    return;
  }
  state.playMode = nextMode;
  roomPanel.classList.toggle("audio-mode", nextMode === "audio");
  modeVideoBtn.classList.toggle("active", nextMode === "video");
  modeAudioBtn.classList.toggle("active", nextMode === "audio");

  const fromPlayer = nextMode === "audio" ? videoPlayer : audioPlayer;
  const toPlayer = getActivePlayer();
  const wasPlaying = !fromPlayer.paused;
  const fromTime = Number(fromPlayer.currentTime || 0);
  const targetUrl = resolveMediaUrlForMode(activeItem, nextMode, { trackIndex: state.activeTrackIndex });
  if (targetUrl && normalizeUrl(toPlayer.currentSrc || toPlayer.src) !== normalizeUrl(targetUrl)) {
    toPlayer.src = targetUrl;
    state.activeMediaUrl = targetUrl;
    state.lastSyncedMediaPath = toMediaPath(targetUrl) || state.lastSyncedMediaPath;
  }
  if (toPlayer.currentSrc) {
    toPlayer.currentTime = fromTime;
  }
  fromPlayer.pause();
  if (wasPlaying && toPlayer.currentSrc) {
    toPlayer.play().catch(() => {});
  }
  renderMediaLibrary();
  renderAudioTrackSelector();
  if (!silent) {
    state.forceTakeover = true;
    state.localOverrideUntil = Date.now() + 1200;
    sendSync();
  }
}

function renderMediaLibrary() {
  mediaList.innerHTML = "";
  if (!state.mediaLibrary.length) {
    const li = document.createElement("li");
    li.textContent = "No media files in library.";
    mediaList.appendChild(li);
    return;
  }
  const visibleItems = getVisibleMediaItems();
  if (!visibleItems.length) {
    const li = document.createElement("li");
    li.textContent = "No media files under current type filter.";
    mediaList.appendChild(li);
    return;
  }
  visibleItems.forEach((item) => {
    const li = document.createElement("li");
    li.className = "media-item";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "media-card";
    const modeUrl = resolveMediaUrlForMode(item, state.playMode, { trackIndex: 0 });
    const playable = !!modeUrl;
    if (
      (state.activeMediaId && state.activeMediaId === item.id) ||
      (state.activeMediaUrl && mediaItemMatchesModeUrl(item, state.activeMediaUrl, state.playMode))
    ) {
      btn.classList.add("active");
    }
    const coverWrap = document.createElement("div");
    coverWrap.className = "media-cover-wrap";

    if (item.coverUrl) {
      const img = document.createElement("img");
      img.className = "media-cover-img";
      img.src = item.coverUrl;
      img.alt = item.name || "cover";
      img.loading = "lazy";
      coverWrap.appendChild(img);
    } else {
      const fallback = document.createElement("div");
      fallback.className = "media-cover-fallback";
      fallback.textContent = "No Cover";
      coverWrap.appendChild(fallback);
    }

    const overlay = document.createElement("div");
    overlay.className = "media-cover-overlay";
    const title = document.createElement("div");
    title.className = "media-cover-title";
    title.textContent = item.name || "Untitled";
    overlay.appendChild(title);
    coverWrap.appendChild(overlay);
    btn.appendChild(coverWrap);

    const nameLine = document.createElement("div");
    nameLine.className = "media-name-line";
    nameLine.textContent = item.name || "Untitled";
    btn.appendChild(nameLine);

    if (!playable) {
      btn.disabled = true;
      btn.title = state.playMode === "audio" ? "No audio track for this item" : "No video track";
    }
    btn.onclick = async () => {
      if (!playable) return;
      const requestedTrackIndex =
        state.playMode === "audio" && state.activeMediaId && state.activeMediaId === item.id
          ? getTrackIndexForItem(item, state.activeMediaUrl, state.activeTrackIndex)
          : 0;
      const selectedUrl = resolveMediaUrlForMode(item, state.playMode, { trackIndex: requestedTrackIndex });
      state.forceTakeover = true;
      state.localOverrideUntil = Date.now() + 1200;
      state.activeMediaId = item.id || null;
      state.activeTrackIndex = state.playMode === "audio" ? requestedTrackIndex : 0;
      state.activeMediaUrl = selectedUrl;
      state.lastSyncedMediaPath = toMediaPath(selectedUrl) || state.lastSyncedMediaPath;
      const player = getActivePlayer();
      player.src = selectedUrl;
      player.currentTime = 0;
      await player.play().catch(() => {});
      sendSync();
      renderMediaLibrary();
      renderAudioTrackSelector();
      setMediaStatus(`Selected: ${item.name} (${state.playMode} mode)`);
    };
    li.appendChild(btn);
    mediaList.appendChild(li);
  });
}

async function loadMediaLibrary() {
  const data = await api("/api/media", { method: "GET" });
  state.mediaLibrary = data.items || [];
  if (state.activeMediaUrl) {
    const found = getActiveMediaItem() || findMediaItemByUrl(state.activeMediaUrl, state.playMode);
    state.activeMediaId = found?.id || null;
    state.activeTrackIndex = found ? getTrackIndexForItem(found, state.activeMediaUrl, state.activeTrackIndex) : 0;
  }
  ensureFilterShowsActiveItem();
  renderMediaLibrary();
  renderAudioTrackSelector();
}

function showTab(which) {
  if (which === "login") {
    tabLogin.classList.add("active");
    tabRegister.classList.remove("active");
    loginForm.classList.remove("hidden");
    registerForm.classList.add("hidden");
  } else {
    tabRegister.classList.add("active");
    tabLogin.classList.remove("active");
    registerForm.classList.remove("hidden");
    loginForm.classList.add("hidden");
  }
}

const api = createApiClient({
  state,
  safeCloseWs,
  stopTabLock,
  setAuthMode,
  setMessage,
  showTab,
});

function setAuthMode(loggedIn) {
  authPanel.classList.toggle("hidden", loggedIn);
  appPanel.classList.toggle("hidden", !loggedIn);
  page.classList.toggle("auth-only", !loggedIn);
  if (loggedIn && state.me) {
    page.classList.remove("guest-mode", "guest-idle", "guest-auth-open");
    authPanel.classList.remove("auth-collapsed");
    userBadge.classList.remove("hidden");
    logoutBtn.classList.remove("hidden");
    userBadge.textContent = `User: ${state.me.username}`;
    enterLobbyView(false);
    startAuthHealthCheck();
  } else {
    stopAuthHealthCheck();
    stopTabLock();
    userBadge.classList.add("hidden");
    logoutBtn.classList.add("hidden");
    guestAuthOpen = false;
    authPanel.classList.remove("hidden");
    authPanel.classList.add("auth-collapsed");
    page.classList.add("guest-mode", "guest-idle");
    page.classList.remove("guest-auth-open");
    page.classList.remove("in-room");
  }
}

function toggleGuestAuth(open) {
  if (state.token) return;
  guestAuthOpen = !!open;
  authPanel.classList.toggle("auth-collapsed", !guestAuthOpen);
  page.classList.toggle("guest-auth-open", guestAuthOpen);
  page.classList.toggle("guest-idle", !guestAuthOpen);
  if (guestAuthOpen) {
    const firstInput = loginForm?.querySelector("input");
    if (firstInput) setTimeout(() => firstInput.focus(), 180);
  }
}

document.addEventListener("click", (ev) => {
  if (state.token) return;
  if (!page.classList.contains("guest-mode")) return;
  const target = ev.target;
  if (!(target instanceof Element)) return;
  if (authPanel.contains(target)) return;
  if (target.closest("button,input,select,textarea,label,form,a")) return;
  toggleGuestAuth(!guestAuthOpen);
});

function enterLobbyView(clearSavedRoom = true) {
  lobbyPanel.classList.remove("hidden");
  roomPanel.classList.add("hidden");
  page.classList.remove("in-room");
  state.roomId = null;
  state.controllerUserId = null;
  state.roomDisplayNo = null;
  state.forceTakeover = false;
  state.activeMediaUrl = "";
  state.activeMediaId = null;
  state.activeTrackIndex = 0;
  state.lastSyncedMediaPath = "";
  state.localOverrideUntil = 0;
  state.localActionCounter = 0;
  state.lastLocalActionId = 0;
  state.lastServerUpdatedAt = 0;
  renderMembers({ onlineCount: 0, members: [] });
  stopRoomHealthCheck();
  videoPlayer.pause();
  audioPlayer.pause();
  videoPlayer.removeAttribute("src");
  audioPlayer.removeAttribute("src");
  videoPlayer.load();
  audioPlayer.load();
  state.playMode = "audio";
  setPlayMode("video", { silent: true });
  currentRoomTitle.textContent = "Room";
  if (clearSavedRoom) {
    clearRoomSession();
  }
}

function enterRoomView(roomName, roomId, displayNo) {
  lobbyPanel.classList.add("hidden");
  roomPanel.classList.remove("hidden");
  page.classList.add("in-room");
  state.roomDisplayNo = displayNo;
  currentRoomTitle.textContent = `Room #${displayNo}: ${roomName}`;
}

function safeCloseWs() {
  state.wsShouldReconnect = false;
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = null;
  }
  if (state.wsPingTimer) {
    clearInterval(state.wsPingTimer);
    state.wsPingTimer = null;
  }
  if (state.wsPongTimeout) {
    clearTimeout(state.wsPongTimeout);
    state.wsPongTimeout = null;
  }
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
}

async function joinRoom(roomId, roomName, displayNo = null, roomPassword = null, options = {}) {
  const { allowPrompt = true } = options;
  const roomMeta = state.roomMeta.get(roomId);
  let password = typeof roomPassword === "string" ? roomPassword : "";
  if (!password && roomMeta?.hasPassword) {
    if (allowPrompt) {
      const input = prompt(`Enter password for room "${roomName}"`);
      if (input === null) {
        throw new Error("Join cancelled");
      }
      password = input;
    }
  }

  await api(`/api/rooms/${roomId}/join`, {
    method: "POST",
    body: JSON.stringify({ password }),
  });
  state.roomId = roomId;
  const finalDisplayNo = displayNo ?? state.roomMeta.get(roomId)?.displayNo ?? 0;
  saveRoomSession(roomId, roomName, finalDisplayNo, password);
  enterRoomView(roomName, roomId, finalDisplayNo);
  statusBar.textContent = "Joining room...";
  setMediaStatus("Loading media library...");
  await loadMediaLibrary();
  setMediaStatus("Select a media file to play and sync.");
  safeCloseWs();
  state.wsShouldReconnect = true;
  connectWs(roomId);
  startRoomHealthCheck();
  const rs = await api(`/api/rooms/${roomId}/state`, { method: "GET" });
  applyRemoteState(rs, "server");
}

function wsUrl(roomId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const token = encodeURIComponent(state.token || "");
  return `${proto}://${location.host}/ws/rooms/${roomId}?token=${token}`;
}

function connectWs(roomId) {
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = null;
  }
  const socket = new WebSocket(wsUrl(roomId));
  state.ws = socket;

  socket.onopen = () => {
    state.wsReconnectAttempts = 0;
    if (state.wsPingTimer) {
      clearInterval(state.wsPingTimer);
    }
    state.wsPingTimer = setInterval(() => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      try {
        state.ws.send(JSON.stringify({ type: "ping" }));
      } catch {
      }
      if (state.wsPongTimeout) {
        clearTimeout(state.wsPongTimeout);
      }
      state.wsPongTimeout = setTimeout(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          state.ws.close();
        }
      }, 12000);
    }, 15000);
    statusBar.textContent = "Connected. Sync active.";
  };

  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "state") {
      applyRemoteState(data, data.by || "peer");
    } else if (data.type === "members") {
      renderMembers(data);
    } else if (data.type === "pong") {
      if (state.wsPongTimeout) {
        clearTimeout(state.wsPongTimeout);
        state.wsPongTimeout = null;
      }
    } else if (data.type === "error") {
      const msg = String(data.message || "Room sync error.");
      statusBar.textContent = msg;
      if (msg.toLowerCase().includes("signed in on another device")) {
        safeCloseWs();
        stopTabLock();
        state.token = "";
        sessionStorage.removeItem("wt_token");
        state.me = null;
        state.roomId = null;
        state.roomDisplayNo = null;
        state.controllerUserId = null;
        state.forceTakeover = false;
        state.localOverrideUntil = 0;
        clearRoomSession();
        setAuthMode(false);
        setMessage("This account was signed in elsewhere. Please login again.", true);
      }
    } else if (data.type === "room_deleted") {
      const by = data.by || "owner";
      const reason = by === "admin" ? "admin" : by;
      alert(`Room "${data.roomName || ""}" was closed by ${reason}. Returning to room list.`);
      safeCloseWs();
      enterLobbyView();
      setLobbyStatus(`Room "${data.roomName || ""}" has been deleted.`, true);
      loadRooms().catch(() => {});
    } else if (data.type === "chat") {
      chatModule?.appendChat(data.by, data.message, data.sentAt);
    }
  };

  socket.onclose = () => {
    if (state.wsPingTimer) {
      clearInterval(state.wsPingTimer);
      state.wsPingTimer = null;
    }
    if (state.wsPongTimeout) {
      clearTimeout(state.wsPongTimeout);
      state.wsPongTimeout = null;
    }
    if (state.ws === socket) {
      state.ws = null;
    }
    if (state.wsShouldReconnect && state.roomId === roomId) {
      state.wsReconnectAttempts += 1;
      const delayMs = Math.min(10000, 1000 * 2 ** Math.min(state.wsReconnectAttempts, 3));
      statusBar.textContent = `Disconnected from room sync. Reconnecting in ${Math.round(delayMs / 1000)}s...`;
      state.wsReconnectTimer = setTimeout(() => {
        if (state.wsShouldReconnect && state.roomId === roomId) {
          connectWs(roomId);
        }
      }, delayMs);
      return;
    }
    statusBar.textContent = "Disconnected from room sync.";
  };
}

function applyRemoteState(data, by) {
  const remoteMode = data.playMode === "audio" ? "audio" : "video";
  const incomingSourceUrl = getSyncSourceUrl(data, remoteMode);
  if (state.activeMediaUrl && !incomingSourceUrl) {
    syncDebug("remote_state_ignored_empty_media", {
      by,
      actionId: data.actionId,
      currentTime: Number(data.currentTime || 0),
      isPlaying: !!data.isPlaying,
    });
    return;
  }
  const updatedAtMs = Date.parse(data.updatedAt || "") || 0;
  if (updatedAtMs && updatedAtMs < state.lastServerUpdatedAt) {
    return;
  }

  if (
    by === state.me?.username &&
    typeof data.actionId === "number" &&
    data.actionId < state.lastLocalActionId
  ) {
    return;
  }

  if (
    by === state.me?.username &&
    typeof data.actionId === "number" &&
    data.actionId === state.lastLocalActionId
  ) {
    if (updatedAtMs) {
      state.lastServerUpdatedAt = Math.max(state.lastServerUpdatedAt, updatedAtMs);
    }
    return;
  }

  if (by !== state.me?.username && Date.now() < state.localOverrideUntil) {
    return;
  }

  if (state.playMode !== remoteMode) {
    setPlayMode(remoteMode, { silent: true });
  }

  state.suppressVideoEvents = true;

  if (Object.prototype.hasOwnProperty.call(data, "controllerUserId")) {
    state.controllerUserId = data.controllerUserId;
  }

  if (updatedAtMs) {
    state.lastServerUpdatedAt = Math.max(state.lastServerUpdatedAt, updatedAtMs);
  }

  const incomingUrl = normalizeUrl(incomingSourceUrl || "");
  const incomingPathKey = canonicalMediaPath(incomingSourceUrl || "");
  const incomingMediaPath = toMediaPath(incomingSourceUrl || "") || "";
  if (incomingMediaPath) {
    state.lastSyncedMediaPath = incomingMediaPath;
  }
  const player = getActivePlayer();
  const desiredTime = Number(data.currentTime || 0);
  const desiredPlaying = !!data.isPlaying;
  if (!incomingUrl) {
    state.activeMediaUrl = "";
    state.activeMediaId = null;
    state.activeTrackIndex = 0;
    renderMediaLibrary();
    renderAudioTrackSelector();
  }
  const currentUrl = normalizeUrl(player.getAttribute("src") || player.currentSrc || "");
  const currentPathKey = canonicalMediaPath(player.getAttribute("src") || player.currentSrc || "");
  let sourceChanged = false;
  if (incomingUrl && currentUrl !== incomingUrl && incomingPathKey !== currentPathKey) {
    state.activeMediaUrl = incomingSourceUrl;
    const media = findMediaItemByUrl(incomingSourceUrl, remoteMode);
    state.activeMediaId = media?.id || null;
    state.activeTrackIndex = media ? getTrackIndexForItem(media, incomingSourceUrl, data.trackIndex) : 0;
    player.src = incomingUrl;
    sourceChanged = true;
    ensureFilterShowsActiveItem("remote");
    renderMediaLibrary();
    renderAudioTrackSelector();
  } else if (incomingUrl) {
    const media = state.activeMediaId != null
      ? state.mediaLibrary.find((item) => item.id === state.activeMediaId) || findMediaItemByUrl(incomingSourceUrl, remoteMode)
      : findMediaItemByUrl(incomingSourceUrl, remoteMode);
    state.activeMediaId = media?.id || null;
    state.activeMediaUrl = incomingSourceUrl;
    state.activeTrackIndex = media ? getTrackIndexForItem(media, incomingSourceUrl, data.trackIndex) : 0;
    renderAudioTrackSelector();
  }

  syncDebug("remote_state_applied", {
    by,
    actionId: data.actionId,
    incomingMode: remoteMode,
    incomingVideoUrl: incomingMediaPath,
    trackIndex: data.trackIndex,
    playerCurrentSrc: toMediaPath(player.currentSrc || player.src || "") || "",
    currentTime: Number(data.currentTime || 0),
    isPlaying: !!data.isPlaying,
    controllerUserId: data.controllerUserId,
  });

  const applySeekAndPlayback = () => {
    const drift = Math.abs((player.currentTime || 0) - desiredTime);
    let jumpedBySeek = false;
    if (drift > 0.35) {
      jumpedBySeek = true;
      try {
        player.currentTime = desiredTime;
      } catch {
      }
    }

    if (desiredPlaying) {
      const ensurePlay = () => {
        player.play().catch(() => {
          statusBar.textContent = "Playback blocked by browser policy. Click play once to enable sync playback.";
        });
      };
      if (jumpedBySeek) {
        player.addEventListener("seeked", ensurePlay, { once: true });
        setTimeout(ensurePlay, 120);
      } else {
        ensurePlay();
      }
    } else {
      player.pause();
    }

    const controllerText = state.controllerUserId === state.me?.id ? "you" : `user#${state.controllerUserId || "?"}`;
    statusBar.textContent = `Synced by ${by}. t=${desiredTime.toFixed(1)}s, controller: ${controllerText}`;
    setTimeout(() => {
      state.suppressVideoEvents = false;
    }, 260);
  };

  const shouldWaitForMediaReady = sourceChanged || player.readyState < 1;
  if (shouldWaitForMediaReady) {
    let done = false;
    const finalize = () => {
      if (done) return;
      done = true;
      applySeekAndPlayback();
    };
    player.addEventListener("loadedmetadata", finalize, { once: true });
    player.addEventListener("canplay", finalize, { once: true });
    setTimeout(finalize, 900);
    return;
  }

  applySeekAndPlayback();
}

function sendSync() {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !state.roomId) return;
  const canPush =
    state.forceTakeover ||
    state.controllerUserId === null ||
    (state.me && state.controllerUserId === state.me.id);
  if (!canPush) return;
  const actionId = ++state.localActionCounter;
  state.lastLocalActionId = actionId;
  const player = getActivePlayer();
  const activeItem = getActiveMediaItem();
  const fallbackMediaPath = toMediaPath(resolveMediaUrlForMode(activeItem, state.playMode, { trackIndex: state.activeTrackIndex }));
  const mediaPath =
    toMediaPath(player.currentSrc) ||
    toMediaPath(player.src) ||
    toMediaPath(state.activeMediaUrl) ||
    fallbackMediaPath ||
    state.lastSyncedMediaPath;
  if (!mediaPath) {
    syncDebug("send_sync_skip_no_media", {
      actionId,
      canPush,
      playerSrc: player.src || "",
      playerCurrentSrc: player.currentSrc || "",
    });
    return;
  }
  const payload = {
    type: "sync",
    actionId,
    forceTakeover: state.forceTakeover,
    playMode: state.playMode,
    videoUrl: mediaPath,
    currentTime: Number(player.currentTime || 0),
    isPlaying: !player.paused,
  };
  const audioTracks = getPureAudioTracks(activeItem);
  if (state.playMode === "audio" && audioTracks.length) {
    payload.trackIndex = getTrackIndexForItem(activeItem, mediaPath, state.activeTrackIndex);
  }
  syncDebug("send_sync", {
    actionId,
    forceTakeover: !!state.forceTakeover,
    canPush,
    payloadVideoUrl: payload.videoUrl,
    payloadTrackIndex: payload.trackIndex,
    payloadMode: payload.playMode,
    payloadTime: payload.currentTime,
    payloadPlaying: payload.isPlaying,
  });
  state.ws.send(JSON.stringify(payload));
  state.forceTakeover = false;
}

function scheduleSync() {
  if (state.suppressVideoEvents) return;
  clearTimeout(state.syncTimer);
  state.syncTimer = setTimeout(sendSync, state.forceTakeover ? 20 : 60);
}

function onLocalPlaybackAction(event) {
  if (event && event.currentTarget !== getActivePlayer()) {
    return;
  }
  if (state.suppressVideoEvents) {
    return;
  }
  if (event && event.isTrusted === false) {
    syncDebug("skip_untrusted_playback_event", {
      eventType: event.type,
    });
    return;
  }
  state.forceTakeover = true;
  state.localOverrideUntil = Date.now() + 450;
  scheduleSync();
}

tabLogin.onclick = () => showTab("login");
tabRegister.onclick = () => showTab("register");

importForm.onsubmit = async (e) => {
  e.preventDefault();
  if (importInFlight) {
    setMediaStatus("Upload in progress, please wait...", true);
    setLobbyStatus("Upload in progress, please wait...", true);
    return;
  }
  if (!videoFileInput.files || !videoFileInput.files[0]) {
    setMediaStatus("Choose a local media file first.", true);
    setLobbyStatus("Choose a local media file first.", true);
    return;
  }
  if (!importTypeSelect.value) {
    setMediaStatus("Choose media type first.", true);
    setLobbyStatus("Choose media type first.", true);
    return;
  }
  try {
    importInFlight = true;
    if (importSubmitBtn) importSubmitBtn.disabled = true;
    hideImportProgress();
    setImportProgress(0, "Preparing upload...");
    setMediaStatus("Importing media into library...");
    setLobbyStatus("Importing media into library...");
    const fd = new FormData();
    const selectedFile = videoFileInput.files[0];
    const currentStamp = importFileStamp(selectedFile);
    if (selectedImportFileStamp && currentStamp !== selectedImportFileStamp) {
      setMediaStatus("Selected file changed during upload preparation. Please submit again.", true);
      setLobbyStatus("Selected file changed during upload preparation. Please submit again.", true);
      return;
    }
    const estimatedSeconds = estimateServerProcessingSeconds(selectedFile);
    fd.append("file", selectedFile);
    fd.append("media_type", importTypeSelect.value);
    if (coverFileInput.files && coverFileInput.files[0]) {
      fd.append("cover", coverFileInput.files[0]);
    }
    const upload = await xhrUpload("/api/upload-video", fd, () => {
      startImportProcessingTicker(estimatedSeconds);
      setMediaStatus(`Upload finished. Server is processing/transcoding media (~${estimatedSeconds}s estimate), please wait...`);
      setLobbyStatus(`Upload finished. Server is processing/transcoding media (~${estimatedSeconds}s estimate), please wait...`);
    });
    stopImportProcessingTicker();
    const profile = upload.profile || {};
    const expectedStage = expectedStageFromProfile(profile, !!upload.transcoded);
    setImportProgress(100, `Server processing complete (${expectedStage}). Finalizing...`);
    videoFileInput.value = "";
    coverFileInput.value = "";
    const modeText = upload.videoUrl ? "video/audio" : "audio-only";
    const transcodeText = upload.transcoded ? "transcoded" : "direct import";
    setMediaStatus(
      `Imported (${modeText}, ${profile.container || "unknown"}/${profile.videoCodec || "none"}/${profile.audioCodec || "none"}, ${transcodeText}).`,
    );
    setLobbyStatus("Import complete. You can now join a room and play it.");
    await loadMediaLibrary();
    setTimeout(hideImportProgress, 1200);
  } catch (err) {
    hideImportProgress();
    const msg = formatImportError(err?.message);
    setMediaStatus(msg, true);
    setLobbyStatus(msg, true);
  } finally {
    importInFlight = false;
    if (importSubmitBtn) importSubmitBtn.disabled = false;
  }
};

refreshMediaBtn.onclick = async () => {
  try {
    setMediaStatus("Refreshing media library...");
    await loadMediaLibrary();
    setMediaStatus("Media library updated.");
  } catch (err) {
    setMediaStatus(err.message, true);
  }
};

modeVideoBtn.onclick = () => {
  setPlayMode("video");
  setMediaStatus("Switched to video mode.");
};

modeAudioBtn.onclick = () => {
  if (state.activeMediaId) {
    const item = state.mediaLibrary.find((it) => it.id === state.activeMediaId);
    if (item && !item.audioUrl) {
      setMediaStatus("Current media has no audio track.", true);
      return;
    }
  }
  setPlayMode("audio");
  setMediaStatus("Switched to audio mode.");
};

if (audioTrackSelect) {
  audioTrackSelect.onchange = async () => {
    if (state.playMode !== "audio") {
      return;
    }
    const activeItem = getActiveMediaItem();
    const tracks = getPureAudioTracks(activeItem);
    if (tracks.length < 2) {
      return;
    }
    const requestedIndex = Number(audioTrackSelect.value || 0);
    const nextIndex = getTrackIndexForItem(activeItem, tracks[requestedIndex] || "", requestedIndex);
    const nextUrl = tracks[nextIndex] || "";
    if (!nextUrl || mediaUrlsMatch(nextUrl, state.activeMediaUrl)) {
      renderAudioTrackSelector();
      return;
    }
    const player = audioPlayer;
    const wasPlaying = !player.paused;
    state.forceTakeover = true;
    state.localOverrideUntil = Date.now() + 1200;
    state.activeMediaId = activeItem?.id || null;
    state.activeTrackIndex = nextIndex;
    state.activeMediaUrl = nextUrl;
    state.lastSyncedMediaPath = toMediaPath(nextUrl) || state.lastSyncedMediaPath;
    player.src = nextUrl;
    try {
      player.currentTime = 0;
    } catch {
    }
    renderMediaLibrary();
    renderAudioTrackSelector();
    if (wasPlaying) {
      await player.play().catch(() => {});
    } else {
      player.pause();
    }
    sendSync();
    setMediaStatus(`Selected ${getTrackLabel(nextUrl, nextIndex)}.`);
  };
}

["play", "pause", "seeked", "ratechange"].forEach((evt) => {
  videoPlayer.addEventListener(evt, onLocalPlaybackAction);
  audioPlayer.addEventListener(evt, onLocalPlaybackAction);
});

videoPlayer.addEventListener("timeupdate", () => {
  if (state.playMode !== "video") return;
  if (!videoPlayer.paused) scheduleSync();
});

audioPlayer.addEventListener("timeupdate", () => {
  if (state.playMode !== "audio") return;
  if (!audioPlayer.paused) scheduleSync();
});

const lobbyModule = createLobbyModule({
  state,
  api,
  roomList,
  createRoomForm,
  refreshRoomsBtn,
  leaveRoomBtn,
  setLobbyStatus,
  enterLobbyView,
  joinRoom,
  safeCloseWs,
  readRoomSession,
  clearRoomSession,
  currentRoomTitle,
  statusBar,
});
loadRooms = lobbyModule.loadRooms;
tryRestoreRoom = lobbyModule.tryRestoreRoom;
lobbyModule.bindLobbyEvents();

chatModule = createChatModule({
  state,
  chatBox,
  chatForm,
  chatInput,
});
chatModule.bindChatEvents();

const authModule = createAuthModule({
  state,
  api,
  startTabLock,
  stopTabLock,
  setMessage,
  setLobbyStatus,
  setAuthMode,
  loadRooms,
  tryRestoreRoom,
  showTab,
  loginForm,
  registerForm,
  logoutBtn,
  safeCloseWs,
  clearRoomSession,
  currentRoomTitle,
});
authModule.bindAuthEvents();
authModule.tryBootSession();

if (roomMediaTypeFilter) {
  roomMediaTypeFilter.value = roomMediaTypeFilter.value || "movie";
  roomMediaTypeFilter.onchange = () => {
    renderMediaLibrary();
  };
}
