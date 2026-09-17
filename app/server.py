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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

YTDLP = shutil.which("yt-dlp")
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
ATOMICPARSLEY = shutil.which("AtomicParsley") or shutil.which("atomicparsley")

MAX_CONCURRENT = 3
PROBE_TIMEOUT = 90
MAX_PLAYLIST_ITEMS = 200
ITEMS_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")

# Markers we ask yt-dlp to emit so stdout is machine-parseable.
P = "@@P@@"
PP = "@@PP@@"
F = "@@F@@"
SRC = "@@S@@"
ORIG = "@@O@@"

PROGRESS_TMPL = (
    "download:" + P + "%(progress.status)s|%(progress.downloaded_bytes)s|"
    "%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
    "%(progress.speed)s|%(progress.eta)s"
)
POSTPROC_TMPL = "postprocess:" + PP + "%(progress.status)s|%(progress.postprocessor)s"

MODES = ("merged", "separate", "audio", "video")

# Task states while a yt-dlp process is alive, by postprocessor name.
PP_STATES = {
    "Merger": "merging",
    "ExtractAudio": "converting",
    "Metadata": "tagging",
    "EmbedThumbnail": "embedding art",
}
ACTIVE_TASK_STATES = ("queued", "downloading", *dict.fromkeys(PP_STATES.values()))
ACTIVE_JOB_STATES = ("queued", "probing formats", "downloading")
RETRYABLE = ("failed", "partial", "cancelled", "interrupted")
QUALITIES = {"best": None, "1080": 1080, "720": 720, "480": 480}

@dataclass(frozen=True)
class AudioFormat:
    key: str
    label: str
    codec: str | None        # yt-dlp --audio-format value; None = keep the source stream
    family: str | None = None  # source codec family yt-dlp stream-copies instead of encoding
    lossless: bool = False
    best: str | None = None  # --audio-quality for "best"; None = let the encoder decide
    best_label: str = "Best"
    art: str | None = None   # what yt-dlp needs to embed cover art: "ffmpeg", "mutagen", "mp4tagger", None

    @property
    def bitrate_ok(self) -> bool:
        return self.codec is not None and not self.lossless


# Audio output: "native" keeps the source stream as-is; the rest are re-encoded by
# ffmpeg via yt-dlp's -x (FFmpegExtractAudio).
AUDIO_FORMATS = {f.key: f for f in (
    AudioFormat("native", "Original", None),
    AudioFormat("mp3", "MP3", "mp3", "mp3", best="0", best_label="Best (VBR ~245 kbps)", art="ffmpeg"),
    # ffmpeg's native AAC VBR mode is weak, and yt-dlp maps VBR numbers to nothing
    # for libopus, so "best" means a fixed high bitrate for these two.
    # m4a art: yt-dlp tries mutagen, then AtomicParsley, then ffmpeg -- but ffmpeg 9 writes
    # .m4a with the ipod muxer, which rejects JPEG covers, so that last fallback fails.
    AudioFormat("m4a", "M4A", "m4a", "aac", best="256K", best_label="Best (256 kbps AAC)", art="mp4tagger"),
    AudioFormat("opus", "Opus", "opus", "opus", best="160K", best_label="Best (160 kbps)", art="mutagen"),
    AudioFormat("flac", "FLAC", "flac", "flac", lossless=True, art="mutagen"),
    AudioFormat("wav", "WAV", "wav", lossless=True),
)}


def codec_family(acodec: str | None, ext: str | None) -> str | None:
    """Normalize yt-dlp's acodec (or, when unset, the extension) to a codec family."""
    a = (acodec or "").lower()
    if a in ("", "none", "na"):
        return {"mp3": "mp3", "m4a": "aac", "aac": "aac", "opus": "opus", "flac": "flac"}.get(ext or "")
    if a.startswith("mp4a") or a == "aac":
        return "aac"
    return a.split(".")[0]

