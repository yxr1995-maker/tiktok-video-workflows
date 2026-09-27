#!/usr/bin/env python3
"""videoctl.py: Minimal CLI for TikTok Shop commerce and drama video workflows.

Ponytail rules:
- Generate is isolated; never automatically triggered or credit-spending from other stages.
- Commerce handles .venv build_video.py and system python3 OM/HF (HF runs OM first).
  Dry-run NEVER triggers real build or paid generation. Non-dry-run build/all requires approved review_status
  and verified claims/price sources. Non-dry-run HF passes --render.
  Stage all fails non-zero if output missing or QC fails, and writes delivery_manifest with config/output hash.
- Drama concatenates scenes (clips, audio, subtitles, BGM); LibTV material is input only.
  Requires explicit approved review_status; blocks pending/unreviewed (no fallback default).
  Rejects static images as clips. Rejects audio longer than clip.
  Supports fontfile verification, BGM loop, and input/output SHA256 manifests.
- Preflight & QC fail on missing inputs, undecodable media, no audio, consecutive/dual-subtitle conflicts,
  unverified claims/price (requires manual verification: source + verified_at).
- Brightness anomalies generate warnings only.
- Manifest generation rejects empty lists and missing files.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}
DEFAULT_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def job_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def validate_server(server: str) -> str:
    parsed = urllib.parse.urlsplit(server.rstrip("/"))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ComfyUI server must be an http(s) URL with a host and no credentials, query, or fragment")
    return server.rstrip("/")


def validate_job(job: Any, expected_output_dir: Path) -> Dict[str, Any]:
    required = {"status", "workflow", "workflow_sha256", "server", "client_id", "output_dir", "outputs"}
    if not isinstance(job, dict) or not required.issubset(job):
        raise ValueError("Invalid job file: missing required job fields")
    if job["status"] not in {"submitting", "submission_unknown", "submitted", "timeout", "failed", "manifest_pending", "completed"}:
        raise ValueError("Invalid job file: unsupported status")
    if not re.fullmatch(r"[0-9a-f]{64}", str(job["workflow_sha256"])):
        raise ValueError("Invalid job file: workflow_sha256 must be a SHA-256 digest")
    if validate_server(str(job["server"])) != job["server"]:
        raise ValueError("Invalid job file: server URL is not normalized")
    if Path(job["output_dir"]).resolve() != expected_output_dir.resolve():
        raise ValueError("Invalid job file: output directory does not match")
    if not isinstance(job["outputs"], list):
        raise ValueError("Invalid job file: outputs must be a list")
    if job["status"] in {"submitted", "timeout", "failed", "manifest_pending", "completed"} and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(job.get("prompt_id", ""))):
        raise ValueError("Invalid job file: missing or malformed prompt_id")
    if job["status"] == "completed":
        for output in job["outputs"]:
            if not isinstance(output, dict) or not {"path", "filename", "sha256", "size", "node_id", "category"}.issubset(output):
                raise ValueError("Invalid job file: malformed completed output entry")
            path = Path(output["path"]).resolve()
            if not path.is_relative_to(expected_output_dir.resolve()) or path.name != output["filename"]:
                raise ValueError("Invalid job file: output path is outside output directory")
    return job


def pick_ffmpeg() -> str:
    if os.environ.get("FFMPEG_PATH") and os.path.isfile(os.environ["FFMPEG_PATH"]):
        return os.environ["FFMPEG_PATH"]
    v_cand = "/Users/earan/Documents/tiktokshop/video/.venv/lib/python3.14/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1"
    if os.path.isfile(v_cand) and os.access(v_cand, os.X_OK):
        return v_cand
    return shutil.which("ffmpeg") or "ffmpeg"


def pick_ffprobe() -> str:
    return os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe") or "ffprobe"


def probe_media(path: Path | str) -> Dict[str, Any]:
    cmd = [pick_ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {p.stderr.strip()}")
    return json.loads(p.stdout)


def check_decodable(path: Path | str) -> Tuple[bool, str]:
    cmd = [pick_ffmpeg(), "-v", "error", "-i", str(path), "-f", "null", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return (p.returncode == 0, p.stderr.strip())


def check_brightness(path: Path | str, sample_count: int = 5) -> Dict[str, Any]:
    """Sample frames at distributed midpoints across the entire video duration.
    Returns individual sample timestamps and YAVG values (no false fade-in bias).
    Any sample < 100.0 or > 235.0 triggers a warning only.
    Fails if any requested sample cannot be extracted (never false-pass).
    """
    if sample_count < 1:
        return {"error": f"sample_count must be positive (got {sample_count})", "samples": [], "avg_luma": None, "warnings": []}
    try:
        probe = probe_media(path)
    except Exception as e:
        return {"error": f"Failed to probe media for brightness check: {e}", "samples": [], "avg_luma": None, "warnings": []}

    format_info = probe.get("format", {})
    try:
        dur = float(format_info.get("duration", 0.0))
    except (ValueError, TypeError):
        dur = 0.0

    if dur <= 0.05:
        v_stream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
        if v_stream and v_stream.get("duration"):
            try:
                dur = float(v_stream["duration"])
            except (ValueError, TypeError):
                dur = 0.0

    if dur <= 0.05:
        return {"error": f"Video duration is zero or invalid ({dur}s); cannot sample brightness midpoints", "samples": [], "avg_luma": None, "warnings": []}

    ffmpeg_bin = pick_ffmpeg()
    samples: List[Dict[str, float]] = []
    warnings: List[str] = []
    timestamps = [(i + 0.5) * (dur / sample_count) for i in range(sample_count)]

    for t in timestamps:
        cmd = [
            ffmpeg_bin, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
            "-vframes", "1",
            "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-f", "null", "-",
        ]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            return {"error": f"Brightness sample at {t:.2f}s failed: {e}", "samples": samples, "avg_luma": None, "warnings": warnings}
        if p.returncode != 0:
            return {"error": f"Brightness sample at {t:.2f}s failed (ffmpeg exit {p.returncode}): {p.stderr.strip()}", "samples": samples, "avg_luma": None, "warnings": warnings}
        try:
            vals = [float(l.split("=", 1)[1]) for l in p.stdout.splitlines() if "lavfi.signalstats.YAVG=" in l]
        except (ValueError, IndexError) as e:
            return {"error": f"Invalid brightness sample at {t:.2f}s: {e}", "samples": samples, "avg_luma": None, "warnings": warnings}
        if vals:
            yavg_val = round(vals[0], 2)
            t_val = round(t, 2)
            samples.append({"time": t_val, "yavg": yavg_val})
            if yavg_val < 100.0:
                warnings.append(f"Sample at {t_val:.2f}s luminance is low ({yavg_val:.1f} < 100.0); scene may appear underexposed")
            elif yavg_val > 235.0:
                warnings.append(f"Sample at {t_val:.2f}s luminance is high ({yavg_val:.1f} > 235.0); scene may appear overexposed")
        else:
            return {"error": f"No brightness value extracted at {t:.2f}s", "samples": samples, "avg_luma": None, "warnings": warnings}

    if not samples:
        return {"error": "No brightness samples could be extracted; ffmpeg decode failed or produced no frames", "samples": [], "avg_luma": None, "warnings": []}

    avg_luma = round(sum(s["yavg"] for s in samples) / len(samples), 2)
    warning_str = "; ".join(warnings) if warnings else None
    return {"avg_luma": avg_luma, "sample_count": len(samples), "samples": samples, "warnings": warnings, "warning": warning_str}


# --- Subcommands ---

def run_generate(
    workflow_path: Path | str, server: str = "http://127.0.0.1:8188", output_dir: Path | str = "./output/generated",
    manifest_path: Optional[Path | str] = None, client_id: Optional[str] = None, poll_interval: float = 1.0,
    timeout: float = 120.0, job_file: Optional[Path | str] = None,
) -> Dict[str, Any]:
    out_dir = Path(output_dir).resolve()
    job_path = (Path(job_file) if job_file else out_dir / "generation-job.json").resolve()
    with job_lock(job_path):
        return _run_generate_locked(workflow_path, server, out_dir, manifest_path, client_id, poll_interval, timeout, job_path)


def _run_generate_locked(
    workflow_path: Path | str,
    server: str = "http://127.0.0.1:8188",
    output_dir: Path | str = "./output/generated",
    manifest_path: Optional[Path | str] = None,
    client_id: Optional[str] = None,
    poll_interval: float = 1.0,
    timeout: float = 120.0,
    job_file: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Execute ComfyUI API workflow JSON via /prompt -> /history -> /view.
    Zero auto-spend: checks failures, empty outputs, path traversal, and collisions.
    Includes workflow_sha256.
    """
    wf_path, out_dir = Path(workflow_path), Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not wf_path.is_file():
        raise FileNotFoundError(f"Workflow file not found: {wf_path}")

    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    base = validate_server(server)
    job_path = Path(job_file)
    wf_hash = sha256_file(wf_path)
    if job_path.exists():
        try:
            job = validate_job(json.loads(job_path.read_text(encoding="utf-8")), out_dir)
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"Invalid job file {job_path}: {e}") from e
        if job.get("workflow_sha256") != wf_hash or job.get("server") != base:
            raise RuntimeError("Existing job workflow SHA-256 or server does not match this request")
        if job.get("status") == "completed" and _job_outputs_valid(job, out_dir):
            _upsert_manifest(job, manifest_path)
            return job
        if job.get("status") == "manifest_pending" and _job_outputs_valid(job, out_dir):
            _finish_manifest_pending(job, job_path, manifest_path)
            return json.loads(job_path.read_text(encoding="utf-8"))
        if not job.get("prompt_id"):
            raise RuntimeError(f"Existing job has no prompt_id (status={job.get('status')}); refusing duplicate submission")
        return _collect_generation(job, job_path, out_dir, manifest_path, poll_interval, timeout)

    cid = client_id or f"videoctl-{int(time.time())}"
    job = {"status": "submitting", "workflow": str(wf_path.resolve()), "workflow_sha256": wf_hash, "server": base, "client_id": cid, "output_dir": str(out_dir), "outputs": []}
    write_json_atomic(job_path, job)
    payload = {"prompt": wf.get("prompt", wf), "client_id": cid}
    req = urllib.request.Request(f"{base}/prompt", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        job["status"] = "submission_unknown"
        job["error"] = str(e)
        write_json_atomic(job_path, job)
        raise RuntimeError(f"ComfyUI submission outcome unknown; refusing automatic retry: {e}") from e

    prompt_id = resp.get("prompt_id")
    if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prompt_id):
        job["status"] = "submission_unknown"
        job["error"] = f"Server response missing prompt_id: {resp}"
        write_json_atomic(job_path, job)
        raise RuntimeError(job["error"])
    job["prompt_id"] = prompt_id
    job["status"] = "submitted"
    write_json_atomic(job_path, job)
    return _collect_generation(job, job_path, out_dir, manifest_path, poll_interval, timeout)


