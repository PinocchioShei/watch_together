"""运行时全局状态（连接池、管理员会话等）。"""

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import WebSocket


class RoomConnectionHub:
    """房间级 WebSocket 连接管理与广播。"""

    def __init__(self) -> None:
        self._rooms: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def add(self, room_id: int, ws: WebSocket) -> None:
        async with self._lock:
            if room_id not in self._rooms:
                self._rooms[room_id] = set()
            self._rooms[room_id].add(ws)

    async def remove(self, room_id: int, ws: WebSocket) -> None:
        async with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return
            room.discard(ws)
            if not room:
                self._rooms.pop(room_id, None)

    async def broadcast(self, room_id: int, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        room = list(self._rooms.get(room_id, set()))
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


hub = RoomConnectionHub()

# 管理员 token -> 过期时间
ADMIN_TOKENS: dict[str, datetime] = {}
