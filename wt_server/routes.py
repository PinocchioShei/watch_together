"""FastAPI 路由装配与应用创建。"""

import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    create_admin_session_token,
    create_session,
    extract_bearer_token,
    get_user_by_token,
    hash_password,
    require_admin,
    require_user,
    verify_password,
)
from .config import ADMIN_PASSWORD, ADMIN_USERNAME, MEDIA_DIR, STATIC_DIR
from .media import (
    collect_media_files,
    import_media_file,
    is_audio_media_url,
    is_valid_media_url,
    list_media_library,
    migrate_media_layout,
    normalize_media_url,
    rename_media_work,
    remove_media_by_stem,
    stem_from_media_url,
)
from .schemas import (
    AdminCreateUserPayload,
    AdminLoginPayload,
    AdminRenameMediaPayload,
    AdminUpdateMediaTypePayload,
    AdminUpdateProfilePayload,
    AdminUpdateUserPayload,
    LoginPayload,
    RegisterPayload,
    RoomJoinPayload,
    RoomPayload,
    User,
)
from .state import ADMIN_TOKENS, hub
from .storage import db_conn, init_db
from .utils import dt_to_str, now_utc, str_to_dt


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_admin_row(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT id, username, password_salt, password_hash FROM admin_account WHERE id = 1"
    ).fetchone()


def ensure_admin_account(conn: sqlite3.Connection):
    row = get_admin_row(conn)
    if row:
        return row
    salt, password_hash = hash_password(ADMIN_PASSWORD)
    conn.execute(
        "INSERT INTO admin_account(id, username, password_salt, password_hash, updated_at) VALUES (1, ?, ?, ?, ?)",
        (ADMIN_USERNAME, salt, password_hash, dt_to_str(now_utc())),
    )
    conn.commit()
    return get_admin_row(conn)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时保证目录与数据库结构可用。
    init_db()
    conn = db_conn()
    try:
        migrate_media_layout(conn)
    finally:
        conn.close()
    yield


