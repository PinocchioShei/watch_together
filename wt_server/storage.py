"""数据库初始化与连接。"""

import sqlite3

from .config import (
    ALLOWED_VIDEO_EXTENSIONS,
    DB_PATH,
    MEDIA_AUDIO_DIR,
    MEDIA_DIR,
    MEDIA_TMP_DIR,
    MEDIA_VIDEO_DIR,
)


def init_db() -> None:
    """初始化数据库与媒体目录结构。"""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_TMP_DIR.mkdir(parents=True, exist_ok=True)

    # 兼容旧版本：如果历史文件直接放在 media 根目录，迁移到 media/video。
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
            password_salt TEXT,
            password_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS room_members (
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            room_password_cache TEXT,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (room_id, user_id),
            FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS room_access_cache (
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            room_password_cache TEXT NOT NULL,
            updated_at TEXT NOT NULL,
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

        CREATE TABLE IF NOT EXISTS admin_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            username TEXT NOT NULL UNIQUE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    columns = {row[1] for row in conn.execute("PRAGMA table_info(room_state)").fetchall()}
    if "controller_user_id" not in columns:
        conn.execute("ALTER TABLE room_state ADD COLUMN controller_user_id INTEGER")

    room_columns = {row[1] for row in conn.execute("PRAGMA table_info(rooms)").fetchall()}
    if "password_salt" not in room_columns:
        conn.execute("ALTER TABLE rooms ADD COLUMN password_salt TEXT")
    if "password_hash" not in room_columns:
        conn.execute("ALTER TABLE rooms ADD COLUMN password_hash TEXT")

    member_columns = {row[1] for row in conn.execute("PRAGMA table_info(room_members)").fetchall()}
    if "room_password_cache" not in member_columns:
        conn.execute("ALTER TABLE room_members ADD COLUMN room_password_cache TEXT")

    conn.commit()
    conn.close()


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 20000;")
    return conn
