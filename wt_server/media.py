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
    MEDIA_WORK_DIR,
)
from .storage import db_conn
from .utils import dt_to_str, now_utc


MEDIA_WORK_SUBDIR = MEDIA_WORK_DIR / "work"


def sanitize_filename_stem(name: str) -> str:
    stem = (name or "").strip()
    stem = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", stem)
    stem = stem.strip(" .")
    stem = re.sub(r"\s+", " ", stem)
    if not stem:
        stem = "media"
    return stem[:64]


def media_url_from_work(work_key: str, filename: str) -> str:
    return f"/media/work/{work_key}/{filename}"


def _split_media_url(url: str) -> tuple[str, str, str] | None:
    if not url or not url.startswith("/media/"):
        return None
    rel = url.removeprefix("/media/")
    parts = rel.split("/")
    if len(parts) == 4 and parts[0] == "work":
        _, work_key, filename = parts[0], parts[1], parts[3]
        if parts[2] != "":
            # here rel is work/<key>/<filename>, split gave 3 parts; defensive fallback
            pass
    if len(parts) == 3 and parts[0] == "work":
        work_key, filename = parts[1], parts[2]
        return ("work", work_key, filename)
    if len(parts) == 2 and parts[0] in {"video", "audio"}:
        return (parts[0], "", parts[1])
    if len(parts) == 1:
        return ("legacy", "", parts[0])
    return None


def is_valid_media_url(url: str) -> bool:
    if not url:
        return True
    split = _split_media_url(url)
    if not split:
        return False
    kind, work_key, filename = split
    if kind == "work":
        if not work_key or not filename or "/" in filename or "\\" in filename:
            return False
        suffix = Path(filename).suffix.lower()
        return suffix in ALLOWED_VIDEO_EXTENSIONS or suffix in ALLOWED_AUDIO_EXTENSIONS
    if kind == "video":
        return Path(filename).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS
    if kind == "audio":
        return Path(filename).suffix.lower() in ALLOWED_AUDIO_EXTENSIONS
    return Path(filename).suffix.lower() in (ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS)


def is_audio_media_url(url: str) -> bool:
    split = _split_media_url(url)
    if not split:
        return False
    kind, _work_key, filename = split
    if kind == "audio":
        return True
    return Path(filename).suffix.lower() in ALLOWED_AUDIO_EXTENSIONS and Path(filename).suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS


def _find_first_media_file(work_dir: Path, ext_set: set[str], name_prefix: str | None = None) -> Path | None:
    if not work_dir.exists() or not work_dir.is_dir():
        return None
    files = [p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in ext_set]
    if not files:
        return None
    if name_prefix:
        preferred = [p for p in files if p.stem.lower().startswith(name_prefix)]
        if preferred:
            return sorted(preferred, key=lambda p: p.name.lower())[0]
    return sorted(files, key=lambda p: p.name.lower())[0]


def _resolve_work_media(work_key: str, prefer: str = "video") -> str:
    work_dir = MEDIA_WORK_SUBDIR / work_key
    if not work_dir.exists():
        return ""
    if prefer == "audio":
        audio = _find_first_media_file(work_dir, ALLOWED_AUDIO_EXTENSIONS, "audio")
        if audio:
            return media_url_from_work(work_key, audio.name)
        return ""
    video = _find_first_media_file(work_dir, ALLOWED_VIDEO_EXTENSIONS, "video")
    if video:
        return media_url_from_work(work_key, video.name)
    return ""


def normalize_media_url(url: str) -> str:
    """兼容旧格式媒体路径并统一到 /media/work/<work>/<file>。"""
    if not url:
        return ""
    split = _split_media_url(url)
    if not split:
        return url
    kind, work_key, filename = split
    if kind == "work":
        full = MEDIA_WORK_SUBDIR / work_key / filename
        return url if full.exists() else ""

    # Legacy to work layout
    stem = Path(filename).stem
    if kind == "audio":
        return _resolve_work_media(stem, "audio") or url
    return _resolve_work_media(stem, "video") or url


