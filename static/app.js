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
  localActionCounter: 0,
  lastLocalActionId: 0,
  lastServerUpdatedAt: 0,
};

const ROOM_SESSION_KEY = "wt_active_room";

const tabId = `${Date.now()}_${Math.random().toString(16).slice(2)}`;
let lockTimer = null;
let activeLockKey = null;

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
const refreshMediaBtn = document.getElementById("refreshMediaBtn");
const mediaList = document.getElementById("mediaList");
const mediaStatus = document.getElementById("mediaStatus");
const importForm = document.getElementById("importForm");
const videoFileInput = document.getElementById("videoFileInput");
const statusBar = document.getElementById("statusBar");
const userBadge = document.getElementById("userBadge");
const logoutBtn = document.getElementById("logoutBtn");
const leaveRoomBtn = document.getElementById("leaveRoomBtn");
const chatBox = document.getElementById("chatBox");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");

function lockKeyForUser(userId) {
  return `wt_active_tab_lock_${userId}`;
}

function readTabLock(lockKey) {
  try {
    return JSON.parse(localStorage.getItem(lockKey) || "null");
  } catch {
    return null;
  }
}

function writeTabLock(lockKey) {
  localStorage.setItem(lockKey, JSON.stringify({ tabId, ts: Date.now() }));
}

function stopTabLock() {
  if (lockTimer) {
    clearInterval(lockTimer);
    lockTimer = null;
  }
  if (!activeLockKey) return;
  const lock = readTabLock(activeLockKey);
  if (lock && lock.tabId === tabId) {
    localStorage.removeItem(activeLockKey);
  }
  activeLockKey = null;
}

function startTabLock(userId) {
  const lockKey = lockKeyForUser(userId);
  const lock = readTabLock(lockKey);
  if (lock && lock.tabId !== tabId && Date.now() - lock.ts < 15000) {
    throw new Error("This account is already active in another tab/window.");
  }
  activeLockKey = lockKey;
  writeTabLock(lockKey);
  lockTimer = setInterval(() => writeTabLock(lockKey), 4000);
}

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

function normalizeUrl(url) {
  if (!url) return "";
  try {
    return new URL(url, location.origin).href;
  } catch {
    return String(url);
  }
}

function toMediaPath(url) {
  if (!url) return "";
  try {
    const parsed = new URL(url, location.origin);
    return parsed.pathname.startsWith("/media/") ? parsed.pathname : "";
  } catch {
    return String(url).startsWith("/media/") ? String(url) : "";
  }
}

function saveRoomSession(roomId, roomName, displayNo) {
  sessionStorage.setItem(
    ROOM_SESSION_KEY,
    JSON.stringify({ roomId, roomName, displayNo, savedAt: Date.now() }),
  );
}

function readRoomSession() {
  try {
    return JSON.parse(sessionStorage.getItem(ROOM_SESSION_KEY) || "null");
  } catch {
    return null;
  }
}

