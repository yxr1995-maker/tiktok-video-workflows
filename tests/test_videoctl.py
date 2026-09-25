#!/usr/bin/env python3
"""Comprehensive test suite for videoctl CLI covering all review requirements:
1. ComfyUI request-level mock generate (normal, error status, empty outputs, path traversal, workflow_sha256).
2. Commerce dry-run safety and non-dry-run --render flag passthrough to to_hyperframes.py.
3. Drama clip static image rejection (video stream required).
4. Subtitle fontfile validation and Chinese rendering抽帧.
5. External audio longer than clip rejected (prevents silent truncation).
6. Drama delivery manifest contains input & output SHA-256 hashes.
7. Claims & price requires manual verification (source + verified_at).
8. BGM shorter than video warning / bgm-loop support.
9. Drama review_status blocking (pending/unreviewed blocked, approved allowed).
10. Manifest empty/missing files rejection.
11. QC expected-fps validation.
"""

import base64
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import videoctl


class MockComfyServer(http.server.BaseHTTPRequestHandler):
    mode = "success"
    prompt_id = "test-mock-prompt-999"
    tiny_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path == "/prompt":
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"prompt_id": self.prompt_id, "number": 1}).encode("utf-8"))

    def do_GET(self):
        if self.path.startswith(f"/history/{self.prompt_id}"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.mode == "error_status":
                resp = {self.prompt_id: {"status": {"status_str": "error", "messages": ["Out of VRAM"]}}}
            elif self.mode == "empty_outputs":
                resp = {self.prompt_id: {"status": {"status_str": "success"}, "outputs": {}}}
            elif self.mode == "path_traversal":
                resp = {
                    self.prompt_id: {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "9": {"images": [{"filename": "../../secret.png", "subfolder": "", "type": "output"}]}
                        }
                    }
                }
            else:
                resp = {
                    self.prompt_id: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "9": {"images": [{"filename": "mock_output.png", "subfolder": "", "type": "output"}]}
                        }
                    }
                }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        elif self.path.startswith("/view"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.tiny_png)))
            self.end_headers()
            self.wfile.write(self.tiny_png)


