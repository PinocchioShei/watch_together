"""Migration regressions use only a disposable database and generated media."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("legacy_migration", Path(__file__).resolve().parents[1] / "tools/migrate_legacy_media.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class LegacyMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("ffmpeg/ffprobe required")
        fixture = tempfile.TemporaryDirectory(prefix="migration-fixtures-")
        cls.addClassCleanup(fixture.cleanup)
        root = Path(fixture.name)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(root / "video.mp4")], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(root / "video.mp4"), "-vn", "-c:a", "aac",
                        "-b:a", "160k", str(root / "audio.m4a")], check=True)
        cls.video = (root / "video.mp4").read_bytes()
        cls.audio = (root / "audio.m4a").read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="migration-site-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backup = self.root / "backups/run"
        (self.root / "media/work").mkdir(parents=True)
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn, conn:
            conn.executescript("""
                CREATE TABLE media_assets(id INTEGER PRIMARY KEY, title TEXT, video_url TEXT,
                  audio_url TEXT, cover_url TEXT, media_type TEXT, duration REAL, size INTEGER,
                  updated_at TEXT, created_at TEXT);
                CREATE TABLE room_state(room_id INTEGER PRIMARY KEY, video_url TEXT);
            """)

    def work(self, name, files, meta=None):
        directory = self.root / "media/work" / name
        directory.mkdir()
        for filename, content in files.items():
            (directory / filename).write_bytes(content)
        (directory / "cover.jpg").write_bytes(b"cover")
        if meta is not None:
            (directory / ".meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return directory

    def test_migration_archives_audio_preserves_bytes_and_repairs_indexes(self):
        original = self.work("旧视频 #1", {"video.mp4": self.video, "audio.m4a": self.audio}, {"mediaType": "shot"})
        album = self.work("Album", {"Track 2.m4a": self.audio, "Track 10.m4a": self.audio}, {"mediaType": "RJ"})
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn, conn:
            conn.execute("INSERT INTO media_assets(id,title,video_url,audio_url) VALUES(7,?,?,?)",
                         (original.name, migration.url(original.name, "video.mp4"), migration.url(original.name, "audio.m4a")))
            conn.execute("INSERT INTO media_assets(id,title,video_url,audio_url) VALUES(8,'Ghost','/missing.mp4','')")
            conn.execute("INSERT INTO room_state VALUES(1,?)", (migration.url(original.name, "audio.m4a"),))
        result = migration.migrate(self.root, migration.build_plan(self.root), self.backup)
        self.assertEqual(result["archivedAudioFiles"], 1)
        self.assertEqual(result["removedStaleRows"], 1)
        new = self.root / "media/work/旧视频 #1"
        self.assertEqual((new / "旧视频 #1.mp4").read_bytes(), self.video)
        self.assertFalse((new / "audio.m4a").exists())
        self.assertEqual((self.backup / "media/work/旧视频 #1/audio.m4a").read_bytes(), self.audio)
        self.assertEqual((album / "Track 2.m4a").read_bytes(), self.audio)
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn, conn:
            row = conn.execute("SELECT id,title,audio_url,cover_url FROM media_assets WHERE id=7").fetchone()
            self.assertEqual(row[:3], (7, "旧视频 #1", ""))
            self.assertEqual(row[3], migration.url("旧视频 #1", "cover.jpg"))
            self.assertEqual(conn.execute("SELECT count(*) FROM media_assets").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT video_url FROM room_state").fetchone()[0], migration.url("旧视频 #1", "旧视频 #1.mp4"))
        second = migration.build_plan(self.root)
        self.assertTrue(all(p["oldKey"] == p["newKey"] and not p["archived"] for p in second))

    def test_original_filename_metadata_and_audio_collections_are_preserved(self):
        self.work("Source.flac", {"Source.m4a": self.audio}, {"originalNames": {"Source.m4a": "Source.flac"}})
        self.work("Old song", {"audio.m4a": self.audio})
        plans = migration.build_plan(self.root)
        source = next(p for p in plans if p["oldKey"] == "Source.flac")
        self.assertEqual(source["newKey"], "Source")
        song = next(p for p in plans if p["oldKey"] == "Old song")
        self.assertEqual(song["newKey"], "Old song")
        self.assertEqual(song["renames"], {"audio.m4a": "Old song.m4a"})
        migration.migrate(self.root, plans, self.backup)
        self.assertEqual((self.root / "media/work/Source/Source.m4a").read_bytes(), self.audio)
        meta = json.loads((self.root / "media/work/Source/.meta.json").read_text())
        self.assertEqual(meta["originalNames"]["Source.m4a"], "Source.flac")
        self.assertFalse((self.root / "media/work/Source.flac").exists())

    def test_name_collision_refuses_without_mutation(self):
        old = self.work("Same", {"video.mp4": self.video})
        self.work("Same.mp4", {"Same.mp4": self.video})
        with self.assertRaisesRegex(ValueError, "collision"):
            migration.build_plan(self.root)
        self.assertTrue((old / "video.mp4").exists())
        self.assertFalse(self.backup.exists())

    def test_unrecognized_mixed_work_is_not_silently_reduced(self):
        directory = self.work("Mixed", {"video.mp4": self.video, "independent.m4a": self.audio})
        with self.assertRaisesRegex(ValueError, "manual review"):
            migration.build_plan(self.root)
        self.assertEqual((directory / "independent.m4a").read_bytes(), self.audio)

    def test_database_failure_rolls_back_files_and_metadata(self):
        meta = {"title": "Before", "extra": "preserved"}
        directory = self.work("Before.mp4", {"video.mp4": self.video, "audio.m4a": self.audio}, meta)
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn, conn:
            conn.executescript("CREATE TRIGGER reject_insert BEFORE INSERT ON media_assets BEGIN SELECT RAISE(ABORT,'injected failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            migration.migrate(self.root, migration.build_plan(self.root), self.backup)
        self.assertEqual((directory / "video.mp4").read_bytes(), self.video)
        self.assertEqual((directory / "audio.m4a").read_bytes(), self.audio)
        self.assertEqual(json.loads((directory / ".meta.json").read_text()), meta)
        self.assertFalse((self.root / "media/work/Before").exists())
        self.assertFalse((self.backup / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
