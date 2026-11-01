"""媒体扫描、索引与导入转码逻辑。"""

import asyncio
from contextlib import asynccontextmanager
import errno
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import HTTPException, UploadFile

from .config import (
    BASE_DIR,
    IMPORT_MAX_FILES,
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
MEDIA_TYPE_OPTIONS = {"movie", "RJ", "ASMR", "music", "shot"}
_IMPORT_LOCK = asyncio.Lock()


def validate_work_key(name: str) -> str:
    """Keep user names verbatim; reject unsafe names instead of silently renaming."""
    if (not name or name in {".", ".."} or name != name.strip(" .")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name)):
        raise HTTPException(status_code=422, detail="Invalid work or media filename")
    return name


def normalize_work_name(name: str) -> str:
    """A work is a title; media filenames keep their own format extensions."""
    name = validate_work_key(name)
    while Path(name).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS:
        name = Path(name).stem
    return validate_work_key(name)


@asynccontextmanager
async def _import_lock():
    # Both legacy server ports write the same media tree. Lock publication across processes too.
    async with _IMPORT_LOCK:
        with (MEDIA_TMP_DIR / ".import.lock").open("a+b") as lock:
            if lock.seek(0, 2) == 0:
                lock.write(b"0")
                lock.flush()
            acquired = False
            try:
                while not acquired:
                    lock.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except OSError as error:
                        if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        await asyncio.sleep(0.1)
                yield
            finally:
                if acquired:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sanitize_filename_stem(name: str) -> str:
    stem = (name or "").strip()
    stem = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", stem)
    stem = stem.strip(" .")
    stem = re.sub(r"\s+", " ", stem)
    if not stem:
        stem = "media"
    return stem[:64]


def iter_work_dirs() -> list[Path]:
    """Read categorized storage and the previous flat work layout during upgrades."""
    if not MEDIA_WORK_SUBDIR.exists():
        return []
    result = []
    for directory in MEDIA_WORK_SUBDIR.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        if (directory.name in MEDIA_TYPE_OPTIONS and not (directory / ".meta.json").exists()
                and not any(p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS
                            for p in directory.iterdir())):
            result.extend(p for p in directory.iterdir() if p.is_dir() and not p.is_symlink())
        else:
            result.append(directory)
    names = [p.name.casefold() for p in result]
    if len(names) != len(set(names)):
        raise HTTPException(status_code=409, detail="Duplicate work names across category directories")
    return result


def resolve_work_dir(work_key: str) -> Path:
    validate_work_key(work_key)
    for directory in iter_work_dirs():
        if directory.name.casefold() == work_key.casefold():
            return directory
    fallback = MEDIA_WORK_SUBDIR / work_key
    fallback = fallback / work_key if work_key in MEDIA_TYPE_OPTIONS and fallback.is_dir() else fallback
    if (fallback.is_symlink() or fallback.parent.is_symlink()
            or not fallback.resolve().is_relative_to(MEDIA_WORK_SUBDIR.resolve())):
        raise HTTPException(status_code=409, detail="Work path escapes media storage")
    return fallback


def work_category(work_dir: Path) -> str:
    return work_dir.parent.name if work_dir.parent.parent == MEDIA_WORK_SUBDIR and work_dir.parent.name in MEDIA_TYPE_OPTIONS else ""


def media_url_from_work(work_key: str, filename: str, media_type: str | None = None) -> str:
    category = work_category(resolve_work_dir(work_key)) if media_type is None else media_type
    prefix = f"{category}/" if category else ""
    return f"/media/work/{prefix}{quote(work_key, safe='')}/{quote(filename, safe='')}"


def _natural_name_key(path: Path) -> list[Any]:
    parts = re.split(r"(\d+)", path.name.casefold())
    return [int(part) if part.isdigit() else part for part in parts]


def _split_media_url(url: str) -> tuple[str, str, str] | None:
    if not url or not url.startswith("/media/"):
        return None
    rel = url.removeprefix("/media/")
    parts = rel.split("/")
    if parts[0] == "work" and len(parts) in {3, 4}:
        if len(parts) == 4 and parts[1] not in MEDIA_TYPE_OPTIONS:
            return None
        work_key, filename = unquote(parts[-2]), unquote(parts[-1])
        try:
            validate_work_key(work_key)
            validate_work_key(filename)
        except HTTPException:
            return None
        return ("work", work_key, filename)
    if len(parts) == 2 and parts[0] in {"video", "audio"}:
        return (parts[0], "", unquote(parts[1]))
    if len(parts) == 1:
        return ("legacy", "", unquote(parts[0]))
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
    return Path(filename).suffix.lower() in (
        ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS
    )