def ensure_room_member(room_id: int, user_id: int) -> None:
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=403, detail="Not in room")
    finally:
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

    async def build_room_members_payload(room_id: int) -> dict:
        online_ids = await hub.room_user_ids(room_id)
        if not online_ids:
            return {"type": "members", "roomId": room_id, "onlineCount": 0, "members": []}

        conn = db_conn()
        try:
            room = conn.execute("SELECT owner_id FROM rooms WHERE id = ?", (room_id,)).fetchone()
            owner_id = room["owner_id"] if room else None
            placeholders = ",".join("?" for _ in online_ids)
            rows = conn.execute(
                f"SELECT id, username FROM users WHERE id IN ({placeholders}) ORDER BY username COLLATE NOCASE",
                tuple(online_ids),
            ).fetchall()
        finally:
            conn.close()

        members = [{"id": row["id"], "username": row["username"], "isOwner": row["id"] == owner_id} for row in rows]
        return {"type": "members", "roomId": room_id, "onlineCount": len(members), "members": members}

    async def broadcast_room_members(room_id: int) -> None:
        await hub.broadcast(room_id, await build_room_members_payload(room_id))

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/admin")
    def admin_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html")

    @app.post("/api/admin/login")
    def admin_login(payload: AdminLoginPayload):
        conn = db_conn()
        try:
            admin_row = ensure_admin_account(conn)
        finally:
            conn.close()
        if payload.username != admin_row["username"] or not verify_password(
            payload.password,
            admin_row["password_salt"],
            admin_row["password_hash"],
        ):
            raise HTTPException(status_code=401, detail="Invalid admin credentials")
        token = create_admin_session_token()
        return {"ok": True, "token": token}

    @app.patch("/api/admin/profile")
    def admin_update_profile(payload: AdminUpdateProfilePayload, _: str = Depends(require_admin)):
        conn = db_conn()
        try:
            admin_row = ensure_admin_account(conn)
            if not verify_password(payload.currentPassword, admin_row["password_salt"], admin_row["password_hash"]):
                raise HTTPException(status_code=401, detail="Current password is incorrect")

            next_username = (payload.newUsername or admin_row["username"]).strip()
            next_password = payload.newPassword or None
            if not next_username:
                raise HTTPException(status_code=400, detail="Username cannot be empty")
            if not next_password and next_username == admin_row["username"]:
                raise HTTPException(status_code=400, detail="No changes to apply")

            salt = admin_row["password_salt"]
            password_hash = admin_row["password_hash"]
            if next_password:
                salt, password_hash = hash_password(next_password)

            conn.execute(
                "UPDATE admin_account SET username = ?, password_salt = ?, password_hash = ?, updated_at = ? WHERE id = 1",
                (next_username, salt, password_hash, dt_to_str(now_utc())),
            )
            conn.commit()
            return {"ok": True, "username": next_username}
        finally:
            conn.close()

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
        return {
            "users": users,
            "rooms": rooms,
            "sessions": sessions,
            "mediaDbRows": media_rows,
            "mediaScanned": len(collect_media_files()),
        }

    @app.get("/api/admin/users")
    def admin_users(_: str = Depends(require_admin)):
        conn = db_conn()
        try:
            rows = conn.execute("SELECT id, username, created_at FROM users ORDER BY id DESC").fetchall()
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

    @app.get("/api/admin/rooms")
    async def admin_rooms(_: str = Depends(require_admin)):
        conn = db_conn()
        try:
            rows = conn.execute(
                """
                SELECT r.id, r.name, r.owner_id, u.username AS owner_name,
                       r.password_hash,
                       (SELECT COUNT(*) FROM room_members rm WHERE rm.room_id = r.id) AS members
                FROM rooms r
                JOIN users u ON u.id = r.owner_id
                ORDER BY r.id DESC
                """
            ).fetchall()
        finally:
            conn.close()

        online_map: dict[int, int] = {}
        for row in rows:
            online_map[row["id"]] = len(await hub.room_user_ids(int(row["id"])))

        return {
            "items": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "ownerId": row["owner_id"],
                    "owner": row["owner_name"],
                    "members": row["members"],
                    "online": online_map.get(row["id"], 0),
                }
                for row in rows
            ]
        }

    @app.delete("/api/admin/media/{media_key}")
    def admin_delete_media(media_key: str, _: str = Depends(require_admin)):
        conn = db_conn()
        try:
            result = remove_media_by_stem(conn, media_key)
            return {"ok": True, **result}
        finally:
            conn.close()

    @app.patch("/api/admin/media/{media_key}")
    def admin_rename_media(media_key: str, payload: AdminRenameMediaPayload, _: str = Depends(require_admin)):
        conn = db_conn()
        try:
            return rename_media_work(conn, media_key, payload.newWorkName)
        finally:
            conn.close()

    @app.patch("/api/admin/media/{media_key}/type")
    def admin_update_media_type(media_key: str, payload: AdminUpdateMediaTypePayload, _: str = Depends(require_admin)):
        media_type = (payload.mediaType or "").strip()
        if media_type not in {"movie", "RJ", "ASMR", "music", "shot"}:
            raise HTTPException(status_code=422, detail="Invalid media type")

        conn = db_conn()
        try:
            row = conn.execute(
                "SELECT id FROM media_assets WHERE video_url LIKE ? OR audio_url LIKE ? ORDER BY updated_at DESC LIMIT 1",
                (f"/media/work/{media_key}/%", f"/media/work/{media_key}/%"),
            ).fetchone()
            now_str = dt_to_str(now_utc())
            if row:
                conn.execute(
                    "UPDATE media_assets SET media_type = ?, updated_at = ? WHERE id = ?",
                    (media_type, now_str, row["id"]),
                )
            else:
                # Some historical assets exist only on filesystem under media/work/<work>/ and
                # have no DB row yet. Create a lightweight row so type update can persist.
                file_index = collect_media_files()
                file_meta = file_index.get(media_key)
                if not file_meta:
                    raise HTTPException(status_code=404, detail="Media not found")
                conn.execute(
                    """
                    INSERT INTO media_assets(title, video_url, audio_url, cover_url, media_type, duration, size, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        media_key,
                        file_meta.get("videoUrl") or "",
                        file_meta.get("audioUrl") or "",
                        f"/media/work/{media_key}/cover.jpg",
                        media_type,
                        0,
                        int(file_meta.get("size") or 0),
                        now_str,
                        now_str,
                    ),
                )
            conn.commit()
            return {"ok": True, "mediaKey": media_key, "type": media_type}
        finally:
            conn.close()

    @app.delete("/api/admin/rooms/{room_id}")
    async def admin_delete_room(room_id: int, _: str = Depends(require_admin)):
        conn = db_conn()
        try:
            room = conn.execute("SELECT id, name FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room:
                raise HTTPException(status_code=404, detail="Room not found")
            room_name = room["name"]
            conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
            conn.commit()
        finally:
            conn.close()

        await hub.broadcast(room_id, {"type": "room_deleted", "roomId": room_id, "roomName": room_name, "by": "admin"})
        return {"ok": True, "roomId": room_id}

    @app.post("/api/admin/import")
    async def admin_import(
        file: UploadFile = File(...),
        media_type: str = File("movie"),
        cover: UploadFile | None = File(default=None),
        _: str = Depends(require_admin),
    ):
        return await import_media_file(file, media_type, cover)

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
    async def login(payload: LoginPayload):
        conn = db_conn()
        try:
            row = conn.execute(
                "SELECT id, password_salt, password_hash FROM users WHERE username = ?",
                (payload.username,),
            ).fetchone()
            if not row or not verify_password(payload.password, row["password_salt"], row["password_hash"]):
                raise HTTPException(status_code=401, detail="Invalid credentials")

            existing_rows = conn.execute(
                "SELECT token, expires_at FROM sessions WHERE user_id = ?",
                (row["id"],),
            ).fetchall()
            if existing_rows:
                valid_tokens = [r["token"] for r in existing_rows if str_to_dt(r["expires_at"]) >= now_utc()]
                # 放宽策略：新登录覆盖旧会话，避免本机退登后短暂锁死。
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
                conn.commit()
                if valid_tokens:
                    await hub.disconnect_user(int(row["id"]))

            token = create_session(conn, row["id"])
            res = JSONResponse({"ok": True, "token": token})
            res.set_cookie(key="session_token", value=token, httponly=True, samesite="lax", max_age=14 * 24 * 3600)
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
        del user
        conn = db_conn()
        try:
            rows = conn.execute(
                """
                SELECT r.id, r.name, r.owner_id, u.username AS owner_name,
                       r.password_hash,
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
                        "hasPassword": bool(row["password_hash"]),
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
        room_password = payload.password
        if len(room_password) < 4:
            raise HTTPException(status_code=422, detail="Room password too short")
        conn = db_conn()
        try:
            room_salt, room_hash = hash_password(room_password)
            cursor = conn.execute(
                "INSERT INTO rooms(name, owner_id, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (room_name, user.id, room_salt, room_hash, dt_to_str(now_utc())),
            )
            room_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO room_members(room_id, user_id, room_password_cache, joined_at) VALUES (?, ?, ?, ?)",
                (room_id, user.id, room_password, dt_to_str(now_utc())),
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
    def join_room(room_id: int, payload: RoomJoinPayload, user: User = Depends(require_user)):
        conn = db_conn()
        try:
            room = conn.execute("SELECT id, owner_id, password_salt, password_hash FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room:
                raise HTTPException(status_code=404, detail="Room not found")

            room_salt = room["password_salt"]
            room_hash = room["password_hash"]
            member = conn.execute(
                "SELECT room_password_cache FROM room_members WHERE room_id = ? AND user_id = ?",
                (room_id, user.id),
            ).fetchone()
            access_cache = conn.execute(
                "SELECT room_password_cache FROM room_access_cache WHERE room_id = ? AND user_id = ?",
                (room_id, user.id),
            ).fetchone()
            cached_password = (member["room_password_cache"] or "") if member else ""
            access_cached_password = (access_cache["room_password_cache"] or "") if access_cache else ""
            submitted_password = (payload.password or "").strip()

            if room_salt and room_hash:
                password_ok = False
                if submitted_password:
                    password_ok = verify_password(submitted_password, room_salt, room_hash)
                elif cached_password:
                    password_ok = verify_password(cached_password, room_salt, room_hash)
                elif access_cached_password:
                    password_ok = verify_password(access_cached_password, room_salt, room_hash)
                if not password_ok:
                    raise HTTPException(status_code=403, detail="Invalid room password")

            cache_to_save = submitted_password or cached_password or access_cached_password
            conn.execute(
                """
                INSERT INTO room_members(room_id, user_id, room_password_cache, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(room_id, user_id) DO UPDATE SET
                    room_password_cache = CASE
                        WHEN excluded.room_password_cache IS NULL OR excluded.room_password_cache = '' THEN room_members.room_password_cache
                        ELSE excluded.room_password_cache
                    END,
                    joined_at = excluded.joined_at
                """,
                (room_id, user.id, cache_to_save, dt_to_str(now_utc())),
            )
            if cache_to_save:
                conn.execute(
                    """
                    INSERT INTO room_access_cache(room_id, user_id, room_password_cache, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(room_id, user_id) DO UPDATE SET
                        room_password_cache = excluded.room_password_cache,
                        updated_at = excluded.updated_at
                    """,
                    (room_id, user.id, cache_to_save, dt_to_str(now_utc())),
                )
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @app.get("/api/rooms/{room_id}/state")
    def room_state(room_id: int, user: User = Depends(require_user)):
        conn = db_conn()
        try:
            member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, user.id)).fetchone()
            if not member:
                raise HTTPException(status_code=403, detail="Join room first")
            state = conn.execute(
                """
                SELECT rs.video_url, rs.current_time AS current_time_sec, rs.is_playing, rs.updated_at, rs.controller_user_id
                FROM room_state rs
                WHERE rs.room_id = ?
                """,
                (room_id,),
            ).fetchone()
            if not state:
                raise HTTPException(status_code=404, detail="State not found")
            controller_user_id = state["controller_user_id"]
            if controller_user_id is not None and not conn.execute(
                "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
                (room_id, controller_user_id),
            ).fetchone():
                controller_user_id = None
                conn.execute(
                    "UPDATE room_state SET controller_user_id = NULL, updated_at = ? WHERE room_id = ?",
                    (dt_to_str(now_utc()), room_id),
                )
                conn.commit()
            normalized_video_url = normalize_media_url(state["video_url"] or "")
            safe_video_url = normalized_video_url if is_valid_media_url(normalized_video_url) else ""
            if safe_video_url and safe_video_url != (state["video_url"] or ""):
                conn.execute("UPDATE room_state SET video_url = ?, updated_at = ? WHERE room_id = ?", (safe_video_url, dt_to_str(now_utc()), room_id))
                conn.commit()
            play_mode = "audio" if is_audio_media_url(safe_video_url) else "video"
            can_control = controller_user_id in (None, user.id)
            return {
                "videoUrl": safe_video_url,
                "playMode": play_mode,
                "currentTime": state["current_time_sec"],
                "isPlaying": bool(state["is_playing"]),
                "updatedAt": state["updated_at"],
                "controllerUserId": controller_user_id,
                "canControl": can_control,
            }
        finally:
            conn.close()

    @app.delete("/api/rooms/{room_id}")
    async def delete_room(room_id: int, user: User = Depends(require_user)):
        conn = db_conn()
        try:
            room = conn.execute("SELECT id, owner_id, name FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room:
                raise HTTPException(status_code=404, detail="Room not found")
            if room["owner_id"] != user.id:
                raise HTTPException(status_code=403, detail="Only the room owner can delete this room")
            room_name = room["name"]
            conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
            conn.commit()
        finally:
            conn.close()

        await hub.broadcast(room_id, {"type": "room_deleted", "roomId": room_id, "roomName": room_name, "by": user.username})
        return {"ok": True, "roomId": room_id}

    @app.post("/api/rooms/{room_id}/leave")
    async def leave_room(room_id: int, user: User = Depends(require_user)):
        conn = db_conn()
        try:
            room = conn.execute("SELECT id, owner_id, name FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room:
                raise HTTPException(status_code=404, detail="Room not found")
            member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, user.id)).fetchone()
            if not member:
                raise HTTPException(status_code=403, detail="Not in room")

            if room["owner_id"] == user.id:
                room_name = room["name"]
                conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
                conn.commit()
                deleted = True
            else:
                conn.execute("DELETE FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, user.id))
                conn.commit()
                deleted = False
                room_name = room["name"]
        finally:
            conn.close()

        if deleted:
            await hub.broadcast(room_id, {"type": "room_deleted", "roomId": room_id, "roomName": room_name, "by": user.username})
        else:
            await broadcast_room_members(room_id)
        return {"ok": True, "roomDeleted": deleted}

    @app.post("/api/upload-video")
    async def upload_video(
        file: UploadFile = File(...),
        media_type: str = File("movie"),
        cover: UploadFile | None = File(default=None),
        user: User = Depends(require_user),
    ):
        del user
        return await import_media_file(file, media_type, cover)

    @app.websocket("/ws/rooms/{room_id}")
    async def room_ws(websocket: WebSocket, room_id: int, token: str | None = None):
        conn = db_conn()
        is_owner = False
        try:
            user = get_user_by_token(conn, token or websocket.cookies.get("session_token"))
            row = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, user.id)).fetchone()
            if not row:
                raise HTTPException(status_code=403, detail="Not in room")
            owner_row = conn.execute("SELECT owner_id FROM rooms WHERE id = ?", (room_id,)).fetchone()
            is_owner = bool(owner_row and owner_row["owner_id"] == user.id)
        except HTTPException:
            await websocket.close(code=1008)
            conn.close()
            return
        conn.close()

        await websocket.accept()
        await hub.add(room_id, websocket, user.id)
        await broadcast_room_members(room_id)

        try:
            init_conn = db_conn()
            state = init_conn.execute(
                """
                SELECT rs.video_url, rs.current_time AS current_time_sec, rs.is_playing, rs.updated_at, rs.controller_user_id
                FROM room_state rs
                WHERE rs.room_id = ?
                """,
                (room_id,),
            ).fetchone()
            init_conn.close()
            initial_raw = state["video_url"] if state else ""
            normalized_initial = normalize_media_url(initial_raw)
            initial_video_url = normalized_initial if is_valid_media_url(normalized_initial) else ""
            initial_play_mode = "audio" if is_audio_media_url(initial_video_url) else "video"
            initial_controller = state["controller_user_id"] if state else user.id
            if initial_controller is not None and not await hub.has_user(room_id, int(initial_controller)):
                initial_controller = None
                fix_conn = db_conn()
                try:
                    fix_conn.execute(
                        "UPDATE room_state SET controller_user_id = NULL, updated_at = ? WHERE room_id = ?",
                        (dt_to_str(now_utc()), room_id),
                    )
                    fix_conn.commit()
                finally:
                    fix_conn.close()
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "state",
                        "videoUrl": initial_video_url,
                        "playMode": initial_play_mode,
                        "currentTime": state["current_time_sec"] if state else 0,
                        "isPlaying": bool(state["is_playing"]) if state else False,
                        "updatedAt": state["updated_at"] if state else dt_to_str(now_utc()),
                        "controllerUserId": initial_controller,
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
                    video_url = normalize_media_url(video_url)
                    play_mode = str(payload.get("playMode", "video")).strip().lower()
                    if play_mode not in {"video", "audio"}:
                        play_mode = "audio" if is_audio_media_url(video_url) else "video"
                    if not is_valid_media_url(video_url):
                        await websocket.send_text(json.dumps({"type": "error", "message": "Only media files under /media are allowed"}))
                        continue
                    current_time = max(float(payload.get("currentTime", 0)), 0)
                    is_playing = bool(payload.get("isPlaying", False))

                    up_conn = db_conn()
                    should_broadcast_state = False
                    try:
                        latest_state = up_conn.execute(
                            "SELECT controller_user_id FROM room_state WHERE room_id = ?",
                            (room_id,),
                        ).fetchone()
                        active_controller = latest_state["controller_user_id"] if latest_state else None
                        if active_controller is not None and not await hub.has_user(room_id, int(active_controller)):
                            active_controller = None

                        takeover = bool(payload.get("forceTakeover", False))
                        if active_controller not in (None, user.id) and not takeover:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "state",
                                        "videoUrl": video_url,
                                        "playMode": play_mode,
                                        "currentTime": current_time,
                                        "isPlaying": is_playing,
                                        "updatedAt": dt_to_str(now_utc()),
                                        "controllerUserId": active_controller,
                                        "actionId": action_id,
                                        "by": "controller-locked",
                                    }
                                )
                            )
                            continue

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
                            (room_id, video_url, current_time, 1 if is_playing else 0, user.id, user.id, dt_to_str(now_utc())),
                        )
                        up_conn.commit()
                        should_broadcast_state = True
                    except sqlite3.IntegrityError:
                        # 房间或用户关系已失效，避免打爆连接并提示客户端退出当前房间。
                        await websocket.send_text(json.dumps({"type": "error", "message": "Room no longer exists"}))
                    except sqlite3.OperationalError:
                        await websocket.send_text(json.dumps({"type": "error", "message": "Server busy, please retry"}))
                    finally:
                        up_conn.close()

                    if should_broadcast_state:
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
                elif msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "ts": dt_to_str(now_utc())}))
                elif msg_type == "chat":
                    msg = str(payload.get("message", "")).strip()[:400]
                    if not msg:
                        continue
                    await hub.broadcast(room_id, {"type": "chat", "message": msg, "by": user.username, "sentAt": dt_to_str(now_utc())})
        except WebSocketDisconnect:
            pass
        finally:
            await hub.remove(room_id, websocket)
            # 控制者掉线后，快速释放控制权，避免房间状态长期冻结。
            cleanup_conn = db_conn()
            try:
                row = cleanup_conn.execute(
                    "SELECT video_url, current_time AS current_time_sec, is_playing, controller_user_id FROM room_state WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
                if row and row["controller_user_id"] == user.id and not await hub.has_user(room_id, user.id):
                    cleanup_conn.execute(
                        "UPDATE room_state SET controller_user_id = NULL, updated_at = ? WHERE room_id = ?",
                        (dt_to_str(now_utc()), room_id),
                    )
                    cleanup_conn.commit()
                    normalized_url = normalize_media_url(row["video_url"] or "")
                    safe_url = normalized_url if is_valid_media_url(normalized_url) else ""
                    await hub.broadcast(
                        room_id,
                        {
                            "type": "state",
                            "videoUrl": safe_url,
                            "playMode": "audio" if is_audio_media_url(safe_url) else "video",
                            "currentTime": safe_float(row["current_time_sec"], 0.0),
                            "isPlaying": bool(row["is_playing"]),
                            "updatedAt": dt_to_str(now_utc()),
                            "controllerUserId": None,
                            "actionId": 0,
                            "by": "system-controller-release",
                        },
                    )
            finally:
                cleanup_conn.close()

            await broadcast_room_members(room_id)

    return app
