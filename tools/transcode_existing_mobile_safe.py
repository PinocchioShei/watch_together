from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "watch_together.db"
WORK_ROOT = ROOT / "media" / "work"


def now_str() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ffprobe_json(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-print_format",
        "json",
        str(path),
    ]
    out = subprocess.check_output(cmd)
    return json.loads(out.decode("utf-8", errors="ignore"))


def video_stream(meta: dict) -> dict:
    for stream in meta.get("streams", []):
        if stream.get("codec_type") == "video" and not bool((stream.get("disposition") or {}).get("attached_pic", 0)):
            return stream
    return {}


def is_mobile_safe(meta: dict) -> bool:
    fmt = (meta.get("format") or {}).get("format_name", "")
    stream = video_stream(meta)
    codec = str(stream.get("codec_name") or "")
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    try:
        level = int(stream.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    container_ok = any(x in fmt for x in ["mp4", "mov", "m4a", "3gp", "3g2", "mj2"])
    return container_ok and codec == "h264" and pix_fmt == "yuv420p" and (level == 0 or level <= 41)


def transcode_video_in_place(video_path: Path) -> None:
    tmp_path = video_path.with_name(video_path.stem + ".mobile.tmp" + video_path.suffix)
    meta = ffprobe_json(video_path)
    has_audio = any(s.get("codec_type") == "audio" for s in meta.get("streams", []))
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(tmp_path)]

    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not tmp_path.exists():
        detail = (proc.stderr or b"").decode("utf-8", errors="ignore")[-500:]
        raise RuntimeError(f"ffmpeg failed for {video_path}: {detail}")

    backup = video_path.with_suffix(video_path.suffix + ".bak")
    if backup.exists():
        backup.unlink()
    shutil.move(str(video_path), str(backup))
    shutil.move(str(tmp_path), str(video_path))
    backup.unlink(missing_ok=True)


def update_db_for_work(conn: sqlite3.Connection, work: str, video_path: Path) -> None:
    meta = ffprobe_json(video_path)
    try:
        duration = float((meta.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    size = video_path.stat().st_size
    conn.execute(
        """
        UPDATE media_assets
        SET duration = ?, size = ?, updated_at = ?
        WHERE video_url LIKE ?
        """,
        (duration, size, now_str(), f"/media/work/{work}/%"),
    )


def main() -> None:
    videos = sorted(WORK_ROOT.glob("*/video.*"))
    targets: list[tuple[str, Path]] = []
    for path in videos:
        meta = ffprobe_json(path)
        if not is_mobile_safe(meta):
            targets.append((path.parent.name, path))

    print(f"found_videos={len(videos)}")
    print(f"to_transcode={len(targets)}")

    conn = sqlite3.connect(DB_PATH)
    try:
        for work, video_path in targets:
            print(f"transcoding {work}: {video_path.name}")
            transcode_video_in_place(video_path)
            update_db_for_work(conn, work, video_path)
            conn.commit()
            print(f"done {work}")
    finally:
        conn.close()

    print("completed")


if __name__ == "__main__":
    main()