def _job_outputs_valid(job: Dict[str, Any], output_dir: Path) -> bool:
    outputs = job.get("outputs")
    try:
        return bool(outputs) and all(Path(o["path"]).resolve().is_relative_to(output_dir.resolve()) and Path(o["path"]).is_file() and Path(o["path"]).stat().st_size == o["size"] and sha256_file(o["path"]) == o["sha256"] for o in outputs)
    except (KeyError, OSError, TypeError):
        return False


def resume_generate(job_file: Path | str, output_dir: Path | str = "./output/generated", manifest_path: Optional[Path | str] = None, poll_interval: float = 1.0, timeout: float = 120.0) -> Dict[str, Any]:
    job_path, out_dir = Path(job_file).resolve(), Path(output_dir).resolve()
    with job_lock(job_path):
        return _resume_generate_locked(job_path, out_dir, manifest_path, poll_interval, timeout)


def _resume_generate_locked(job_path: Path, out_dir: Path, manifest_path: Optional[Path | str], poll_interval: float, timeout: float) -> Dict[str, Any]:
    try:
        job = validate_job(json.loads(job_path.read_text(encoding="utf-8")), out_dir)
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Invalid job file {job_path}: {e}") from e
    if job.get("status") == "completed" and _job_outputs_valid(job, out_dir):
        _upsert_manifest(job, manifest_path)
        return job
    if job.get("status") == "manifest_pending" and _job_outputs_valid(job, out_dir):
        _finish_manifest_pending(job, job_path, manifest_path)
        return json.loads(job_path.read_text(encoding="utf-8"))
    if not job.get("prompt_id"):
        raise RuntimeError(f"Job has no prompt_id (status={job.get('status')}); cannot resume")
    return _collect_generation(job, job_path, out_dir, manifest_path, poll_interval, timeout)


