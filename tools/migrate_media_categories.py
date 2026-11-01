"""Offline migration to media/work/<category>/<work>; default is a read-only plan."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wt_server import media
try:
    from .migrate_legacy_media import snapshot, file_hash, child
except ImportError:
    from migrate_legacy_media import snapshot, file_hash, child


def build_plan(project, conn):
    root = project / "media/work"
    rows = conn.execute("SELECT video_url,audio_url,media_type FROM media_assets").fetchall()
    categories = {media.stem_from_media_url(row[0] or row[1] or ""): row[2] for row in rows}
    plans = []
    for directory in media.iter_work_dirs():
        media.validate_work_key(directory.name)
        if any(p.is_symlink() or p.is_dir() for p in directory.iterdir()):
            raise ValueError(f"Nested directory or link requires review: {directory}")
        meta = media._read_work_meta(directory)
        category = media.work_category(directory) or meta.get("mediaType") or categories.get(directory.name) or media._guess_media_type(directory.name)
        if category not in media.MEDIA_TYPE_OPTIONS:
            raise ValueError(f"Invalid category for {directory.name}: {category}")
        target = root / category / directory.name
        if directory.resolve() in target.resolve().parents:
            raise ValueError(f"Legacy work named like a category requires review: {directory.name}")
        if target.parent.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Destination escapes media/work")
        if target.exists() and target != directory:
            raise ValueError(f"Destination already exists: {target}")
        videos, audios = media.work_media_files(directory)
        if not videos and not audios:
            raise ValueError(f"Work has no playable media: {directory}")
        plans.append({"workKey": directory.name, "category": category,
                      "source": str(directory.relative_to(root)), "target": str(target.relative_to(root))})
    return plans


def migrate(project, plans, backup):
    manifest = snapshot(project, backup)
    (backup / "plan.json").write_text(json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8")
    root = project / "media/work"
    journal, metadata = [], []
    conn = sqlite3.connect(project / "watch_together.db")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        for plan in plans:
            source, target = child(root, plan["source"]), child(root, plan["target"])
            if source != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
                journal.append((source, target))
            old_meta = media._read_work_meta(target)
            if old_meta.get("mediaType") != plan["category"]:
                metadata.append(plan["source"])
                media._write_work_meta(target, plan["category"], old_meta.get("title") or plan["workKey"])
            media.rewrite_work_urls(conn, plan["workKey"], target)
            for path in target.iterdir():
                if path.is_file() and path.name != ".meta.json":
                    original = str(Path("work") / plan["source"] / path.name)
                    if file_hash(path) != manifest[original]["sha256"]:
                        raise ValueError(f"Checksum mismatch: {path}")
        result = {"works": len(plans), "movedWorks": sum(p["source"] != p["target"] for p in plans),
                  "categories": dict(Counter(p["category"] for p in plans)),
                  "backup": str(backup), "sha256Verified": True}
        (backup / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        (backup / "result.json").unlink(missing_ok=True)
        for source, target in reversed(journal):
            target.rename(source)
        for relative in metadata:
            source = backup / "media/work" / relative / ".meta.json"
            target = root / relative / ".meta.json"
            if source.exists():
                shutil.copy2(source, target)
            else:
                target.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.apply:
        if not args.backup_dir:
            parser.error("--apply requires a new nonpublic --backup-dir")
        for host in ("127.0.0.1", "::1"):
            for port in (8090, 8091):
                try:
                    with socket.create_connection((host, port), timeout=0.2):
                        parser.error("Stop every server before migrating")
                except OSError:
                    pass
    with closing(sqlite3.connect((project / "watch_together.db").as_uri() + "?mode=ro", uri=True)) as conn:
        plans = build_plan(project, conn)
    result = migrate(project, plans, args.backup_dir.resolve()) if args.apply else {
        "works": len(plans), "movedWorks": sum(p["source"] != p["target"] for p in plans),
        "categories": dict(Counter(p["category"] for p in plans)), "plan": plans}
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "plan"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
