// 房间页会话管理：同账号多标签页锁 + 当前房间恢复。

const ROOM_SESSION_KEY = "wt_active_room";
const ROOM_SESSION_FALLBACK_KEY = "wt_active_room_fallback";

export function createTabLock(tabId) {
  let lockTimer = null;
  let activeLockKey = null;

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
    activeLockKey = lockKey;
    writeTabLock(lockKey);
    lockTimer = setInterval(() => writeTabLock(lockKey), 4000);
  }

  return { startTabLock, stopTabLock };
}

export function saveRoomSession(roomId, roomName, displayNo, roomPassword = "") {
  const payload = JSON.stringify({ roomId, roomName, displayNo, roomPassword, savedAt: Date.now() });
  sessionStorage.setItem(ROOM_SESSION_KEY, payload);
  try {
    localStorage.setItem(ROOM_SESSION_FALLBACK_KEY, payload);
  } catch {
  }
}

export function readRoomSession() {
  try {
    const primary = sessionStorage.getItem(ROOM_SESSION_KEY);
    if (primary) {
      return JSON.parse(primary);
    }
    const fallback = localStorage.getItem(ROOM_SESSION_FALLBACK_KEY);
    if (!fallback) {
      return null;
    }
    const parsed = JSON.parse(fallback);
    if (!parsed || !parsed.savedAt || Date.now() - parsed.savedAt > 24 * 3600 * 1000) {
      localStorage.removeItem(ROOM_SESSION_FALLBACK_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function clearRoomSession() {
  sessionStorage.removeItem(ROOM_SESSION_KEY);
  try {
    localStorage.removeItem(ROOM_SESSION_FALLBACK_KEY);
  } catch {
  }
}
