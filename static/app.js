import { formatBytes, normalizeUrl, toMediaPath } from "./js/shared/format.js";
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

const authPanel = document.getElementById("authPanel");
const appPanel = document.getElementById("appPanel");
const page = document.querySelector(".page");
const tabLogin = document.getElementById("tabLogin");
const tabRegister = document.getElementById("tabRegister");
const loginForm = document.getElementById("loginForm");
const registerForm = document.getElementById("registerForm");
const authMsg = document.getElementById("authMsg");
const roomList = document.getElementById("roomList");
const createRoomForm = document.getElementById("createRoomForm");
const refreshRoomsBtn = document.getElementById("refreshRooms");
const lobbyStatus = document.getElementById("lobbyStatus");
const lobbyPanel = document.getElementById("lobbyPanel");
const roomPanel = document.getElementById("roomPanel");
const currentRoomTitle = document.getElementById("currentRoomTitle");
const videoPlayer = document.getElementById("videoPlayer");
const audioPlayer = document.getElementById("audioPlayer");
const modeVideoBtn = document.getElementById("modeVideoBtn");
const modeAudioBtn = document.getElementById("modeAudioBtn");
const refreshMediaBtn = document.getElementById("refreshMediaBtn");
const mediaList = document.getElementById("mediaList");
const mediaStatus = document.getElementById("mediaStatus");
const importForm = document.getElementById("importForm");
const videoFileInput = document.getElementById("videoFileInput");
const importTypeSelect = document.getElementById("importTypeSelect");
const coverFileInput = document.getElementById("coverFileInput");
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

window.addEventListener("beforeunload", stopTabLock);

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
    return "Import failed: file too large (max 1GB).";
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

function resolveMediaUrlForMode(item, mode) {
  if (!item) return "";
  if (mode === "audio") {
    return item.audioUrl || "";
  }
  return item.videoUrl || "";
}

function setPlayMode(mode, { silent = false } = {}) {
  const nextMode = mode === "audio" ? "audio" : "video";
  if (state.playMode === nextMode && !silent) {
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
  const activeItem = state.mediaLibrary.find((item) => item.id === state.activeMediaId);
  const targetUrl = resolveMediaUrlForMode(activeItem, nextMode);
  if (targetUrl && normalizeUrl(toPlayer.currentSrc || toPlayer.src) !== normalizeUrl(targetUrl)) {
    toPlayer.src = targetUrl;
    state.activeMediaUrl = targetUrl;
  }
  if (toPlayer.currentSrc) {
    toPlayer.currentTime = fromTime;
  }
  fromPlayer.pause();
  if (wasPlaying && toPlayer.currentSrc) {
    toPlayer.play().catch(() => {});
  }
  renderMediaLibrary();
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
  state.mediaLibrary.forEach((item) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    const modeUrl = resolveMediaUrlForMode(item, state.playMode);
    const playable = !!modeUrl;
    if (
      (state.activeMediaId && state.activeMediaId === item.id) ||
      (state.activeMediaUrl && normalizeUrl(modeUrl) === normalizeUrl(state.activeMediaUrl))
    ) {
      btn.classList.add("active");
    }
    const mediaType = item.audioUrl ? "AV" : "V";
    btn.textContent = `${item.name}  |  ${formatBytes(item.size)}  |  ${mediaType}`;
    if (!playable) {
      btn.disabled = true;
      btn.title = state.playMode === "audio" ? "No audio track for this item" : "No video track";
    }
    btn.onclick = async () => {
      if (!playable) return;
      state.forceTakeover = true;
      state.localOverrideUntil = Date.now() + 1200;
      state.activeMediaId = item.id || null;
      state.activeMediaUrl = modeUrl;
      const player = getActivePlayer();
      player.src = modeUrl;
      player.currentTime = 0;
      await player.play().catch(() => {});
      sendSync();
      renderMediaLibrary();
      setMediaStatus(`Selected: ${item.name} (${state.playMode} mode)`);
    };
    li.appendChild(btn);
    mediaList.appendChild(li);
  });
}

async function loadMediaLibrary() {
  const data = await api("/api/media", { method: "GET" });
  state.mediaLibrary = data.items || [];
  if (state.activeMediaUrl && !state.activeMediaId) {
    const found = state.mediaLibrary.find((item) => {
      const modeUrl = resolveMediaUrlForMode(item, state.playMode);
      return normalizeUrl(modeUrl) === normalizeUrl(state.activeMediaUrl);
    });
    state.activeMediaId = found?.id || null;
  }
  renderMediaLibrary();
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
  }
}

