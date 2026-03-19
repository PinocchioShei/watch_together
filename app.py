import asyncio
import hashlib
import json
import secrets
import shutil
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "watch_together.db"
STATIC_DIR = BASE_DIR / "static"
MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def media_url_from_name(name: str) -> str:
    return f"/media/{name}"


def is_valid_media_url(url: str) -> bool:
    if not url:
        return True
    if not url.startswith("/media/"):
        return False
    name = url.removeprefix("/media/")
    if not name or "/" in name or "\\" in name:
        return False
    suffix = Path(name).suffix.lower()
    return suffix in ALLOWED_MEDIA_EXTENSIONS


def list_media_library() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in MEDIA_DIR.iterdir():
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in ALLOWED_MEDIA_EXTENSIONS:
            continue
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "url": media_url_from_name(path.name),
                "size": stat.st_size,
                "updatedAt": dt_to_str(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
            }
        )
    items.sort(key=lambda item: item["updatedAt"], reverse=True)
    return items


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )
    return salt.hex(), key.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )
    return secrets.compare_digest(candidate.hex(), hash_hex)


class RegisterPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=6, max_length=128)


class LoginPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class RoomPayload(BaseModel):
    name: str = Field(min_length=2, max_length=60)


@dataclass
class User:
    id: int
    username: str


class RoomConnectionHub:
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