# Values over 10 make yt-dlp pass `-b:a <n>k` (constant bitrate).
AUDIO_QUALITIES = {"best": None, "320": "320K", "192": "192K", "128": "128K"}
AUDIO_QUALITY_LABELS = {"best": "Best", "320": "320 kbps", "192": "192 kbps", "128": "128 kbps"}

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

    def __init__(self, key: str, label: str):
        self.key = key
        self.label = label
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
        self.source_path = None  # downloaded file before postprocessing
        self.files: list[dict] = []
        self.error = None
        self.notes: list[str] = []
        self.missing = False  # set when a restored file is no longer on disk

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
            "files": self.files,
            "missing": self.missing,
            "error": self.error,
            "notes": self.notes,
        }


class Job:
    def __init__(self, url, mode, quality, audio_format, audio_quality, outdir: Path,
                 keep_original=False, embed=False, playlist=False, items="", skip_existing=False):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.mode = mode
        self.quality = quality
        self.audio_format = audio_format
        self.audio_quality = audio_quality
        self.keep_original = keep_original
        self.embed = embed
        self.playlist = playlist
        self.items = items
        self.skip_existing = skip_existing
        self.playlist_title = None
        self.playlist_count = None
        self.outdir = str(outdir)
        self.title = None
        self.state = "queued"
        self.error = None
        self.created = time.time()
        self.tasks: list[Task] = []
        self.plan: list[Step] = []
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
            "audio_format_label": AUDIO_FORMATS[self.audio_format].label,
            "audio_quality": self.audio_quality,
            "audio_label": audio_label(self.audio_format, self.audio_quality)
                           + (" + original" if self.keep_original else "")
                           + (" + tags" if self.embed else ""),
            "keep_original": self.keep_original,
            "embed": self.embed,
            "playlist": self.playlist,
            "items": self.items,
            "skip_existing": self.skip_existing,
            "playlist_title": self.playlist_title,
            "playlist_count": self.playlist_count,
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
MAX_HISTORY = 200
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
VERSION_LOCK = threading.Lock()
STATE_VERSION = [0]


def bump():
    with VERSION_LOCK:
        STATE_VERSION[0] += 1
    DIRTY.set()


def snapshot():
    with JOBS_LOCK:
        jobs = [JOBS[j].to_dict() for j in reversed(JOBS_ORDER)]
    return {"version": STATE_VERSION[0], "jobs": jobs}


# --------------------------------------------------------------------------- #
# Persistence: the job list survives a restart; running jobs do not
# --------------------------------------------------------------------------- #

SCHEMA = 1
STATE_FILE: Path | None = None
DIRTY = threading.Event()
SAVE_DELAY = 2.0


def default_state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "yt-gui" / "jobs.json"


def save_state() -> None:
    if not STATE_FILE:
        return
    with JOBS_LOCK:
        jobs = [JOBS[i].to_dict() for i in JOBS_ORDER[-MAX_HISTORY:]]
    payload = {"schema": SCHEMA, "saved": time.time(), "jobs": jobs}
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print(f"warning: could not save job history to {STATE_FILE}: {exc}", flush=True)


def state_writer() -> None:
    """Coalesce updates: save at most every SAVE_DELAY seconds."""
    while True:
        DIRTY.wait()
        time.sleep(SAVE_DELAY)
        DIRTY.clear()
        save_state()


def job_from_dict(d: dict) -> Job:
    """Rebuild a finished job record. Interrupted work is marked, never resumed."""
    job = Job(d["url"], d["mode"], d.get("quality", "best"), d.get("audio_format", "native"),
              d.get("audio_quality", "best"), Path(d["outdir"]), d.get("keep_original", False),
              d.get("embed", False), d.get("playlist", False), d.get("items", ""),
              d.get("skip_existing", False))
    job.id = d.get("id") or job.id
    job.title = d.get("title")
    job.created = d.get("created") or time.time()
    job.playlist_title = d.get("playlist_title")
    job.playlist_count = d.get("playlist_count")
    interrupted = d.get("state") in ACTIVE_JOB_STATES
    job.state = "interrupted" if interrupted else d.get("state", "done")
    job.error = d.get("error") or ("The server stopped while this job was running." if interrupted else None)

    for td in d.get("tasks") or []:
        t = Task(td.get("key", "task"), td.get("label", "Task"))
        t.state = "interrupted" if td.get("state") in ACTIVE_TASK_STATES else td.get("state", "done")
        t.downloaded = td.get("downloaded") or 0
        t.total = td.get("total")
        t.filepath = td.get("filepath")
        t.files = td.get("files") or []
        t.notes = list(td.get("notes") or [])
        t.error = td.get("error")
        for f in t.files:
            f["missing"] = not Path(f["path"]).exists()
        t.missing = bool(t.filepath) and not Path(t.filepath).exists()
        job.tasks.append(t)
    return job


