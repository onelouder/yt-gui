#!/usr/bin/env python3
"""Local web GUI for yt-dlp.

Stdlib only. Binds to 127.0.0.1 and spawns the system `yt-dlp` as argv lists
(never shell strings). See app/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

YTDLP = shutil.which("yt-dlp")
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

MAX_CONCURRENT = 3
PROBE_TIMEOUT = 90

# Markers we ask yt-dlp to emit so stdout is machine-parseable.
P = "@@P@@"
PP = "@@PP@@"
F = "@@F@@"

PROGRESS_TMPL = (
    "download:" + P + "%(progress.status)s|%(progress.downloaded_bytes)s|"
    "%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
    "%(progress.speed)s|%(progress.eta)s"
)
POSTPROC_TMPL = "postprocess:" + PP + "%(progress.status)s|%(progress.postprocessor)s"

MODES = ("merged", "separate", "audio", "video")
QUALITIES = {"best": None, "1080": 1080, "720": 720, "480": 480}

# Audio output: "native" keeps the source stream as-is; the rest are re-encoded by
# ffmpeg via yt-dlp's -x (FFmpegExtractAudio). mp3 uses LAME VBR V0 (~245 kbps).
AUDIO_FORMATS = {
    "native": None,
    "mp3": ["-x", "--audio-format", "mp3", "--audio-quality", "0"],
    "wav": ["-x", "--audio-format", "wav"],
}
AUDIO_FORMAT_LABELS = {"native": "Original", "mp3": "MP3", "wav": "WAV"}

MODE_LABELS = {
    "merged": "Merged file",
    "separate": "Separate video + audio",
    "audio": "Audio only",
    "video": "Video only",
}


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

class PathError(ValueError):
    pass


ALLOWED_ROOT = Path.home().resolve()
DEFAULT_OUTDIR = (Path.cwd() / "downloads").resolve()


def validate_outdir(raw: str) -> Path:
    """Resolve a user-supplied output directory, or raise PathError."""
    raw = (raw or "").strip()
    if not raw:
        raise PathError("Output path is empty.")
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        raise PathError(f"Output path must be absolute: {raw}")
    p = p.resolve()
    try:
        p.relative_to(ALLOWED_ROOT)
    except ValueError:
        raise PathError(
            f"Output path {p} is outside the allowed root {ALLOWED_ROOT}."
        ) from None
    if p.exists() and not p.is_dir():
        raise PathError(f"Output path exists but is not a directory: {p}")
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PathError(f"Could not create output path {p}: {exc}") from None
    if not os.access(p, os.W_OK | os.X_OK):
        raise PathError(f"Output path is not writable: {p}")
    return p


# --------------------------------------------------------------------------- #
# Job model
# --------------------------------------------------------------------------- #

def _num(tok: str):
    """yt-dlp renders unavailable template fields as 'NA'."""
    tok = tok.strip()
    if not tok or tok == "NA" or tok == "None":
        return None
    try:
        return float(tok)
    except ValueError:
        return None


class Task:
    """One yt-dlp invocation inside a job (one output file)."""

    def __init__(self, key: str, label: str, pp_state: str = "merging"):
        self.key = key
        self.label = label
        self.pp_state = pp_state  # state shown while yt-dlp postprocesses
        self.state = "queued"
        self.downloaded = 0
        self.total = None
        # yt-dlp reports progress per stream; merged mode downloads two in one
        # process, so fold finished streams into a base to keep the bar monotonic.
        self._base_dl = 0
        self._base_total = 0
        self._raw_dl = 0
        self._raw_total = 0
        self.speed = None
        self.eta = None
        self.filepath = None
        self.error = None

    @property
    def percent(self):
        if self.state == "done":
            return 100.0
        if not self.total:
            return None
        return min(100.0, round(self.downloaded / self.total * 100, 1))

    def to_dict(self):
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "percent": self.percent,
            "downloaded": self.downloaded,
            "total": self.total,
            "speed": self.speed,
            "eta": self.eta,
            "filepath": self.filepath,
            "error": self.error,
        }


class Job:
    def __init__(self, url, mode, quality, audio_format, outdir: Path):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.mode = mode
        self.quality = quality
        self.audio_format = audio_format
        self.outdir = str(outdir)
        self.title = None
        self.state = "queued"
        self.error = None
        self.created = time.time()
        self.tasks: list[Task] = []
        self.plan: list[tuple[Task, str, str, list[str]]] = []
        self.cancelled = False
        self._proc = None
        self._lock = threading.Lock()

    # -- process handling ---------------------------------------------------
    def attach(self, proc):
        with self._lock:
            self._proc = proc
            if self.cancelled:
                _killpg(proc)

    def detach(self):
        with self._lock:
            self._proc = None

    def cancel(self):
        with self._lock:
            self.cancelled = True
            if self._proc is not None:
                _killpg(self._proc)

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode, self.mode),
            "quality": self.quality,
            "audio_format": self.audio_format,
            "audio_format_label": AUDIO_FORMAT_LABELS.get(self.audio_format, self.audio_format),
            "outdir": self.outdir,
            "title": self.title,
            "state": self.state,
            "error": self.error,
            "created": self.created,
            "tasks": [t.to_dict() for t in self.tasks],
        }


def _killpg(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass


JOBS: dict[str, Job] = {}
JOBS_ORDER: list[str] = []
JOBS_LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
VERSION_LOCK = threading.Lock()
STATE_VERSION = [0]


def bump():
    with VERSION_LOCK:
        STATE_VERSION[0] += 1


def snapshot():
    with JOBS_LOCK:
        jobs = [JOBS[j].to_dict() for j in reversed(JOBS_ORDER)]
    return {"version": STATE_VERSION[0], "jobs": jobs}


# --------------------------------------------------------------------------- #
# yt-dlp invocation
# --------------------------------------------------------------------------- #

def fmt_for(mode: str, quality: str, part: str) -> str:
    h = QUALITIES.get(quality)
    vfilter = f"[height<={h}]" if h else ""
    if part == "audio":
        return "bestaudio"
    if part == "video":
        return f"bestvideo{vfilter}"
    # merged
    return f"bestvideo{vfilter}+bestaudio/best{vfilter}/best"


def base_argv() -> list[str]:
    return [
        YTDLP,
        "--no-playlist",
        "--no-color",
        "--newline",
        "--progress",
        "--progress-delta", "0.5",
        "--trim-filenames", "180",
        "--progress-template", PROGRESS_TMPL,
        "--progress-template", POSTPROC_TMPL,
        "-O", "after_move:" + F + "%(filepath)s",
    ]


def probe(job: Job) -> dict:
    argv = [YTDLP, "-J", "--no-playlist", "--no-warnings", "--", job.url]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timed out after {PROBE_TIMEOUT}s probing the URL.") from None
    if proc.returncode != 0:
        raise RuntimeError(clean_error(proc.stderr) or "yt-dlp could not resolve this URL.")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("yt-dlp returned no usable metadata for this URL.") from None


def check_formats(info: dict, job: Job) -> None:
    """Fail early when the requested streams plainly do not exist."""
    formats = info.get("formats") or []
    if not formats:
        return  # some extractors report a single direct URL; let the download try.
    h = QUALITIES.get(job.quality)

    # Only reject when a codec is *explicitly* absent. Plenty of extractors
    # leave vcodec/acodec/height unset, and guessing there means false failures.
    def maybe_video(f):
        if f.get("vcodec") == "none":
            return False
        height = f.get("height")
        return h is None or height is None or height <= h

    def maybe_audio(f):
        return f.get("acodec") != "none"

    if job.mode in ("merged", "video", "separate") and not any(map(maybe_video, formats)):
        want = "any height" if h is None else f"<= {h}p"
        raise RuntimeError(f"No video format available at {want} for this URL.")
    if job.mode in ("merged", "audio", "separate") and not any(map(maybe_audio, formats)):
        raise RuntimeError("No audio format available for this URL.")


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean_error(text: str) -> str:
    lines = [ANSI.sub("", ln).rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    keep = [ln for ln in lines if "ERROR" in ln or "error" in ln.lower()]
    out = keep or lines[-4:]
    return "\n".join(out[-6:]).strip()


HINTS = (
    ("sign in to confirm", "YouTube is bot-checking this request. Try again later, use a VPN, or pass cookies to yt-dlp."),
    ("confirm your age", "Age-restricted video — yt-dlp needs cookies from a signed-in browser session."),
    ("not available in your country", "Geo-blocked — a VPN in a permitted region is usually needed."),
    ("blocked it in your country", "Geo-blocked — a VPN in a permitted region is usually needed."),
    ("private video", "This video is private; cookies from an account with access are required."),
    ("ffprobe", "Audio conversion needs ffprobe (ships with ffmpeg) on PATH."),
    ("ffmpeg", "ffmpeg is required for this operation but was not usable."),
    ("unsupported url", "yt-dlp has no extractor for this URL."),
    ("requested format is not available",
     "This site may not publish separate video and audio streams — try Merged mode, "
     "or a less restrictive quality preset."),
)


def hint_for(msg: str):
    low = (msg or "").lower()
    for needle, hint in HINTS:
        if needle in low:
            return hint
    return None


def run_task(job: Job, task: Task, fmt: str, outtmpl: str, extra: list[str]) -> None:
    argv = base_argv() + extra + [
        "-f", fmt,
        "-P", job.outdir,
        "-o", outtmpl,
        "--", job.url,
    ]
    task.state = "downloading"
    bump()

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    job.attach(proc)

    err_lines: list[str] = []

    def drain_err():
        for line in proc.stderr:
            err_lines.append(line)
            if len(err_lines) > 200:
                del err_lines[:100]

    t = threading.Thread(target=drain_err, daemon=True)
    t.start()

    try:
        for raw in proc.stdout:
            line = ANSI.sub("", raw).strip()
            if not line:
                continue
            if line.startswith(P):
                parse_progress(task, line[len(P):])
            elif line.startswith(PP):
                status = line[len(PP):].split("|")[0]
                if status in ("started", "processing"):
                    task.state = task.pp_state
                bump()
            elif line.startswith(F):
                task.filepath = line[len(F):].strip()
                bump()
    finally:
        proc.wait()
        t.join(timeout=2)
        job.detach()

    if job.cancelled:
        task.state = "cancelled"
        bump()
        raise Cancelled()

    if proc.returncode != 0:
        msg = clean_error("".join(err_lines)) or f"yt-dlp exited with code {proc.returncode}."
        task.state = "failed"
        task.error = msg
        bump()
        raise RuntimeError(msg)

    task.state = "done"
    if task.total and task.downloaded < task.total:
        task.downloaded = task.total
    task.speed = None
    task.eta = None
    bump()


def parse_progress(task: Task, payload: str) -> None:
    parts = payload.split("|")
    if len(parts) < 6:
        return
    status, dl, total, total_est, speed, eta = parts[:6]
    d = _num(dl)
    tot = _num(total) or _num(total_est)

    if d is not None:
        if d < task._raw_dl:
            # counter went backwards: a new stream started in the same process
            task._base_dl += task._raw_dl
            task._base_total += task._raw_total
            task._raw_total = 0
        task._raw_dl = int(d)
    if tot:
        task._raw_total = int(tot)

    task.downloaded = task._base_dl + task._raw_dl
    task.total = (task._base_total + task._raw_total) or None
    task.speed = _num(speed)
    e = _num(eta)
    task.eta = int(e) if e is not None else None
    if status == "finished":
        task.speed = None
        task.eta = None
    bump()


class Cancelled(Exception):
    pass


def audio_task(job: Job) -> tuple[Task, str, str, list[str]]:
    stem = "%(title)s [%(id)s]"
    convert = AUDIO_FORMATS[job.audio_format]
    if convert:
        t = Task("audio", f"Audio \u2192 {AUDIO_FORMAT_LABELS[job.audio_format]}", "converting")
        # -x picks the final extension itself, so %(ext)s ends up .mp3/.wav
        return (t, "bestaudio/best", f"{stem}.audio.%(ext)s", convert)
    return (Task("audio", "Audio"), "bestaudio", f"{stem}.audio.%(ext)s", [])


def build_plan(job: Job) -> list[tuple[Task, str, str, list[str]]]:
    """(task, format selector, output template, extra argv) per file to produce."""
    stem = "%(title)s [%(id)s]"
    if job.mode == "merged":
        t = Task("merged", "Video + audio")
        return [(t, fmt_for(job.mode, job.quality, "merged"), f"{stem}.%(ext)s", [])]
    if job.mode == "audio":
        return [audio_task(job)]
    if job.mode == "video":
        t = Task("video", "Video (no audio)")
        return [(t, fmt_for(job.mode, job.quality, "video"), f"{stem}.video.%(ext)s", [])]
    # separate: two independent runs, never combined, so yt-dlp cannot merge them
    tv = Task("video", "Video (no audio)")
    return [
        (tv, fmt_for(job.mode, job.quality, "video"), f"{stem}.video.%(ext)s", []),
        audio_task(job),
    ]


def run_job(job: Job) -> None:
    with SLOTS:
        if job.cancelled:
            job.state = "cancelled"
            bump()
            return
        try:
            job.state = "probing formats"
            bump()
            info = probe(job)
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                if not entries:
                    raise RuntimeError("That URL is a playlist with no entries; v1 handles single videos.")
                raise RuntimeError(
                    "That URL resolves to a playlist. v1 supports single videos only — "
                    "use the URL of one item."
                )
            job.title = info.get("title") or info.get("id")
            check_formats(info, job)

            if job.cancelled:
                raise Cancelled()

            job.state = "downloading"
            bump()
            for task, fmt, outtmpl, extra in job.plan:
                if job.cancelled:
                    raise Cancelled()
                run_task(job, task, fmt, outtmpl, extra)

            job.state = "done"
        except Cancelled:
            job.state = "cancelled"
            job.error = "Cancelled by user."
            for t in job.tasks:
                if t.state in ("queued", "downloading", "merging", "converting"):
                    t.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            job.state = "failed"
            msg = str(exc)
            hint = hint_for(msg)
            job.error = msg + (f"\n\nHint: {hint}" if hint else "")
            for t in job.tasks:
                if t.state in ("queued", "downloading", "merging", "converting"):
                    t.state = "failed" if t.error else "skipped"
        finally:
            bump()


def create_job(url: str, mode: str, quality: str, audio_format: str, outdir_raw: str) -> Job:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    if quality not in QUALITIES:
        raise ValueError(f"Unknown quality: {quality}")
    if mode == "merged" and not FFMPEG:
        raise ValueError("Merged mode needs ffmpeg, which was not found on PATH.")
    if audio_format not in AUDIO_FORMATS:
        raise ValueError(f"Unknown audio format: {audio_format}")
    if mode not in ("audio", "separate"):
        audio_format = "native"  # only audio outputs are converted
    if AUDIO_FORMATS[audio_format] and not (FFMPEG and FFPROBE):
        raise ValueError(
            f"Converting to {AUDIO_FORMAT_LABELS[audio_format]} needs ffmpeg and ffprobe on PATH."
        )

    outdir = validate_outdir(outdir_raw or str(DEFAULT_OUTDIR))
    job = Job(url, mode, quality, audio_format, outdir)
    job.plan = build_plan(job)
    job.tasks = [step[0] for step in job.plan]

    with JOBS_LOCK:
        JOBS[job.id] = job
        JOBS_ORDER.append(job.id)
    bump()
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ytdlp-gui"
    protocol_version = "HTTP/1.1"

    # -- helpers ------------------------------------------------------------
    def log_message(self, fmt, *args):
        if os.environ.get("YTDLP_GUI_VERBOSE"):
            super().log_message(fmt, *args)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def local_host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1", "[::1]", "::1", "")

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes -------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        if not self.local_host_ok():
            self.send_json({"error": "Forbidden host header."}, 403)
            return
        path = urlparse(self.path).path

        if path == "/api/config":
            self.send_json(config_payload())
        elif path == "/api/jobs":
            self.send_json(snapshot())
        elif path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                self.send_json({"error": "No such job."}, 404)
            else:
                self.send_json(job.to_dict())
        elif path == "/api/events":
            self.stream_events()
        elif path in ("/", "/index.html"):
            self.send_static(STATIC / "index.html")
        else:
            name = path.lstrip("/")
            candidate = (STATIC / name).resolve()
            if candidate.is_file() and str(candidate).startswith(str(STATIC)):
                self.send_static(candidate)
            else:
                self.send_json({"error": "Not found."}, 404)

    def do_POST(self):  # noqa: N802
        if not self.local_host_ok():
            self.send_json({"error": "Forbidden host header."}, 403)
            return
        path = urlparse(self.path).path

        if path == "/api/jobs":
            data = self.read_json()
            try:
                job = create_job(
                    data.get("url", ""),
                    data.get("mode", "merged"),
                    str(data.get("quality", "best")),
                    str(data.get("audio_format", "native")),
                    data.get("outdir", ""),
                )
            except (ValueError, PathError) as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            self.send_json(job.to_dict(), 201)
        elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job = JOBS.get(path.split("/")[3])
            if not job:
                self.send_json({"error": "No such job."}, 404)
                return
            job.cancel()
            bump()
            self.send_json(job.to_dict())
        elif path == "/api/validate-path":
            data = self.read_json()
            try:
                p = validate_outdir(data.get("outdir", ""))
            except PathError as exc:
                self.send_json({"ok": False, "error": str(exc)})
                return
            self.send_json({"ok": True, "path": str(p)})
        else:
            self.send_json({"error": "Not found."}, 404)

    # -- static & SSE -------------------------------------------------------
    def send_static(self, file: Path):
        try:
            body = file.read_bytes()
        except OSError:
            self.send_json({"error": "Not found."}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = -1
        try:
            while True:
                snap = snapshot()
                if snap["version"] != last:
                    last = snap["version"]
                    payload = json.dumps(snap)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def config_payload():
    return {
        "ffmpeg": bool(FFMPEG),
        "ffmpeg_path": FFMPEG,
        "ffprobe": bool(FFPROBE),
        "ytdlp_path": YTDLP,
        "ytdlp_version": ytdlp_version(),
        "default_outdir": str(DEFAULT_OUTDIR),
        "allowed_root": str(ALLOWED_ROOT),
        "max_concurrent": MAX_CONCURRENT,
        "modes": [{"key": k, "label": v} for k, v in MODE_LABELS.items()],
        "qualities": list(QUALITIES),
        "audio_formats": [{"key": k, "label": v} for k, v in AUDIO_FORMAT_LABELS.items()],
    }


_VERSION_CACHE = []


def ytdlp_version():
    if _VERSION_CACHE:
        return _VERSION_CACHE[0]
    if not YTDLP:
        return None
    try:
        out = subprocess.run([YTDLP, "--version"], capture_output=True, text=True, timeout=15)
        _VERSION_CACHE.append(out.stdout.strip() or None)
    except (OSError, subprocess.SubprocessError):
        _VERSION_CACHE.append(None)
    return _VERSION_CACHE[0]


def main():
    global DEFAULT_OUTDIR, ALLOWED_ROOT, MAX_CONCURRENT, SLOTS

    ap = argparse.ArgumentParser(description="Local web GUI for yt-dlp")
    ap.add_argument("--port", type=int, default=8723)
    ap.add_argument("--host", default="127.0.0.1", help="keep this local (default 127.0.0.1)")
    ap.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="default download directory")
    ap.add_argument("--root", default=str(ALLOWED_ROOT), help="allowed root for output paths")
    ap.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT)
    args = ap.parse_args()

    if not YTDLP:
        sys.exit("yt-dlp was not found on PATH. Install it first.")

    ALLOWED_ROOT = Path(os.path.expanduser(args.root)).resolve()
    DEFAULT_OUTDIR = Path(os.path.expanduser(args.outdir)).resolve()
    MAX_CONCURRENT = max(1, args.max_concurrent)
    SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)

    try:
        DEFAULT_OUTDIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(f"Cannot create default output directory {DEFAULT_OUTDIR}: {exc}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    banner = [
        f"yt-dlp GUI  \u2192  http://localhost:{args.port}",
        f"  yt-dlp    : {YTDLP} ({ytdlp_version()})",
        f"  ffmpeg    : {FFMPEG or 'NOT FOUND \u2014 merged mode disabled'}",
        f"  downloads : {DEFAULT_OUTDIR}",
        f"  allowed   : {ALLOWED_ROOT}",
        "  note      : no auth \u2014 any user on this machine can reach this port.",
    ]
    print("\n".join(banner), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        for job in list(JOBS.values()):
            if job.state in ("queued", "probing formats", "downloading", "merging", "converting"):
                job.cancel()
        server.shutdown()


if __name__ == "__main__":
    main()
