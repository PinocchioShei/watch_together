import asyncio
import hashlib
import json
import os
import re
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
MEDIA_VIDEO_DIR = MEDIA_DIR / "video"
MEDIA_AUDIO_DIR = MEDIA_DIR / "audio"
MEDIA_TMP_DIR = MEDIA_DIR / "tmp"
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov"}
ALLOWED_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".ogg", ".wav", ".aac"}
ADMIN_USERNAME = os.getenv("WATCH_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("WATCH_ADMIN_PASSWORD", "admin123")
ADMIN_SESSION_TTL = timedelta(hours=12)

ADMIN_TOKENS: dict[str, datetime] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def media_url_from_name(kind: str, name: str) -> str:
    return f"/media/{kind}/{name}"


def sanitize_filename_stem(name: str) -> str:
    stem = (name or "").strip()
    stem = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", stem)
    stem = stem.strip(" .")
    stem = re.sub(r"\s+", " ", stem)
    if not stem:
        stem = "media"
    return stem[:64]


def allocate_media_stem(original_stem: str) -> str:
    base = sanitize_filename_stem(original_stem)
    pattern = re.compile(rf"^{re.escape(base)}_(\d+)$")

    max_no = 0
    for directory, ext_set in ((MEDIA_VIDEO_DIR, ALLOWED_VIDEO_EXTENSIONS), (MEDIA_AUDIO_DIR, ALLOWED_AUDIO_EXTENSIONS)):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in ext_set:
                continue
            m = pattern.match(path.stem)
            if m:
                max_no = max(max_no, int(m.group(1)))

    next_no = max_no + 1
    return f"{base}_{next_no:03d}"


def is_valid_media_url(url: str) -> bool:
    if not url:
        return True
    if not url.startswith("/media/"):
        return False
    rel = url.removeprefix("/media/")
    if not rel:
        return False
    parts = rel.split("/")
    if len(parts) != 2:
        return False
    kind, name = parts
    if kind not in {"video", "audio"} or not name or "/" in name or "\\" in name:
        return False
    suffix = Path(name).suffix.lower()
    if kind == "video":
        return suffix in ALLOWED_VIDEO_EXTENSIONS
    return suffix in ALLOWED_AUDIO_EXTENSIONS


def collect_media_files() -> dict[str, dict[str, Any]]:
    audio_by_stem: dict[str, dict[str, Any]] = {}
    if MEDIA_AUDIO_DIR.exists():
        for path in MEDIA_AUDIO_DIR.iterdir():
            if not path.is_file() or path.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
                continue
            stat = path.stat()
            audio_by_stem[path.stem] = {
                "audioUrl": media_url_from_name("audio", path.name),
                "size": stat.st_size,
                "updatedAt": dt_to_str(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
            }

    files: dict[str, dict[str, Any]] = {}
    if MEDIA_VIDEO_DIR.exists():
        for path in MEDIA_VIDEO_DIR.iterdir():
            if not path.is_file() or path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
                continue
            stat = path.stat()
            stem = path.stem
            files[stem] = {
                "videoUrl": media_url_from_name("video", path.name),
                "audioUrl": (audio_by_stem.get(stem) or {}).get("audioUrl", ""),
                "size": stat.st_size,
                "updatedAt": dt_to_str(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
            }

    for stem, audio_meta in audio_by_stem.items():
        if stem in files:
            continue
        files[stem] = {
            "videoUrl": "",
            "audioUrl": audio_meta["audioUrl"],
            "size": audio_meta["size"],
            "updatedAt": audio_meta["updatedAt"],
        }
    return files


def list_media_library(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    file_index = collect_media_files()
    rows = conn.execute(
        """
        SELECT id, title, video_url, audio_url, duration, size, updated_at
        FROM media_assets
        ORDER BY updated_at DESC
        """
    ).fetchall()
    items: list[dict[str, Any]] = []
    seen_stems: set[str] = set()

    for row in rows:
        video_url = row["video_url"] or ""
        video_name = Path(video_url).name
        stem = Path(video_name).stem
        file_meta = file_index.get(stem)
        if not file_meta:
            continue
        seen_stems.add(stem)
        items.append(
            {
                "id": row["id"],
                "name": row["title"] or stem,
                "videoUrl": file_meta["videoUrl"],
                "audioUrl": file_meta["audioUrl"] or row["audio_url"] or "",
                "duration": row["duration"] or 0,
                "size": file_meta["size"],
                "updatedAt": file_meta["updatedAt"],
            }
        )

    for stem, file_meta in file_index.items():
        if stem in seen_stems:
            continue
        items.append(
            {
                "id": None,
                "name": stem,
                "videoUrl": file_meta["videoUrl"],
                "audioUrl": file_meta["audioUrl"],
                "duration": 0,
                "size": file_meta["size"],
                "updatedAt": file_meta["updatedAt"],
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


class AdminLoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class AdminCreateUserPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=6, max_length=128)


class AdminUpdateUserPayload(BaseModel):
    password: str = Field(min_length=6, max_length=128)


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
    MEDIA_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_TMP_DIR.mkdir(parents=True, exist_ok=True)
    for path in MEDIA_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS:
            target = MEDIA_VIDEO_DIR / path.name
            if not target.exists():
                path.replace(target)
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

        CREATE TABLE IF NOT EXISTS media_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            video_url TEXT NOT NULL UNIQUE,
            audio_url TEXT,
            duration REAL NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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


def create_admin_session_token() -> str:
    token = secrets.token_urlsafe(32)
    ADMIN_TOKENS[token] = now_utc() + ADMIN_SESSION_TTL
    return token


def cleanup_admin_tokens() -> None:
    expired = [token for token, exp in ADMIN_TOKENS.items() if exp < now_utc()]
    for token in expired:
        ADMIN_TOKENS.pop(token, None)


def require_admin(request: Request) -> str:
    token = extract_bearer_token(request)
    cleanup_admin_tokens()
    if not token or token not in ADMIN_TOKENS:
        raise HTTPException(status_code=401, detail="Admin auth required")
    return token


def stem_from_media_url(url: str) -> str:
    return Path(url).stem


def remove_media_by_stem(conn: sqlite3.Connection, stem: str) -> dict[str, Any]:
    if not re.fullmatch(r"[\w\-\u4e00-\u9fff\s\.]+", stem):
        raise HTTPException(status_code=400, detail="Invalid media key")

    deleted_files: list[str] = []
    for directory, ext_set in ((MEDIA_VIDEO_DIR, ALLOWED_VIDEO_EXTENSIONS), (MEDIA_AUDIO_DIR, ALLOWED_AUDIO_EXTENSIONS)):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in ext_set:
                continue
            if path.stem == stem:
                path.unlink(missing_ok=True)
                deleted_files.append(str(path.name))

    rows = conn.execute("SELECT id, video_url FROM media_assets").fetchall()
    delete_ids = [row["id"] for row in rows if stem_from_media_url(row["video_url"] or "") == stem]
    if delete_ids:
        conn.executemany("DELETE FROM media_assets WHERE id = ?", [(mid,) for mid in delete_ids])
    conn.commit()

    return {"deletedFiles": deleted_files, "deletedAssetRows": len(delete_ids)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.post("/api/admin/login")
def admin_login(payload: AdminLoginPayload):
    if payload.username != ADMIN_USERNAME or payload.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    token = create_admin_session_token()
    return {"ok": True, "token": token}


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    token = extract_bearer_token(request)
    if token:
        ADMIN_TOKENS.pop(token, None)
    return {"ok": True}


@app.get("/api/admin/overview")
def admin_overview(_: str = Depends(require_admin)):
    conn = db_conn()
    try:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        rooms = conn.execute("SELECT COUNT(*) AS c FROM rooms").fetchone()["c"]
        sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
        media_rows = conn.execute("SELECT COUNT(*) AS c FROM media_assets").fetchone()["c"]
    finally:
        conn.close()
    file_index = collect_media_files()
    return {
        "users": users,
        "rooms": rooms,
        "sessions": sessions,
        "mediaDbRows": media_rows,
        "mediaScanned": len(file_index),
    }


@app.get("/api/admin/users")
def admin_users(_: str = Depends(require_admin)):
    conn = db_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, created_at FROM users ORDER BY id DESC"
        ).fetchall()
        return {"items": [{"id": r["id"], "username": r["username"], "createdAt": r["created_at"]} for r in rows]}
    finally:
        conn.close()


@app.post("/api/admin/users")
def admin_create_user(payload: AdminCreateUserPayload, _: str = Depends(require_admin)):
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


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, payload: AdminUpdateUserPayload, _: str = Depends(require_admin)):
    conn = db_conn()
    try:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        salt, password_hash = hash_password(payload.password)
        conn.execute(
            "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
            (salt, password_hash, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, _: str = Depends(require_admin)):
    conn = db_conn()
    try:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/admin/media")
def admin_media(_: str = Depends(require_admin)):
    conn = db_conn()
    try:
        items = list_media_library(conn)
    finally:
        conn.close()
    for item in items:
        item["mediaKey"] = stem_from_media_url(item["videoUrl"] or item["audioUrl"])
    return {"items": items}


@app.delete("/api/admin/media/{media_key}")
def admin_delete_media(media_key: str, _: str = Depends(require_admin)):
    conn = db_conn()
    try:
        result = remove_media_by_stem(conn, media_key)
        return {"ok": True, **result}
    finally:
        conn.close()


@app.post("/api/admin/import")
async def admin_import(file: UploadFile = File(...), _: str = Depends(require_admin)):
    return await import_media_file(file)


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
    conn = db_conn()
    try:
        return {"items": list_media_library(conn)}
    finally:
        conn.close()


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
        play_mode = "audio" if safe_video_url.startswith("/media/audio/") else "video"
        can_control = state["controller_user_id"] in (None, user.id)
        return {
            "videoUrl": safe_video_url,
            "playMode": play_mode,
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


async def import_media_file(file: UploadFile) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only mp4/webm/ogg/mov files are supported")

    ensure_ffmpeg_tools()

    source_stem = Path(file.filename or "media").stem
    media_stem = allocate_media_stem(source_stem)
    raw_name = f"raw_{media_stem}_{secrets.token_hex(4)}{suffix}"
    raw_path = MEDIA_TMP_DIR / raw_name
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
    duration = 0.0
    try:
        duration = float((probe_result.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    streams = probe_result.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    video_name = f"{media_stem}.mp4"
    video_path = MEDIA_VIDEO_DIR / video_name
    audio_name = f"{media_stem}.m4a"
    audio_path = MEDIA_AUDIO_DIR / audio_name

    if is_browser_friendly_mp4(profile):
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_path),
            "-c:v",
            "copy",
            "-c:a",
            "copy" if has_audio else "aac",
            "-movflags",
            "+faststart",
            str(video_path),
        ]
    else:
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
        cmd += ["-movflags", "+faststart", str(video_path)]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    raw_path.unlink(missing_ok=True)

    if process.returncode != 0:
        video_path.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", errors="ignore")[-400:]
        raise HTTPException(status_code=500, detail=f"Transcode failed: {detail or 'unknown error'}")

    audio_url = ""
    if has_audio:
        audio_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(audio_path),
        ]
        audio_proc = await asyncio.create_subprocess_exec(
            *audio_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, audio_stderr = await audio_proc.communicate()
        if audio_proc.returncode == 0:
            audio_url = media_url_from_name("audio", audio_name)
        else:
            audio_path.unlink(missing_ok=True)
            _ = audio_stderr

    title = sanitize_filename_stem(Path(file.filename or video_name).stem)
    now_str = dt_to_str(now_utc())
    conn = db_conn()
    try:
        conn.execute(
            """
            INSERT INTO media_assets(title, video_url, audio_url, duration, size, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                media_url_from_name("video", video_name),
                audio_url,
                duration,
                video_path.stat().st_size,
                now_str,
                now_str,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "videoUrl": media_url_from_name("video", video_name),
        "audioUrl": audio_url,
        "profile": profile,
        "transcoded": not is_browser_friendly_mp4(profile),
        "imported": True,
    }


@app.post("/api/upload-video")
async def upload_video(file: UploadFile = File(...), user: User = Depends(require_user)):
    del user
    return await import_media_file(file)


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
        initial_play_mode = "audio" if initial_video_url.startswith("/media/audio/") else "video"
        await websocket.send_text(
            json.dumps(
                {
                    "type": "state",
                    "videoUrl": initial_video_url,
                    "playMode": initial_play_mode,
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
                play_mode = str(payload.get("playMode", "video")).strip().lower()
                if play_mode not in {"video", "audio"}:
                    play_mode = "audio" if video_url.startswith("/media/audio/") else "video"
                if not is_valid_media_url(video_url):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": "Only media files under /media are allowed",
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
                        "playMode": play_mode,
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