def load_state() -> None:
    if not STATE_FILE or not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        jobs = data["jobs"] if data.get("schema") == SCHEMA else []
    except (OSError, ValueError, KeyError, TypeError) as exc:
        bad = STATE_FILE.with_suffix(f".bad-{int(time.time())}")
        try:
            os.replace(STATE_FILE, bad)
        except OSError:
            bad = None
        print(f"warning: unreadable job history ({exc}); starting empty"
              + (f", old file kept at {bad}" if bad else ""), flush=True)
        return

    restored = 0
    for d in jobs[-MAX_HISTORY:]:
        try:
            job = job_from_dict(d)
        except (KeyError, TypeError, ValueError):
            continue  # one broken record must not cost the whole history
        JOBS[job.id] = job
        JOBS_ORDER.append(job.id)
        restored += 1
    if restored:
        print(f"restored {restored} job(s) from {STATE_FILE}", flush=True)


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
        "--no-color",
        "--newline",
        "--progress",
        "--progress-delta", "0.5",
        "--trim-filenames", "180",
        "--progress-template", PROGRESS_TMPL,
        "--progress-template", POSTPROC_TMPL,
        "-O", "video:" + SRC + "%(acodec)s|%(ext)s",
        "-O", "post_process:" + ORIG + "%(filepath)s",
        "-O", "after_move:" + F + "%(filepath)s",
    ]


def probe(job: Job, flat: bool = False) -> dict:
    argv = [YTDLP, "-J", "--no-warnings"]
    argv += ["--flat-playlist", "--yes-playlist"] if flat else ["--no-playlist"]
    if flat and job.items:
        argv += ["-I", job.items]
    argv += ["--", job.url]
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
    ("unable to embed", "Cover art could not be embedded — untick \"Embed cover art & tags\" or install mutagen for yt-dlp."),
    ("ffprobe and ffmpeg not found", "Audio conversion needs ffmpeg and ffprobe on PATH."),
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


@dataclass
class Step:
    """One yt-dlp invocation: the task it reports into and how to run it."""
    task: Task
    fmt: str
    outtmpl: str
    extra: list[str] = field(default_factory=list)
    audio: AudioFormat | None = None  # set when this step converts audio
    audio_quality: str = "best"
    keep_original: bool = False
    index: int | None = None  # playlist position this step downloads
    archive: str | None = None


def task_argv(job: Job, step: Step) -> list[str]:
    where = ["--yes-playlist", "-I", str(step.index)] if step.index else ["--no-playlist"]
    if step.archive:
        where += ["--download-archive", step.archive]
    return base_argv() + where + step.extra + [
        "-f", step.fmt,
        "-P", job.outdir,
        "-o", step.outtmpl,
        "--", job.url,
    ]


def run_task(job: Job, step: Step) -> None:
    task = step.task
    argv = task_argv(job, step)
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
                name = line[len(PP):].split("|")[1] if "|" in line else ""
                if status in ("started", "processing") and name in PP_STATES:
                    task.state = PP_STATES[name]
                bump()
            elif line.startswith(SRC):
                acodec, _, ext = line[len(SRC):].partition("|")
                note = source_note(step, acodec, ext)
                if note:
                    task.notes.append(note)
                bump()
            elif line.startswith(ORIG):
                task.source_path = line[len(ORIG):].strip()
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

    if step.archive and not task.filepath:
        # yt-dlp exited 0 without producing a file: the archive already had this id
        task.state = "skipped"
        task.notes.append("Already downloaded earlier (recorded in the archive file).")
        bump()
        return

    task.state = "done"
    task.files = collect_files(step, task)
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


STEM = "%(title)s [%(id)s]"