def is_audio_media_url(url: str) -> bool:
    split = _split_media_url(url)
    if not split:
        return False
    kind, _work_key, filename = split
    if kind == "audio":
        return True
    if Path(filename).suffix.lower() == ".ogg" and kind == "work":
        path = resolve_work_dir(_work_key) / filename
        if path.is_file():
            stat = path.stat()
            return _ambiguous_media_kind(str(path), stat.st_mtime_ns, stat.st_size) == "audio"
    return (
        Path(filename).suffix.lower() in ALLOWED_AUDIO_EXTENSIONS
        and Path(filename).suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS
    )


def _find_first_media_file(
    work_dir: Path, ext_set: set[str], name_prefix: str | None = None
) -> Path | None:
    if not work_dir.exists() or not work_dir.is_dir():
        return None
    files = [
        p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in ext_set
    ]
    if not files:
        return None
    if name_prefix:
        preferred = [p for p in files if p.stem.lower().startswith(name_prefix)]
        if preferred:
            return sorted(preferred, key=lambda p: p.name.lower())[0]
    return sorted(files, key=lambda p: p.name.lower())[0]


def _list_media_files(work_dir: Path, ext_set: set[str]) -> list[Path]:
    if not work_dir.exists() or not work_dir.is_dir():
        return []
    files = [
        p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in ext_set
    ]
    return sorted(files, key=_natural_name_key)


def _guess_media_type(title: str) -> str:
    text = (title or "").lower()
    if any(k in text for k in ["asmr", "睡", "耳", "舔", "voice"]):
        return "ASMR"
    if any(k in text for k in ["rj", "dlsite"]):
        return "RJ"
    if any(k in text for k in ["music", "song", "ost", "曲", "歌"]):
        return "music"
    if any(k in text for k in ["shot", "clip", "cut", "短片", "片段"]):
        return "shot"
    return "movie"


def _default_cover_path() -> Path:
    return MEDIA_WORK_DIR / "default_cover.jpg"


def _work_meta_path(work_dir: Path) -> Path:
    return work_dir / ".meta.json"


def _read_work_meta(work_dir: Path) -> dict[str, Any]:
    path = _work_meta_path(work_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_work_meta(
    work_dir: Path,
    media_type: str,
    title: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    path = _work_meta_path(work_dir)
    old_meta = _read_work_meta(work_dir)
    payload = {
        **old_meta,
        "mediaType": media_type,
        "title": title or work_dir.name,
        "updatedAt": dt_to_str(now_utc()),
    }
    if extra:
        payload.update(extra)
    if "audioTrackOrder" in old_meta and "audioTrackOrder" not in payload:
        payload["audioTrackOrder"] = old_meta["audioTrackOrder"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _list_ordered_audio_files(work_dir: Path) -> list[Path]:
    audio_files = _list_media_files(work_dir, ALLOWED_AUDIO_EXTENSIONS)
    if not audio_files:
        return []
    return audio_files


def backfill_work_meta(conn: sqlite3.Connection) -> dict[str, int]:
    MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        """
        SELECT title, video_url, audio_url, media_type, updated_at
        FROM media_assets
        ORDER BY updated_at DESC
        """
    ).fetchall()

    by_work: dict[str, dict[str, Any]] = {}
    for row in rows:
        work_key = stem_from_media_url(row["video_url"] or row["audio_url"] or "")
        if not work_key:
            continue
        if work_key not in by_work:
            by_work[work_key] = {
                "title": row["title"] or work_key,
                "mediaType": row["media_type"]
                or _guess_media_type(row["title"] or work_key),
                "updatedAt": row["updated_at"] or dt_to_str(now_utc()),
            }

    created = 0
    updated = 0
    for work_dir in iter_work_dirs():
        work_key = work_dir.name
        meta_path = _work_meta_path(work_dir)
        old_meta = _read_work_meta(work_dir)
        src = by_work.get(work_key, {})
        payload = {
            **old_meta,
            "title": src.get("title") or old_meta.get("title") or work_key,
            "mediaType": work_category(work_dir) or src.get("mediaType")
            or old_meta.get("mediaType")
            or _guess_media_type(work_key),
            "updatedAt": src.get("updatedAt")
            or old_meta.get("updatedAt")
            or dt_to_str(now_utc()),
        }
        if "audioTrackOrder" in old_meta:
            payload["audioTrackOrder"] = old_meta["audioTrackOrder"]
        if meta_path.exists():
            if old_meta != payload:
                meta_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                updated += 1
        else:
            meta_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            created += 1

    return {"created": created, "updated": updated}


def _resolve_cover_url(work_key: str, row_cover_url: str = "") -> str:
    work_dir = resolve_work_dir(work_key)
    if row_cover_url:
        row_path = BASE_DIR / unquote(row_cover_url).lstrip("/")
        if row_path.exists():
            return row_cover_url
    cover_file = work_dir / "cover.jpg"
    if cover_file.exists():
        return media_url_from_work(work_key, "cover.jpg")
    default_cover = _default_cover_path()
    if default_cover.exists():
        return "/media/default_cover.jpg"
    return ""


def _ensure_default_cover() -> Path:
    default_cover = _default_cover_path()
    if default_cover.exists():
        return default_cover

    default_cover.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x1f2937:s=960x540",
        "-frames:v",
        "1",
        str(default_cover),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not default_cover.exists():
        detail = (proc.stderr or b"").decode("utf-8", errors="ignore")[-240:]
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create default cover: {detail or 'ffmpeg failed'}",
        )
    return default_cover


def _build_cover_for_work(work_dir: Path, video_file: Path | None) -> str:
    cover_path = work_dir / "cover.jpg"
    if cover_path.exists():
        return media_url_from_work(work_dir.name, cover_path.name)

    if video_file and video_file.exists():
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_file),
            "-vf",
            "select=eq(pict_type\\,I)",
            "-frames:v",
            "1",
            str(cover_path),
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not cover_path.exists():
            cmd2 = [
                "ffmpeg",
                "-y",
                "-i",
                str(video_file),
                "-frames:v",
                "1",
                str(cover_path),
            ]
            subprocess.run(cmd2, capture_output=True)
        if cover_path.exists():
            return media_url_from_work(work_dir.name, cover_path.name)

    default_cover = _ensure_default_cover()
    shutil.copyfile(default_cover, cover_path)
    return media_url_from_work(work_dir.name, cover_path.name)


