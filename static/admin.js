import { escapeHtml, formatBytes } from "./js/admin/helpers.js";

const state = {
  token: sessionStorage.getItem("wt_admin_token") || "",
};

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

function setAuthUI(ok) {
  loginCard.classList.toggle("hidden", ok);
  dashboard.classList.toggle("hidden", !ok);
  logoutBtn.classList.toggle("hidden", !ok);
  document.body.classList.toggle("login-mode", !ok);
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
  let html = "<table class=\"media-table\"><thead><tr><th>Name</th><th>Video</th><th>Audio</th><th>Size</th><th>Actions</th></tr></thead><tbody>";
  rows.forEach((m) => {
    html += `<tr><td>${escapeHtml(m.name)}</td><td>${escapeHtml(m.videoUrl || "-")}</td><td>${escapeHtml(m.audioUrl || "-")}</td><td>${formatBytes(m.size || 0)}</td><td><div class=\"row-actions\"><button class=\"danger\" data-del-media=\"${escapeHtml(m.mediaKey || "")}\">Delete</button></div></td></tr>`;
  });
  html += "</tbody></table>";
  mediaTable.innerHTML = html;

  mediaTable.querySelectorAll("button[data-del-media]").forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.getAttribute("data-del-media");
      if (!key) return;
      if (!confirm(`Delete media ${key}?`)) return;
      await api(`/api/admin/media/${encodeURIComponent(key)}`, { method: "DELETE" });
      await loadMedia();
      await loadOverview();
    };
  });
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
  if (!fileInput.files || !fileInput.files[0]) {
    setImportMsg("Choose a file first.", true);
    return;
  }
  try {
    setImportMsg("Importing and transcoding...");
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    const data = await api("/api/admin/import", { method: "POST", body: fd });
    setImportMsg(`Imported. video=${data.videoUrl || "-"} audio=${data.audioUrl || "-"}`);
    fileInput.value = "";
    await loadMedia();
    await loadOverview();
  } catch (err) {
    setImportMsg(err.message, true);
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
