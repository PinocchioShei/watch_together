import { escapeHtml, formatBytes } from "./js/admin/helpers.js";

const state = {
  token: sessionStorage.getItem("wt_admin_token") || "",
};
let guestAuthOpen = false;
const THEME_KEY = "wt_admin_theme";

const loginCard = document.getElementById("loginCard");
const dashboard = document.getElementById("dashboard");
const logoutBtn = document.getElementById("logoutBtn");
const loginForm = document.getElementById("loginForm");
const loginMsg = document.getElementById("loginMsg");
const adminProfileMsg = document.getElementById("adminProfileMsg");
const usersTable = document.getElementById("usersTable");
const mediaTable = document.getElementById("mediaTable");
const roomsTable = document.getElementById("roomsTable");
const importMsg = document.getElementById("importMsg");
const mediaDetailOverlay = document.getElementById("mediaDetailOverlay");
const mediaDetailModal = document.getElementById("mediaDetailModal");
const mediaDetailBody = document.getElementById("mediaDetailBody");
const mediaDetailClose = document.getElementById("mediaDetailClose");
const mediaDetailType = document.getElementById("mediaDetailType");
const mediaDetailRename = document.getElementById("mediaDetailRename");
const mediaDetailDelete = document.getElementById("mediaDetailDelete");
const mediaTypeFilter = document.getElementById("mediaTypeFilter");
const themeToggleBtn = document.getElementById("themeToggleBtn");
const tabButtons = Array.from(document.querySelectorAll(".admin-tab-btn"));
const tabPanels = {
  users: document.getElementById("tab-users"),
  media: document.getElementById("tab-media"),
  rooms: document.getElementById("tab-rooms"),
};
let mediaItems = [];
let activeMediaItem = null;

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

function setLoginMsg(msg, err = false) {
  loginMsg.textContent = msg || "";
  loginMsg.style.color = err ? "#fca5a5" : "#94a3b8";
}

function setImportMsg(msg, err = false) {
  importMsg.textContent = msg || "";
  importMsg.style.color = err ? "#fca5a5" : "#94a3b8";
}

function setAdminProfileMsg(msg, err = false) {
  adminProfileMsg.textContent = msg || "";
  adminProfileMsg.style.color = err ? "#fca5a5" : "#94a3b8";
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

function parseWorkPath(url) {
  if (!url) return { work: "-", file: "-" };
  const m = String(url).match(/^\/media\/work\/([^/]+)\/([^/]+)$/);
  if (!m) return { work: "legacy", file: url };
  return { work: m[1], file: m[2] };
}

function closeMediaDetail() {
  activeMediaItem = null;
  if (!mediaDetailOverlay) return;
  mediaDetailOverlay.classList.add("hidden");
}

function openMediaDetail(item) {
  if (!item || !mediaDetailOverlay || !mediaDetailBody) return;
  activeMediaItem = item;
  const v = parseWorkPath(item.videoUrl || "");
  const a = parseWorkPath(item.audioUrl || "");
  const workFolder = v.work !== "-" ? v.work : a.work;
  const cover = item.coverUrl || "";
  mediaDetailBody.innerHTML = [
    ["_cover", cover],
    ["Work", workFolder || item.name || "-"],
    ["Type", item.type || "movie"],
    ["Name", item.name || "-"],
    ["Media Key", item.mediaKey || "-"],
    ["Video URL", item.videoUrl || "-"],
    ["Audio URL", item.audioUrl || "-"],
    ["Cover URL", item.coverUrl || "-"],
    ["Size", formatBytes(item.size || 0)],
    ["Duration", `${Math.round(Number(item.duration || 0))}s`],
    ["Updated At", item.updatedAt || "-"],
  ].map(([k, val]) => {
    if (k === "_cover") {
      return `<img class="media-detail-cover" src="${escapeHtml(String(val || ""))}" alt="cover" loading="lazy" />`;
    }
    return `<div class="media-detail-row"><b>${escapeHtml(String(k))}</b><span>${escapeHtml(String(val))}</span></div>`;
  }).join("");
  mediaDetailOverlay.classList.remove("hidden");
}

function setAuthUI(ok) {
  loginCard.classList.toggle("hidden", ok);
  dashboard.classList.toggle("hidden", !ok);
  logoutBtn.classList.toggle("hidden", !ok);
  document.body.classList.toggle("login-mode", !ok);
  if (ok) {
    guestAuthOpen = false;
    loginCard.classList.remove("auth-collapsed");
    document.body.classList.remove("guest-mode", "guest-idle", "guest-auth-open");
  } else {
    guestAuthOpen = false;
    loginCard.classList.remove("hidden");
    loginCard.classList.add("auth-collapsed");
    document.body.classList.add("guest-mode", "guest-idle");
    document.body.classList.remove("guest-auth-open");
  }
}

function toggleGuestAuth(open) {
  if (state.token) return;
  guestAuthOpen = !!open;
  loginCard.classList.toggle("auth-collapsed", !guestAuthOpen);
  document.body.classList.toggle("guest-auth-open", guestAuthOpen);
  document.body.classList.toggle("guest-idle", !guestAuthOpen);
  if (guestAuthOpen) {
    const firstInput = loginForm?.querySelector("input");
    if (firstInput) setTimeout(() => firstInput.focus(), 180);
  }
}

document.addEventListener("click", (ev) => {
  if (state.token) return;
  if (!document.body.classList.contains("guest-mode")) return;
  const target = ev.target;
  if (!(target instanceof Element)) return;
  if (loginCard.contains(target)) return;
  if (target.closest("button,input,select,textarea,label,form,a")) return;
  toggleGuestAuth(!guestAuthOpen);
});

function setActiveTab(tabName) {
  const target = tabPanels[tabName] ? tabName : "users";
  tabButtons.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === target);
  });
  Object.entries(tabPanels).forEach(([name, panel]) => {
    if (!panel) return;
    panel.classList.toggle("active", name === target);
  });
}