def _save_uploaded_cover(work_dir: Path, cover: UploadFile) -> str:
    suffix = Path(cover.filename or "").suffix.lower()
    raw_path = (
        MEDIA_TMP_DIR
        / f"raw_cover_{work_dir.name}_{secrets.token_hex(4)}{suffix or '.img'}"
    )
    with raw_path.open("wb") as out:
        while True:
            chunk = cover.file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    cover.file.close()

    final_cover = work_dir / "cover.jpg"
    if suffix in {".jpg", ".jpeg"}:
        shutil.move(str(raw_path), str(final_cover))
    else:
        cmd = ["ffmpeg", "-y", "-i", str(raw_path), "-frames:v", "1", str(final_cover)]
        proc = subprocess.run(cmd, capture_output=True)
        raw_path.unlink(missing_ok=True)
        if proc.returncode != 0 or not final_cover.exists():
            raise HTTPException(status_code=400, detail="Invalid cover image")

    return media_url_from_work(work_dir.name, final_cover.name)


def _resolve_work_media(work_key: str, prefer: str = "video") -> str:
    work_dir = resolve_work_dir(work_key)
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
    """Resolve old flat/category URLs to the current physical category directory."""
    if not url:
        return ""
    split = _split_media_url(url)
    if not split:
        return url
    kind, work_key, filename = split
    if kind == "work":
        full = resolve_work_dir(work_key) / filename
        return media_url_from_work(full.parent.name, filename) if full.is_file() else ""

    # Legacy to work layout
    stem = Path(filename).stem
    if kind == "audio":
        return _resolve_work_media(stem, "audio") or url
    return _resolve_work_media(stem, "video") or url


@lru_cache(maxsize=512)
def _ambiguous_media_kind(filename: str, mtime_ns: int, size: int) -> str:
    # OGG can contain video or audio; suffix alone cannot enforce work homogeneity.
    profile = extract_media_profile(probe_media(Path(filename)))
    return "video" if profile["videoCodec"] != "unknown" else "audio"


def work_media_files(work_dir: Path) -> tuple[list[Path], list[Path]]:
    videos, audios = [], []
    if not work_dir.is_dir():
        return videos, audios
    for path in work_dir.iterdir():
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS:
            continue
        if suffix in ALLOWED_VIDEO_EXTENSIONS & ALLOWED_AUDIO_EXTENSIONS:
            stat = path.stat()
            kind = _ambiguous_media_kind(str(path), stat.st_mtime_ns, stat.st_size)
        else:
            kind = "video" if suffix in ALLOWED_VIDEO_EXTENSIONS else "audio"
        (videos if kind == "video" else audios).append(path)
    return sorted(videos, key=_natural_name_key), sorted(audios, key=_natural_name_key)


def work_media_kind(work_dir: Path) -> str:
    videos, audios = work_media_files(work_dir)
    if videos and audios:
        return "mixed"
    if videos:
        return "video"
    if audios:
        return "audio"
    # Historical empty audio albums didn't record their kind.
    return _read_work_meta(work_dir).get("mediaKind", "audio")