# Some extractors (archive.org) leave codecs unset, so plain `bestaudio` matches
# nothing; fall back to a single-file format that looks like audio by extension.
AUDIO_SELECTOR = (
    "bestaudio/best[vcodec=?none][ext~='^(mp3|m4a|aac|ogg|oga|opus|flac|wav|weba)$']"
)


def audio_label(fmt_key: str, quality: str) -> str:
    fmt = AUDIO_FORMATS[fmt_key]
    if not fmt.bitrate_ok:
        return fmt.label
    return f"{fmt.label} {'best' if quality == 'best' else quality + 'k'}"


def extract_argv(fmt: AudioFormat, quality: str) -> list[str]:
    argv = ["-x", "--audio-format", fmt.codec]
    q = fmt.best if quality == "best" else AUDIO_QUALITIES[quality]
    if fmt.bitrate_ok and q:
        argv += ["--audio-quality", q]
    return argv


def collect_files(step: Step, task: Task) -> list[dict]:
    """The files a finished task left on disk, with their role."""
    files = []
    final = task.filepath
    if step.keep_original and task.source_path:
        src = Path(task.source_path)
        # re-encoding to the same extension makes yt-dlp rename the source <name>.orig.<ext>
        orig = src.with_name(f"{src.stem}.orig{src.suffix}")
        if orig.exists():
            files.append({"path": str(orig), "role": "original"})
        elif task.source_path != final and src.exists():
            files.append({"path": str(src), "role": "original"})
    if final:
        files.append({"path": final, "role": "converted" if step.audio else "download"})
    if step.keep_original and len(files) < 2:
        task.notes.append("The download was already the final file, so there is no separate original to keep.")
    return files


def source_note(step: Step, acodec: str, ext: str) -> str | None:
    """Explain when yt-dlp will stream-copy instead of encoding what was asked."""
    fmt = step.audio
    if not fmt or not fmt.family or codec_family(acodec, ext) != fmt.family:
        return None
    msg = f"Source is already {fmt.label}: copied without re-encoding"
    return msg + " (bitrate not applied)." if step.audio_quality != "best" else msg + "."


def art_ok(fmt: AudioFormat, mutagen: bool, atomicparsley: bool) -> bool:
    return (fmt.art == "ffmpeg"
            or (fmt.art == "mutagen" and mutagen)
            or (fmt.art == "mp4tagger" and (mutagen or atomicparsley)))


def embed_argv(fmt: AudioFormat, mutagen: bool, atomicparsley: bool = False,
               keep_original: bool = False, playlist: bool = False) -> tuple[list[str], str | None]:
    """Tags always; cover art only where yt-dlp can embed it without failing the job."""
    argv = ["--embed-metadata"]
    if playlist:  # a playlist is an album: number the tracks
        argv += ["--parse-metadata", "%(playlist_index)s:%(track_number)s",
                 "--parse-metadata", "%(playlist_title)s:%(album)s"]
    if art_ok(fmt, mutagen, atomicparsley):
        argv.append("--embed-thumbnail")
        # -k also keeps the pre-conversion thumbnail; without --convert-thumbnails yt-dlp
        # converts to PNG internally and cleans up (bigger cover, no stray .webp)
        return argv + ([] if keep_original else ["--convert-thumbnails", "jpg"]), None
    if fmt.art in ("mutagen", "mp4tagger"):
        extra = " or AtomicParsley" if fmt.art == "mp4tagger" else ""
        return argv, (f"Cover art for {fmt.label} needs the mutagen Python module in yt-dlp's "
                      f"install (e.g. `pacman -S python-mutagen`){extra}; embedded tags only.")
    if fmt.codec is None:
        return argv, "Cover art is only embedded when converting; embedded tags only."
    return argv, f"{fmt.label} files can't hold cover art; embedded tags only."