def collect_media_files() -> dict[str, dict[str, Any]]:
    MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    for work_dir in MEDIA_WORK_SUBDIR.iterdir():
        if not work_dir.is_dir():
            continue
        video_file = _find_first_media_file(work_dir, ALLOWED_VIDEO_EXTENSIONS, "video")
        audio_file = _find_first_media_file(work_dir, ALLOWED_AUDIO_EXTENSIONS, "audio")
        if not video_file and not audio_file:
            continue
        stat_target = video_file or audio_file
        if not stat_target:
            continue
        stat = stat_target.stat()
        files[work_dir.name] = {
            "videoUrl": media_url_from_work(work_dir.name, video_file.name) if video_file else "",
            "audioUrl": media_url_from_work(work_dir.name, audio_file.name) if audio_file else "",
            "size": video_file.stat().st_size if video_file else audio_file.stat().st_size,
            "updatedAt": dt_to_str(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
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
        work_key = stem_from_media_url(row["video_url"] or row["audio_url"] or "")
        if not work_key:
            continue
        file_meta = file_index.get(work_key)
        if not file_meta:
            continue
        seen_stems.add(work_key)
        items.append(
            {
                "id": row["id"],
                "name": row["title"] or work_key,
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
    split = _split_media_url(url)
    if not split:
        return ""
    kind, work_key, filename = split
    if kind == "work":
        return work_key
    return Path(filename).stem


def remove_media_by_stem(conn: sqlite3.Connection, stem: str) -> dict[str, Any]:
    if not re.fullmatch(r"[\w\-\u4e00-\u9fff\s\.]+", stem):
        raise HTTPException(status_code=400, detail="Invalid media key")

    deleted_files: list[str] = []
    work_dir = MEDIA_WORK_SUBDIR / stem
    if work_dir.exists() and work_dir.is_dir():
        for path in work_dir.iterdir():
            if path.is_file():
                deleted_files.append(str(path.relative_to(MEDIA_WORK_DIR)))
        shutil.rmtree(work_dir, ignore_errors=True)

    # Legacy cleanup fallback.
    for directory, ext_set in ((MEDIA_VIDEO_DIR, ALLOWED_VIDEO_EXTENSIONS), (MEDIA_AUDIO_DIR, ALLOWED_AUDIO_EXTENSIONS)):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in ext_set:
                continue
            if path.stem == stem:
                path.unlink(missing_ok=True)
                deleted_files.append(path.name)

    rows = conn.execute("SELECT id, video_url, audio_url FROM media_assets").fetchall()
    delete_ids = [row["id"] for row in rows if stem_from_media_url(row["video_url"] or row["audio_url"] or "") == stem]
    if delete_ids:
        conn.executemany("DELETE FROM media_assets WHERE id = ?", [(mid,) for mid in delete_ids])
    conn.commit()
    return {"deletedFiles": deleted_files, "deletedAssetRows": len(delete_ids)}


def rename_media_work(conn: sqlite3.Connection, old_key: str, new_name: str) -> dict[str, Any]:
    if not old_key:
        raise HTTPException(status_code=400, detail="Invalid media key")
    target_key = sanitize_filename_stem(new_name)
    if not target_key:
        raise HTTPException(status_code=400, detail="Invalid target work name")

    src_dir = MEDIA_WORK_SUBDIR / old_key
    if not src_dir.exists() or not src_dir.is_dir():
        raise HTTPException(status_code=404, detail="Work folder not found")

    dst_dir = MEDIA_WORK_SUBDIR / target_key
    if src_dir.resolve() != dst_dir.resolve() and dst_dir.exists():
        raise HTTPException(status_code=409, detail="Target work name already exists")

    if src_dir.resolve() != dst_dir.resolve():
        src_dir.rename(dst_dir)

    video_file = _find_first_media_file(dst_dir, ALLOWED_VIDEO_EXTENSIONS, "video")
    audio_file = _find_first_media_file(dst_dir, ALLOWED_AUDIO_EXTENSIONS, "audio")
    if not video_file:
        raise HTTPException(status_code=400, detail="Work folder missing video file")

    rows = conn.execute("SELECT id, video_url, audio_url FROM media_assets").fetchall()
    update_ids = [row["id"] for row in rows if stem_from_media_url(row["video_url"] or row["audio_url"] or "") == old_key]
    for row_id in update_ids:
        conn.execute(
            "UPDATE media_assets SET title = ?, video_url = ?, audio_url = ?, updated_at = ? WHERE id = ?",
            (
                target_key,
                media_url_from_work(target_key, video_file.name),
                media_url_from_work(target_key, audio_file.name) if audio_file else "",
                dt_to_str(now_utc()),
                row_id,
            ),
        )
    conn.commit()
    return {"ok": True, "work": target_key, "updatedRows": len(update_ids)}


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
    result = subprocess.run(cmd, capture_output=True, text=False)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="ignore")[-300:]
        raise HTTPException(status_code=400, detail=f"Cannot parse uploaded media: {detail or 'ffprobe failed'}")
    try:
        stdout_text = (result.stdout or b"").decode("utf-8", errors="ignore")
        return json.loads(stdout_text)
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


def allocate_media_stem(original_stem: str) -> str:
    MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename_stem(original_stem)
    existing = {p.name for p in MEDIA_WORK_SUBDIR.iterdir() if p.is_dir()}
    if base not in existing:
        return base
    index = 2
    while True:
        candidate = f"{base}_{index}"
        if candidate not in existing:
            return candidate
        index += 1


async def import_media_file(file: UploadFile) -> dict[str, Any]:
    """导入本地视频/音频，输出 work/<title>/video|audio 资源并入库。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Only supported video/audio formats are allowed")

    ensure_ffmpeg_tools()
    MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)

    source_stem = Path(file.filename or "media").stem
    media_stem = allocate_media_stem(source_stem)
    work_dir = MEDIA_WORK_SUBDIR / media_stem
    work_dir.mkdir(parents=True, exist_ok=True)

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
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    video_name = "video.mp4"
    video_path = work_dir / video_name
    audio_name = "audio.m4a"
    audio_path = work_dir / audio_name

    video_url = ""
    audio_url = ""
    transcoded = False

    if has_video:
        if is_browser_friendly_mp4(profile):
            cmd = [
                "ffmpeg", "-y", "-i", str(raw_path),
                "-c:v", "copy", "-c:a", "copy" if has_audio else "aac",
                "-movflags", "+faststart", str(video_path),
            ]
        else:
            transcoded = True
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
        if process.returncode != 0:
            raw_path.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)
            detail = stderr.decode("utf-8", errors="ignore")[-400:]
            raise HTTPException(status_code=500, detail=f"Transcode failed: {detail or 'unknown error'}")

        video_url = media_url_from_work(media_stem, video_name)
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
            await audio_proc.communicate()
            if audio_proc.returncode == 0:
                audio_url = media_url_from_work(media_stem, audio_name)
            else:
                audio_path.unlink(missing_ok=True)
    else:
        if not has_audio:
            raw_path.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="Uploaded file has no audio or video stream")
        transcoded = suffix not in {".m4a", ".aac"}
        audio_cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-vn", "-c:a", "aac", "-b:a", "160k", str(audio_path),
        ]
        audio_proc = await asyncio.create_subprocess_exec(
            *audio_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, audio_err = await audio_proc.communicate()
        if audio_proc.returncode != 0:
            raw_path.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)
            detail = audio_err.decode("utf-8", errors="ignore")[-400:]
            raise HTTPException(status_code=500, detail=f"Audio import failed: {detail or 'unknown error'}")
        audio_url = media_url_from_work(media_stem, audio_name)

    raw_path.unlink(missing_ok=True)

    title = media_stem
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
                video_url,
                audio_url,
                duration,
                (video_path.stat().st_size if video_path.exists() else audio_path.stat().st_size),
                now_str,
                now_str,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "videoUrl": video_url,
        "audioUrl": audio_url,
        "profile": profile,
        "transcoded": transcoded,
        "imported": True,
    }


def migrate_media_layout(conn: sqlite3.Connection) -> dict[str, int]:
    """把旧的 video/audio 平铺结构迁移到 work/<name>/ 结构。"""
    MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)
    moved_dirs = 0
    moved_files = 0

    video_by_stem: dict[str, Path] = {}
    audio_by_stem: dict[str, Path] = {}

    if MEDIA_VIDEO_DIR.exists():
        for p in MEDIA_VIDEO_DIR.iterdir():
            if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS:
                video_by_stem[p.stem] = p
    if MEDIA_AUDIO_DIR.exists():
        for p in MEDIA_AUDIO_DIR.iterdir():
            if p.is_file() and p.suffix.lower() in ALLOWED_AUDIO_EXTENSIONS:
                audio_by_stem[p.stem] = p

    all_stems = sorted(set(video_by_stem.keys()) | set(audio_by_stem.keys()))
    for stem in all_stems:
        work_key = sanitize_filename_stem(stem)
        if not work_key:
            continue
        target = MEDIA_WORK_SUBDIR / work_key
        target.mkdir(parents=True, exist_ok=True)
        created = False

        video = video_by_stem.get(stem)
        if video and video.exists():
            dst = target / f"video{video.suffix.lower()}"
            if not dst.exists():
                shutil.move(str(video), str(dst))
                moved_files += 1
                created = True

        audio = audio_by_stem.get(stem)
        if audio and audio.exists():
            dst = target / f"audio{audio.suffix.lower()}"
            if not dst.exists():
                shutil.move(str(audio), str(dst))
                moved_files += 1
                created = True

        if created:
            moved_dirs += 1

    rows = conn.execute("SELECT id, video_url, audio_url FROM media_assets").fetchall()
    for row in rows:
        stem = stem_from_media_url(row["video_url"] or row["audio_url"] or "")
        if not stem:
            continue
        work_key = sanitize_filename_stem(stem)
        work_dir = MEDIA_WORK_SUBDIR / work_key
        if not work_dir.exists():
            continue
        video_file = _find_first_media_file(work_dir, ALLOWED_VIDEO_EXTENSIONS, "video")
        audio_file = _find_first_media_file(work_dir, ALLOWED_AUDIO_EXTENSIONS, "audio")
        if not video_file:
            continue
        conn.execute(
            "UPDATE media_assets SET video_url = ?, audio_url = ?, updated_at = ? WHERE id = ?",
            (
                media_url_from_work(work_key, video_file.name),
                media_url_from_work(work_key, audio_file.name) if audio_file else "",
                dt_to_str(now_utc()),
                row["id"],
            ),
        )
    conn.commit()

    return {"movedDirs": moved_dirs, "movedFiles": moved_files}