def collect_media_files() -> dict[str, dict[str, Any]]:
    MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)
    files = {}
    for work_dir in iter_work_dirs():
        videos, audios = work_media_files(work_dir)
        if not videos and not audios:
            continue
        category = work_category(work_dir)
        video_tracks = [media_url_from_work(work_dir.name, p.name, category) for p in videos]
        audio_tracks = [media_url_from_work(work_dir.name, p.name, category) for p in audios]
        # Retain playback of legacy extracted audio, but never offer mixed works for append.
        files[work_dir.name] = {
            "workKey": work_dir.name,
            "mediaKind": "mixed" if videos and audios else ("video" if videos else "audio"),
            "videoUrl": video_tracks[0] if video_tracks else "",
            "audioUrl": audio_tracks[0] if audio_tracks else "",
            "videoTracks": video_tracks,
            "audioTracks": audio_tracks if not videos else [],
            "originalNames": {
                media_url_from_work(work_dir.name, filename, category): original
                for filename, original in (_read_work_meta(work_dir).get("originalNames") or {}).items()
            },
            "coverUrl": _resolve_cover_url(work_dir.name),
            "size": sum(p.stat().st_size for p in videos + audios),
            "updatedAt": dt_to_str(datetime.fromtimestamp(
                max(p.stat().st_mtime for p in work_dir.iterdir() if p.is_file()), timezone.utc)),
            "meta": {**_read_work_meta(work_dir), **({"mediaType": category} if category else {})},
        }
    return files