function clearRoomSession() {
  sessionStorage.removeItem(ROOM_SESSION_KEY);
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)}GB`;
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
    if (state.activeMediaUrl && normalizeUrl(state.activeMediaUrl) === normalizeUrl(item.url)) {
      btn.classList.add("active");
    }
    btn.textContent = `${item.name}  |  ${formatBytes(item.size)}`;
    btn.onclick = async () => {
      state.forceTakeover = true;
      state.localOverrideUntil = Date.now() + 1200;
      state.activeMediaUrl = item.url;
      videoPlayer.src = item.url;
      videoPlayer.currentTime = 0;
      await videoPlayer.play().catch(() => {});
      sendSync();
      renderMediaLibrary();
      setMediaStatus(`Selected: ${item.name}`);
    };
    li.appendChild(btn);
    mediaList.appendChild(li);
  });
}

async function loadMediaLibrary() {
  const data = await api("/api/media", { method: "GET" });
  state.mediaLibrary = data.items || [];
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

async function api(path, options = {}) {
  const hasBody = Object.prototype.hasOwnProperty.call(options, "body");
  const isFormData = hasBody && options.body instanceof FormData;
  const authHeader = state.token ? { Authorization: `Bearer ${state.token}` } : {};
  const res = await fetch(path, {
    ...options,
    headers: isFormData
      ? { ...authHeader, ...(options.headers || {}) }
      : {
        "Content-Type": "application/json",
        ...authHeader,
        ...(options.headers || {}),
      },
    credentials: "include",
  });
  if (!res.ok) {
    let detail = "Request failed";
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {}
    if (res.status === 401) {
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
      setAuthMode(false);
      setMessage("Session expired. Please login again.", true);
    }
    throw new Error(detail);
  }
  return res.json();
}

async function tryBootSession() {
  if (!state.token) {
    setAuthMode(false);
    return;
  }
  try {
    const me = await api("/api/me", { method: "GET" });
    state.me = me;
    startTabLock(me.id);
    setAuthMode(true);
    setLobbyStatus("Create a room or join an existing one.");
    const rooms = await loadRooms();
    await tryRestoreRoom(rooms);
  } catch {
    setAuthMode(false);
  }
}

function setAuthMode(loggedIn) {
  authPanel.classList.toggle("hidden", loggedIn);
  appPanel.classList.toggle("hidden", !loggedIn);
  page.classList.toggle("auth-only", !loggedIn);
  if (loggedIn && state.me) {
    userBadge.classList.remove("hidden");
    logoutBtn.classList.remove("hidden");
    userBadge.textContent = `User: ${state.me.username}`;
    enterLobbyView(false);
  } else {
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
  state.activeMediaUrl = "";
  state.localOverrideUntil = 0;
  state.localActionCounter = 0;
  state.lastLocalActionId = 0;
  state.lastServerUpdatedAt = 0;
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

async function loadRooms() {
  const data = await api("/api/rooms", { method: "GET" });
  roomList.innerHTML = "";
  state.roomMeta.clear();
  if (!data.rooms.length) {
    const li = document.createElement("li");
    li.textContent = "No rooms yet.";
    roomList.appendChild(li);
    return [];
  }

  const rooms = data.rooms.map((room, index) => ({ ...room, displayNo: index }));
  rooms.forEach((room) => {
    state.roomMeta.set(room.id, room);
    const li = document.createElement("li");
    const row = document.createElement("div");
    row.className = "room-row";

    const joinBtn = document.createElement("button");
    joinBtn.textContent = `#${room.displayNo} ${room.name}  | owner: ${room.owner}  | members: ${room.members}`;
    joinBtn.onclick = () => joinRoom(room.id, room.name, room.displayNo);
    row.appendChild(joinBtn);

    if (state.me && room.ownerId === state.me.id) {
      const deleteBtn = document.createElement("button");
      deleteBtn.className = "danger";
      deleteBtn.textContent = "Delete";
      deleteBtn.onclick = async (event) => {
        event.stopPropagation();
        if (!confirm(`Delete room "${room.name}"?`)) return;
        try {
          await api(`/api/rooms/${room.id}`, { method: "DELETE" });
          if (state.roomId === room.id) {
            safeCloseWs();
            state.roomId = null;
            currentRoomTitle.textContent = "No room selected";
            statusBar.textContent = "Room deleted.";
          }
          await loadRooms();
        } catch (err) {
          statusBar.textContent = err.message;
        }
      };
      row.appendChild(deleteBtn);
    }

    li.appendChild(row);
    roomList.appendChild(li);
  });
  return rooms;
}

async function tryRestoreRoom(rooms) {
  const saved = readRoomSession();
  if (!saved || !saved.roomId) return;
  const match = (rooms || []).find((r) => r.id === saved.roomId);
  if (!match) {
    clearRoomSession();
    return;
  }
  try {
    await joinRoom(match.id, match.name, match.displayNo);
    setLobbyStatus("Restored previous room.");
  } catch {
    clearRoomSession();
  }
}

function safeCloseWs() {
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
}

async function joinRoom(roomId, roomName, displayNo = null) {
  await api(`/api/rooms/${roomId}/join`, { method: "POST" });
  state.roomId = roomId;
  const finalDisplayNo = displayNo ?? state.roomMeta.get(roomId)?.displayNo ?? 0;
  saveRoomSession(roomId, roomName, finalDisplayNo);
  enterRoomView(roomName, roomId, finalDisplayNo);
  statusBar.textContent = "Joining room...";
  setMediaStatus("Loading media library...");
  await loadMediaLibrary();
  setMediaStatus("Select a media file to play and sync.");
  safeCloseWs();
  connectWs(roomId);
  const rs = await api(`/api/rooms/${roomId}/state`, { method: "GET" });
  applyRemoteState(rs, "server");
}

function wsUrl(roomId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const token = encodeURIComponent(state.token || "");
  return `${proto}://${location.host}/ws/rooms/${roomId}?token=${token}`;
}