function enterLobbyView(clearSavedRoom = true) {
  lobbyPanel.classList.remove("hidden");
  roomPanel.classList.add("hidden");
  state.roomId = null;
  state.controllerUserId = null;
  state.roomDisplayNo = null;
  state.forceTakeover = false;
  state.playMode = "video";
  state.activeMediaUrl = "";
  state.activeMediaId = null;
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
  setPlayMode("video", { silent: true });
  currentRoomTitle.textContent = "Room";
  if (clearSavedRoom) {
    clearRoomSession();
  }
}

function enterRoomView(roomName, roomId, displayNo) {
  lobbyPanel.classList.add("hidden");
  roomPanel.classList.remove("hidden");
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
  if (state.activeMediaUrl && !data.videoUrl && typeof data.currentTime === "number" && data.currentTime > 0) {
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

  const remoteMode = data.playMode === "audio" ? "audio" : "video";
  setPlayMode(remoteMode, { silent: true });

  state.suppressVideoEvents = true;

  if (Object.prototype.hasOwnProperty.call(data, "controllerUserId")) {
    state.controllerUserId = data.controllerUserId;
  }

  if (updatedAtMs) {
    state.lastServerUpdatedAt = Math.max(state.lastServerUpdatedAt, updatedAtMs);
  }

  const incomingUrl = normalizeUrl(data.videoUrl || "");
  const player = getActivePlayer();
  if (!incomingUrl) {
    state.activeMediaUrl = "";
    state.activeMediaId = null;
    renderMediaLibrary();
  }
  const currentUrl = normalizeUrl(player.getAttribute("src") || player.currentSrc || "");
  if (incomingUrl && currentUrl !== incomingUrl) {
    state.activeMediaUrl = data.videoUrl;
    const media = state.mediaLibrary.find((item) => {
      const modeUrl = resolveMediaUrlForMode(item, state.playMode);
      return normalizeUrl(modeUrl) === incomingUrl;
    });
    state.activeMediaId = media?.id || null;
    player.src = incomingUrl;
    renderMediaLibrary();
  }

  const drift = Math.abs((player.currentTime || 0) - (data.currentTime || 0));
  let jumpedBySeek = false;
  if (drift > 1.2) {
    jumpedBySeek = true;
    player.currentTime = data.currentTime || 0;
  }

  if (data.isPlaying) {
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
  statusBar.textContent = `Synced by ${by}. t=${(data.currentTime || 0).toFixed(1)}s, controller: ${controllerText}`;
  setTimeout(() => {
    state.suppressVideoEvents = false;
  }, 260);
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
  const mediaPath =
    toMediaPath(player.currentSrc) ||
    toMediaPath(player.src) ||
    toMediaPath(state.activeMediaUrl);
  if (!mediaPath) {
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
  state.ws.send(JSON.stringify(payload));
  state.forceTakeover = false;
}

function scheduleSync() {
  if (state.suppressVideoEvents) return;
  clearTimeout(state.syncTimer);
  state.syncTimer = setTimeout(sendSync, state.forceTakeover ? 40 : 120);
}

function onLocalPlaybackAction(event) {
  if (event && event.currentTarget !== getActivePlayer()) {
    return;
  }
  if (state.suppressVideoEvents) {
    return;
  }
  state.forceTakeover = true;
  state.localOverrideUntil = Date.now() + 1200;
  scheduleSync();
}

tabLogin.onclick = () => showTab("login");
tabRegister.onclick = () => showTab("register");

importForm.onsubmit = async (e) => {
  e.preventDefault();
  if (!videoFileInput.files || !videoFileInput.files[0]) {
    setMediaStatus("Choose a local media file first.", true);
    return;
  }
  if (!importTypeSelect.value) {
    setMediaStatus("Choose media type first.", true);
    return;
  }
  try {
    setMediaStatus("Importing media into library...");
    const fd = new FormData();
    fd.append("file", videoFileInput.files[0]);
    fd.append("media_type", importTypeSelect.value);
    if (coverFileInput.files && coverFileInput.files[0]) {
      fd.append("cover", coverFileInput.files[0]);
    }
    const upload = await api("/api/upload-video", { method: "POST", body: fd });
    videoFileInput.value = "";
    coverFileInput.value = "";
    const profile = upload.profile || {};
    const modeText = upload.videoUrl ? "video/audio" : "audio-only";
    const transcodeText = upload.transcoded ? "transcoded" : "direct import";
    setMediaStatus(
      `Imported (${modeText}, ${profile.container || "unknown"}/${profile.videoCodec || "none"}/${profile.audioCodec || "none"}, ${transcodeText}).`,
    );
    await loadMediaLibrary();
  } catch (err) {
    setMediaStatus(formatImportError(err?.message), true);
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