def list_media_library(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    file_index = collect_media_files()
    rows = conn.execute(
        """
        SELECT id, title, video_url, audio_url, cover_url, media_type, duration, size, updated_at
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
        if work_key in seen_stems:
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
                "audioUrl": file_meta["audioUrl"],
                "coverUrl": _resolve_cover_url(work_key, row["cover_url"] or ""),
                "type": (file_meta.get("meta", {}) or {}).get("mediaType")
                or row["media_type"]
                or _guess_media_type(row["title"] or work_key),
                "duration": row["duration"] or 0,
                "size": file_meta["size"],
                "updatedAt": file_meta["updatedAt"],
            }
        )
        items[-1].update({key: file_meta[key] for key in
                          ("workKey", "mediaKind", "videoTracks", "audioTracks", "originalNames")})

    for stem, file_meta in file_index.items():
        if stem in seen_stems:
            continue
        items.append(
            {
                "id": None,
                "name": stem,
                "videoUrl": file_meta["videoUrl"],
                "audioUrl": file_meta["audioUrl"],
                "coverUrl": file_meta.get("coverUrl") or _resolve_cover_url(stem),
                "type": (file_meta.get("meta", {}) or {}).get("mediaType")
                or _guess_media_type(stem),
                "duration": 0,
                "size": file_meta["size"],
                "updatedAt": file_meta["updatedAt"],
            }
        )
        items[-1].update({key: file_meta[key] for key in
                          ("workKey", "mediaKind", "videoTracks", "audioTracks", "originalNames")})

    items.sort(key=lambda item: item["updatedAt"], reverse=True)
    return items


def list_appendable_works() -> list[dict[str, Any]]:
    MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)
    items = []
    for work_dir in iter_work_dirs():
        kind = work_media_kind(work_dir)
        if kind not in {"audio", "video"}:
            continue
        meta = _read_work_meta(work_dir)
        videos, audios = work_media_files(work_dir)
        if not meta and not videos and not audios:
            continue
        items.append({
            "workKey": work_dir.name,
            "name": meta.get("title") or work_dir.name,
            "type": work_category(work_dir) or meta.get("mediaType") or _guess_media_type(work_dir.name),
            "mediaKind": kind,
            "trackCount": len(videos if kind == "video" else audios),
            "coverUrl": _resolve_cover_url(work_dir.name),
            "updatedAt": meta.get("updatedAt") or dt_to_str(now_utc()),
        })
    return sorted(items, key=lambda item: item["updatedAt"], reverse=True)


def list_appendable_audio_works() -> list[dict[str, Any]]:
    return [item for item in list_appendable_works() if item["mediaKind"] == "audio"]


def stem_from_media_url(url: str) -> str:
    split = _split_media_url(url)
    if not split:
        return ""
    kind, work_key, filename = split
    if kind == "work":
        return work_key
    return Path(filename).stem


def derive_media_track_index(url: str) -> int | None:
    split = _split_media_url(url)
    if not split or split[0] != "work":
        return None
    _, work_key, filename = split
    videos, audios = work_media_files(resolve_work_dir(work_key))
    tracks = audios if is_audio_media_url(url) else videos
    return next((index for index, path in enumerate(tracks) if path.name == filename), None)


def derive_audio_track_index(url: str) -> int | None:
    return derive_media_track_index(url)


def remove_media_by_stem(conn: sqlite3.Connection, stem: str) -> dict[str, Any]:
    validate_work_key(stem)

    deleted_files: list[str] = []
    work_dir = resolve_work_dir(stem)
    if work_dir.exists() and work_dir.is_dir():
        for path in work_dir.iterdir():
            if path.is_file():
                deleted_files.append(str(path.relative_to(MEDIA_WORK_DIR)))
        shutil.rmtree(work_dir, ignore_errors=True)

    # Legacy cleanup fallback.
    for directory, ext_set in (
        (MEDIA_VIDEO_DIR, ALLOWED_VIDEO_EXTENSIONS),
        (MEDIA_AUDIO_DIR, ALLOWED_AUDIO_EXTENSIONS),
    ):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in ext_set:
                continue
            if path.stem == stem:
                path.unlink(missing_ok=True)
                deleted_files.append(path.name)

    rows = conn.execute("SELECT id, video_url, audio_url FROM media_assets").fetchall()
    delete_ids = [
        row["id"]
        for row in rows
        if stem_from_media_url(row["video_url"] or row["audio_url"] or "") == stem
    ]
    if delete_ids:
        conn.executemany(
            "DELETE FROM media_assets WHERE id = ?", [(mid,) for mid in delete_ids]
        )
    conn.commit()
    return {"deletedFiles": deleted_files, "deletedAssetRows": len(delete_ids)}


def rewrite_work_urls(conn: sqlite3.Connection, old_key: str, directory: Path) -> int:
    """Update the index and every persisted episode URL inside the caller's transaction."""
    key, category = directory.name, work_category(directory)
    videos, audios = work_media_files(directory)
    if not videos and not audios:
        raise HTTPException(status_code=400, detail="Work folder missing playable media")
    meta = _read_work_meta(directory)
    now = dt_to_str(now_utc())
    video = media_url_from_work(key, videos[0].name, category) if videos else ""
    audio = media_url_from_work(key, audios[0].name, category) if audios else ""
    cover = media_url_from_work(key, "cover.jpg", category) if (directory / "cover.jpg").exists() else "/media/default_cover.jpg"
    rows = [row for row in conn.execute("SELECT id,video_url,audio_url FROM media_assets").fetchall()
            if stem_from_media_url(row["video_url"] or row["audio_url"] or "") == old_key]
    values = (meta.get("title") or key, video, audio, cover, category or meta.get("mediaType") or "movie", now)
    for row in rows:
        conn.execute("UPDATE media_assets SET title=?,video_url=?,audio_url=?,cover_url=?,media_type=?,updated_at=? WHERE id=?",
                     (*values, row["id"]))
    if not rows:
        conn.execute("""INSERT INTO media_assets(title,video_url,audio_url,cover_url,media_type,updated_at,
                     created_at,size,duration) VALUES (?,?,?,?,?,?,?,?,?)""",
                     (*values, now, sum(p.stat().st_size for p in videos + audios),
                      sum(float(value) for value in (meta.get("trackDurations") or {}).values())))
    for row in conn.execute("SELECT room_id,video_url FROM room_state").fetchall():
        split = _split_media_url(row["video_url"] or "")
        if split and split[0] == "work" and split[1] == old_key and (directory / split[2]).is_file():
            conn.execute("UPDATE room_state SET video_url=? WHERE room_id=?",
                         (media_url_from_work(key, split[2], category), row["room_id"]))
    return len(rows)


def relocate_media_work(conn: sqlite3.Connection, key: str, category: str, new_key: str | None = None) -> dict[str, Any]:
    if category not in MEDIA_TYPE_OPTIONS:
        raise HTTPException(status_code=422, detail="Invalid media type")
    source = resolve_work_dir(key)
    if not source.is_dir():
        raise HTTPException(status_code=404, detail="Work folder not found")
    key = source.name
    target_key = normalize_work_name(new_key) if new_key is not None else key
    existing = resolve_work_dir(target_key)
    if existing.is_dir() and existing.resolve() != source.resolve():
        raise HTTPException(status_code=409, detail="Target work name already exists")
    target = MEDIA_WORK_SUBDIR / category / target_key
    if target.parent.is_symlink() or not target.resolve().is_relative_to(MEDIA_WORK_SUBDIR.resolve()):
        raise HTTPException(status_code=409, detail="Work destination escapes media storage")
    if source.resolve() in target.resolve().parents:
        raise HTTPException(status_code=409, detail="Legacy category-name work requires offline migration")
    if target.exists() and target.resolve() != source.resolve():
        raise HTTPException(status_code=409, detail="Target directory already exists")
    old_meta = (source / ".meta.json").read_bytes() if (source / ".meta.json").exists() else None
    moved = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        if source != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            moved = True
        _write_work_meta(target, category, target_key if new_key is not None else _read_work_meta(target).get("title") or key)
        count = rewrite_work_urls(conn, key, target)
        conn.commit()
    except BaseException:
        conn.rollback()
        if moved:
            target.rename(source)
        if old_meta is None:
            (source / ".meta.json").unlink(missing_ok=True)
        else:
            (source / ".meta.json").write_bytes(old_meta)
        raise
    return {"ok": True, "work": target_key, "mediaKey": target_key, "type": category, "updatedRows": count}


def rename_media_work(conn: sqlite3.Connection, old_key: str, new_name: str) -> dict[str, Any]:
    directory = resolve_work_dir(old_key)
    category = work_category(directory) or _read_work_meta(directory).get("mediaType") or _guess_media_type(old_key)
    return relocate_media_work(conn, old_key, category, new_name.strip())


def ensure_ffmpeg_tools() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(
            status_code=500, detail="ffmpeg/ffprobe not found on server"
        )


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
        raise HTTPException(
            status_code=400,
            detail=f"Cannot parse uploaded media: {detail or 'ffprobe failed'}",
        )
    try:
        stdout_text = (result.stdout or b"").decode("utf-8", errors="ignore")
        return json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid media metadata") from exc


def extract_media_profile(probe_result: dict[str, Any]) -> dict[str, str]:
    fmt = (probe_result.get("format") or {}).get("format_name", "")
    streams = probe_result.get("streams") or []
    v_stream = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not bool((s.get("disposition") or {}).get("attached_pic", 0))
        ),
        {},
    )
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "container": (fmt.split(",")[0] if fmt else "unknown"),
        "videoCodec": v_stream.get("codec_name", "unknown"),
        "audioCodec": a_stream.get("codec_name", "none") if a_stream else "none",
    }