function connectWs(roomId) {
  const socket = new WebSocket(wsUrl(roomId));
  state.ws = socket;

  socket.onopen = () => {
    statusBar.textContent = "Connected. Sync active.";
  };

  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "state") {
      applyRemoteState(data, data.by || "peer");
    } else if (data.type === "error") {
      statusBar.textContent = data.message || "Room sync error.";
    } else if (data.type === "room_deleted") {
      alert(`Room "${data.roomName || ""}" was closed by ${data.by || "owner"}. Returning to room list.`);
      safeCloseWs();
      enterLobbyView();
      setLobbyStatus(`Room "${data.roomName || ""}" has been deleted.`, true);
      loadRooms().catch(() => {});
    } else if (data.type === "chat") {
      appendChat(data.by, data.message, data.sentAt);
    }
  };

  socket.onclose = () => {
    statusBar.textContent = "Disconnected from room sync.";
  };
}

function appendChat(by, message, sentAt) {
  const row = document.createElement("div");
  row.className = "chat-row";
  const t = new Date(sentAt || Date.now()).toLocaleTimeString();
  row.innerHTML = `<b>${by}</b> [${t}]: ${escapeHtml(message)}`;
  chatBox.appendChild(row);
  chatBox.scrollTop = chatBox.scrollHeight;
}