async function api(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const headers = isForm
    ? { ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), ...(options.headers || {}) }
    : {
      "Content-Type": "application/json",
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
      ...(options.headers || {}),
    };
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let detail = "Request failed";
    try {
      detail = (await res.json()).detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

async function loadOverview() {
  const data = await api("/api/admin/overview", { method: "GET" });
  document.getElementById("metricUsers").textContent = data.users;
  document.getElementById("metricRooms").textContent = data.rooms;
  document.getElementById("metricSessions").textContent = data.sessions;
  document.getElementById("metricMedia").textContent = `${data.mediaScanned} files`;
}

async function loadUsers() {
  const data = await api("/api/admin/users", { method: "GET" });
  const rows = data.items || [];
  let html = "<table class=\"users-table\"><thead><tr><th>ID</th><th>Username</th><th>Actions</th></tr></thead><tbody>";
  rows.forEach((u) => {
    html += `<tr><td>${u.id}</td><td>${escapeHtml(u.username)}</td><td><div class=\"row-actions\"><button data-reset=\"${u.id}\">Reset Password</button><button class=\"danger\" data-del=\"${u.id}\">Delete</button></div></td></tr>`;
  });
  html += "</tbody></table>";
  usersTable.innerHTML = html;

  usersTable.querySelectorAll("button[data-reset]").forEach((btn) => {
    btn.onclick = async () => {
      const userId = btn.getAttribute("data-reset");
      const pwd = prompt("Set new password (>=6 chars):");
      if (!pwd) return;
      await api(`/api/admin/users/${userId}`, {
        method: "PATCH",
        body: JSON.stringify({ password: pwd }),
      });
      alert("Password reset complete.");
    };
  });

  usersTable.querySelectorAll("button[data-del]").forEach((btn) => {
    btn.onclick = async () => {
      const userId = btn.getAttribute("data-del");
      if (!confirm(`Delete user #${userId}?`)) return;
      await api(`/api/admin/users/${userId}`, { method: "DELETE" });
      await loadUsers();
      await loadOverview();
    };
  });
}

async function loadMedia() {
  const data = await api("/api/admin/media", { method: "GET" });
  const rows = data.items || [];
  const selectedType = mediaTypeFilter ? (mediaTypeFilter.value || "movie") : "movie";
  const visible = selectedType === "all"
    ? rows
    : rows.filter((m) => normalizeMediaType(m.type) === selectedType);
  mediaItems = visible;
  let html = "<div class=\"media-card-grid\">";
  visible.forEach((m, idx) => {
    const v = parseWorkPath(m.videoUrl || "");
    const a = parseWorkPath(m.audioUrl || "");
    const workFolder = v.work !== "-" ? v.work : a.work;
    const videoState = v.file && v.file !== "-" ? '<span class="state-badge yes">Yes</span>' : '<span class="state-badge no">No</span>';
    const audioState = a.file && a.file !== "-" ? '<span class="state-badge yes">Yes</span>' : '<span class="state-badge no">No</span>';
    html += `
      <button type="button" class="admin-media-card" data-media-index="${idx}">
        <div class="admin-media-cover-wrap">
          <img class="admin-media-cover-img" src="${escapeHtml(m.coverUrl || "")}" alt="${escapeHtml(workFolder || m.name || "media")}" loading="lazy" />
          <div class="admin-media-badges">
            ${videoState}
            ${audioState}
          </div>
        </div>
        <div class="admin-media-meta">
          <div class="admin-media-name">${escapeHtml(workFolder || m.name || "-")}</div>
          <div class="admin-media-sub">${escapeHtml(m.type || "movie")} · ${formatBytes(m.size || 0)}</div>
        </div>
      </button>
    `;
  });
  html += "</div>";
  if (!visible.length) {
    html = "<p class=\"msg\">No media under current type filter.</p>";
  }
  mediaTable.innerHTML = html;

  mediaTable.querySelectorAll("button[data-media-index]").forEach((btn) => {
    btn.onclick = async () => {
      const idx = Number(btn.getAttribute("data-media-index") || -1);
      const item = idx >= 0 ? mediaItems[idx] : null;
      if (!item) return;
      openMediaDetail(item);
    };
  });

  if (mediaDetailRename) {
    mediaDetailRename.onclick = async () => {
      const item = activeMediaItem;
      if (!item) return;
      const mediaKey = item.mediaKey || "";
      const nextName = prompt("New work folder name:");
      if (!nextName || !nextName.trim()) return;
      try {
        await api(`/api/admin/media/${encodeURIComponent(mediaKey)}`, {
          method: "PATCH",
          body: JSON.stringify({ newWorkName: nextName.trim() }),
        });
        setImportMsg("Work renamed.");
        closeMediaDetail();
        await loadMedia();
      } catch (err) {
        setImportMsg(err.message || "Rename failed", true);
      }
    };
  }

  if (mediaDetailType) {
    mediaDetailType.onclick = async () => {
      const item = activeMediaItem;
      if (!item) return;
      const mediaKey = item.mediaKey || "";
      if (!mediaKey) return;
      const current = normalizeMediaType(item.type || "movie");
      const next = prompt("Set media type (movie/RJ/ASMR/music/shot):", current);
      if (!next) return;
      const value = next.trim();
      if (!["movie", "RJ", "ASMR", "music", "shot"].includes(value)) {
        setImportMsg("Invalid type. Use movie/RJ/ASMR/music/shot.", true);
        return;
      }
      try {
        await api(`/api/admin/media/${encodeURIComponent(mediaKey)}/type`, {
          method: "PATCH",
          body: JSON.stringify({ mediaType: value }),
        });
        setImportMsg("Media type updated.");
        closeMediaDetail();
        await loadMedia();
      } catch (err) {
        setImportMsg(err.message || "Update type failed", true);
      }
    };
  }

  if (mediaDetailDelete) {
    mediaDetailDelete.onclick = async () => {
      const item = activeMediaItem;
      const key = item?.mediaKey || "";
      if (!key) return;
      if (!confirm(`Delete media ${key}?`)) return;
      await api(`/api/admin/media/${encodeURIComponent(key)}`, { method: "DELETE" });
      closeMediaDetail();
      await loadMedia();
      await loadOverview();
    };
  }
}

async function loadRooms() {
  const data = await api("/api/admin/rooms", { method: "GET" });
  const rows = data.items || [];
  let html = "<table class=\"rooms-table\"><thead><tr><th>ID</th><th>Name</th><th>Owner</th><th>Members</th><th>Online</th><th>Actions</th></tr></thead><tbody>";
  rows.forEach((r) => {
    html += `<tr><td>${r.id}</td><td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.owner)}</td><td>${r.members}</td><td>${r.online}</td><td><div class=\"row-actions\"><button class=\"danger\" data-del-room=\"${r.id}\">Delete Room</button></div></td></tr>`;
  });
  html += "</tbody></table>";
  roomsTable.innerHTML = html;

  roomsTable.querySelectorAll("button[data-del-room]").forEach((btn) => {
    btn.onclick = async () => {
      const roomId = btn.getAttribute("data-del-room");
      if (!confirm(`Delete room #${roomId}? All members will be kicked out.`)) return;
      await api(`/api/admin/rooms/${roomId}`, { method: "DELETE" });
      await Promise.all([loadRooms(), loadOverview()]);
    };
  });
}

async function bootstrapDashboard() {
  await Promise.all([loadOverview(), loadUsers(), loadMedia(), loadRooms()]);
  setActiveTab("users");
}

tabButtons.forEach((btn) => {
  btn.onclick = () => {
    setActiveTab(btn.dataset.tab || "users");
  };
});

if (mediaTypeFilter) {
  mediaTypeFilter.value = mediaTypeFilter.value || "movie";
  mediaTypeFilter.onchange = async () => {
    await loadMedia();
  };
}

if (mediaDetailClose) {
  mediaDetailClose.onclick = () => {
    closeMediaDetail();
  };
}

if (mediaDetailOverlay) {
  mediaDetailOverlay.addEventListener("click", (ev) => {
    if (ev.target === mediaDetailOverlay) {
      closeMediaDetail();
    }
  });
}

if (mediaDetailModal) {
  mediaDetailModal.addEventListener("click", (ev) => {
    ev.stopPropagation();
  });
}

loginForm.onsubmit = async (e) => {
  e.preventDefault();
  try {
    const payload = {
      username: document.getElementById("adminUser").value.trim(),
      password: document.getElementById("adminPass").value,
    };
    const data = await api("/api/admin/login", { method: "POST", body: JSON.stringify(payload) });
    state.token = data.token;
    sessionStorage.setItem("wt_admin_token", data.token);
    setAuthUI(true);
    setLoginMsg("");
    await bootstrapDashboard();
  } catch (err) {
    setLoginMsg(err.message, true);
  }
};

logoutBtn.onclick = async () => {
  try {
    await api("/api/admin/logout", { method: "POST" });
  } catch {}
  state.token = "";
  sessionStorage.removeItem("wt_admin_token");
  setAuthUI(false);
};

document.getElementById("refreshUsers").onclick = async () => {
  await loadUsers();
  await loadOverview();
};

document.getElementById("refreshMedia").onclick = async () => {
  await loadMedia();
  await loadOverview();
};

document.getElementById("refreshRooms").onclick = async () => {
  await loadRooms();
  await loadOverview();
};

document.getElementById("createUserForm").onsubmit = async (e) => {
  e.preventDefault();
  const username = document.getElementById("newUsername").value.trim();
  const password = document.getElementById("newPassword").value;
  await api("/api/admin/users", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  document.getElementById("newUsername").value = "";
  document.getElementById("newPassword").value = "";
  await loadUsers();
  await loadOverview();
};

document.getElementById("updateAdminForm").onsubmit = async (e) => {
  e.preventDefault();
  const newUsername = document.getElementById("adminNewUsername").value.trim();
  const newPassword = document.getElementById("adminNewPassword").value;
  const currentPassword = document.getElementById("adminCurrentPassword").value;
  try {
    setAdminProfileMsg("Updating admin profile...");
    await api("/api/admin/profile", {
      method: "PATCH",
      body: JSON.stringify({
        currentPassword,
        newUsername: newUsername || null,
        newPassword: newPassword || null,
      }),
    });
    document.getElementById("adminCurrentPassword").value = "";
    document.getElementById("adminNewPassword").value = "";
    setAdminProfileMsg("Admin profile updated.");
  } catch (err) {
    setAdminProfileMsg(err.message, true);
  }
};

document.getElementById("importForm").onsubmit = async (e) => {
  e.preventDefault();
  const fileInput = document.getElementById("importFile");
  const typeSelect = document.getElementById("importType");
  const coverInput = document.getElementById("importCover");
  if (!fileInput.files || !fileInput.files[0]) {
    setImportMsg("Choose a file first.", true);
    return;
  }
  if (!typeSelect.value) {
    setImportMsg("Choose media type first.", true);
    return;
  }
  try {
    setImportMsg("Importing and transcoding...");
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("media_type", typeSelect.value);
    if (coverInput.files && coverInput.files[0]) {
      fd.append("cover", coverInput.files[0]);
    }
    const data = await api("/api/admin/import", { method: "POST", body: fd });
    const mode = data.videoUrl ? "video/audio" : "audio-only";
    setImportMsg(`Imported (${mode}). video=${data.videoUrl || "-"} audio=${data.audioUrl || "-"}`);
    fileInput.value = "";
    coverInput.value = "";
    await loadMedia();
    await loadOverview();
  } catch (err) {
    setImportMsg(formatImportError(err?.message), true);
  }
};

if (state.token) {
  setAuthUI(true);
  bootstrapDashboard().catch((err) => {
    state.token = "";
    sessionStorage.removeItem("wt_admin_token");
    setAuthUI(false);
    setLoginMsg(err.message, true);
  });
} else {
  setAuthUI(false);
}