def audio_step(job: Job, stem: str = None, index: int = None, label: str = "") -> Step:
    stem = stem or STEM
    fmt = AUDIO_FORMATS[job.audio_format]
    embed, embed_note = (embed_argv(fmt, has_mutagen(), bool(ATOMICPARSLEY), job.keep_original,
                                    job.playlist and index is not None)
                         if job.embed else ([], None))
    if not fmt.codec:
        t = Task(task_key("audio", index), label or "Audio")
        step = Step(t, AUDIO_SELECTOR, f"{stem}.audio.%(ext)s", embed)
    else:
        t = Task(task_key("audio", index), label or f"Audio \u2192 {audio_label(fmt.key, job.audio_quality)}")
        # -x picks the final extension itself, so %(ext)s ends up .mp3/.wav.
        # Any file with an audio track will do; ffmpeg drops the video.
        extra = extract_argv(fmt, job.audio_quality) + (["-k"] if job.keep_original else []) + embed
        step = Step(t, f"{AUDIO_SELECTOR}/best", f"{stem}.audio.%(ext)s",
                    extra, fmt, job.audio_quality, job.keep_original)
    if embed_note:
        t.notes.append(embed_note)
    return with_playlist(job, step, index, f"audio-{job.audio_format}")


def video_step(job: Job, stem: str = None, index: int = None, label: str = "") -> Step:
    t = Task(task_key("video", index), label or "Video (no audio)")
    step = Step(t, fmt_for(job.mode, job.quality, "video"), f"{stem or STEM}.video.%(ext)s")
    return with_playlist(job, step, index, "video")


def task_key(kind: str, index: int | None) -> str:
    return kind if index is None else f"{index}:{kind}"


def with_playlist(job: Job, step: Step, index: int | None, archive_kind: str) -> Step:
    """Tag a step with its playlist position and, optionally, an archive file."""
    step.index = index
    if index is not None and job.skip_existing:
        # one archive per output kind, so Separate mode and a re-run in another
        # format are not skipped by an entry another kind recorded
        step.archive = str(Path(job.outdir) / f".yt-gui-archive-{archive_kind}.txt")
    return step


def build_plan(job: Job, stem: str = None, index: int = None, label: str = "") -> list[Step]:
    """One Step per file to produce."""
    stem = stem or STEM
    sep = " \u00b7 " if label else ""
    if job.mode == "merged":
        t = Task(task_key("merged", index), label or "Video + audio")
        return [with_playlist(job, Step(t, fmt_for(job.mode, job.quality, "merged"), f"{stem}.%(ext)s"),
                              index, "merged")]
    if job.mode == "audio":
        return [audio_step(job, stem, index, label)]
    if job.mode == "video":
        return [video_step(job, stem, index, label)]
    # separate: two independent runs, never combined, so yt-dlp cannot merge them
    return [video_step(job, stem, index, f"{label}{sep}Video" if label else ""),
            audio_step(job, stem, index, f"{label}{sep}Audio" if label else "")]


PLAYLIST_STEM = "%(playlist_title)s/%(playlist_index)03d - " + STEM


def playlist_plan(job: Job, entries: list[tuple[int, dict]]) -> list[Step]:
    """Plan every selected playlist entry as its own yt-dlp run."""
    steps = []
    for index, entry in entries:
        title = entry.get("title") or entry.get("id") or f"item {index}"
        steps += build_plan(job, PLAYLIST_STEM, index, f"{index:03d} \u00b7 {title}")
    return steps


def playlist_entries(job: Job, info: dict) -> list[tuple[int, dict]]:
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise RuntimeError("That playlist has no downloadable entries.")
    # flat entries carry no playlist_index, but the playlist reports which it selected
    indices = info.get("requested_entries") or list(range(1, len(entries) + 1))
    if len(indices) != len(entries):
        indices = list(range(1, len(entries) + 1))
    if len(entries) > MAX_PLAYLIST_ITEMS:
        raise RuntimeError(
            f"That playlist has {len(entries)} items, above the {MAX_PLAYLIST_ITEMS} limit. "
            f"Use the Items field to pick a range, e.g. 1-{MAX_PLAYLIST_ITEMS}."
        )
    return list(zip(indices, entries))


def prepare(job: Job) -> None:
    """Probe the URL and, for a playlist, turn its entries into the plan."""
    info = probe(job, flat=job.playlist)
    is_playlist = info.get("_type") == "playlist"

    if job.playlist and not is_playlist:
        job.playlist = False  # a single video URL: nothing to enumerate
        set_plan(job, build_plan(job))
    if not job.playlist:
        if is_playlist:
            raise RuntimeError(
                "That URL resolves to a playlist. Tick \"Download whole playlist\" to "
                "fetch every item, or use the URL of a single video."
            )
        job.title = info.get("title") or info.get("id")
        check_formats(info, job)
        return

    entries = playlist_entries(job, info)
    job.title = job.playlist_title = info.get("title") or info.get("id")
    job.playlist_count = info.get("playlist_count") or len(entries)
    set_plan(job, playlist_plan(job, entries))