def init_db() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS room_members (
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (room_id, user_id),
            FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS room_state (
            room_id INTEGER PRIMARY KEY,
            video_url TEXT NOT NULL DEFAULT '',
            current_time REAL NOT NULL DEFAULT 0,
            is_playing INTEGER NOT NULL DEFAULT 0,
            controller_user_id INTEGER,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY (controller_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(room_state)").fetchall()}
    if "controller_user_id" not in columns:
        conn.execute("ALTER TABLE room_state ADD COLUMN controller_user_id INTEGER")
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_ffmpeg_tools() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(status_code=500, detail="ffmpeg/ffprobe not found on server")


def probe_media(file_path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail="Cannot parse uploaded media")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid media metadata") from exc


def extract_media_profile(probe_result: dict[str, Any]) -> dict[str, str]:
    fmt = (probe_result.get("format") or {}).get("format_name", "")
    streams = probe_result.get("streams") or []
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "container": (fmt.split(",")[0] if fmt else "unknown"),
        "videoCodec": v_stream.get("codec_name", "unknown"),
        "audioCodec": a_stream.get("codec_name", "none") if a_stream else "none",
    }


def is_browser_friendly_mp4(profile: dict[str, str]) -> bool:
    return (
        profile["container"] in {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
        and profile["videoCodec"] == "h264"
        and profile["audioCodec"] in {"aac", "mp3", "none"}
    )


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO sessions(token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, user_id, dt_to_str(now_utc() + timedelta(days=14)), dt_to_str(now_utc())),
    )
    conn.commit()
    return token


def get_user_by_token(conn: sqlite3.Connection, token: str | None) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    row = conn.execute(
        """
        SELECT u.id, u.username, s.expires_at
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Session expired")
    if str_to_dt(row["expires_at"]) < now_utc():
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        raise HTTPException(status_code=401, detail="Session expired")
    return User(id=row["id"], username=row["username"])


def extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    return None


def require_user(request: Request) -> User:
    conn = db_conn()
    try:
        token = extract_bearer_token(request) or request.cookies.get("session_token")
        return get_user_by_token(conn, token)
    finally:
        conn.close()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/register")
def register(payload: RegisterPayload):
    conn = db_conn()
    try:
        exists = conn.execute("SELECT id FROM users WHERE username = ?", (payload.username,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Username already exists")
        salt, password_hash = hash_password(payload.password)
        conn.execute(
            "INSERT INTO users(username, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (payload.username, salt, password_hash, dt_to_str(now_utc())),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/login")
def login(payload: LoginPayload):
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT id, password_salt, password_hash FROM users WHERE username = ?",
            (payload.username,),
        ).fetchone()
        if not row or not verify_password(payload.password, row["password_salt"], row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = create_session(conn, row["id"])
        from fastapi.responses import JSONResponse

        res = JSONResponse({"ok": True, "token": token})
        res.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            samesite="lax",
            max_age=14 * 24 * 3600,
        )
        return res
    finally:
        conn.close()


@app.post("/api/logout")
def logout(request: Request):
    conn = db_conn()
    try:
        token = request.cookies.get("session_token")
        if token:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        from fastapi.responses import JSONResponse

        res = JSONResponse({"ok": True})
        res.delete_cookie("session_token")
        return res
    finally:
        conn.close()


@app.get("/api/me")
def me(user: User = Depends(require_user)):
    return {"id": user.id, "username": user.username}


@app.get("/api/media")
def media_library(user: User = Depends(require_user)):
    del user
    return {"items": list_media_library()}


@app.get("/api/rooms")
def list_rooms(user: User = Depends(require_user)):
    conn = db_conn()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.name, r.owner_id, u.username AS owner_name,
                   (SELECT COUNT(*) FROM room_members rm WHERE rm.room_id = r.id) AS members
            FROM rooms r
            JOIN users u ON u.id = r.owner_id
            ORDER BY r.id DESC
            """
        ).fetchall()
        return {
            "rooms": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "ownerId": row["owner_id"],
                    "owner": row["owner_name"],
                    "members": row["members"],
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


@app.post("/api/rooms")
def create_room(payload: RoomPayload, user: User = Depends(require_user)):
    room_name = payload.name.strip()
    if len(room_name) < 2:
        raise HTTPException(status_code=422, detail="Room name too short")
    conn = db_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO rooms(name, owner_id, created_at) VALUES (?, ?, ?)",
            (room_name, user.id, dt_to_str(now_utc())),
        )
        room_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
            (room_id, user.id, dt_to_str(now_utc())),
        )
        conn.execute(
            "INSERT INTO room_state(room_id, updated_at, updated_by, controller_user_id) VALUES (?, ?, ?, ?)",
            (room_id, dt_to_str(now_utc()), user.id, user.id),
        )
        conn.commit()
        return {"id": room_id, "name": room_name}
    finally:
        conn.close()


@app.post("/api/rooms/{room_id}/join")
def join_room(room_id: int, user: User = Depends(require_user)):
    conn = db_conn()
    try:
        room = conn.execute("SELECT id FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        conn.execute(
            """
            INSERT INTO room_members(room_id, user_id, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(room_id, user_id) DO NOTHING
            """,
            (room_id, user.id, dt_to_str(now_utc())),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/rooms/{room_id}/state")
def room_state(room_id: int, user: User = Depends(require_user)):
    conn = db_conn()
    try:
        member = conn.execute(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user.id),
        ).fetchone()
        if not member:
            raise HTTPException(status_code=403, detail="Join room first")
        state = conn.execute(
            """
            SELECT
                rs.video_url,
                rs.current_time AS current_time_sec,
                rs.is_playing,
                rs.updated_at,
                rs.controller_user_id
            FROM room_state rs
            WHERE rs.room_id = ?
            """,
            (room_id,),
        ).fetchone()
        if not state:
            raise HTTPException(status_code=404, detail="State not found")
        safe_video_url = state["video_url"] if is_valid_media_url(state["video_url"]) else ""
        can_control = state["controller_user_id"] in (None, user.id)
        return {
            "videoUrl": safe_video_url,
            "currentTime": state["current_time_sec"],
            "isPlaying": bool(state["is_playing"]),
            "updatedAt": state["updated_at"],
            "controllerUserId": state["controller_user_id"],
            "canControl": can_control,
        }
    finally:
        conn.close()


@app.delete("/api/rooms/{room_id}")
async def delete_room(room_id: int, user: User = Depends(require_user)):
    conn = db_conn()
    try:
        room = conn.execute(
            "SELECT id, owner_id, name FROM rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        if room["owner_id"] != user.id:
            raise HTTPException(status_code=403, detail="Only the room owner can delete this room")
        room_name = room["name"]
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        conn.commit()
    finally:
        conn.close()

    await hub.broadcast(
        room_id,
        {
            "type": "room_deleted",
            "roomId": room_id,
            "roomName": room_name,
            "by": user.username,
        },
    )
    return {"ok": True, "roomId": room_id}


@app.post("/api/rooms/{room_id}/leave")
async def leave_room(room_id: int, user: User = Depends(require_user)):
    conn = db_conn()
    try:
        room = conn.execute(
            "SELECT id, owner_id, name FROM rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")

        member = conn.execute(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user.id),
        ).fetchone()
        if not member:
            raise HTTPException(status_code=403, detail="Not in room")

        if room["owner_id"] == user.id:
            room_name = room["name"]
            conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
            conn.commit()
            deleted = True
        else:
            conn.execute(
                "DELETE FROM room_members WHERE room_id = ? AND user_id = ?",
                (room_id, user.id),
            )
            conn.commit()
            deleted = False
            room_name = room["name"]
    finally:
        conn.close()

    if deleted:
        await hub.broadcast(
            room_id,
            {
                "type": "room_deleted",
                "roomId": room_id,
                "roomName": room_name,
                "by": user.username,
            },
        )

    return {"ok": True, "roomDeleted": deleted}


@app.post("/api/upload-video")
async def upload_video(file: UploadFile = File(...), user: User = Depends(require_user)):
    del user
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only mp4/webm/ogg/mov files are supported")

    ensure_ffmpeg_tools()

    raw_name = f"raw_{int(now_utc().timestamp())}_{secrets.token_hex(8)}{suffix}"
    raw_path = MEDIA_DIR / raw_name
    max_size = 1024 * 1024 * 1024
    size = 0

    with raw_path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_size:
                out.close()
                raw_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large, max 1GB")
            out.write(chunk)

    await file.close()

    probe_result = await asyncio.to_thread(probe_media, raw_path)
    profile = extract_media_profile(probe_result)

    if is_browser_friendly_mp4(profile):
        final_name = raw_name.replace("raw_", "playable_", 1)
        final_path = MEDIA_DIR / final_name
        raw_path.replace(final_path)
        return {
            "ok": True,
            "url": media_url_from_name(final_name),
            "profile": profile,
            "transcoded": False,
            "imported": True,
        }

    final_name = f"playable_{int(now_utc().timestamp())}_{secrets.token_hex(8)}.mp4"
    final_path = MEDIA_DIR / final_name
    streams = probe_result.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(raw_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(final_path)]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    raw_path.unlink(missing_ok=True)

    if process.returncode != 0:
        final_path.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", errors="ignore")[-400:]
        raise HTTPException(status_code=500, detail=f"Transcode failed: {detail or 'unknown error'}")

    return {
        "ok": True,
        "url": media_url_from_name(final_name),
        "profile": profile,
        "transcoded": True,
        "imported": True,
    }


def ensure_room_member(room_id: int, user_id: int, conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
        (room_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Not in room")


@app.websocket("/ws/rooms/{room_id}")
async def room_ws(websocket: WebSocket, room_id: int, token: str | None = None):
    conn = db_conn()
    try:
        user = get_user_by_token(conn, token or websocket.cookies.get("session_token"))
        ensure_room_member(room_id, user.id, conn)
    except HTTPException:
        await websocket.close(code=1008)
        conn.close()
        return
    conn.close()

    await websocket.accept()
    await hub.add(room_id, websocket)

    try:
        init_conn = db_conn()
        state = init_conn.execute(
            """
            SELECT
                rs.video_url,
                rs.current_time AS current_time_sec,
                rs.is_playing,
                rs.updated_at,
                rs.controller_user_id
            FROM room_state rs
            WHERE rs.room_id = ?
            """,
            (room_id,),
        ).fetchone()
        init_conn.close()
        initial_video_url = state["video_url"] if (state and is_valid_media_url(state["video_url"])) else ""
        await websocket.send_text(
            json.dumps(
                {
                    "type": "state",
                    "videoUrl": initial_video_url,
                    "currentTime": state["current_time_sec"] if state else 0,
                    "isPlaying": bool(state["is_playing"]) if state else False,
                    "updatedAt": state["updated_at"] if state else dt_to_str(now_utc()),
                    "controllerUserId": state["controller_user_id"] if state else user.id,
                    "actionId": 0,
                    "by": "system",
                }
            )
        )

        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            msg_type = payload.get("type")
            if msg_type == "sync":
                action_id = int(payload.get("actionId", 0))
                video_url = str(payload.get("videoUrl", "")).strip()[:500]
                if not is_valid_media_url(video_url):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": "Only videos under /media are allowed",
                            }
                        )
                    )
                    continue
                current_time = max(float(payload.get("currentTime", 0)), 0)
                is_playing = bool(payload.get("isPlaying", False))

                up_conn = db_conn()
                up_conn.execute(
                    """
                    INSERT INTO room_state(room_id, video_url, current_time, is_playing, controller_user_id, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(room_id)
                    DO UPDATE SET
                        video_url = excluded.video_url,
                        current_time = excluded.current_time,
                        is_playing = excluded.is_playing,
                        controller_user_id = excluded.controller_user_id,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        room_id,
                        video_url,
                        current_time,
                        1 if is_playing else 0,
                        user.id,
                        user.id,
                        dt_to_str(now_utc()),
                    ),
                )
                up_conn.commit()
                up_conn.close()

                await hub.broadcast(
                    room_id,
                    {
                        "type": "state",
                        "videoUrl": video_url,
                        "currentTime": current_time,
                        "isPlaying": is_playing,
                        "updatedAt": dt_to_str(now_utc()),
                        "controllerUserId": user.id,
                        "actionId": action_id,
                        "by": user.username,
                    },
                )
            elif msg_type == "chat":
                msg = str(payload.get("message", "")).strip()[:400]
                if not msg:
                    continue
                await hub.broadcast(
                    room_id,
                    {
                        "type": "chat",
                        "message": msg,
                        "by": user.username,
                        "sentAt": dt_to_str(now_utc()),
                    },
                )
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(room_id, websocket)