def _upsert_manifest(record: Dict[str, Any], manifest_path: Optional[Path | str]) -> None:
    if not manifest_path:
        return
    mp = Path(manifest_path)
    data = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {}
    generations = data.setdefault("generations", [])
    for i, item in enumerate(generations):
        if item.get("prompt_id") == record["prompt_id"]:
            generations[i] = record
            break
    else:
        generations.append(record)
    write_json_atomic(mp, data)


def _finish_manifest_pending(job: Dict[str, Any], job_path: Path, manifest_path: Optional[Path | str]) -> None:
    completed = job.copy()
    completed["status"] = "completed"
    _upsert_manifest(completed, manifest_path)
    write_json_atomic(job_path, completed)
    job.update(completed)


def _collect_generation(job: Dict[str, Any], job_path: Path, out_dir: Path, manifest_path: Optional[Path | str], poll_interval: float, timeout: float) -> Dict[str, Any]:
    base, prompt_id = job["server"], job["prompt_id"]
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    hist = None
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f"{base}/history/{prompt_id}") as r:
                data = json.loads(r.read().decode("utf-8"))
                if prompt_id in data:
                    hist = data[prompt_id]
                    break
        except Exception:
            pass
        time.sleep(poll_interval)

    if not hist:
        job["status"] = "timeout"
        write_json_atomic(job_path, job)
        raise TimeoutError(f"Timed out after {timeout}s waiting for {prompt_id} on {base}")

    hist_status = hist.get("status", {})
    if hist_status.get("status_str") in ("error", "failed"):
        job["status"] = "failed"
        job["error"] = str(hist_status.get("messages", "status error"))
        write_json_atomic(job_path, job)
        raise RuntimeError(f"ComfyUI prompt execution failed: {hist_status.get('messages', 'status error')}")

    outputs = hist.get("outputs", {})
    if not outputs:
        job["status"] = "failed"
        job["error"] = "empty outputs"
        write_json_atomic(job_path, job)
        raise RuntimeError(f"ComfyUI prompt {prompt_id} finished with empty outputs; cannot record completed")

    downloaded = []
    for nid, node_out in outputs.items():
        for cat, items in node_out.items():
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and "filename" in it:
                        raw_fn = it["filename"]
                        safe_fn = Path(raw_fn).name
                        sub, tp = it.get("subfolder", ""), it.get("type", "output")
                        url = f"{base}/view?{urllib.parse.urlencode({'filename': raw_fn, 'subfolder': sub, 'type': tp})}"
                        dest = out_dir / safe_fn

                        stem, ext = Path(safe_fn).stem, Path(safe_fn).suffix
                        dest = out_dir / safe_fn
                        collision = 0
                        while dest.exists():
                            collision += 1
                            safe_fn = f"{stem}_{prompt_id[:8]}_{collision}{ext}"
                            dest = out_dir / safe_fn
                        with urllib.request.urlopen(url) as vr:
                            fd, tmp_name = tempfile.mkstemp(dir=out_dir, prefix=".download-")
                            try:
                                with os.fdopen(fd, "wb") as f_out:
                                    shutil.copyfileobj(vr, f_out)
                                os.link(tmp_name, dest)
                            finally:
                                os.unlink(tmp_name)

                        downloaded.append({
                            "node_id": nid, "category": cat, "filename": safe_fn,
                            "path": str(dest.resolve()), "size": dest.stat().st_size, "sha256": sha256_file(dest),
                        })

    if not downloaded:
        job["status"] = "failed"
        job["error"] = "no downloadable assets"
        write_json_atomic(job_path, job)
        raise RuntimeError(f"ComfyUI outputs contained no downloadable assets for prompt_id {prompt_id}")

    record = job.copy()
    record.update({
        "status": "manifest_pending",
        "prompt_id": prompt_id,
        "outputs": downloaded,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    write_json_atomic(job_path, record)
    _finish_manifest_pending(record, job_path, manifest_path)
    record["status"] = "completed"

    return record


def run_commerce(
    video_root: Optional[Path | str],
    config: Path | str,
    stage: str = "build",
    dry_run: bool = False,
    bgm: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Execute commerce video pipeline stages (build, om, hf, all).
    Under dry_run, never executes real build or paid generation.
    Non-dry-run build/all requires review_status='approved' and verified claims/price sources.
    Non-dry-run HF passes --render to to_hyperframes.py.
    Stage 'all' orchestrates build -> OM -> HF -> QC/manifest, writing delivery_manifest.json.
    """
    if not video_root:
        raise ValueError("Missing required --video-root (pass --video-root <path> or set VIDEO_ROOT env var)")

    vr = Path(video_root).resolve()
    if not vr.is_dir():
        raise FileNotFoundError(f"Video root directory not found: {vr}")

    cfg = Path(config)
    resolved_cfg = cfg if cfg.is_absolute() else (vr / cfg if (vr / cfg).is_file() else cfg.resolve())
    if not resolved_cfg.is_file():
        raise FileNotFoundError(f"Config not found: {resolved_cfg}")
    rel_cfg = resolved_cfg.relative_to(vr) if resolved_cfg.is_relative_to(vr) else resolved_cfg

    with open(resolved_cfg, "r", encoding="utf-8") as f_cfg:
        prod_cfg = json.load(f_cfg)
    prod_id = prod_cfg.get("id", "")

    # Non-dry-run build or all requires review_status == approved and verified claims/price
    if not dry_run and stage in ("build", "all"):
        r_status = prod_cfg.get("review_status")
        if not r_status or str(r_status).strip().lower() != "approved":
            raise ValueError(f"Cannot execute commerce '{stage}': review_status must be 'approved', but got '{r_status}'")

        if prod_cfg.get("claims"):
            if not (prod_cfg.get("claims_verified") and prod_cfg.get("claims_source") and prod_cfg.get("claims_verified_at")):
                raise ValueError(f"Unverified claims in {resolved_cfg.name}: requires claims_verified=True, claims_source, and claims_verified_at")

        if prod_cfg.get("price"):
            if not (prod_cfg.get("price_verified") and prod_cfg.get("price_source") and prod_cfg.get("price_verified_at")):
                raise ValueError(f"Unverified price in {resolved_cfg.name}: requires price_verified=True, price_source, and price_verified_at")

    venv_py = vr / ".venv/bin/python"
    py_exec = str(venv_py) if venv_py.is_file() else sys.executable
    res: Dict[str, Any] = {"stages_run": []}

    def _call(cmd: List[str], s_name: str) -> None:
        p = subprocess.run(cmd, cwd=vr, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"Stage '{s_name}' failed ({p.returncode}): {p.stderr or p.stdout}")
        res["stages_run"].append({"stage": s_name, "cmd": cmd, "stdout": p.stdout.strip()})

    # Stage: build
    if stage in ("build", "all"):
        if dry_run:
            res["stages_run"].append({
                "stage": "build (dry-run)",
                "cmd": [py_exec, "build_video.py", str(rel_cfg)] + (["--bgm", str(bgm)] if bgm else []),
                "stdout": json.dumps({"status": "dry_run_pass", "config": str(rel_cfg), "action": "simulated_build_skipped"}),
            })
        else:
            b_cmd = [py_exec, "build_video.py", str(rel_cfg)] + (["--bgm", str(bgm)] if bgm else [])
            _call(b_cmd, "build")

    # Stage: om
    if stage in ("om", "all"):
        _call(["python3", "scripts/to_openmontage.py"] + (["--dry-run"] if dry_run else []) + [str(rel_cfg)], "to_openmontage")

    # Stage: hf (HF requires OM first; non-dry-run passes --render)
    if stage in ("hf", "all"):
        if stage != "all":
            _call(["python3", "scripts/to_openmontage.py"] + (["--dry-run"] if dry_run else []) + [str(rel_cfg)], "to_openmontage (prerequisite)")
        hf_args = ["--dry-run"] if dry_run else ["--render"]
        _call(["python3", "scripts/to_hyperframes.py"] + hf_args + [str(rel_cfg)], "to_hyperframes")

    # Stage: all includes post-render QC and delivery manifest verification
    if stage == "all":
        if dry_run:
            delivery_data = {
                "status": "dry_run_pass",
                "product_id": prod_id,
                "config": {
                    "path": str(resolved_cfg.resolve()),
                    "filename": resolved_cfg.name,
                    "sha256": sha256_file(resolved_cfg),
                },
                "checks": ["qc", "manifest"],
            }
            res["stages_run"].append({
                "stage": "qc_and_manifest (dry-run)",
                "stdout": json.dumps(delivery_data, ensure_ascii=False)
            })
            res["delivery_manifest"] = delivery_data
        else:
            out_candidates = [
                vr / "output" / f"{prod_id}_hyperframes.mp4",
                vr / "output" / f"{prod_id}.mp4",
            ]
            found_mp4 = next((p for p in out_candidates if p.is_file()), None)
            if not found_mp4:
                raise FileNotFoundError(f"Stage 'all' failed to produce finished output video in {vr / 'output'} for product {prod_id}")

            qc_res = run_qc(found_mp4, expected_width=1080, expected_height=1920)
            if qc_res["status"] == "fail":
                raise RuntimeError(f"Stage 'all' post-render QC failed for {found_mp4.name}:\n" + "\n".join(qc_res["errors"]))

            delivery_mf_path = vr / "output" / f"{prod_id}_delivery_manifest.json"
            delivery_data = {
                "status": "success",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "product_id": prod_id,
                "review_status": prod_cfg.get("review_status"),
                "config": {
                    "path": str(resolved_cfg.resolve()),
                    "filename": resolved_cfg.name,
                    "sha256": sha256_file(resolved_cfg),
                },
                "output": {
                    "path": str(found_mp4.resolve()),
                    "filename": found_mp4.name,
                    "size": found_mp4.stat().st_size,
                    "sha256": sha256_file(found_mp4),
                },
                "qc": qc_res,
            }
            with open(delivery_mf_path, "w", encoding="utf-8") as f_d:
                json.dump(delivery_data, f_d, indent=2, ensure_ascii=False)

            res["qc"] = qc_res
            res["delivery_manifest_path"] = str(delivery_mf_path.resolve())
            res["delivery_manifest"] = delivery_data

    res["status"] = "success"
    return res


def run_preflight(
    manifest: Optional[Dict[str, Any]] = None,
    manifest_path: Optional[Path | str] = None,
    manifest_base_dir: Optional[Path] = None,
    video_path: Optional[Path | str] = None,
    strict_review: bool = True,
    font_file: Optional[str] = None,
    bgm_loop: bool = False,
) -> Dict[str, Any]:
    errors, warnings = [], []
    if manifest_path and not manifest:
        mp = Path(manifest_path).resolve()
        manifest_base_dir = mp.parent
        if not mp.is_file():
            return {"status": "fail", "errors": [f"Manifest not found: {mp}"], "warnings": []}
        with open(mp, "r", encoding="utf-8") as f_in:
            manifest = json.load(f_in)

    if manifest:
        base = manifest_base_dir or Path.cwd()

        r_status = manifest.get("review_status")
        if not r_status:
            errors.append("Missing required 'review_status' in manifest")
        elif str(r_status).strip().lower() != "approved":
            errors.append(f"Drama manifest review_status must be 'approved', but found '{r_status}'")

        if strict_review:
            if manifest.get("price"):
                pv = manifest.get("price_verified")
                ps = manifest.get("price_source")
                pt = manifest.get("price_verified_at")
                if not (pv and ps and pt):
                    errors.append(f"Unverified price ('{manifest['price']}'): requires price_verified=True, price_source, and price_verified_at (manual verification required)")

            if manifest.get("claims"):
                cv = manifest.get("claims_verified")
                cs = manifest.get("claims_source")
                ct = manifest.get("claims_verified_at")
                if not (cv and cs and ct):
                    errors.append(f"Unverified claims ({manifest['claims']}): requires claims_verified=True, claims_source, and claims_verified_at (manual verification required)")

        chosen_font = font_file or manifest.get("font_file") or (DEFAULT_FONT if os.path.isfile(DEFAULT_FONT) else None)
        if chosen_font and not os.path.isfile(chosen_font):
            errors.append(f"Specified font_file does not exist: {chosen_font}")

        seen_sub, has_audio = [], False
        total_duration = 0.0

        for i, sc in enumerate(manifest.get("scenes", [])):
            clip_dur = 0.0
            if sc.get("clip"):
                raw_clip = sc["clip"]
                cp = base / raw_clip if not Path(raw_clip).is_absolute() else Path(raw_clip)
                if not cp.is_file():
                    errors.append(f"Missing input clip for scene {i}: {cp}")
                elif cp.suffix.lower() in IMAGE_EXTENSIONS:
                    errors.append(f"Scene {i} clip is a static image ({cp.name}); video clip required")
                else:
                    ok, err = check_decodable(cp)
                    if not ok:
                        errors.append(f"Undecodable clip for scene {i}: {cp} ({err})")
                    else:
                        probe = probe_media(cp)
                        has_v = any(s.get("codec_type") == "video" for s in probe.get("streams", []))
                        if not has_v:
                            errors.append(f"Scene {i} clip has no video stream (static images not permitted): {cp}")
                        clip_dur = float(probe.get("format", {}).get("duration", 0.0))
                        if clip_dur <= 0.05:
                            errors.append(f"Scene {i} clip has invalid/zero duration ({clip_dur}s): {cp}")

                        if any(s.get("codec_type") == "audio" for s in probe.get("streams", [])):
                            has_audio = True
                        br = check_brightness(cp, sample_count=3)
                        if br.get("error"):
                            errors.append(f"Scene {i} clip ({cp.name}) brightness check error: {br['error']}")
                        for w in br.get("warnings", []):
                            warnings.append(f"Scene {i} clip ({cp.name}): {w}")

            effective_sc_dur = float(sc.get("duration", clip_dur or 0.0))
            total_duration += effective_sc_dur

            if sc.get("audio"):
                ap = base / sc["audio"] if not Path(sc["audio"]).is_absolute() else Path(sc["audio"])
                if not ap.is_file():
                    errors.append(f"Missing input audio for scene {i}: {ap}")
                elif not check_decodable(ap)[0]:
                    errors.append(f"Undecodable audio for scene {i}: {ap}")
                else:
                    has_audio = True
                    a_probe = probe_media(ap)
                    a_dur = float(a_probe.get("format", {}).get("duration", 0.0))
                    if effective_sc_dur > 0 and a_dur > effective_sc_dur + 0.15:
                        errors.append(f"Scene {i} audio duration ({a_dur:.2f}s) exceeds clip duration ({effective_sc_dur:.2f}s); audio would be truncated")

            sub = (sc.get("subtitle") or "").strip()
            cap = (sc.get("caption") or "").strip()

            if sub and cap and sub != cap:
                errors.append(f"Scene {i} dual-subtitle conflict: 'subtitle' ('{sub}') and 'caption' ('{cap}') differ")

            active_sub = sub or cap
            if active_sub:
                if sc.get("subtitles_burned") or sc.get("has_hardsub") or sc.get("has_burned_subtitles"):
                    errors.append(f"Scene {i} dual-subtitle conflict: clip already has burned subtitles, cannot also burn '{active_sub}'")

                if seen_sub and seen_sub[-1] == active_sub:
                    errors.append(f"Duplicated consecutive subtitle in scene {i}: '{active_sub}'")
                seen_sub.append(active_sub)

                if strict_review and re.search(r"[\$฿¥€]\s*\d+|\d+\s*(?:baht|dollars|元)", active_sub, re.IGNORECASE) and not manifest.get("price_verified"):
                    errors.append(f"Scene {i} subtitle mentions price without verification: '{active_sub}'")

        if manifest.get("bgm"):
            bgm = base / manifest["bgm"] if not Path(manifest["bgm"]).is_absolute() else Path(manifest["bgm"])
            if not bgm.is_file():
                errors.append(f"Missing input BGM file: {bgm}")
            elif not check_decodable(bgm)[0]:
                errors.append(f"Undecodable BGM file: {bgm}")
            else:
                has_audio = True
                bgm_probe = probe_media(bgm)
                bgm_dur = float(bgm_probe.get("format", {}).get("duration", 0.0))
                is_loop = bgm_loop or manifest.get("bgm_loop", False)
                if total_duration > 0 and bgm_dur < total_duration - 0.5 and not is_loop:
                    warnings.append(f"BGM duration ({bgm_dur:.1f}s) is shorter than total video duration ({total_duration:.1f}s); enable --bgm-loop or bgm_loop in manifest")

        if not has_audio and manifest.get("scenes"):
            errors.append("No audio track detected in any scene or manifest BGM (videos must have audio)")

    if video_path:
        vp = Path(video_path).resolve()
        if not vp.is_file():
            errors.append(f"Video file not found: {vp}")
        else:
            ok, err = check_decodable(vp)
            if not ok:
                errors.append(f"Video file is undecodable: {vp} ({err})")
            else:
                probe = probe_media(vp)
                if not any(s.get("codec_type") == "audio" for s in probe.get("streams", [])):
                    errors.append(f"Video has no audio stream: {vp}")
                br = check_brightness(vp, sample_count=5)
                if br.get("error"):
                    errors.append(f"Video {vp.name} brightness check error: {br['error']}")
                elif not br.get("samples"):
                    errors.append(f"Video {vp.name} brightness check error: no luminance samples extracted")
                for w in br.get("warnings", []):
                    warnings.append(f"Video {vp.name}: {w}")

    return {"status": "fail" if errors else ("warning" if warnings else "pass"), "errors": errors, "warnings": warnings}


def run_qc(
    video_path: Path | str,
    expected_width: Optional[int] = None,
    expected_height: Optional[int] = None,
    expected_fps: Optional[int] = None,
) -> Dict[str, Any]:
    vp = Path(video_path).resolve()
    if not vp.is_file():
        raise FileNotFoundError(f"Video not found: {vp}")

    errors, warnings = [], []
    ok, err = check_decodable(vp)
    if not ok:
        errors.append(f"Video cannot be decoded: {err}")

    probe = probe_media(vp)
    streams = probe.get("streams", [])
    v_st = next((s for s in streams if s.get("codec_type") == "video"), None)
    a_st = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fps_val = None
    if not v_st:
        errors.append("Missing video stream")
    else:
        w, h = int(v_st.get("width", 0)), int(v_st.get("height", 0))
        if expected_width and w != expected_width:
            warnings.append(f"Width {w} does not match expected {expected_width}")
        if expected_height and h != expected_height:
            warnings.append(f"Height {h} does not match expected {expected_height}")

        r_fps_str = v_st.get("r_frame_rate", "") or v_st.get("avg_frame_rate", "")
        if "/" in r_fps_str:
            num, den = r_fps_str.split("/", 1)
            if float(den) > 0:
                fps_val = float(num) / float(den)
        elif r_fps_str:
            fps_val = float(r_fps_str)

        if expected_fps and fps_val is not None:
            if round(fps_val) != expected_fps:
                warnings.append(f"FPS {fps_val:.2f} does not match expected {expected_fps}")

    if not a_st:
        errors.append("Missing audio stream (video has no audio)")

    br = check_brightness(vp, sample_count=5)
    if br.get("error"):
        errors.append(f"Brightness QC error: {br['error']}")
    elif not br.get("samples"):
        errors.append("Brightness QC error: no luminance samples extracted")
    for w in br.get("warnings", []):
        warnings.append(w)

    return {
        "status": "fail" if errors else ("warning" if warnings else "pass"),
        "video": str(vp), "sha256": sha256_file(vp),
        "duration_seconds": float(probe.get("format", {}).get("duration", 0.0)),
        "width": v_st.get("width") if v_st else None,
        "height": v_st.get("height") if v_st else None,
        "fps": round(fps_val, 2) if fps_val else None,
        "has_audio": a_st is not None,
        "audio_codec": a_st.get("codec_name") if a_st else None,
        "brightness": {
            "avg_luma": br.get("avg_luma"),
            "samples": br.get("samples", []),
        },
        "brightness_avg_luma": br.get("avg_luma"),
        "errors": errors, "warnings": warnings,
    }


def run_drama(
    manifest_path: Path | str,
    output_path: Path | str,
    target_width: int = 1080,
    target_height: int = 1920,
    target_fps: int = 30,
    font_file: Optional[str] = None,
    bgm_loop: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    mp, out_p = Path(manifest_path).resolve(), Path(output_path).resolve()
    with open(mp, "r", encoding="utf-8") as f_in:
        manifest = json.load(f_in)

    r_status = manifest.get("review_status")
    if not r_status or str(r_status).strip().lower() != "approved":
        raise ValueError(f"Cannot render drama: review_status must be 'approved', but got '{r_status}'")

    chosen_font = font_file or manifest.get("font_file") or (DEFAULT_FONT if os.path.isfile(DEFAULT_FONT) else None)
    if chosen_font and not os.path.isfile(chosen_font):
        raise FileNotFoundError(f"Subtitle font file not found: {chosen_font}")

    is_bgm_loop = bgm_loop or manifest.get("bgm_loop", False)

    pf = run_preflight(manifest=manifest, manifest_base_dir=mp.parent, strict_review=True, font_file=chosen_font, bgm_loop=is_bgm_loop)
    if pf["status"] == "fail":
        raise ValueError("Drama preflight failed:\n" + "\n".join(pf["errors"]))

    for sc in manifest.get("scenes", []):
        cp = (mp.parent / sc["clip"]).resolve() if not Path(sc["clip"]).is_absolute() else Path(sc["clip"])
        if cp == out_p:
            raise ValueError(f"Output path cannot overwrite source clip: {cp}")

    if out_p.is_file() and not force:
        raise FileExistsError(f"Output file already exists (use --force): {out_p}")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = pick_ffmpeg()

    clip_hashes, audio_hashes = [], []

    with tempfile.TemporaryDirectory(prefix="videoctl_drama_") as tmpdir:
        t_dir = Path(tmpdir)
        concat_txt = t_dir / "concat.txt"
        segs = []

        for i, sc in enumerate(manifest.get("scenes", [])):
            cp = (mp.parent / sc["clip"]).resolve() if not Path(sc["clip"]).is_absolute() else Path(sc["clip"])
            clip_hashes.append({"scene": i, "path": str(cp), "sha256": sha256_file(cp)})

            ap = (mp.parent / sc["audio"]).resolve() if sc.get("audio") and not Path(sc["audio"]).is_absolute() else (Path(sc["audio"]) if sc.get("audio") else None)
            if ap:
                audio_hashes.append({"scene": i, "path": str(ap), "sha256": sha256_file(ap)})

            sub = sc.get("subtitle", "").strip() or sc.get("caption", "").strip()
            dur = sc.get("duration")

            s_out = t_dir / f"seg_{i:04d}.mp4"
            vf = [
                f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease",
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2", "setsar=1", f"fps={target_fps}",
            ]
            if sub:
                c_sub = sub.replace("'", "'\\''").replace(":", "\\:").replace("%", "\\%")
                font_param = f":fontfile='{chosen_font}'" if chosen_font else ""
                vf.append(f"drawtext=text='{c_sub}'{font_param}:fontsize=48:fontcolor=white:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-220")

            cmd = [ffmpeg, "-y", "-v", "error", "-i", str(cp)]
            if ap:
                cmd.extend(["-i", str(ap), "-vf", ",".join(vf), "-map", "0:v:0", "-map", "1:a:0"])
            else:
                has_a = any(s.get("codec_type") == "audio" for s in probe_media(cp).get("streams", []))
                if has_a:
                    cmd.extend(["-vf", ",".join(vf), "-map", "0:v:0", "-map", "0:a:0"])
                else:
                    cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-vf", ",".join(vf), "-map", "0:v:0", "-map", "1:a:0"])

            if dur:
                cmd.extend(["-t", str(dur)])
            cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", str(s_out)])
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            segs.append(s_out)

        with open(concat_txt, "w", encoding="utf-8") as cf:
            for s in segs:
                cf.write(f"file '{s.resolve()}'\n")

        comb = t_dir / "combined.mp4"
        subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(comb)], check=True)

        bgm_hash = None
        if manifest.get("bgm"):
            bgm = (mp.parent / manifest["bgm"]).resolve() if not Path(manifest["bgm"]).is_absolute() else Path(manifest["bgm"])
            bgm_hash = {"path": str(bgm), "sha256": sha256_file(bgm)}

            mix_cmd = [ffmpeg, "-y", "-v", "error", "-i", str(comb)]
            if is_bgm_loop:
                mix_cmd.extend(["-stream_loop", "-1"])
            mix_cmd.extend([
                "-i", str(bgm),
                "-filter_complex", "[0:a]volume=1.0[a1];[1:a]volume=0.25[a2];[a1][a2]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_p),
            ])
            subprocess.run(mix_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        else:
            shutil.copy2(comb, out_p)

    qc = run_qc(out_p, expected_width=target_width, expected_height=target_height, expected_fps=target_fps)
    return {
        "status": "success",
        "output": str(out_p),
        "sha256": qc["sha256"],
        "inputs": {
            "clips": clip_hashes,
            "audios": audio_hashes,
            "bgm": bgm_hash,
        },
        "source": manifest.get("source"),
        "review_status": manifest.get("review_status"),
        "font_file": chosen_font,
        "qc": qc,
    }


def run_manifest(
    files: List[Path | str],
    output_path: Optional[Path | str] = None,
    verify_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    if verify_path:
        vp = Path(verify_path).resolve()
        with open(vp, "r", encoding="utf-8") as f_in:
            existing = json.load(f_in)
        mismatches = []
        for it in existing.get("files", []):
            fp = Path(it["path"])
            if not fp.is_file():
                mismatches.append(f"Missing file: {fp}")
            elif sha256_file(fp) != it["sha256"]:
                mismatches.append(f"Hash mismatch for {fp}: expected {it['sha256']}, got {sha256_file(fp)}")
        return {"status": "fail" if mismatches else "pass", "verified_count": len(existing.get("files", [])), "mismatches": mismatches}

    if not files:
        raise ValueError("No files specified for manifest generation (must provide at least one file)")

    missing = [str(f) for f in files if not Path(f).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing files for manifest generation: {', '.join(missing)}")

    entries = [{"path": str(Path(f).resolve()), "filename": Path(f).name, "size": Path(f).stat().st_size, "sha256": sha256_file(f)} for f in files]
    res = {"status": "success", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": entries}
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f_out:
            json.dump(res, f_out, indent=2, ensure_ascii=False)
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description="videoctl: Unified CLI for TikTok Shop commerce and drama workflows")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--workflow", required=True)
    p_gen.add_argument("--server", default="http://127.0.0.1:8188")
    p_gen.add_argument("--output-dir", default="./output/generated")
    p_gen.add_argument("--manifest")
    p_gen.add_argument("--client-id")
    p_gen.add_argument("--poll-interval", type=float, default=1.0)
    p_gen.add_argument("--timeout", type=float, default=120.0)
    p_gen.add_argument("--job-file")

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--job-file", required=True)
    p_resume.add_argument("--output-dir", default="./output/generated")
    p_resume.add_argument("--manifest")
    p_resume.add_argument("--poll-interval", type=float, default=1.0)
    p_resume.add_argument("--timeout", type=float, default=120.0)

    p_com = sub.add_parser("commerce")
    p_com.add_argument("--video-root", default=os.environ.get("VIDEO_ROOT"), help="tiktokshop/video root directory (or set VIDEO_ROOT env var)")
    p_com.add_argument("--config", required=True)
    p_com.add_argument("--stage", choices=["build", "om", "hf", "all"], default="build")
    p_com.add_argument("--bgm")
    p_com.add_argument("--dry-run", action="store_true")

    p_dra = sub.add_parser("drama")
    p_dra.add_argument("--manifest", required=True)
    p_dra.add_argument("--output", required=True)
    p_dra.add_argument("--target-width", type=int, default=1080)
    p_dra.add_argument("--target-height", type=int, default=1920)
    p_dra.add_argument("--target-fps", type=int, default=30)
    p_dra.add_argument("--font-file", help="Path to TTF/TTC font file for subtitles")
    p_dra.add_argument("--bgm-loop", action="store_true", help="Loop BGM if shorter than video")
    p_dra.add_argument("--force", action="store_true")

    p_pre = sub.add_parser("preflight")
    p_pre.add_argument("--manifest")
    p_pre.add_argument("--video")
    p_pre.add_argument("--font-file")
    p_pre.add_argument("--bgm-loop", action="store_true")
    p_pre.add_argument("--no-strict-review", dest="strict_review", action="store_false", default=True)

    p_qc = sub.add_parser("qc")
    p_qc.add_argument("--video", required=True)
    p_qc.add_argument("--expected-width", type=int, default=1080)
    p_qc.add_argument("--expected-height", type=int, default=1920)
    p_qc.add_argument("--expected-fps", type=int, default=30)
    p_qc.add_argument("--output-json")

    p_man = sub.add_parser("manifest")
    p_man.add_argument("--files", nargs="*", default=[])
    p_man.add_argument("--output")
    p_man.add_argument("--verify")

    args = parser.parse_args()
    try:
        if args.subcommand == "generate":
            res = run_generate(args.workflow, args.server, args.output_dir, args.manifest, args.client_id, args.poll_interval, args.timeout, args.job_file)
        elif args.subcommand == "resume":
            res = resume_generate(args.job_file, args.output_dir, args.manifest, args.poll_interval, args.timeout)
        elif args.subcommand == "commerce":
            res = run_commerce(args.video_root, args.config, args.stage, args.dry_run, args.bgm)
        elif args.subcommand == "drama":
            res = run_drama(args.manifest, args.output, args.target_width, args.target_height, args.target_fps, args.font_file, args.bgm_loop, args.force)
        elif args.subcommand == "preflight":
            res = run_preflight(manifest_path=args.manifest, video_path=args.video, strict_review=args.strict_review, font_file=args.font_file, bgm_loop=args.bgm_loop)
        elif args.subcommand == "qc":
            res = run_qc(args.video, args.expected_width, args.expected_height, args.expected_fps)
            if args.output_json:
                with open(args.output_json, "w", encoding="utf-8") as f_out:
                    json.dump(res, f_out, indent=2, ensure_ascii=False)
        elif args.subcommand == "manifest":
            res = run_manifest(args.files, args.output, args.verify)

        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") != "fail" else 1
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