def run_plan(job: Job) -> None:
    """Run every step; for playlists, one bad item does not stop the rest."""
    by_item: dict[int | None, list[Step]] = {}
    for step in job.plan:
        by_item.setdefault(step.index, []).append(step)

    failures = 0
    for steps in by_item.values():
        if job.cancelled:
            raise Cancelled()
        try:
            for step in steps:
                if job.cancelled:
                    raise Cancelled()
                run_task(job, step)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded on the task, shown in the UI
            if not job.playlist:
                raise
            failures += 1
            for step in steps:
                if step.task.state in ACTIVE_TASK_STATES:
                    step.task.state = "failed" if step.task.error else "skipped"
                    step.task.error = step.task.error or str(exc)
            bump()

    if failures and failures == len(by_item):
        raise RuntimeError(f"Every one of the {failures} playlist items failed.")
    job.state = "partial" if failures else "done"
    if failures:
        job.error = f"{failures} of {len(by_item)} playlist items failed; the rest finished."


def run_job(job: Job) -> None:
    with SLOTS:
        if job.cancelled:
            job.state = "cancelled"
            bump()
            return
        try:
            job.state = "probing formats"
            bump()
            prepare(job)

            if job.cancelled:
                raise Cancelled()

            job.state = "downloading"
            bump()
            run_plan(job)
        except Cancelled:
            job.state = "cancelled"
            job.error = "Cancelled by user."
            for t in job.tasks:
                if t.state in ACTIVE_TASK_STATES:
                    t.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            job.state = "failed"
            msg = str(exc)
            hint = hint_for(msg)
            job.error = msg + (f"\n\nHint: {hint}" if hint else "")
            for t in job.tasks:
                if t.state in ACTIVE_TASK_STATES:
                    t.state = "failed" if t.error else "skipped"
        finally:
            bump()


def trim_history() -> None:
    """Drop the oldest finished jobs once the list is over MAX_HISTORY. Caller holds the lock."""
    for jid in list(JOBS_ORDER):
        if len(JOBS_ORDER) <= MAX_HISTORY:
            break
        if JOBS[jid].state not in ACTIVE_JOB_STATES:
            JOBS_ORDER.remove(jid)
            JOBS.pop(jid, None)


def retry_data(job: Job) -> dict:
    """The request body that would recreate this job."""
    return {"url": job.url, "mode": job.mode, "quality": job.quality,
            "audio_format": job.audio_format, "audio_quality": job.audio_quality,
            "keep_original": job.keep_original, "embed": job.embed, "playlist": job.playlist,
            "items": job.items, "skip_existing": job.skip_existing, "outdir": job.outdir}


def set_plan(job: Job, plan: list[Step]) -> None:
    job.plan = plan
    job.tasks = [step.task for step in plan]


