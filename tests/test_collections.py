r"""Integration checks against a disposable site; never open the deployed database.

Run: .venv\Scripts\python -m unittest discover -s tests -v
Requires the runtime requirements and ffmpeg/ffprobe in PATH.
"""
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import unquote
import wave

from websockets.sync.client import connect
from concurrent.futures import ThreadPoolExecutor


class CollectionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("ffmpeg/ffprobe are required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="watch-together-collections-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.site = Path(cls.temporary.name)
        source = Path(__file__).resolve().parents[1]
        shutil.copy2(source / "app.py", cls.site / "app.py")
        for name in ("wt_server", "static", "resource"):
            if (source / name).exists():
                shutil.copytree(source / name, cls.site / name, ignore=shutil.ignore_patterns("__pycache__"))
        (cls.site / "media").mkdir()
        (cls.site / "log/debug").mkdir(parents=True)
        cls.password = secrets.token_urlsafe(18)
        cls.log = (cls.site / "test-server.log").open("w", encoding="utf-8")
        cls.addClassCleanup(cls.log.close)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(cls.port), "--workers", "1"],
            cwd=cls.site, stdout=cls.log, stderr=subprocess.STDOUT,
            env=dict(os.environ, WATCH_ADMIN_USER="collection_admin", WATCH_ADMIN_PASSWORD=cls.password, WATCH_SYNC_DEBUG="0"),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 25
        while True:
            try:
                if cls.server.poll() is not None:
                    raise RuntimeError((cls.site / "test-server.log").read_text(encoding="utf-8"))
                if cls.request("/")[0] == 200:
                    break
            except urllib.error.URLError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        for user in ("collection_owner", "collection_peer"):
            cls.request("/api/register", payload={"username": user, "password": cls.password})
        cls.owner = cls.request("/api/login", payload={"username": "collection_owner", "password": cls.password})[1]["token"]
        cls.peer = cls.request("/api/login", payload={"username": "collection_peer", "password": cls.password})[1]["token"]
        cls.admin = cls.request("/api/admin/login", payload={"username": "collection_admin", "password": cls.password})[1]["token"]
        wav = io.BytesIO()
        with wave.open(wav, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 8000)
        cls.audio = wav.getvalue()
        sample = cls.site / "fixture.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=15:d=4",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(sample),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.video = sample.read_bytes()

    @classmethod
    def stop_server(cls):
        cls.stop_process(cls.server)

    @staticmethod
    def stop_process(server):
        if os.name == "nt" and server.poll() is None:
            # A Windows venv launcher can own a Python child; stop the tree before the launcher.
            subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif server.poll() is None:
            server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    @classmethod
    def request(cls, path, payload=None, token=None, files=None, fields=None, method=None, origin=None):
        headers = {}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        if files is not None:
            boundary = "collections_" + secrets.token_hex(12)
            parts = []
            for name, value in (fields or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            for filename, content in files:
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
            body = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            response = urllib.request.urlopen(urllib.request.Request((origin or cls.origin) + path, data=body, headers=headers, method=method), timeout=45)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            return response.status, json.loads(content) if "json" in response.headers.get("Content-Type", "") else content

    def upload(self, files, *, endpoint="/api/import", **fields):
        token = self.admin if endpoint.startswith("/api/admin/") else self.owner
        return self.request(endpoint, token=token, files=files, fields={"media_type": "movie", **fields})

    def local_path(self, url):
        return self.site / unquote(url).lstrip("/")

    def assert_clean_staging(self):
        self.assertFalse(list((self.site / "media/tmp").glob("import_*")))

    def test_single_video_work_uses_stem_and_media_keeps_original_filename(self):
        filename = "单集 #1.mp4"
        status, result = self.upload([(filename, self.video)])
        self.assertEqual(status, 200, result)
        self.assertEqual(result["workKey"], "单集 #1")
        self.assertEqual(self.local_path(result["videoUrl"]).name, filename)
        self.assertEqual(result["audioTracks"], [])
        self.assertEqual(result["audioUrl"], "")
        self.assertEqual(list(self.local_path(result["videoUrl"]).parent.glob("*.m4a")), [])
        self.assert_clean_staging()

    def test_audio_keeps_original_display_name_after_conversion(self):
        status, result = self.upload([("Original song.wav", self.audio)], media_type="music")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["workKey"], "Original song")
        self.assertEqual(self.local_path(result["audioUrl"]).name, "Original song.m4a")
        items = self.request("/api/media", token=self.owner)[1]["items"]
        item = next(item for item in items if item["workKey"] == result["workKey"])
        self.assertEqual(item["originalNames"][result["audioUrl"]], "Original song.wav")

    def test_both_import_apis_create_and_append_video_collections(self):
        for index, endpoint in enumerate(("/api/import", "/api/admin/import")):
            with self.subTest(endpoint=endpoint):
                work = f"Video collection {index} #合集"
                status, result = self.upload([("Episode 10.mp4", self.video), ("Episode 2.mp4", self.video)], endpoint=endpoint, work_name=work)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["workKey"], work)
                self.assertEqual([self.local_path(url).name for url in result["videoTracks"]], ["Episode 2.mp4", "Episode 10.mp4"])
                status, appended = self.upload([("Episode 3.mp4", self.video)], endpoint=endpoint, import_mode="append", target_work_key=work)
                self.assertEqual(status, 200, appended)
                self.assertEqual(len(appended["videoTracks"]), 3)
                self.assertEqual(appended["mediaKind"], "video")
                self.assertEqual(appended["audioUrl"], "")

    def test_audio_collection_and_legacy_upload_alias(self):
        status, result = self.upload([("Track 2.wav", self.audio), ("Track 10.wav", self.audio)], endpoint="/api/upload-video", work_name="Audio collection", media_type="music")
        self.assertEqual(status, 200, result)
        status, result = self.upload([("Track 3.wav", self.audio)], endpoint="/api/admin/import", import_mode="append_audio", target_work_key="Audio collection")
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["audioTracks"]), 3)
        self.assertEqual(result["videoTracks"], [])

    def test_mixed_batch_and_cross_kind_append_are_rejected(self):
        for endpoint in ("/api/import", "/api/admin/import"):
            status, _ = self.upload([("v.mp4", self.video), ("a.wav", self.audio)], endpoint=endpoint, work_name="Should not exist")
            self.assertEqual(status, 409)
        self.assertFalse((self.site / "media/work/movie/Should not exist").exists())
        for filename, content, other_name, other in (("Kinds video.mp4", self.video, "No audio.wav", self.audio), ("Kinds audio.wav", self.audio, "No video.mp4", self.video)):
            result = self.upload([(filename, content)])[1]
            for endpoint in ("/api/import", "/api/admin/import"):
                status, _ = self.upload([(other_name, other)], endpoint=endpoint, import_mode="append", target_work_key=result["workKey"])
                self.assertEqual(status, 409)
        self.assert_clean_staging()

    def test_batch_requires_name_and_duplicate_names_never_get_renamed(self):
        files = [("Duplicate.mp4", self.video), ("Second.mp4", self.video)]
        self.assertEqual(self.upload(files)[0], 422)
        self.assertEqual(self.upload(files, work_name="Name collision")[0], 200)
        self.assertEqual(self.upload(files, work_name="Name collision")[0], 409)
        self.assertEqual(self.upload([files[0]], import_mode="append", target_work_key="Name collision")[0], 409)
        self.assertEqual(len(list((self.site / "media/work/movie/Name collision").glob("*.mp4"))), 2)
        self.assertEqual(self.upload([("duplicate.mp4", self.video), ("DUPLICATE.mp4", self.video)], work_name="Duplicate batch")[0], 409)
        self.assertFalse((self.site / "media/work/movie/Duplicate batch").exists())
        self.assert_clean_staging()

    def test_work_titles_strip_only_media_extensions_on_both_apis(self):
        for index, endpoint in enumerate(("/api/import", "/api/admin/import")):
            name = f"Version.1.{index}"
            status, result = self.upload([(name + ".wav", self.audio)], endpoint=endpoint)
            self.assertEqual(status, 200, result)
            self.assertEqual(result["workKey"], name)
            self.assertEqual(self.local_path(result["audioUrl"]).name, name + ".m4a")
            self.assertEqual(self.upload([(name + ".mp4", self.video)], endpoint=endpoint)[0], 409)
            status, result = self.upload([("A.mp4", self.video), ("B.mp4", self.video)],
                                         endpoint=endpoint, work_name=f"Season.{index}.MP4")
            self.assertEqual(status, 200, result)
            self.assertEqual(result["workKey"], f"Season.{index}")

    def test_invalid_media_cleans_temporary_files_without_partial_work(self):
        status, _ = self.upload([("valid.mp4", self.video), ("invalid.mp4", b"not a media file")], work_name="Invalid batch")
        self.assertEqual(status, 400)
        self.assertFalse((self.site / "media/work/movie/Invalid batch").exists())
        self.assert_clean_staging()

    def test_work_named_like_category_is_scanned_once_and_globally_unique(self):
        status, result = self.upload([("music.mp4", self.video)])
        self.assertEqual(status, 200, result)
        self.assertTrue(self.local_path(result["videoUrl"]).is_file())
        self.assertIn("/work/movie/music/", result["videoUrl"])
        library = self.request("/api/media", token=self.owner)[1]["items"]
        self.assertEqual(len([p for p in library if p["workKey"] == "music"]), 1)
        self.assertEqual(self.upload([("music.mp4", self.video)], media_type="ASMR")[0], 409)

    def test_legacy_mixed_work_is_preserved_and_not_appendable(self):
        work = self.site / "media/work/Legacy mixed"
        work.mkdir()
        (work / "video.mp4").write_bytes(self.video)
        (work / "audio.m4a").write_bytes(self.audio)
        (work / ".meta.json").write_text('{"title":"Legacy mixed","mediaType":"movie"}', encoding="utf-8")
        for path, token in (("/api/media/append-targets", self.owner), ("/api/admin/media/append-targets", self.admin)):
            targets = self.request(path, token=token)[1]["items"]
            self.assertNotIn("Legacy mixed", [item["workKey"] for item in targets])
        self.assertEqual(self.upload([("new.mp4", self.video)], import_mode="append", target_work_key="Legacy mixed")[0], 409)
        self.assertTrue((work / "audio.m4a").is_file())

    def test_limits_and_auth_are_the_same_on_both_endpoints(self):
        for endpoint in ("/api/import", "/api/admin/import"):
            self.assertEqual(self.request(endpoint, files=[("x.wav", self.audio)])[0], 401)
            self.assertEqual(self.upload([(f"{i}.wav", self.audio) for i in range(31)], endpoint=endpoint, work_name="Too many")[0], 422)

    def test_category_updates_preserve_original_names_and_media_kind(self):
        from urllib.parse import quote
        status, result = self.upload([("原曲 1.wav", self.audio), ("原曲 2.wav", self.audio)], work_name="分类 #合集", media_type="music")
        self.assertEqual(status, 200, result)
        status, _ = self.request("/api/admin/media/" + quote(result["workKey"], safe="") + "/type",
                                 payload={"mediaType": "ASMR"}, token=self.admin, method="PATCH")
        self.assertEqual(status, 200)
        item = next(item for item in self.request("/api/media", token=self.owner)[1]["items"] if item["workKey"] == result["workKey"])
        self.assertEqual(item["type"], "ASMR")
        self.assertEqual(item["mediaKind"], "audio")
        self.assertEqual(item["originalNames"][item["audioUrl"]], "原曲 1.wav")
        self.assertIn("/media/work/ASMR/", item["audioUrl"])
        self.assertFalse(self.local_path(result["audioUrl"]).exists())
        self.assertEqual(self.request(result["audioUrl"])[0], 200)  # Old category URL redirects.

    def test_restart_preserves_names_and_concurrent_append_never_overwrites(self):
        _, created = self.upload([("Concurrent video.mp4", self.video)])
        restart_names = ("Restart video.mp4", "Restart  video.mp4")
        for name in restart_names:
            status, result = self.upload([(name, self.video)])
            self.assertEqual(status, 200, result)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with (self.site / "second-server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=self.site, stdout=log, stderr=subprocess.STDOUT,
                env=dict(os.environ, WATCH_SYNC_DEBUG="0"),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if self.request("/", origin=origin)[0] == 200:
                            break
                    except urllib.error.URLError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.1)
                library = self.request("/api/media", token=self.owner, origin=origin)[1]["items"]
                for name in restart_names:
                    item = next(item for item in library if item["workKey"] == Path(name).stem)
                    self.assertIsNotNone(item["id"], item)
                    self.assertEqual(item["name"], Path(name).stem)
                    self.assertEqual(self.local_path(item["videoUrl"]).name, name)
                fields = {"import_mode": "append", "target_work_key": created["workKey"]}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self.request, "/api/import", token=self.owner,
                                           files=[("Same name.mp4", self.video)], fields=fields, origin=server_origin)
                               for server_origin in (self.origin, origin)]
                    results = [future.result() for future in futures]
                self.assertEqual(sorted(status for status, _ in results), [200, 409], results)
                self.assertEqual(len(list((self.site / "media/work/movie" / created["workKey"]).glob("*.mp4"))), 2)
            finally:
                self.stop_process(server)

    def test_category_move_updates_room_urls_and_supports_rename_append_delete(self):
        from urllib.parse import quote
        status, result = self.upload([("Part #1.mp4", self.video), ("Part %2.mp4", self.video)],
                                     work_name="Folder #retained", media_type="shot")
        self.assertEqual(status, 200, result)
        self.assertIn("/work/shot/", result["videoUrl"])
        old_directory = self.local_path(result["videoUrl"]).parent
        room = self.request("/api/rooms", payload={"name": "Move category", "password": self.password}, token=self.owner)[1]["id"]
        with connect(f"ws://127.0.0.1:{self.port}/ws/rooms/{room}?token={self.owner}") as ws:
            self.receive(ws, lambda data: data["type"] == "state")
            ws.send(json.dumps({"type": "sync", "videoUrl": result["videoTracks"][1], "currentTime": 1,
                                "isPlaying": False, "forceTakeover": True, "albumMode": "next"}))
            self.receive(ws, lambda data: data.get("by") == "collection_owner")
            key = quote(result["workKey"], safe="")
            status, _ = self.request(f"/api/admin/media/{key}/type", payload={"mediaType": "ASMR"}, token=self.admin, method="PATCH")
            self.assertEqual(status, 200)
            state = self.request(f"/api/rooms/{room}/state", token=self.owner)[1]
            self.assertIn("/work/ASMR/", state["videoUrl"])
            self.assertEqual(state["trackIndex"], 1)
            self.assertFalse(old_directory.exists())
            for old_url in result["videoTracks"]:
                self.assertEqual(self.request(old_url)[0], 200)
                old_flat = "/media/work/" + quote(result["workKey"], safe="") + "/" + old_url.rsplit("/", 1)[1]
                self.assertEqual(self.request(old_flat)[0], 200)
            status, renamed = self.request(f"/api/admin/media/{key}", payload={"newWorkName": "Renamed category work.mp4"},
                                           token=self.admin, method="PATCH")
            self.assertEqual(status, 200, renamed)
            self.assertEqual(renamed["work"], "Renamed category work")
            status, appended = self.upload([("Part 3.mp4", self.video)], endpoint="/api/admin/import",
                                            import_mode="append", target_work_key=renamed["work"])
            self.assertEqual(status, 200, appended)
            self.assertEqual(appended["type"], "ASMR")
            self.assertIn("/work/ASMR/", appended["videoUrl"])
            directory = self.local_path(appended["videoUrl"]).parent
            status, _ = self.request("/api/admin/media/" + quote(renamed["work"], safe=""), token=self.admin, method="DELETE")
            self.assertEqual(status, 200)
            self.assertFalse(directory.exists())
        self.assertEqual(self.request("/media/work/movie/%2e%2e%2f%2e%2e/watch_together.db")[0], 404)

    @staticmethod
    def receive(ws, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            message = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if predicate(message):
                return message
        raise AssertionError("Expected WebSocket message not received")

    def test_video_collection_sync_takeover_and_rest_restore(self):
        result = self.upload([("Sync 1.mp4", self.video), ("Sync 2.mp4", self.video)], work_name="Sync collection")[1]
        room = self.request("/api/rooms", payload={"name": "Collection sync", "password": self.password}, token=self.owner)[1]["id"]
        self.request(f"/api/rooms/{room}/join", payload={"password": self.password}, token=self.peer)
        url = f"ws://127.0.0.1:{self.port}/ws/rooms/{room}"
        with connect(url + "?token=" + self.owner) as owner, connect(url + "?token=" + self.peer) as peer:
            self.receive(owner, lambda data: data["type"] == "state")
            self.receive(peer, lambda data: data["type"] == "state")
            payload = {"type": "sync", "actionId": 1, "videoUrl": result["videoTracks"][1], "currentTime": 1.25,
                       "isPlaying": False, "albumMode": "next", "forceTakeover": True, "trackIndex": 999, "playMode": "audio"}
            owner.send(json.dumps(payload))
            message = self.receive(peer, lambda data: data.get("by") == "collection_owner")
            self.assertEqual(message["trackIndex"], 1)
            self.assertEqual(message["playMode"], "video")
            self.assertEqual(message["albumMode"], "next")
            peer.send(json.dumps({**payload, "forceTakeover": False, "currentTime": 3, "videoUrl": result["videoTracks"][0]}))
            denied = self.receive(peer, lambda data: data.get("by") == "controller-locked")
            self.assertEqual(denied["currentTime"], 1.25)
            self.assertEqual(denied["videoUrl"], result["videoTracks"][1])
            peer.send(json.dumps({**payload, "actionId": 2, "albumMode": "repeat"}))
            accepted = self.receive(owner, lambda data: data.get("by") == "collection_peer")
            self.assertEqual(accepted["albumMode"], "repeat")
            state = self.request(f"/api/rooms/{room}/state", token=self.owner)[1]
            self.assertEqual(state["trackIndex"], 1)
            self.assertEqual(state["albumMode"], "repeat")
            with connect(url + "?token=" + self.owner) as restored:
                state = self.receive(restored, lambda data: data["type"] == "state")
                self.assertEqual(state["albumMode"], "repeat")
                self.assertEqual(state["trackIndex"], 1)

    def test_long_encoded_category_urls_sync_without_truncation(self):
        for kind, endpoint, content, extension in (
            ("video", "/api/import", self.video, "mp4"),
            ("audio", "/api/admin/import", self.audio, "wav"),
        ):
            with self.subTest(kind=kind):
                title = "中文分类合集" * 6 + kind
                files = [("中文媒体名称" * 6 + f" 第{index}集 #100%.{extension}", content) for index in (1, 2)]
                status, result = self.upload(files, endpoint=endpoint, work_name=title, media_type="ASMR")
                self.assertEqual(status, 200, result)
                selected = result[kind + "Tracks"][1]
                self.assertGreater(len(selected), 500)
                self.assertEqual(self.request(selected)[0], 200)
                room = self.request("/api/rooms", payload={"name": "Long category URL", "password": self.password}, token=self.owner)[1]["id"]
                self.request(f"/api/rooms/{room}/join", payload={"password": self.password}, token=self.peer)
                ws_url = f"ws://127.0.0.1:{self.port}/ws/rooms/{room}"
                with connect(ws_url + "?token=" + self.owner) as owner, connect(ws_url + "?token=" + self.peer) as peer:
                    self.receive(owner, lambda data: data["type"] == "state")
                    self.receive(peer, lambda data: data["type"] == "state")
                    owner.send(json.dumps({"type": "sync", "videoUrl": selected, "currentTime": 0.25,
                                           "isPlaying": False, "forceTakeover": True, "albumMode": "next"}))
                    received = self.receive(peer, lambda data: data.get("by") == "collection_owner")
                    self.assertEqual(received["videoUrl"], selected)
                    self.assertEqual(received["playMode"], kind)
                    self.assertEqual(received["trackIndex"], 1)
                    state = self.request(f"/api/rooms/{room}/state", token=self.owner)[1]
                    self.assertEqual(state["videoUrl"], selected)
                    with connect(ws_url + "?token=" + self.owner) as restored:
                        state = self.receive(restored, lambda data: data["type"] == "state")
                        self.assertEqual(state["videoUrl"], selected)
                        self.assertEqual(state["trackIndex"], 1)


if __name__ == "__main__":
    unittest.main()
