// 房间页会话管理：同账号多标签页锁 + 当前房间恢复。

const ROOM_SESSION_KEY = "wt_active_room";

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
    const lock = readTabLock(lockKey);
    if (lock && lock.tabId !== tabId && Date.now() - lock.ts < 15000) {
      throw new Error("This account is already active in another tab/window.");
    }
    activeLockKey = lockKey;
    writeTabLock(lockKey);
    lockTimer = setInterval(() => writeTabLock(lockKey), 4000);
  }

  return { startTabLock, stopTabLock };
}

export function saveRoomSession(roomId, roomName, displayNo) {
  sessionStorage.setItem(
    ROOM_SESSION_KEY,
    JSON.stringify({ roomId, roomName, displayNo, savedAt: Date.now() }),
  );
}

export function readRoomSession() {
  try {
    return JSON.parse(sessionStorage.getItem(ROOM_SESSION_KEY) || "null");
  } catch {
    return null;
  }
}

export function clearRoomSession() {
  sessionStorage.removeItem(ROOM_SESSION_KEY);
}
