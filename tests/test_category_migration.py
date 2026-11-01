"""Category storage migration and failure recovery in a disposable site."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_legacy_migration as fixtures
from tools import migrate_media_categories as migration
from tools import migrate_legacy_media as legacy
from wt_server import media
from fastapi import HTTPException


class CategoryMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.LegacyMigrationTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.LegacyMigrationTests.setUp(self)
        self.patcher = patch.multiple(media, BASE_DIR=self.root, MEDIA_WORK_DIR=self.root / "media",
                                     MEDIA_WORK_SUBDIR=self.root / "media/work",
                                     MEDIA_TMP_DIR=self.root / "media/tmp")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def work(self, name, files, meta):
        (self.root / "media/work" / name).parent.mkdir(parents=True, exist_ok=True)
        return fixtures.LegacyMigrationTests.work(self, name, files, meta)

    def plan(self):
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn:
            return migration.build_plan(self.root, conn)

    def test_moves_by_metadata_preserves_ids_episode_urls_and_is_repeatable(self):
        self.work("Film #1", {"Film.mp4": self.video}, {"mediaType": "shot", "custom": "keep"})
        self.work("Album", {"Song.m4a": self.audio}, {"mediaType": "music"})
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn, conn:
            conn.execute("INSERT INTO media_assets(id,title,video_url,media_type) VALUES(7,'Film #1',?,'shot')",
                         (legacy.url("Film #1", "Film.mp4"),))
            conn.execute("INSERT INTO room_state VALUES(1,?)", (legacy.url("Film #1", "Film.mp4"),))
        result = migration.migrate(self.root, self.plan(), self.backup)
        self.assertEqual(result["movedWorks"], 2)
        self.assertEqual((self.root / "media/work/shot/Film #1/Film.mp4").read_bytes(), self.video)
        self.assertEqual((self.root / "media/work/music/Album/Song.m4a").read_bytes(), self.audio)
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn:
            self.assertEqual(conn.execute("SELECT video_url FROM media_assets WHERE id=7").fetchone()[0],
                             media.media_url_from_work("Film #1", "Film.mp4"))
            self.assertEqual(conn.execute("SELECT video_url FROM room_state").fetchone()[0],
                             media.media_url_from_work("Film #1", "Film.mp4"))
        self.assertEqual(media.normalize_media_url(legacy.url("Film #1", "Film.mp4")),
                         media.media_url_from_work("Film #1", "Film.mp4"))
        self.assertTrue(all(p["source"] == p["target"] for p in self.plan()))
        self.assertEqual(len(media.list_appendable_works()), 2)

    def test_offline_failure_restores_directories_and_database(self):
        source = self.work("Film", {"Film.mp4": self.video}, {"mediaType": "shot"})
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn, conn:
            conn.execute("INSERT INTO media_assets(id,title,video_url) VALUES(7,'Film',?)", (legacy.url("Film", "Film.mp4"),))
            conn.executescript("CREATE TRIGGER reject_update BEFORE UPDATE ON media_assets BEGIN SELECT RAISE(ABORT,'injected'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            migration.migrate(self.root, self.plan(), self.backup)
        self.assertEqual((source / "Film.mp4").read_bytes(), self.video)
        self.assertFalse((self.root / "media/work/shot/Film").exists())
        self.assertFalse((self.backup / "result.json").exists())

    def test_runtime_category_failure_restores_metadata_and_files(self):
        source = self.work("music/Album", {"Song.m4a": self.audio}, {"mediaType": "music", "custom": "keep"})
        before = (source / ".meta.json").read_bytes()
        with closing(sqlite3.connect(self.root / "watch_together.db")) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO media_assets(id,title,audio_url) VALUES(7,'Album',?)", (media.media_url_from_work("Album", "Song.m4a"),))
            conn.commit()
            conn.executescript("CREATE TRIGGER reject_update BEFORE UPDATE ON media_assets BEGIN SELECT RAISE(ABORT,'injected'); END;")
            with self.assertRaises(sqlite3.IntegrityError):
                media.relocate_media_work(conn, "Album", "ASMR")
        self.assertEqual((source / ".meta.json").read_bytes(), before)
        self.assertEqual((source / "Song.m4a").read_bytes(), self.audio)
        self.assertFalse((self.root / "media/work/ASMR/Album").exists())

    def test_duplicate_global_titles_refuse(self):
        self.work("music/Same", {"Song.m4a": self.audio}, {"mediaType": "music"})
        self.work("movie/Same", {"Film.mp4": self.video}, {"mediaType": "movie"})
        with self.assertRaises(HTTPException):
            self.plan()
        self.assertFalse(self.backup.exists())

    def test_invalid_category_refuses_before_backup(self):
        directory = self.work("Film", {"Film.mp4": self.video}, {"mediaType": "unknown"})
        with self.assertRaisesRegex(ValueError, "Invalid category"):
            self.plan()
        self.assertTrue((directory / "Film.mp4").exists())
        self.assertFalse(self.backup.exists())

    def test_legacy_cleanup_also_understands_categorized_directories(self):
        self.work("music/Source.flac", {"Source.m4a": self.audio},
                  {"mediaType": "music", "originalNames": {"Source.m4a": "Source.flac"}})
        plans = legacy.build_plan(self.root)
        legacy.migrate(self.root, plans, self.backup)
        self.assertEqual((self.root / "media/work/music/Source/Source.m4a").read_bytes(), self.audio)
        meta = json.loads((self.root / "media/work/music/Source/.meta.json").read_text())
        self.assertEqual(meta["originalNames"]["Source.m4a"], "Source.flac")


if __name__ == "__main__":
    unittest.main()
