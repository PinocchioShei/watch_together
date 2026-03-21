"""运行时全局状态（连接池、管理员会话等）。"""

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import WebSocket


class RoomConnectionHub:
    """房间级 WebSocket 连接管理与广播。"""

    def __init__(self) -> None:
        self._rooms: dict[int, dict[WebSocket, int]] = {}
        self._lock = asyncio.Lock()

    async def add(self, room_id: int, ws: WebSocket, user_id: int) -> None:
        async with self._lock:
            if room_id not in self._rooms:
                self._rooms[room_id] = {}
            self._rooms[room_id][ws] = user_id

    async def remove(self, room_id: int, ws: WebSocket) -> None:
        async with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return
            room.pop(ws, None)
            if not room:
                self._rooms.pop(room_id, None)

    async def has_user(self, room_id: int, user_id: int) -> bool:
        async with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return False
            return user_id in room.values()

    async def room_user_ids(self, room_id: int) -> set[int]:
        async with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return set()
            return set(room.values())

    async def broadcast(self, room_id: int, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        room = list(self._rooms.get(room_id, {}).keys())
        if not room:
            return
        broken: list[WebSocket] = []
        for ws in room:
            try:
                await ws.send_text(message)
            except Exception:
                broken.append(ws)
        for ws in broken:
            await self.remove(room_id, ws)

    async def disconnect_user(self, user_id: int, code: int = 4001, reason: str = "session-replaced") -> None:
        async with self._lock:
            targets: list[tuple[int, WebSocket]] = []
            for room_id, sockets in self._rooms.items():
                for ws, uid in sockets.items():
                    if uid == user_id:
                        targets.append((room_id, ws))

        for room_id, ws in targets:
            try:
                await ws.send_text(json.dumps({"type": "error", "message": "Your account signed in on another device."}))
            except Exception:
                pass
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass
            await self.remove(room_id, ws)


hub = RoomConnectionHub()

# 管理员 token -> 过期时间
ADMIN_TOKENS: dict[str, datetime] = {}
