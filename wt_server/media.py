"""媒体扫描、索引与导入转码逻辑。"""

import asyncio
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

from .config import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_VIDEO_EXTENSIONS,
    MEDIA_AUDIO_DIR,
    MEDIA_TMP_DIR,
    MEDIA_VIDEO_DIR,
)
from .storage import db_conn
from .utils import dt_to_str, now_utc


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
    return f"{base}_{max_no + 1:03d}"


def is_valid_media_url(url: str) -> bool:
    if not url:
        return True
    if not url.startswith("/media/"):
        return False
    rel = url.removeprefix("/media/")
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


def normalize_media_url(url: str) -> str:
    """兼容旧格式媒体路径，并尽量归一化为新格式。

    - 旧格式: /media/<filename>   -> /media/video/<filename>
    - 新格式: /media/video/<filename> 或 /media/audio/<filename>
    - 非法或不存在文件: 返回原值（由上层继续校验）
    """
    if not url:
        return ""
    if url.startswith("/media/video/") or url.startswith("/media/audio/"):
        return url
    if not url.startswith("/media/"):
        return url

    rel = url.removeprefix("/media/")
    if "/" in rel or "\\" in rel or not rel:
        return url

    suffix = Path(rel).suffix.lower()
    if suffix in ALLOWED_VIDEO_EXTENSIONS:
        video_candidate = MEDIA_VIDEO_DIR / rel
        if video_candidate.exists():
            return media_url_from_name("video", rel)
    if suffix in ALLOWED_AUDIO_EXTENSIONS:
        audio_candidate = MEDIA_AUDIO_DIR / rel
        if audio_candidate.exists():
            return media_url_from_name("audio", rel)
    return url


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
        stem = Path(Path(row["video_url"] or "").name).stem
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
                deleted_files.append(path.name)

    rows = conn.execute("SELECT id, video_url FROM media_assets").fetchall()
    delete_ids = [row["id"] for row in rows if stem_from_media_url(row["video_url"] or "") == stem]
    if delete_ids:
        conn.executemany("DELETE FROM media_assets WHERE id = ?", [(mid,) for mid in delete_ids])
    conn.commit()
    return {"deletedFiles": deleted_files, "deletedAssetRows": len(delete_ids)}


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


async def import_media_file(file: UploadFile) -> dict[str, Any]:
    """导入本地视频，输出标准化 video/audio 资源并入库。"""
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
            "ffmpeg", "-y", "-i", str(raw_path),
            "-c:v", "copy", "-c:a", "copy" if has_audio else "aac",
            "-movflags", "+faststart", str(video_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        ]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(video_path)]

    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    raw_path.unlink(missing_ok=True)
    if process.returncode != 0:
        video_path.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", errors="ignore")[-400:]
        raise HTTPException(status_code=500, detail=f"Transcode failed: {detail or 'unknown error'}")

    audio_url = ""
    if has_audio:
        audio_cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-c:a", "aac", "-b:a", "160k", str(audio_path),
        ]
        audio_proc = await asyncio.create_subprocess_exec(
            *audio_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _audio_stderr = await audio_proc.communicate()
        if audio_proc.returncode == 0:
            audio_url = media_url_from_name("audio", audio_name)
        else:
            audio_path.unlink(missing_ok=True)

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