def is_browser_friendly_mp4(
    profile: dict[str, str], probe_result: dict[str, Any]
) -> bool:
    if not (
        profile["container"] in {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
        and profile["videoCodec"] == "h264"
        and profile["audioCodec"] in {"aac", "mp3", "none"}
    ):
        return False

    streams = probe_result.get("streams") or []
    v_stream = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not bool((s.get("disposition") or {}).get("attached_pic", 0))
        ),
        {},
    )

    pix_fmt = str(v_stream.get("pix_fmt") or "").lower()
    if pix_fmt and pix_fmt != "yuv420p":
        return False

    try:
        level = int(v_stream.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    # Mobile/tablet browser hardware decoders are less tolerant for H.264 L5+.
    if level and level > 41:
        return False

    return True


async def _convert_import_file(raw_path: Path, output_path: Path,
                               kind: str, probe: dict[str, Any]) -> bool:
    has_audio = any(s.get("codec_type") == "audio" for s in probe.get("streams", []))
    video_stream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"
                         and not (s.get("disposition") or {}).get("attached_pic")), {})
    video_map = f"0:{video_stream.get('index', 0)}"
    copied = kind == "video" and is_browser_friendly_mp4(extract_media_profile(probe), probe)
    cmd = ["ffmpeg", "-y", "-i", str(raw_path)]
    if kind == "audio":
        cmd += ["-vn", "-c:a", "aac", "-b:a", "160k"]
    elif copied:
        cmd += ["-map", video_map, "-c:v", "copy"]
        cmd += ["-map", "0:a:0?", "-c:a", "copy"] if has_audio else ["-an"]
    else:
        cmd += ["-map", video_map, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k"] if has_audio else ["-an"]
    cmd += ["-movflags", "+faststart", str(output_path)]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[-400:]
        raise HTTPException(status_code=500, detail=f"Media import failed: {detail}")
    return not copied


def _persist_import(work_key: str, kind: str, title: str, media_type: str,
                    duration: float) -> dict[str, Any]:
    work_dir = resolve_work_dir(work_key)
    videos, audios = work_media_files(work_dir)
    video_tracks = [media_url_from_work(work_key, path.name) for path in videos]
    audio_tracks = [media_url_from_work(work_key, path.name) for path in audios]
    now = dt_to_str(now_utc())
    result = {
        "workKey": work_key, "mediaKind": kind,
        "videoUrl": video_tracks[0] if video_tracks else "",
        "audioUrl": audio_tracks[0] if audio_tracks else "",
        "videoTracks": video_tracks, "audioTracks": audio_tracks,
        "coverUrl": _resolve_cover_url(work_key), "type": media_type,
    }
    conn = db_conn()
    try:
        # Compare decoded work keys, not LIKE: encoded names and SQL wildcards are valid filenames.
        matches = [row for row in conn.execute("SELECT id, video_url, audio_url FROM media_assets").fetchall()
                   if stem_from_media_url(row["video_url"] or row["audio_url"] or "") == work_key]
        values = (title, result["videoUrl"], result["audioUrl"], result["coverUrl"],
                  media_type, duration, sum(p.stat().st_size for p in videos + audios), now)
        if matches:
            conn.execute("""UPDATE media_assets SET title=?, video_url=?, audio_url=?, cover_url=?,
                media_type=?, duration=?, size=?, updated_at=? WHERE id=?""", (*values, matches[0]["id"]))
            conn.executemany("DELETE FROM media_assets WHERE id=?", [(row["id"],) for row in matches[1:]])
        else:
            conn.execute("""INSERT INTO media_assets(title,video_url,audio_url,cover_url,
                media_type,duration,size,updated_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)""", (*values, now))
        conn.commit()
    finally:
        conn.close()
    return result


async def import_media_files(files: list[UploadFile], media_type: str = "movie",
                             cover: UploadFile | None = None, import_mode: str = "create",
                             target_work_key: str = "", work_name: str = "") -> dict[str, Any]:
    """Shared user/admin import: validate an entire homogeneous batch, stage, then publish."""
    staging = None
    try:
        mode = (import_mode or "create").strip().lower()
        if mode not in {"create", "append", "append_audio", "append_video"}:
            raise HTTPException(status_code=422, detail="Invalid import mode")
        append = mode != "create"
        if not files or len(files) > IMPORT_MAX_FILES:
            raise HTTPException(status_code=422, detail=f"Choose 1 to {IMPORT_MAX_FILES} media files")
        if append and cover:
            raise HTTPException(status_code=422, detail="Append mode does not accept cover uploads")
        if not append and media_type not in MEDIA_TYPE_OPTIONS:
            raise HTTPException(status_code=422, detail="Invalid media type")
        ensure_ffmpeg_tools()
        MEDIA_TMP_DIR.mkdir(parents=True, exist_ok=True)
        MEDIA_WORK_SUBDIR.mkdir(parents=True, exist_ok=True)
        staging = MEDIA_TMP_DIR / ("import_" + secrets.token_hex(12))
        staging.mkdir()
        prepared = []
        for index, upload in enumerate(files):
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS:
                raise HTTPException(status_code=400, detail="Only supported video/audio formats are allowed")
            raw_path = staging / f"raw_{index}{suffix}"
            size = 0
            with raw_path.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > int(1.5 * 1024**3):
                        raise HTTPException(status_code=413, detail="File too large, max 1.5GB")
                    output.write(chunk)
            probe = await asyncio.to_thread(probe_media, raw_path)
            streams = probe.get("streams") or []
            has_video = any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic") for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            if not has_video and not has_audio:
                raise HTTPException(status_code=400, detail="Uploaded file has no audio or video stream")
            prepared.append((upload.filename or "media", raw_path, probe, "video" if has_video else "audio"))
        kinds = {entry[3] for entry in prepared}
        if len(kinds) != 1:
            raise HTTPException(status_code=409, detail="A work cannot mix video and audio files")
        kind = prepared[0][3]
        if mode in {"append_audio", "append_video"} and kind != mode.removeprefix("append_"):
            raise HTTPException(status_code=409, detail="Uploaded media does not match the append mode")

        # Serialize publication/imports in this process so concurrent appends cannot mix kinds or names.
        async with _import_lock():
            if append:
                work_key = target_work_key.strip()
                validate_work_key(work_key)
                work_dir = resolve_work_dir(work_key)
                work_key = work_dir.name
                if not work_dir.is_dir():
                    raise HTTPException(status_code=404, detail="Append target not found")
                existing_kind = work_media_kind(work_dir)
                if existing_kind == "mixed":
                    raise HTTPException(status_code=409, detail="Legacy mixed works cannot be appended; create a new work")
                if existing_kind != kind:
                    raise HTTPException(status_code=409, detail="A work cannot mix video and audio files")
            else:
                if len(prepared) > 1 and not work_name.strip():
                    raise HTTPException(status_code=422, detail="A collection name is required for multiple files")
                work_key = normalize_work_name(work_name.strip() if len(prepared) > 1 else prepared[0][0])
                if resolve_work_dir(work_key).exists():
                    raise HTTPException(status_code=409, detail="A work with this name already exists; append to it or choose another collection name")
                work_dir = MEDIA_WORK_SUBDIR / media_type / work_key
                if work_dir.parent.is_symlink() or not work_dir.resolve().is_relative_to(MEDIA_WORK_SUBDIR.resolve()):
                    raise HTTPException(status_code=409, detail="Work destination escapes media storage")
            old_meta = _read_work_meta(work_dir) if append else {}
            effective_type = work_category(work_dir) or old_meta.get("mediaType") or media_type or _guess_media_type(work_key)
            if effective_type not in MEDIA_TYPE_OPTIONS:
                raise HTTPException(status_code=422, detail="Invalid media type")
            title = old_meta.get("title") or work_key
            output_dir = staging / "output" / work_key
            output_dir.mkdir(parents=True)
            existing_names = {p.name.casefold() for p in work_dir.iterdir()} if append else set()
            imported = []
            durations = dict(old_meta.get("trackDurations") or {})
            original_names = dict(old_meta.get("originalNames") or {})
            for original, raw_path, probe, _ in prepared:
                validate_work_key(original)
                base = Path(original).stem
                suffix = ".mp4" if kind == "video" else ".m4a"
                name = original if Path(original).suffix.lower() == suffix else base + suffix
                if name.casefold() in existing_names:
                    raise HTTPException(status_code=409, detail=f"Media filename already exists in this work: {name}")
                existing_names.add(name.casefold())
                transcoded = await _convert_import_file(raw_path, output_dir / name, kind, probe)
                durations[name] = float((probe.get("format") or {}).get("duration") or 0)
                original_names[name] = original
                imported.append({"filename": original, "url": media_url_from_work(work_key, name, work_category(work_dir)),
                                 "profile": extract_media_profile(probe), "transcoded": transcoded})
            if not append or not (work_dir / "cover.jpg").exists():
                if cover:
                    await asyncio.to_thread(_save_uploaded_cover, output_dir, cover)
                else:
                    first_video = next(output_dir.glob("*.mp4"), None) if kind == "video" else None
                    await asyncio.to_thread(_build_cover_for_work, output_dir, first_video)
            (output_dir / ".meta.json").write_text(json.dumps({
                **old_meta, "mediaKind": kind, "mediaType": effective_type, "title": title,
                "updatedAt": dt_to_str(now_utc()), "trackDurations": durations,
                "originalNames": original_names,
            }, ensure_ascii=False), encoding="utf-8")
            old_meta_bytes = (work_dir / ".meta.json").read_bytes() if append and (work_dir / ".meta.json").exists() else None
            published = []
            published_work = False
            try:
                if append:
                    for path in output_dir.iterdir():
                        if path.name == ".meta.json":
                            continue
                        target = work_dir / path.name
                        path.replace(target)
                        published.append(target)
                    (output_dir / ".meta.json").replace(work_dir / ".meta.json")
                else:
                    work_dir.parent.mkdir(parents=True, exist_ok=True)
                    output_dir.replace(work_dir)
                    published_work = True
                result = _persist_import(work_key, kind, title, effective_type, sum(durations.values()))
            except BaseException:
                if append:
                    for path in published:
                        path.unlink(missing_ok=True)
                    if old_meta_bytes is None:
                        (work_dir / ".meta.json").unlink(missing_ok=True)
                    else:
                        (work_dir / ".meta.json").write_bytes(old_meta_bytes)
                elif published_work:
                    work_dir.replace(output_dir)
                raise
            return {
                **result, "ok": True, "imported": len(imported), "count": len(imported),
                "failed": 0, "errors": [], "items": imported, "batch": len(imported) > 1,
                "appendMode": append, "targetWorkKey": work_key,
                "profile": imported[0]["profile"], "transcoded": any(item["transcoded"] for item in imported),
                "appendedTrackUrl": imported[-1]["url"] if append else "",
            }
    finally:
        for upload in files:
            await upload.close()
        if cover:
            await cover.close()
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


async def import_media_file(file: UploadFile, media_type: str, cover: UploadFile | None = None,
                            import_mode: str = "create", target_work_key: str = "") -> dict[str, Any]:
    return await import_media_files([file], media_type, cover, import_mode, target_work_key)


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

    rows = conn.execute(
        "SELECT id, title, video_url, audio_url, cover_url, media_type FROM media_assets"
    ).fetchall()
    for row in rows:
        source_url = row["video_url"] or row["audio_url"] or ""
        stem = stem_from_media_url(source_url)
        if not stem:
            continue
        # Only legacy flat paths need migration naming. Existing work names are exact identifiers.
        split = _split_media_url(source_url)
        work_key = stem if split and split[0] == "work" else sanitize_filename_stem(stem)
        work_dir = resolve_work_dir(work_key)
        if not work_dir.exists():
            continue
        videos, audios = work_media_files(work_dir)
        video_file = videos[0] if videos else None
        audio_file = audios[0] if audios else None
        if not video_file and not audio_file:
            continue
        conn.execute(
            "UPDATE media_assets SET video_url = ?, audio_url = ?, cover_url = ?, media_type = ?, updated_at = ? WHERE id = ?",
            (
                media_url_from_work(work_key, video_file.name) if video_file else "",
                media_url_from_work(work_key, audio_file.name) if audio_file else "",
                (row["cover_url"] or media_url_from_work(work_key, "cover.jpg")),
                (row["media_type"] or _guess_media_type(row["title"] or work_key)),
                dt_to_str(now_utc()),
                row["id"],
            ),
        )

        cover_file = work_dir / "cover.jpg"
        if not cover_file.exists():
            try:
                _build_cover_for_work(work_dir, video_file)
            except HTTPException:
                pass
    conn.commit()

    # Deduplicate rows that point to the same work folder (keep newest update).
    rows2 = conn.execute(
        "SELECT id, video_url, audio_url, updated_at FROM media_assets"
    ).fetchall()
    by_work: dict[str, list[sqlite3.Row]] = {}
    for row in rows2:
        work = stem_from_media_url(row["video_url"] or row["audio_url"] or "")
        if not work:
            continue
        by_work.setdefault(work, []).append(row)
    for _work, entries in by_work.items():
        if len(entries) < 2:
            continue
        ordered = sorted(
            entries, key=lambda r: str(r["updated_at"] or ""), reverse=True
        )
        for dup in ordered[1:]:
            conn.execute("DELETE FROM media_assets WHERE id = ?", (dup["id"],))
    conn.commit()

    return {"movedDirs": moved_dirs, "movedFiles": moved_files}
