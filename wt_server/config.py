"""项目配置与路径常量。"""

import os
from datetime import timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "watch_together.db"
STATIC_DIR = BASE_DIR / "static"

MEDIA_DIR = BASE_DIR / "media"
MEDIA_VIDEO_DIR = MEDIA_DIR / "video"
MEDIA_AUDIO_DIR = MEDIA_DIR / "audio"
MEDIA_TMP_DIR = MEDIA_DIR / "tmp"

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov"}
ALLOWED_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".ogg", ".wav", ".aac"}

# 管理员账号支持环境变量覆盖，方便部署时自定义。
ADMIN_USERNAME = os.getenv("WATCH_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("WATCH_ADMIN_PASSWORD", "admin123")
ADMIN_SESSION_TTL = timedelta(hours=12)
