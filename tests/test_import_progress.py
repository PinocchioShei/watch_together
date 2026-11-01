"""Real ffmpeg progress and authenticated polling against a disposable site."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import time
import unittest

import test_collections as integration


class ImportProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = integration.CollectionIntegrationTests
        fixture.setUpClass()
        cls.addClassCleanup(fixture.doClassCleanups)
        cls.f = fixture()
        source = cls.f.site / "progress.mov"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=960x540:r=30:d=35",
                        "-c:v", "mpeg4", "-q:v", "5", str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.video = source.read_bytes()

    def job(self, admin=False):
        base = "/api/admin/import" if admin else "/api/import"
        token = self.f.admin if admin else self.f.owner
        status, job = self.f.request(base + "/jobs", token=token, method="POST")
        self.assertEqual(status, 200, job)
        return base, token, job["jobId"]

    def test_real_ffmpeg_progress_and_batch_duration(self):
        for admin in (False, True):
            with self.subTest(admin=admin):
                base, token, job = self.job(admin)
                samples = []
                with ThreadPoolExecutor(max_workers=1) as pool:
                    upload = pool.submit(self.f.upload, [(f"Progress {admin}.mov", self.video)], endpoint=base,
                                         media_type="shot", job_id=job)
                    deadline = time.monotonic() + 40
                    while not upload.done() and time.monotonic() < deadline:
                        status, progress = self.f.request(base + "/jobs/" + job, token=token)
                        self.assertEqual(status, 200)
                        samples.append(progress)
                        time.sleep(0.08)
                    status, result = upload.result(timeout=5)
                self.assertEqual(status, 200, result)
                conversion = [p for p in samples if p["phase"] == "converting"]
                self.assertTrue(conversion, samples)
                self.assertTrue(any(0 < p["processedSeconds"] < 35 and p["percent"] < 99 for p in conversion), conversion)
                self.assertTrue(any(p["speed"] > 0 and p["etaSeconds"] is not None for p in conversion), conversion)
                complete = self.f.request(base + "/jobs/" + job, token=token)[1]
                self.assertEqual((complete["status"], complete["percent"]), ("complete", 100))
                self.assertEqual(complete["fileCount"], 1)
                self.assertIn("/work/shot/", result["videoUrl"])

    def test_job_auth_owner_failure_and_reuse(self):
        base, token, job = self.job()
        self.assertEqual(self.f.request(base + "/jobs", method="POST")[0], 401)
        self.assertEqual(self.f.request(base + "/jobs/" + job)[0], 401)
        self.assertEqual(self.f.request(base + "/jobs/" + job, token=self.f.peer)[0], 404)
        self.assertEqual(self.f.request("/api/admin/import/jobs/" + job, token=self.f.admin)[0], 404)
        self.assertEqual(self.f.request(base + "/jobs/not-real", token=token)[0], 404)
        status, _ = self.f.upload([("Broken.mp4", b"invalid")], job_id=job)
        self.assertEqual(status, 400)
        failed = self.f.request(base + "/jobs/" + job, token=token)[1]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["error"])
        self.assertEqual(self.f.upload([("Reuse.wav", self.f.audio)], job_id=job)[0], 409)
        self.f.assert_clean_staging()

    def test_batch_progress_counts_audio_and_finishes_after_save(self):
        for admin in (False, True):
            base, token, job = self.job(admin)
            status, result = self.f.upload([("One.wav", self.f.audio), ("Two.wav", self.f.audio)],
                endpoint=base, media_type="music", work_name=f"Progress batch {admin}", job_id=job)
            self.assertEqual(status, 200, result)
            complete = self.f.request(base + "/jobs/" + job, token=token)[1]
            self.assertEqual(complete["totalSeconds"], 2)
            self.assertEqual(complete["fileIndex"], 2)
            self.assertEqual(complete["fileCount"], 2)
            self.assertEqual(complete["status"], "complete")
            self.assertTrue(self.f.local_path(result["audioUrl"]).is_file())

    def test_progress_is_shared_with_another_process(self):
        base, token, job = self.job()
        owner_id = self.f.request("/api/me", token=token)[1]["id"]
        script = "from wt_server.import_progress import read_job; import json; print(json.dumps(read_job(" + repr(f"user:{owner_id}") + "," + repr(job) + ")))"
        before = subprocess.check_output([sys.executable, "-c", script], cwd=self.f.site)
        self.assertEqual(json.loads(before)["status"], "waiting")
        self.assertEqual(self.f.upload([("Shared progress.wav", self.f.audio)], job_id=job)[0], 200)
        after = subprocess.check_output([sys.executable, "-c", script], cwd=self.f.site)
        self.assertEqual(json.loads(after)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
