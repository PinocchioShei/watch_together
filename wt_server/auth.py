"""用户与管理员认证相关逻辑。"""

import hashlib
import secrets
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import HTTPException, Request

from .config import ADMIN_SESSION_TTL
from .schemas import User
from .state import ADMIN_TOKENS
from .storage import db_conn
from .utils import dt_to_str, now_utc, str_to_dt


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


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO sessions(token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, user_id, dt_to_str(now_utc() + timedelta(days=14)), dt_to_str(now_utc())),
    )
    conn.commit()
    return token


def get_user_by_token(conn: sqlite3.Connection, token: Optional[str]) -> User:
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


def extract_bearer_token(request: Request) -> Optional[str]:
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
