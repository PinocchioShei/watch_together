// 大厅业务：房间列表、创建、刷新、恢复、离房。

export function createLobbyModule(ctx) {
  const {
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
  } = ctx;

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
      const lockMark = room.hasPassword ? " [locked]" : "";
      joinBtn.textContent = `#${room.displayNo} ${room.name}${lockMark}  | owner: ${room.owner}  | members: ${room.members}`;
      joinBtn.onclick = async () => {
        try {
          await joinRoom(room.id, room.name, room.displayNo, "", { allowPrompt: false });
        } catch (err) {
          const msg = String(err?.message || "");
          if (/invalid room password/i.test(msg)) {
            const input = prompt(`Enter password for room "${room.name}"`);
            if (input === null) {
              setLobbyStatus("Join cancelled.", true);
              return;
            }
            try {
              await joinRoom(room.id, room.name, room.displayNo, input, { allowPrompt: false });
              return;
            } catch (retryErr) {
              const retryMsg = String(retryErr?.message || "");
              if (/invalid room password/i.test(retryMsg)) {
                setLobbyStatus("Wrong room password.", true);
                return;
              }
              setLobbyStatus(retryMsg || "Join failed", true);
              return;
            }
          } else if (!/join cancelled/i.test(msg.toLowerCase())) {
            setLobbyStatus(msg || "Join failed", true);
          }
        }
      };
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
      await joinRoom(match.id, match.name, match.displayNo, saved.roomPassword || "", { allowPrompt: false });
      setLobbyStatus("Restored previous room.");
    } catch (err) {
      const msg = String(err?.message || "").toLowerCase();
      if (msg.includes("invalid room password")) {
        setLobbyStatus("Saved room password is no longer valid. Please re-enter password.", true);
        try {
          await joinRoom(match.id, match.name, match.displayNo, null, { allowPrompt: true });
          setLobbyStatus("Restored previous room.");
          return;
        } catch (retryErr) {
          const retryMsg = String(retryErr?.message || "").toLowerCase();
          if (retryMsg.includes("invalid room password")) {
            setLobbyStatus("Wrong room password.", true);
          }
        }
      }
      clearRoomSession();
    }
  }

  function bindLobbyEvents() {
    createRoomForm.onsubmit = async (e) => {
      e.preventDefault();
      try {
        setLobbyStatus("Creating room...");
        const roomName = document.getElementById("roomName").value.trim();
        const roomPassword = document.getElementById("roomPassword").value;
        if (!roomName) return;
        const room = await api("/api/rooms", {
          method: "POST",
          body: JSON.stringify({ name: roomName, password: roomPassword }),
        });
        document.getElementById("roomName").value = "";
        document.getElementById("roomPassword").value = "";
        const rooms = await loadRooms();
        const created = rooms.find((r) => r.id === room.id);
        setLobbyStatus("Room created.");
        await joinRoom(room.id, room.name, created ? created.displayNo : 0, roomPassword, { allowPrompt: false });
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
        const msg = String(err.message || "").toLowerCase();
        if (!msg.includes("room not found") && !msg.includes("not in room")) {
          statusBar.textContent = err.message;
          return;
        }
      }

      safeCloseWs();
      enterLobbyView(true);
      await loadRooms();
      setLobbyStatus("You have left the room.");
    };
  }

  return {
    loadRooms,
    tryRestoreRoom,
    bindLobbyEvents,
  };
}
