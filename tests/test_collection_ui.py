"""Optional browser regression: pip install -r requirements-dev.txt; playwright install chromium."""
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
import wave

import test_collections as integration

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "Install requirements-dev.txt for browser checks")
class CollectionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        options = {"headless": True, "args": ["--autoplay-policy=no-user-gesture-required"]}
        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        if os.name == "nt" and edge.exists():
            options["executable_path"] = str(edge)
        try:
            cls.browser = cls.playwright.chromium.launch(**options)
        except Exception as error:
            raise unittest.SkipTest(f"Install a Playwright Chromium browser: {error}")
        cls.addClassCleanup(cls.browser.close)
        cls.fixture = integration.CollectionIntegrationTests
        cls.fixture.setUpClass()
        cls.addClassCleanup(cls.fixture.doClassCleanups)

    def page(self, token, admin=False, room=None):
        context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(context.close)
        key = "wt_admin_token" if admin else "wt_token"
        script = f"sessionStorage.setItem({json.dumps(key)}, {json.dumps(token)});"
        if room:
            script += "sessionStorage.setItem('wt_active_room', " + json.dumps(json.dumps(room)) + ");"
        context.add_init_script(script)
        page = context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(self.fixture.origin + ("/admin" if admin else "/"))
        return page

    @staticmethod
    def media_file(name, content, mime):
        return {"name": name, "buffer": content, "mimeType": mime}

    def upload(self, page, admin=False):
        endpoint = "/api/admin/import" if admin else "/api/import"
        with page.expect_response(lambda response: response.url.endswith(endpoint) and response.request.method == "POST", timeout=45000) as uploaded:
            page.locator("#importForm button[type=submit]").click()
        self.assertEqual(uploaded.value.status, 200, uploaded.value.text())
        return uploaded.value.json()

    def test_upload_displays_real_server_progress_on_both_pages(self):
        self.errors = []
        f = self.fixture
        source = f.site / "browser-progress.mov"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=960x540:r=30:d=35",
                        "-c:v", "mpeg4", "-q:v", "5", str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for admin in (False, True):
            page = self.page(f.admin if admin else f.owner, admin=admin)
            if admin:
                expect(page.locator("#dashboard")).to_be_visible()
                page.locator("button[data-tab=media]").click()
            else:
                expect(page.locator("#appPanel")).to_be_visible()
            progress_id = "adminImportProgressText" if admin else "importProgressText"
            page.evaluate("id => { window.progressHistory=[]; const node=document.getElementById(id); new MutationObserver(() => window.progressHistory.push(node.textContent)).observe(node,{childList:true,subtree:true,characterData:true}); }", progress_id)
            page.locator("#importFile" if admin else "#videoFileInput").set_input_files(self.media_file(f"Browser progress {admin}.mov", source.read_bytes(), "video/quicktime"))
            page.locator("#importType" if admin else "#importTypeSelect").select_option("movie")
            result = self.upload(page, admin=admin)
            self.assertEqual(result["mediaKind"], "video")
            history = page.evaluate("window.progressHistory")
            self.assertTrue(any("Transcoding" in text and "remaining" in text and "x" in text for text in history), history)
            self.assertFalse(any("taking longer than estimate" in text for text in history), history)
        self.assertEqual(self.errors, [])

    def test_upload_and_two_user_video_audio_collection_playback(self):
        self.errors = []
        f = self.fixture
        video_work = "中文视频合集" * 9 + " #100%"
        audio_work = "中文音频合集" * 9 + " #100%"
        owner = self.page(f.owner)
        expect(owner.locator("#appPanel")).to_be_visible()
        owner.locator("#videoFileInput").set_input_files([
            self.media_file("Episode 10.mp4", f.video, "video/mp4"),
            self.media_file("Episode 2.mp4", f.video, "video/mp4"),
        ])
        owner.locator("#importTypeSelect").select_option("movie")
        expect(owner.locator("#userWorkName")).to_be_visible()
        owner.locator("#userWorkName").fill(video_work)
        self.assertEqual(self.upload(owner)["mediaKind"], "video")
        expect(owner.locator("#importForm button[type=submit]")).to_be_enabled()

        admin = self.page(f.admin, admin=True)
        expect(admin.locator("#dashboard")).to_be_visible()
        admin.locator("button[data-tab=media]").click()
        video_card = admin.locator(".admin-media-card").filter(has_text=video_work)
        expect(video_card.locator(".admin-media-name")).to_have_text(video_work)
        expect(video_card.locator(".state-badge")).to_have_text("video")
        video_card.click()
        expect(admin.locator(".media-detail-row").filter(has=admin.locator("b", has_text="Work"))).to_have_text("Work" + video_work)
        admin.locator("#mediaDetailClose").click()
        wav = io.BytesIO()
        with wave.open(wav, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 32000)
        admin.locator("#importFile").set_input_files([
            self.media_file("Track 1.wav", wav.getvalue(), "audio/wav"),
            self.media_file("Track 2.wav", wav.getvalue(), "audio/wav"),
        ])
        admin.locator("#importType").select_option("music")
        admin.locator("#adminWorkName").fill(audio_work)
        self.assertEqual(self.upload(admin, admin=True)["mediaKind"], "audio")
        admin.locator("#mediaTypeFilter").select_option("all")
        audio_card = admin.locator(".admin-media-card").filter(has_text=audio_work)
        expect(audio_card.locator(".admin-media-name")).to_have_text(audio_work)
        expect(audio_card.locator(".state-badge")).to_have_text("audio")
        expect(admin.locator(".state-badge.yes, .state-badge.no")).to_have_count(0)
        # Filtering uses the current library immediately, without another library request.
        media_requests = []
        admin.on("request", lambda request: media_requests.append(request.url) if request.url.endswith("/api/admin/media") else None)
        for category, work in (("music", audio_work), ("movie", video_work), ("all", audio_work)):
            admin.locator("#mediaTypeFilter").select_option(category)
            expect(admin.locator(".admin-media-card").filter(has_text=work)).to_be_visible()
        self.assertEqual(media_requests, [])

        # Both actual forms append to the same video work with the same request model.
        for page, is_admin, mode_id, target_id, file_id, filename in (
            (owner, False, "userImportMode", "userAppendTarget", "videoFileInput", "Episode 3.mp4"),
            (admin, True, "importMode", "appendTargetWork", "importFile", "Episode 4.mp4"),
        ):
            expect(page.locator("#importForm button[type=submit]")).to_be_enabled()
            page.locator("#" + mode_id).select_option("append")
            page.wait_for_function("([id, work]) => [...document.getElementById(id).options].some(o => o.value === work)", arg=[target_id, video_work])
            page.locator("#" + target_id).select_option(video_work)
            page.locator("#" + file_id).set_input_files(self.media_file(filename, f.video, "video/mp4"))
            self.assertTrue(self.upload(page, admin=is_admin)["appendMode"])

        room = f.request("/api/rooms", payload={"name": "Browser collection", "password": f.password}, token=f.owner)[1]
        f.request(f"/api/rooms/{room['id']}/join", payload={"password": f.password}, token=f.peer)
        saved = {"roomId": room["id"], "roomName": room["name"], "roomPassword": f.password, "displayNo": 0}
        owner.evaluate("saved => sessionStorage.setItem('wt_active_room', JSON.stringify(saved))", saved)
        owner.reload()
        peer = self.page(f.peer, room=saved)
        for page in (owner, peer):
            expect(page.locator("#roomPanel")).to_be_visible()
            expect(page.locator("#onlineCount")).to_contain_text("2", timeout=10000)
            self.assertEqual(page.locator("#modeSwitch").count(), 0)

        owner.locator(".media-card").filter(has_text=video_work).click()
        for page in (owner, peer):
            expect(page.locator("#videoPlayer")).to_be_visible()
            page.wait_for_function("document.getElementById('videoPlayer').readyState >= 2")
            expect(page.locator("#collectionTrackSelect option")).to_have_count(4)
        owner.evaluate("document.getElementById('videoPlayer').pause()")
        peer.wait_for_function("document.getElementById('videoPlayer').paused")
        owner.locator("#collectionTrackSelect").select_option("1")
        for page in (owner, peer):
            expect(page.locator("#collectionTrackSelect")).to_have_value("1")
            page.wait_for_function("document.getElementById('videoPlayer').src.includes('Episode%203.mp4')")
        owner.locator("#collectionPrevious").click()
        expect(peer.locator("#collectionTrackSelect")).to_have_value("0")
        owner.locator("#collectionNext").click()
        expect(peer.locator("#collectionTrackSelect")).to_have_value("1")

        # Refresh restores a long categorized URL and seeking remains synchronized.
        peer.reload()
        expect(peer.locator("#roomPanel")).to_be_visible()
        expect(peer.locator("#collectionTrackSelect")).to_have_value("1")
        peer.wait_for_function("document.getElementById('videoPlayer').readyState >= 2")
        # Let the join/restore media events settle before simulating a new native seek.
        owner.wait_for_timeout(400)
        owner.evaluate("document.getElementById('videoPlayer').currentTime = 1.4")
        peer.wait_for_function("Math.abs(document.getElementById('videoPlayer').currentTime - 1.4) < 0.35")

        owner.locator("#albumModeSelect").select_option("next")
        expect(peer.locator("#albumModeSelect")).to_have_value("next")
        owner.evaluate("async () => { const p=document.getElementById('videoPlayer'); p.currentTime=p.duration-0.25; await p.play(); }")
        expect(owner.locator("#collectionTrackSelect")).to_have_value("2", timeout=12000)
        expect(peer.locator("#collectionTrackSelect")).to_have_value("2", timeout=12000)

        owner.locator("#albumModeSelect").select_option("repeat")
        expect(peer.locator("#albumModeSelect")).to_have_value("repeat")
        owner.evaluate("async () => { const p=document.getElementById('videoPlayer'); p.currentTime=p.duration-0.25; await p.play(); }")
        owner.wait_for_function("document.getElementById('videoPlayer').currentTime < 1.5")
        expect(owner.locator("#collectionTrackSelect")).to_have_value("2")
        expect(peer.locator("#collectionTrackSelect")).to_have_value("2")

        owner.locator("#albumModeSelect").select_option("stop")
        expect(peer.locator("#albumModeSelect")).to_have_value("stop")
        owner.evaluate("async () => { const p=document.getElementById('videoPlayer'); p.currentTime=p.duration-0.25; await p.play(); }")
        owner.wait_for_function("document.getElementById('videoPlayer').ended")
        peer.wait_for_function("document.getElementById('videoPlayer').paused")
        expect(owner.locator("#collectionTrackSelect")).to_have_value("2")

        # A paused peer can take over immediately, including during the previous local action window.
        peer.locator("#collectionTrackSelect").select_option("0")
        expect(owner.locator("#collectionTrackSelect")).to_have_value("0")

        screenshot_dir = os.getenv("WATCH_TEST_SCREENSHOTS")
        if screenshot_dir:
            target = Path(screenshot_dir)
            target.mkdir(parents=True, exist_ok=True)
            owner.screenshot(path=str(target / "video-collection-desktop.png"), full_page=True)
            owner.set_viewport_size({"width": 390, "height": 844})
            owner.screenshot(path=str(target / "video-collection-mobile.png"), full_page=True)
            self.assertTrue(owner.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"),
                owner.evaluate("[...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 2).map(e => ({tag:e.tagName,id:e.id,cls:e.className,width:e.getBoundingClientRect().width})).slice(0,15)"))
            owner.set_viewport_size({"width": 1440, "height": 1000})

        owner.locator("#roomMediaTypeFilter").select_option("all")
        owner.locator(".media-card").filter(has_text=audio_work).click()
        for page in (owner, peer):
            expect(page.locator("#audioPlayer")).to_be_visible()
            expect(page.locator("#videoPlayer")).not_to_be_visible()
            expect(page.locator("#collectionTrackSelect option")).to_have_count(2)
            expect(page.locator("#collectionTrackSelect option").first).to_contain_text("Track 1.wav")
        owner.locator("#albumModeSelect").select_option("next")
        expect(peer.locator("#albumModeSelect")).to_have_value("next")
        owner.wait_for_function("document.getElementById('audioPlayer').readyState >= 2")
        owner.evaluate("async () => { const p=document.getElementById('audioPlayer'); p.currentTime=p.duration-0.25; await p.play(); }")
        expect(owner.locator("#collectionTrackSelect")).to_have_value("1", timeout=12000)
        expect(peer.locator("#collectionTrackSelect")).to_have_value("1", timeout=12000)
        peer.reload()
        expect(peer.locator("#audioPlayer")).to_be_visible()
        expect(peer.locator("#collectionTrackSelect")).to_have_value("1")
        peer.wait_for_function("document.getElementById('audioPlayer').readyState >= 2")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