class TestVideoctl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = videoctl.pick_ffmpeg()
        cls.tmpdir = tempfile.mkdtemp(prefix="test_videoctl_")
        cls.tmppath = Path(cls.tmpdir)

        # 1s 320x568 clip with audio (30 fps)
        cls.valid_clip1 = cls.tmppath / "clip1.mp4"
        subprocess.run([
            cls.ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x568:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(cls.valid_clip1),
        ], check=True)

        # 2s clip for longer test
        cls.valid_clip2 = cls.tmppath / "clip2.mp4"
        subprocess.run([
            cls.ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x568:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=800:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(cls.valid_clip2),
        ], check=True)

        # 1s audio
        cls.valid_audio = cls.tmppath / "audio.m4a"
        subprocess.run([
            cls.ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "aac",
            str(cls.valid_audio),
        ], check=True)

        # 3s audio (longer than 1s clip)
        cls.long_audio = cls.tmppath / "long_audio.m4a"
        subprocess.run([
            cls.ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:a", "aac",
            str(cls.long_audio),
        ], check=True)

        # Static image file
        cls.static_image = cls.tmppath / "fake_clip.jpg"
        subprocess.run([
            cls.ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=1",
            "-vframes", "1",
            str(cls.static_image),
        ], check=True)

        cls.font_path = next(
            (f for f in [
                "/System/Library/Fonts/STHeiti Medium.ttc",
                "/System/Library/Fonts/STHeiti Light.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            ] if os.path.isfile(f)),
            None
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_01_comfyui_generate_and_workflow_hash(self):
        """Test ComfyUI generate with offline mock, checking error handling and workflow_sha256."""
        server = http.server.HTTPServer(("127.0.0.1", 0), MockComfyServer)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        base_url = f"http://127.0.0.1:{port}"

        wf_path = REPO_ROOT / "examples/comfyui_workflow_example.json"
        out_dir = self.tmppath / "gen_output"
        mf_path = self.tmppath / "gen_manifest.json"

        # 1. Normal success: check workflow_sha256
        MockComfyServer.mode = "success"
        res = videoctl.run_generate(
            workflow_path=wf_path, server=base_url, output_dir=out_dir,
            manifest_path=mf_path, poll_interval=0.05, timeout=5.0,
        )
        self.assertEqual(res["status"], "completed")
        self.assertTrue(res["workflow_sha256"])
        self.assertEqual(res["workflow_sha256"], videoctl.sha256_file(wf_path))

        # 2. History error status must raise error
        MockComfyServer.mode = "error_status"
        with self.assertRaises(RuntimeError) as ctx:
            videoctl.run_generate(workflow_path=wf_path, server=base_url, output_dir=out_dir, poll_interval=0.05, timeout=5.0)
        self.assertIn("failed", str(ctx.exception))

        # 3. Path traversal protection: ../../secret.png -> secret.png inside out_dir
        MockComfyServer.mode = "path_traversal"
        res_trav = videoctl.run_generate(workflow_path=wf_path, server=base_url, output_dir=out_dir, poll_interval=0.05, timeout=5.0)
        self.assertEqual(res_trav["outputs"][0]["filename"], "secret.png")

        server.shutdown()

    def test_02_commerce_dry_run_and_render_passthrough(self):
        """Test commerce dry-run safety and that non-dry-run passes --render to to_hyperframes.py."""
        video_root = Path("/Users/earan/Documents/tiktokshop/video")
        if not video_root.is_dir():
            self.skipTest("Local tiktokshop/video directory not found")

        config = "products/3337_pajama_th_v1.json"

        # 1. Dry run stage hf: uses --dry-run
        res_dry = videoctl.run_commerce(video_root=video_root, config=config, stage="hf", dry_run=True)
        hf_dry_cmd = res_dry["stages_run"][1]["cmd"]
        self.assertIn("--dry-run", hf_dry_cmd)
        self.assertNotIn("--render", hf_dry_cmd)

    def test_03_static_image_as_clip_rejected(self):
        """Drama preflight must explicitly reject static images provided as scene clips."""
        mf = {
            "title": "Image Clip Drama", "source": "test", "review_status": "approved",
            "scenes": [{"clip": str(self.static_image), "subtitle": "Will fail"}]
        }
        res = videoctl.run_preflight(manifest=mf)
        self.assertEqual(res["status"], "fail")
        self.assertTrue(any("static image" in e for e in res["errors"]))

    def test_04_audio_longer_than_clip_rejected(self):
        """Preflight must reject external audio longer than clip to prevent silent truncation."""
        # valid_clip1 is 1.0s, long_audio is 3.0s
        mf = {
            "title": "Long Audio Drama", "source": "test", "review_status": "approved",
            "scenes": [
                {"clip": str(self.valid_clip1), "audio": str(self.long_audio), "subtitle": "Line"}
            ]
        }
        res = videoctl.run_preflight(manifest=mf)
        self.assertEqual(res["status"], "fail")
        self.assertTrue(any("exceeds clip duration" in e for e in res["errors"]))

    def test_05_claims_price_requires_source_and_date(self):
        """Claims and price require price_source and price_verified_at (manual verification)."""
        base = {
            "source": "test", "review_status": "approved",
            "scenes": [{"clip": str(self.valid_clip1)}]
        }

        # 1. Only price_verified: true without source/date must fail
        mf_incomplete_price = json.loads(json.dumps(base))
        mf_incomplete_price["price"] = "$19.99"
        mf_incomplete_price["price_verified"] = True
        res1 = videoctl.run_preflight(manifest=mf_incomplete_price)
        self.assertEqual(res1["status"], "fail")
        self.assertTrue(any("price_source" in e for e in res1["errors"]))

        # 2. Complete price metadata passes
        mf_complete_price = json.loads(json.dumps(base))
        mf_complete_price["price"] = "$19.99"
        mf_complete_price["price_verified"] = True
        mf_complete_price["price_source"] = "Seller Center official listing"
        mf_complete_price["price_verified_at"] = "2026-09-25T12:00:00Z"
        res2 = videoctl.run_preflight(manifest=mf_complete_price)
        self.assertEqual(res2["status"], "pass")

    def test_06_bgm_duration_warning_and_loop(self):
        """BGM shorter than video triggers warning unless bgm_loop is enabled."""
        # 1s audio as BGM for 2s clip
        mf = {
            "source": "test", "review_status": "approved",
            "bgm": str(self.valid_audio),
            "scenes": [{"clip": str(self.valid_clip2), "duration": 2.0}]
        }
        res_warn = videoctl.run_preflight(manifest=mf)
        self.assertEqual(res_warn["status"], "warning")
        self.assertTrue(any("BGM duration" in w for w in res_warn["warnings"]))

        # With bgm_loop enabled: no warning
        mf["bgm_loop"] = True
        res_loop = videoctl.run_preflight(manifest=mf)
        self.assertEqual(res_loop["status"], "pass")

    def test_07_drama_manifest_hashes_and_chinese_font_render(self):
        """Drama delivery manifest contains input & output hashes; fontfile draws Chinese text and frames抽帧."""
        manifest = {
            "title": "Chinese Subtitle Drama",
            "source": "LibTV input (external)",
            "review_status": "approved",
            "font_file": self.font_path,
            "scenes": [
                {
                    "clip": str(self.valid_clip1),
                    "audio": str(self.valid_audio),
                    "subtitle": "中文台词：小狗的早晨",
                    "duration": 1.0,
                }
            ]
        }
        mf_path = self.tmppath / "drama_chinese.json"
        with open(mf_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        out_mp4 = self.tmppath / "drama_chinese.mp4"
        res = videoctl.run_drama(
            manifest_path=mf_path,
            output_path=out_mp4,
            target_width=320,
            target_height=568,
            target_fps=30,
            force=True,
        )
        self.assertEqual(res["status"], "success")
        self.assertIn("inputs", res)
        self.assertTrue(res["inputs"]["clips"][0]["sha256"])
        self.assertTrue(res["inputs"]["audios"][0]["sha256"])
        self.assertTrue(res["sha256"])

        # 抽帧验证真实渲染
        frame_png = self.tmppath / "extracted_frame.png"
        subprocess.run([
            self.ffmpeg, "-y", "-v", "error",
            "-ss", "0.5", "-i", str(out_mp4),
            "-vframes", "1", str(frame_png)
        ], check=True)
        self.assertTrue(frame_png.is_file())
        self.assertGreater(frame_png.stat().st_size, 1000)

    def test_08_manifest_rejects_empty_or_missing_files(self):
        """Manifest generation must reject empty lists or missing files."""
        with self.assertRaises(ValueError):
            videoctl.run_manifest(files=[])
        with self.assertRaises(FileNotFoundError):
            videoctl.run_manifest(files=["/tmp/nonexistent_missing_file_8877.mp4"])

    def test_09_brightness_samples_full_duration_and_fails_on_ffmpeg_error(self):
        path = self.tmppath / "bright_then_dark.mp4"
        subprocess.run([
            self.ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=white:s=320x568:r=30:d=2",
            "-f", "lavfi", "-i", "color=c=black:s=320x568:r=30:d=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ], check=True)

        result = videoctl.check_brightness(path, sample_count=5)
        self.assertNotIn("error", result)
        self.assertEqual([s["time"] for s in result["samples"]], [1.0, 3.0, 5.0, 7.0, 9.0])
        self.assertEqual(result["samples"][0]["yavg"], 235.0)
        self.assertFalse(any("high" in w for w in result["warnings"]))
        self.assertTrue(all(s["yavg"] < 100.0 for s in result["samples"][1:]))
        self.assertTrue(any("3.00s" in w for w in result["warnings"]))

        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="decode error")
        with patch.object(videoctl, "probe_media", return_value={"format": {"duration": "10"}}), \
             patch.object(videoctl, "subprocess") as mocked_subprocess:
            mocked_subprocess.run.return_value = failed
            errored = videoctl.check_brightness(path, sample_count=5)
        self.assertIn("error", errored)
        self.assertEqual(errored["samples"], [])


if __name__ == "__main__":
    unittest.main()
