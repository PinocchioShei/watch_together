"""Room-entry activity survives leaving rooms and upgrades old databases."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from websockets.sync.client import connect

import test_collections as integration
from wt_server import storage


class UserActivityMigrationTests(unittest.TestCase):
    def test_backfills_known_entries_once_and_preserves_them_after_membership_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.multiple(storage, DB_PATH=root / "test.db", MEDIA_DIR=root / "media",
                                MEDIA_VIDEO_DIR=root / "media/video", MEDIA_AUDIO_DIR=root / "media/audio",
                                MEDIA_TMP_DIR=root / "media/tmp", LOG_DEBUG_DIR=root / "log/debug"):
                storage.init_db()
                with closing(storage.db_conn()) as conn, conn:
                    conn.execute("ALTER TABLE users DROP COLUMN last_room_joined_at")
                    for user_id in (1, 2):
                        conn.execute("INSERT INTO users VALUES (?,?,?,?,?)", (user_id, f"legacy{user_id}", "salt", "hash", "2020-01-01T00:00:00+00:00"))
                    for room in (1, 2):
                        conn.execute("INSERT INTO rooms(id,name,owner_id,created_at) VALUES (?,?,1,?)", (room, f"Room {room}", "2020-01-01T00:00:00+00:00"))
                        conn.execute("INSERT INTO room_members(room_id,user_id,joined_at) VALUES (?,1,?)", (room, f"2026-01-0{room}T08:00:00+00:00"))
                storage.init_db()
                with closing(storage.db_conn()) as conn, conn:
                    rows = conn.execute("SELECT last_room_joined_at FROM users ORDER BY id").fetchall()
                    self.assertEqual(rows[0][0], "2026-01-02T08:00:00+00:00")
                    self.assertIsNone(rows[1][0])
                    conn.execute("DELETE FROM rooms")
                    storage.record_room_entry(conn, 1, "2025-01-01T00:00:00+00:00")
                storage.init_db()
                with closing(storage.db_conn()) as conn:
                    self.assertEqual(conn.execute("SELECT last_room_joined_at FROM users WHERE id=1").fetchone()[0], "2026-01-02T08:00:00+00:00")


class UserActivityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = integration.CollectionIntegrationTests
        fixture.setUpClass()
        cls.addClassCleanup(fixture.doClassCleanups)
        cls.f = fixture()

    def activity(self, username):
        status, data = self.f.request("/api/admin/users", token=self.f.admin)
        self.assertEqual(status, 200)
        return next(p["lastRoomJoinedAt"] for p in data["items"] if p["username"] == username)

    def test_only_successful_entries_update_and_history_survives_leaving(self):
        f = self.f
        self.assertEqual(f.request("/api/admin/users", token=f.owner)[0], 401)
        self.assertIsNone(self.activity("collection_owner"))
        self.assertIsNone(self.activity("collection_peer"))
        room = f.request("/api/rooms", payload={"name": "Activity room", "password": f.password}, token=f.owner)[1]["id"]
        created = self.activity("collection_owner")
        self.assertIsNotNone(datetime.fromisoformat(created).tzinfo)
        self.assertEqual(f.request(f"/api/rooms/{room}/join", payload={"password": "incorrect"}, token=f.peer)[0], 403)
        self.assertEqual(f.request("/api/rooms/999999/join", payload={"password": f.password}, token=f.peer)[0], 404)
        self.assertIsNone(self.activity("collection_peer"))
        self.assertEqual(f.request(f"/api/rooms/{room}/join", payload={"password": f.password}, token=f.peer)[0], 200)
        joined = self.activity("collection_peer")
        self.assertGreaterEqual(joined, created)
        # Heartbeats, sync connections and normal reads do not count as entering again.
        with connect(f"ws://127.0.0.1:{f.port}/ws/rooms/{room}?token={f.peer}") as ws:
            f.receive(ws, lambda p: p["type"] == "state")
            ws.send(json.dumps({"type": "ping"}))
            f.receive(ws, lambda p: p["type"] == "pong")
        f.request(f"/api/rooms/{room}/state", token=f.peer)
        f.request("/api/rooms", token=f.peer)
        self.assertEqual(self.activity("collection_peer"), joined)
        # Logging in creates a session, not a room entry.
        peer = f.request("/api/login", payload={"username": "collection_peer", "password": f.password})[1]["token"]
        self.assertEqual(self.activity("collection_peer"), joined)
        self.assertEqual(f.request(f"/api/rooms/{room}/leave", token=peer, method="POST")[0], 200)
        self.assertEqual(self.activity("collection_peer"), joined)
        self.assertEqual(f.request(f"/api/rooms/{room}/join", payload={"password": ""}, token=peer)[0], 200)
        restored = self.activity("collection_peer")
        self.assertGreater(restored, joined)
        second = f.request("/api/rooms", payload={"name": "Other activity room", "password": f.password}, token=f.owner)[1]["id"]
        f.request(f"/api/rooms/{second}/join", payload={"password": f.password}, token=peer)
        latest = self.activity("collection_peer")
        self.assertGreater(latest, restored)
        self.assertEqual(f.request(f"/api/rooms/{second}", token=f.owner, method="DELETE")[0], 200)
        self.assertEqual(f.request(f"/api/admin/rooms/{room}", token=f.admin, method="DELETE")[0], 200)
        self.assertEqual(self.activity("collection_peer"), latest)
        with closing(sqlite3.connect(f.site / "watch_together.db")) as conn:
            self.assertEqual(conn.execute("SELECT last_room_joined_at FROM users WHERE username='collection_peer'").fetchone()[0], latest)


if __name__ == "__main__":
    unittest.main()