function escapeHtml(str) {
  return str
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function applyRemoteState(data, by) {
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

  state.suppressVideoEvents = true;

  if (Object.prototype.hasOwnProperty.call(data, "controllerUserId")) {
    state.controllerUserId = data.controllerUserId;
  }

  if (updatedAtMs) {
    state.lastServerUpdatedAt = Math.max(state.lastServerUpdatedAt, updatedAtMs);
  }

  const incomingUrl = normalizeUrl(data.videoUrl || "");
  if (!incomingUrl) {
    state.activeMediaUrl = "";
    renderMediaLibrary();
  }
  const currentUrl = normalizeUrl(videoPlayer.getAttribute("src") || videoPlayer.currentSrc || "");
  if (incomingUrl && currentUrl !== incomingUrl) {
    state.activeMediaUrl = data.videoUrl;
    videoPlayer.src = incomingUrl;
    renderMediaLibrary();
  }

  const drift = Math.abs((videoPlayer.currentTime || 0) - (data.currentTime || 0));
  let jumpedBySeek = false;
  if (drift > 1.2) {
    jumpedBySeek = true;
    videoPlayer.currentTime = data.currentTime || 0;
  }

  if (data.isPlaying) {
    const ensurePlay = () => {
      videoPlayer.play().catch(() => {
        statusBar.textContent = "Playback blocked by browser policy. Click play once to enable sync playback.";
      });
    };
    if (jumpedBySeek) {
      videoPlayer.addEventListener("seeked", ensurePlay, { once: true });
      setTimeout(ensurePlay, 120);
    } else {
      ensurePlay();
    }
  } else {
    videoPlayer.pause();
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
  const mediaPath =
    toMediaPath(videoPlayer.currentSrc) ||
    toMediaPath(videoPlayer.src) ||
    toMediaPath(state.activeMediaUrl);
  const payload = {
    type: "sync",
    actionId,
    videoUrl: mediaPath,
    currentTime: Number(videoPlayer.currentTime || 0),
    isPlaying: !videoPlayer.paused,
  };
  state.ws.send(JSON.stringify(payload));
  state.forceTakeover = false;
}

function scheduleSync() {
  if (state.suppressVideoEvents) return;
  clearTimeout(state.syncTimer);
  state.syncTimer = setTimeout(sendSync, state.forceTakeover ? 40 : 120);
}

function onLocalPlaybackAction() {
  if (state.suppressVideoEvents) {
    return;
  }
  state.forceTakeover = true;
  state.localOverrideUntil = Date.now() + 1200;
  scheduleSync();
}

tabLogin.onclick = () => showTab("login");
tabRegister.onclick = () => showTab("register");

loginForm.onsubmit = async (e) => {
  e.preventDefault();
  try {
    const payload = {
      username: document.getElementById("loginUsername").value.trim(),
      password: document.getElementById("loginPassword").value,
    };
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.token = data.token;
    sessionStorage.setItem("wt_token", data.token);
    state.me = await api("/api/me", { method: "GET" });
    startTabLock(state.me.id);
    setMessage("Logged in.");
    setAuthMode(true);
    setLobbyStatus("Create a room or join an existing one.");
    const rooms = await loadRooms();
    await tryRestoreRoom(rooms);
  } catch (err) {
    setMessage(err.message, true);
  }
};

registerForm.onsubmit = async (e) => {
  e.preventDefault();
  try {
    const payload = {
      username: document.getElementById("registerUsername").value.trim(),
      password: document.getElementById("registerPassword").value,
    };
    await api("/api/register", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("Register success. Please login.");
    showTab("login");
  } catch (err) {
    setMessage(err.message, true);
  }
};

createRoomForm.onsubmit = async (e) => {
  e.preventDefault();
  try {
    setLobbyStatus("Creating room...");
    const roomName = document.getElementById("roomName").value.trim();
    if (!roomName) return;
    const room = await api("/api/rooms", {
      method: "POST",
      body: JSON.stringify({ name: roomName }),
    });
    document.getElementById("roomName").value = "";
    const rooms = await loadRooms();
    const created = rooms.find((r) => r.id === room.id);
    setLobbyStatus("Room created.");
    await joinRoom(room.id, room.name, created ? created.displayNo : 0);
  } catch (err) {
    setLobbyStatus(err.message, true);
  }
};

refreshRoomsBtn.onclick = async () => {
  try {
    setLobbyStatus("Refreshing rooms...");
    await loadRooms();
    setLobbyStatus("Room list updated.");
  } catch (err) {
    setLobbyStatus(err.message, true);
  }
};

importForm.onsubmit = async (e) => {
  e.preventDefault();
  if (!videoFileInput.files || !videoFileInput.files[0]) {
    setMediaStatus("Choose a local video first.", true);
    return;
  }
  try {
    setMediaStatus("Importing video into library...");
    const fd = new FormData();
    fd.append("file", videoFileInput.files[0]);
    const upload = await api("/api/upload-video", { method: "POST", body: fd });
    videoFileInput.value = "";
    const profile = upload.profile || {};
    const transcodeText = upload.transcoded ? "transcoded to H.264/AAC MP4" : "already browser-friendly";
    setMediaStatus(
      `Imported (${profile.container || "unknown"}/${profile.videoCodec || "unknown"}/${profile.audioCodec || "none"}, ${transcodeText}).`,
    );
    await loadMediaLibrary();
  } catch (err) {
    setMediaStatus(`Import failed: ${err.message}`, true);
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

["play", "pause", "seeked", "ratechange"].forEach((evt) => {
  videoPlayer.addEventListener(evt, onLocalPlaybackAction);
});

videoPlayer.addEventListener("timeupdate", () => {
  if (!videoPlayer.paused) scheduleSync();
});

chatForm.onsubmit = (e) => {
  e.preventDefault();
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  const message = chatInput.value.trim();
  if (!message) return;
  state.ws.send(JSON.stringify({ type: "chat", message }));
  chatInput.value = "";
};

logoutBtn.onclick = async () => {
  try {
    if (state.roomId) {
      try {
        await api(`/api/rooms/${state.roomId}/leave`, { method: "POST" });
      } catch {
        // best effort; continue logout flow
      }
    }
    await api("/api/logout", { method: "POST" });
  } finally {
    safeCloseWs();
    stopTabLock();
    state.token = "";
    sessionStorage.removeItem("wt_token");
    state.me = null;
    state.roomId = null;
    state.controllerUserId = null;
    state.forceTakeover = false;
    state.roomMeta.clear();
    clearRoomSession();
    setAuthMode(false);
    currentRoomTitle.textContent = "No room selected";
  }
};

leaveRoomBtn.onclick = async () => {
  if (!state.roomId) {
    enterLobbyView(true);
    await loadRooms();
    return;
  }
  const leavingRoomId = state.roomId;
  const roomMeta = state.roomMeta.get(leavingRoomId);
  const isOwner = !!state.me && !!roomMeta && roomMeta.ownerId === state.me.id;
  try {
    if (isOwner) {
      await api(`/api/rooms/${leavingRoomId}`, { method: "DELETE" });
    } else {
      await api(`/api/rooms/${leavingRoomId}/leave`, { method: "POST" });
    }
  } catch (err) {
    statusBar.textContent = err.message;
    return;
  }

  safeCloseWs();
  enterLobbyView(true);
  await loadRooms();
  setLobbyStatus("You have left the room.");
};

tryBootSession();
