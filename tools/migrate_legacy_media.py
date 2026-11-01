"""Offline, backed-up migration of existing media into homogeneous named works.

Default is a read-only plan. Stop every server before --apply. Backups must be
outside media (which is public), and contain complete independent file copies.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess

EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov", ".m4a", ".mp3", ".wav", ".aac", ".flac"}


def valid_name(value):
    if (not value or value != value.strip(" .") or value in {".", ".."}
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value)):
        raise ValueError(f"Unsafe filename: {value!r}")
    return value


def work_title(value):
    value = valid_name(value)
    while Path(value).suffix.lower() in EXTENSIONS:
        value = Path(value).stem
    return valid_name(value)


def child(root, relative):
    path = root / relative
    if root.resolve() not in path.resolve().parents:
        raise ValueError(f"Path escapes its root: {relative}")
    return path


def url(key, filename):
    from urllib.parse import quote
    return f"/media/work/{quote(key, safe='')}/{quote(filename, safe='')}"


def url_work(value):
    from urllib.parse import unquote
    parts = (value or "").split("/")
    return unquote(parts[3]) if len(parts) == 5 and parts[1:3] == ["media", "work"] else ""


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True, check=True)
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video and not audio:
        raise ValueError(f"No playable stream: {path}")
    return {"name": path.name, "kind": "video" if video else "audio", "audio": audio,
            "duration": float(info.get("format", {}).get("duration") or 0), "size": path.stat().st_size}


def build_plan(project):
    media = project / "media"
    work_root = media / "work"
    plans = []
    for directory in sorted(work_root.iterdir()):
        if directory.is_symlink():
            raise ValueError(f"Symlink work needs manual review: {directory}")
        if not directory.is_dir():
            continue
        valid_name(directory.name)
        for path in directory.iterdir():
            if path.is_symlink() or path.is_dir():
                raise ValueError(f"Nested directory/link needs manual review: {path}")
        tracks = [probe(p) for p in sorted(directory.iterdir()) if p.suffix.lower() in EXTENSIONS]
        if not tracks:
            raise ValueError(f"Empty work needs manual review: {directory}")
        meta_path = directory / ".meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        videos = [p for p in tracks if p["kind"] == "video"]
        audios = [p for p in tracks if p["kind"] == "audio"]
        archived = []
        if videos and audios:
            # Only the old importer's exact shape is automatically reduced to video.
            if len(videos) != 1 or len(audios) != 1 or videos[0]["name"] != "video.mp4" or audios[0]["name"] != "audio.m4a":
                raise ValueError(f"Unrecognized mixed work needs manual review: {directory.name}")
            embedded, separate = videos[0]["audio"], audios[0]["audio"]
            if not embedded or not separate:
                raise ValueError(f"Video has no soundtrack to retain: {directory.name}")
            # Old importer reencoded the embedded stream; durations/sample layout must agree.
            if (abs(float(embedded.get("duration") or 0) - float(separate.get("duration") or 0)) > 0.1
                    or any(embedded.get(key) != separate.get(key) for key in ("sample_rate", "channels"))):
                raise ValueError(f"Audio differs from embedded soundtrack: {directory.name}")
            archived = audios
            tracks = videos
        kind = tracks[0]["kind"]
        key = work_title(directory.name)
        renamed = {}
        original_names = {}
        for track in tracks:
            name = track["name"]
            original = (meta.get("originalNames") or {}).get(name)
            destination = name
            if len(tracks) == 1:
                if not original and Path(name).stem.lower() in {"video", "audio"}:
                    suffix = Path(name).suffix
                    destination = key if key.lower().endswith(suffix.lower()) else key + suffix
                elif original and Path(name).stem.lower() in {"video", "audio"}:
                    destination = Path(original).stem + Path(name).suffix
                key = work_title(original or destination)
            renamed[name] = valid_name(destination)
            original_names[destination] = original or destination
        if len({name.casefold() for name in renamed.values()}) != len(renamed):
            raise ValueError(f"Duplicate output filenames: {directory.name}")
        for old, new in renamed.items():
            if new != old and (directory / new).exists():
                raise ValueError(f"Filename collision: {directory / new}")
        plans.append({"oldKey": directory.name, "newKey": key, "kind": kind,
                      "renames": renamed, "tracks": tracks, "archived": archived,
                      "originalNames": original_names, "oldMeta": meta})
    names = [p["newKey"].casefold() for p in plans]
    if len(set(names)) != len(names):
        raise ValueError("Work name collision; no files have been changed")
    for plan in plans:
        destination = child(work_root, plan["newKey"])
        if plan["newKey"] != plan["oldKey"] and destination.exists():
            raise ValueError(f"Work destination already exists: {destination}")
    # Flat legacy directories must already have gone through the normal startup migration.
    for name in ("video", "audio"):
        if any(p.suffix.lower() in EXTENSIONS for p in (media / name).glob("*")):
            raise ValueError(f"Legacy flat media/{name} needs startup migration first")
    return plans


def file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def snapshot(project, backup):
    media = project / "media"
    if backup.exists() or backup.resolve() == project.resolve() or media.resolve() in backup.resolve().parents:
        raise ValueError("Backup must be a new directory outside public media")
    backup.mkdir(parents=True)
    print("Copying and SHA-256 checking the full media backup...", flush=True)
    manifest = {}
    for source in media.rglob("*"):
        if source.is_symlink():
            raise ValueError(f"Cannot snapshot symlink: {source}")
        if source.is_file():
            relative = source.relative_to(media)
            destination = child(backup / "media", relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            digest = file_hash(source)
            if digest != file_hash(destination):
                raise ValueError(f"Backup checksum mismatch: {source}")
            manifest[str(relative)] = {"size": source.stat().st_size, "sha256": digest}
            if len(manifest) % 50 == 0:
                print(f"Backup checked: {len(manifest)} files", flush=True)
    with closing(sqlite3.connect(project / "watch_together.db")) as source:
        with closing(sqlite3.connect(backup / "watch_together.db")) as destination:
            source.backup(destination)
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def migrate(project, plans, backup):
    manifest = snapshot(project, backup)
    (backup / "plan.json").write_text(json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8")
    root = project / "media/work"
    stamp = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(project / "watch_together.db")
    conn.row_factory = sqlite3.Row
    journal = []
    archived_paths = []
    metadata_paths = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM media_assets ORDER BY id").fetchall()
        kept = set()
        mappings = {}
        for plan in plans:
            old_key, key = plan["oldKey"], plan["newKey"]
            directory = child(root, old_key)
            for track in plan["archived"]:
                source = child(directory, track["name"])
                if file_hash(source) != manifest[str(source.relative_to(project / "media"))]["sha256"]:
                    raise ValueError(f"Source changed since backup: {source}")
                archived_paths.append(source)
                source.unlink()  # Complete checked copy remains in the nonpublic backup.
            for old, new in plan["renames"].items():
                if old != new:
                    source, destination = child(directory, old), child(directory, new)
                    source.rename(destination)
                    journal.append((source, destination))
            if key != old_key:
                destination = child(root, key)
                directory.rename(destination)
                journal.append((directory, destination))
                directory = destination
            meta = dict(plan["oldMeta"])
            meta.update({"title": key if len(plan["tracks"]) == 1 else meta.get("title", key),
                         "mediaKind": plan["kind"], "originalNames": plan["originalNames"],
                         "trackDurations": {plan["renames"][p["name"]]: p["duration"] for p in plan["tracks"]}})
            meta.setdefault("mediaType", "movie" if plan["kind"] == "video" else "music")
            meta.pop("audioTrackOrder", None)
            if "legacyMigration" not in meta and meta != plan["oldMeta"]:
                meta["legacyMigration"] = {"at": stamp, "previousWorkKey": old_key,
                                           "nameSource": "legacy-fallback-or-existing-name",
                                           "archivedFiles": [p["name"] for p in plan["archived"]]}
            if meta != plan["oldMeta"]:
                meta["updatedAt"] = stamp
                metadata_paths.append(old_key)
                (directory / ".meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            names = list(plan["renames"].values())
            # Use the same natural ordering as the library.
            names.sort(key=lambda name: [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", name)])
            primary = url(key, names[0])
            video, audio = (primary, "") if plan["kind"] == "video" else ("", primary)
            cover = url(key, "cover.jpg") if (directory / "cover.jpg").exists() else "/media/default_cover.jpg"
            values = (meta["title"], video, audio, cover, meta["mediaType"],
                      sum(p["duration"] for p in plan["tracks"]), sum(p["size"] for p in plan["tracks"]), stamp)
            matches = [row for row in existing if url_work(row["video_url"] or row["audio_url"]) == old_key]
            if matches:
                row_id = matches[0]["id"]
                kept.add(row_id)
                conn.execute("""UPDATE media_assets SET title=?,video_url=?,audio_url=?,cover_url=?,
                             media_type=?,duration=?,size=?,updated_at=? WHERE id=?""", (*values, row_id))
            else:
                conn.execute("""INSERT INTO media_assets(title,video_url,audio_url,cover_url,media_type,
                             duration,size,updated_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)""", (*values, stamp))
            for old, new in plan["renames"].items():
                mappings[url(old_key, old)] = url(key, new)
            for track in plan["archived"]:
                mappings[url(old_key, track["name"])] = primary
        stale = [row["id"] for row in existing if row["id"] not in kept]
        conn.executemany("DELETE FROM media_assets WHERE id=?", [(value,) for value in stale])
        from urllib.parse import unquote
        decoded = {unquote(old): new for old, new in mappings.items()}
        for row in conn.execute("SELECT room_id,video_url FROM room_state").fetchall():
            new_url = decoded.get(unquote(row["video_url"]))
            if new_url and new_url != row["video_url"]:
                conn.execute("UPDATE room_state SET video_url=? WHERE room_id=?", (new_url, row["room_id"]))
        # Every retained/archived media byte is checked against the independent snapshot.
        for plan in plans:
            for track in plan["tracks"]:
                original = str(Path("work") / plan["oldKey"] / track["name"])
                destination = child(child(root, plan["newKey"]), plan["renames"][track["name"]])
                if file_hash(destination) != manifest[original]["sha256"]:
                    raise ValueError(f"Post-migration checksum mismatch: {destination}")
        result = {"works": len(plans), "renamedWorks": sum(p["oldKey"] != p["newKey"] for p in plans),
                  "archivedAudioFiles": sum(len(p["archived"]) for p in plans),
                  "archivedBytes": sum(t["size"] for p in plans for t in p["archived"]),
                  "removedStaleRows": len(stale), "backup": str(backup), "sha256Verified": True}
        (backup / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        (backup / "result.json").unlink(missing_ok=True)
        for source, destination in reversed(journal):
            destination.rename(source)
        for source in archived_paths:
            shutil.copy2(backup / "media" / source.relative_to(project / "media"), source)
        for key in metadata_paths:
            source = backup / "media/work" / key / ".meta.json"
            destination = root / key / ".meta.json"
            if source.exists():
                shutil.copy2(source, destination)
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    if args.apply:
        if not args.backup_dir:
            parser.error("--apply requires --backup-dir")
        for host in ("127.0.0.1", "::1"):
            for port in (8090, 8091):
                try:
                    with socket.create_connection((host, port), timeout=0.2):
                        parser.error(f"Stop all services before migration: {host}:{port} is listening")
                except OSError:
                    pass
    plans = build_plan(project)
    result = migrate(project, plans, args.backup_dir.resolve()) if args.apply else {
        "works": len(plans), "renamedWorks": sum(p["oldKey"] != p["newKey"] for p in plans),
        "archivedAudioFiles": sum(len(p["archived"]) for p in plans), "plan": plans}
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "plan"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