def new_job(data: dict) -> Job:
    """Validate a POST /api/jobs body and build the job (not yet started)."""
    url = str(data.get("url") or "").strip()
    mode = str(data.get("mode") or "merged")
    quality = str(data.get("quality") or "best")
    audio_format = str(data.get("audio_format") or "native")
    audio_quality = str(data.get("audio_quality") or "best")
    keep_original = data.get("keep_original", False)
    embed = data.get("embed", False)
    playlist = data.get("playlist", False)
    items = str(data.get("items") or "").strip()
    skip_existing = data.get("skip_existing", False)
    outdir_raw = str(data.get("outdir") or "")

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
    if audio_quality not in AUDIO_QUALITIES:
        raise ValueError(f"Unknown audio quality: {audio_quality}")
    if mode not in ("audio", "separate"):
        audio_format = "native"  # only audio outputs are converted
    if not isinstance(keep_original, bool):
        raise ValueError("keep_original must be true or false.")
    if not isinstance(embed, bool):
        raise ValueError("embed must be true or false.")
    for name, value in (("playlist", playlist), ("skip_existing", skip_existing)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false.")
    if items and not ITEMS_RE.match(items):
        raise ValueError("Items must look like 1-10, or 1,4,7-9.")
    if not playlist:
        items, skip_existing = "", False
    if not AUDIO_FORMATS[audio_format].bitrate_ok:
        audio_quality = "best"  # lossless or untouched: a bitrate means nothing
    if not AUDIO_FORMATS[audio_format].codec:
        keep_original = False  # nothing is converted, so the download is the original
    if mode not in ("audio", "separate"):
        embed = False  # tagging is for audio files
    if embed and not FFMPEG:
        raise ValueError("Embedding tags needs ffmpeg on PATH.")
    if AUDIO_FORMATS[audio_format].codec and not (FFMPEG and FFPROBE):
        raise ValueError(
            f"Converting to {AUDIO_FORMATS[audio_format].label} needs ffmpeg and ffprobe on PATH."
        )

    outdir = validate_outdir(outdir_raw or str(DEFAULT_OUTDIR))
    job = Job(url, mode, quality, audio_format, audio_quality, outdir, keep_original, embed,
              playlist, items, skip_existing)
    # a playlist's real plan needs the probe first; until then the job has no tasks
    set_plan(job, [] if job.playlist else build_plan(job))
    return job


def submit_job(job: Job) -> Job:
    with JOBS_LOCK:
        JOBS[job.id] = job
        JOBS_ORDER.append(job.id)
        trim_history()
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

    def do_DELETE(self):  # noqa: N802
        if not self.local_host_ok():
            self.send_json({"error": "Forbidden host header."}, 403)
            return
        path = urlparse(self.path).path
        if not path.startswith("/api/jobs/"):
            self.send_json({"error": "Not found."}, 404)
            return
        job = JOBS.get(path.rsplit("/", 1)[-1])
        if not job:
            self.send_json({"error": "No such job."}, 404)
        elif job.state in ACTIVE_JOB_STATES:
            self.send_json({"error": "Cancel the job before removing it."}, 409)
        else:
            with JOBS_LOCK:
                JOBS.pop(job.id, None)
                if job.id in JOBS_ORDER:
                    JOBS_ORDER.remove(job.id)
            bump()
            self.send_json({"removed": 1})

    def do_POST(self):  # noqa: N802
        if not self.local_host_ok():
            self.send_json({"error": "Forbidden host header."}, 403)
            return
        path = urlparse(self.path).path

        if path == "/api/jobs":
            data = self.read_json()
            try:
                job = submit_job(new_job(data))
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
        elif path.startswith("/api/jobs/") and path.endswith("/retry"):
            job = JOBS.get(path.split("/")[3])
            if not job:
                self.send_json({"error": "No such job."}, 404)
            elif job.state in ACTIVE_JOB_STATES:
                self.send_json({"error": "That job is still running."}, 409)
            else:
                try:
                    self.send_json(submit_job(new_job(retry_data(job))).to_dict(), 201)
                except (ValueError, PathError) as exc:
                    self.send_json({"error": str(exc)}, 400)
        elif path == "/api/jobs/clear":
            removed = clear_finished()
            self.send_json({"removed": removed})
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


def clear_finished() -> int:
    """Forget every job that is not running. Files on disk are untouched."""
    with JOBS_LOCK:
        gone = [i for i in JOBS_ORDER if JOBS[i].state not in ACTIVE_JOB_STATES]
        for jid in gone:
            JOBS_ORDER.remove(jid)
            JOBS.pop(jid, None)
    bump()
    return len(gone)


def config_payload():
    return {
        "ffmpeg": bool(FFMPEG),
        "ffmpeg_path": FFMPEG,
        "ffprobe": bool(FFPROBE),
        "mutagen": has_mutagen(),
        "ytdlp_path": YTDLP,
        "ytdlp_version": ytdlp_version(),
        "default_outdir": str(DEFAULT_OUTDIR),
        "allowed_root": str(ALLOWED_ROOT),
        "max_concurrent": MAX_CONCURRENT,
        "max_playlist_items": MAX_PLAYLIST_ITEMS,
        "state_file": str(STATE_FILE) if STATE_FILE else None,
        "modes": [{"key": k, "label": v} for k, v in MODE_LABELS.items()],
        "qualities": list(QUALITIES),
        "audio_formats": [
            {"key": f.key, "label": f.label, "convert": f.codec is not None,
             "lossless": f.lossless, "bitrate_ok": f.bitrate_ok, "best_label": f.best_label,
             "art": art_ok(f, has_mutagen(), bool(ATOMICPARSLEY))}
            for f in AUDIO_FORMATS.values()
        ],
        "audio_qualities": [{"key": k, "label": v} for k, v in AUDIO_QUALITY_LABELS.items()],
    }


_VERSION_CACHE = []
MUTAGEN: bool | None = None  # None = not yet detected


def has_mutagen() -> bool:
    """Whether yt-dlp's own Python can import mutagen (needed for Opus/FLAC cover art)."""
    global MUTAGEN
    if MUTAGEN is None:
        MUTAGEN = False
        if YTDLP:
            try:
                # the verbose header lists optional libraries; "x" then fails fast as a bad URL
                out = subprocess.run([YTDLP, "-v", "--ignore-config", "x"],
                                     capture_output=True, text=True, timeout=30)
                libs = next((ln for ln in out.stderr.splitlines() if "Optional libraries:" in ln), "")
                MUTAGEN = "mutagen" in libs
            except (OSError, subprocess.SubprocessError):
                pass
    return MUTAGEN


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
    global DEFAULT_OUTDIR, ALLOWED_ROOT, MAX_CONCURRENT, SLOTS, MAX_PLAYLIST_ITEMS, STATE_FILE

    ap = argparse.ArgumentParser(description="Local web GUI for yt-dlp")
    ap.add_argument("--port", type=int, default=8723)
    ap.add_argument("--host", default="127.0.0.1", help="keep this local (default 127.0.0.1)")
    ap.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="default download directory")
    ap.add_argument("--root", default=str(ALLOWED_ROOT), help="allowed root for output paths")
    ap.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT)
    ap.add_argument("--max-playlist-items", type=int, default=MAX_PLAYLIST_ITEMS)
    ap.add_argument("--state-file", default=str(default_state_file()),
                    help="where the job list is saved ('none' to keep it in memory only)")
    args = ap.parse_args()

    if not YTDLP:
        sys.exit("yt-dlp was not found on PATH. Install it first.")

    ALLOWED_ROOT = Path(os.path.expanduser(args.root)).resolve()
    DEFAULT_OUTDIR = Path(os.path.expanduser(args.outdir)).resolve()
    MAX_CONCURRENT = max(1, args.max_concurrent)
    MAX_PLAYLIST_ITEMS = max(1, args.max_playlist_items)
    SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)

    if args.state_file.lower() not in ("none", ""):
        STATE_FILE = Path(os.path.expanduser(args.state_file)).resolve()
        load_state()
        threading.Thread(target=state_writer, daemon=True).start()

    try:
        DEFAULT_OUTDIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(f"Cannot create default output directory {DEFAULT_OUTDIR}: {exc}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    def stop(signum, frame):
        # a service manager (or a test) stops us with SIGTERM: take the same exit
        # path as Ctrl+C, so running jobs are marked and the history is saved
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)

    banner = [
        f"yt-dlp GUI  \u2192  http://localhost:{args.port}",
        f"  yt-dlp    : {YTDLP} ({ytdlp_version()})",
        f"  ffmpeg    : {FFMPEG or 'NOT FOUND \u2014 merged mode disabled'}",
        f"  mutagen   : {'yes' if has_mutagen() else 'no \u2014 no cover art for Opus/FLAC'}",
        f"  downloads : {DEFAULT_OUTDIR}",
        f"  allowed   : {ALLOWED_ROOT}",
        f"  history   : {STATE_FILE or 'in memory only'}",
        "  note      : no auth \u2014 any user on this machine can reach this port.",
    ]
    print("\n".join(banner), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        for job in list(JOBS.values()):
            if job.state in ACTIVE_JOB_STATES:
                job.cancel()
                job.state = "interrupted"
                job.error = "The server was stopped while this job was running."
        save_state()
        server.shutdown()


if __name__ == "__main__":
    main()
