// 登录/注册/会话恢复模块。

export function createAuthModule(ctx) {
  const {
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
  } = ctx;

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

  function bindAuthEvents() {
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

    logoutBtn.onclick = async () => {
      try {
        if (state.roomId) {
          try {
            await api(`/api/rooms/${state.roomId}/leave`, { method: "POST" });
          } catch {
            // best effort; continue logout
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
  }

  return {
    tryBootSession,
    bindAuthEvents,
  };
}
