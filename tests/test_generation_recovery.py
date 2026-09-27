import http.server
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import videoctl


class Server(http.server.BaseHTTPRequestHandler):
    posts = 0
    ready = False
    views = 0
    png = b"mock-image-bytes"

    def log_message(self, *_): pass

    def do_POST(self):
        type(self).posts += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"prompt_id":"recovery-1234"}')

    def do_GET(self):
        if self.path.startswith("/history/"):
            self.send_response(200); self.end_headers()
            body = ({"recovery-1234": {"status": {"status_str": "success"}, "outputs": {"1": {"images": [{"filename": "out.png"}]}}}} if type(self).ready else {})
            self.wfile.write(json.dumps(body).encode())
        else:
            type(self).views += 1
            self.send_response(200); self.end_headers(); self.wfile.write(type(self).png)


class GenerationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workflow = self.root / "workflow.json"
        self.workflow.write_text('{"1": {"class_type": "mock"}}')
        Server.posts, Server.ready, Server.views = 0, False, 0
        self.http = http.server.HTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.server = f"http://127.0.0.1:{self.http.server_port}"
        self.job = self.root / "job.json"

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.tmp.cleanup()

    def generate(self, **kwargs):
        return videoctl.run_generate(self.workflow, self.server, self.root / "out", poll_interval=.001, job_file=self.job, **kwargs)

    def test_timeout_resume_posts_once(self):
        with self.assertRaises(TimeoutError): self.generate(timeout=.005)
        self.assertEqual(json.loads(self.job.read_text())["status"], "timeout")
        Server.ready = True
        result = videoctl.resume_generate(self.job, self.root / "out", poll_interval=.001, timeout=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Server.posts, 1)

    def test_unknown_submission_never_reposts(self):
        with patch("videoctl.urllib.request.urlopen", side_effect=OSError("connection reset")):
            with self.assertRaisesRegex(RuntimeError, "outcome unknown"): self.generate()
            with self.assertRaisesRegex(RuntimeError, "refusing duplicate"): self.generate()
        self.assertEqual(json.loads(self.job.read_text())["status"], "submission_unknown")
        self.assertEqual(Server.posts, 0)

    def test_completed_reuse_and_output_hash_validation(self):
        Server.ready = True
        first = self.generate(timeout=1)
        self.assertEqual(self.generate(timeout=1), first)
        self.assertEqual(Server.posts, 1)
        Path(first["outputs"][0]["path"]).write_bytes(b"corrupt")
        again = videoctl.resume_generate(self.job, self.root / "out", poll_interval=.001, timeout=1)
        self.assertEqual(again["status"], "completed")
        self.assertEqual(Server.posts, 1)

    def test_workflow_mismatch_rejected(self):
        with self.assertRaises(TimeoutError): self.generate(timeout=.005)
        self.workflow.write_text('{"1": {"class_type": "other"}}')
        with self.assertRaisesRegex(RuntimeError, "does not match"): self.generate(timeout=1)
        self.assertEqual(Server.posts, 1)

    def test_parallel_generate_same_job_posts_once(self):
        Server.ready = True
        barrier = threading.Barrier(2)
        alias = self.root / "job-alias.json"
        alias.symlink_to(self.job)
        def invoke(job_file):
            barrier.wait()
            return videoctl.run_generate(self.workflow, self.server, self.root / "out", poll_interval=.001, timeout=1, job_file=job_file)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, (self.job, alias)))
        self.assertEqual([r["status"] for r in results], ["completed", "completed"])
        self.assertEqual(Server.posts, 1)

    def test_forged_output_outside_output_dir_rejected(self):
        outside = self.root / "private.txt"
        outside.write_text("user data")
        job = {
            "status": "completed", "workflow": str(self.workflow),
            "workflow_sha256": videoctl.sha256_file(self.workflow), "server": self.server,
            "client_id": "client", "output_dir": str((self.root / "out").resolve()),
            "prompt_id": "recovery-1234", "outputs": [{
                "path": str(outside), "filename": outside.name, "sha256": videoctl.sha256_file(outside),
                "size": outside.stat().st_size, "node_id": "1", "category": "images",
            }],
        }
        self.job.write_text(json.dumps(job))
        with self.assertRaisesRegex(ValueError, "outside output directory"):
            videoctl.resume_generate(self.job, self.root / "out")
        self.assertEqual(Server.posts, 0)
        self.job.write_text('{"status":"completed"}')
        with self.assertRaisesRegex(ValueError, "missing required job fields"):
            videoctl.resume_generate(self.job, self.root / "out")

    def test_manifest_failure_can_be_recovered(self):
        Server.ready = True
        manifest = self.root / "manifest.json"
        manifest.write_text("invalid json")
        with self.assertRaises(json.JSONDecodeError): self.generate(timeout=1, manifest_path=manifest)
        self.assertEqual(json.loads(self.job.read_text())["status"], "manifest_pending")
        manifest.write_text('{"generations": []}')
        result = videoctl.resume_generate(self.job, self.root / "out", manifest_path=manifest, poll_interval=.001, timeout=1)
        self.assertEqual(result["status"], "completed")
        entries = json.loads(manifest.read_text())["generations"]
        self.assertEqual([e["prompt_id"] for e in entries], ["recovery-1234"])
        self.assertEqual(entries[0]["status"], "completed")


if __name__ == "__main__": unittest.main()
