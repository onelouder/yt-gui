#!/usr/bin/env python3
"""End-to-end smoke tests against a real server, real yt-dlp, real network.

Starts app/server.py on a free port with a temp root, submits every scenario,
waits for each job to finish, and checks the files with ffprobe.

    python3 app/tests/smoke.py              # everything
    python3 app/tests/smoke.py -k mp3       # scenarios whose name contains "mp3"
    python3 app/tests/smoke.py --keep       # leave the temp dir for inspection
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server.py"
FIX = json.loads((HERE / "fixtures.json").read_text())
YT = FIX["youtube"]
ARCHIVE = FIX["archive_mp3"]
PLAYLIST = FIX["playlist"]
PL_ITEM_URL = FIX["playlist_item_url"]
PL_TITLE = FIX["playlist_title"]
MUTAGEN = False    # set from /api/config at startup; decides Opus/FLAC cover expectations
MP4TAGGER = False  # mutagen or AtomicParsley available: decides M4A cover expectations
TERMINAL = {"done", "failed", "cancelled", "partial", "interrupted"}


# --------------------------------------------------------------------------- #
# Scenarios. `tasks` lists expectations per task, in order:
#   ext       final file extension
#   streams   exact multiset of "type:codec" for non-cover streams ("*" = any codec)
#   bitrate   (lo, hi) in kbps for the first audio stream
# Optional per-job: state (default "done"), files (count in the scenario dir).
# --------------------------------------------------------------------------- #

def S(name, request, tasks, **extra):
    return {"name": name, "request": request, "tasks": tasks, **extra}


SCENARIOS = [
    # -- baseline (v1 + MP3/WAV) -------------------------------------------
    S("merged", {"url": YT, "mode": "merged", "quality": "480"},
      [{"streams": ["video:*", "audio:*"]}], files=1),
    S("separate", {"url": YT, "mode": "separate", "quality": "480"},
      [{"streams": ["video:*"]}, {"streams": ["audio:*"]}], files=2),
    S("audio-native", {"url": YT, "mode": "audio"},
      [{"streams": ["audio:*"]}], files=1),
    S("video-only", {"url": YT, "mode": "video", "quality": "480"},
      [{"streams": ["video:*"]}], files=1),
    S("audio-mp3", {"url": YT, "mode": "audio", "audio_format": "mp3"},
      [{"ext": "mp3", "streams": ["audio:mp3"]}], files=1),
    S("audio-wav", {"url": YT, "mode": "audio", "audio_format": "wav"},
      [{"ext": "wav", "streams": ["audio:pcm_s16le"]}], files=1),
    S("separate-mp3", {"url": YT, "mode": "separate", "quality": "480", "audio_format": "mp3"},
      [{"streams": ["video:*"]}, {"ext": "mp3", "streams": ["audio:mp3"]}], files=2),
    S("archive-audio", {"url": ARCHIVE, "mode": "audio"},
      [{"streams": ["audio:*"]}], files=1),

    # -- sprint 1: MP3 bitrate ----------------------------------------------
    *[S(f"mp3-{q}", {"url": YT, "mode": "audio", "audio_format": "mp3", "audio_quality": q},
        [{"ext": "mp3", "streams": ["audio:mp3"], "bitrate": (int(q) * 0.98, int(q) * 1.02), "vbr": False}],
        files=1)
      for q in ("320", "192", "128")],
    S("mp3-best-vbr", {"url": YT, "mode": "audio", "audio_format": "mp3", "audio_quality": "best"},
      [{"ext": "mp3", "streams": ["audio:mp3"], "bitrate": (64, 320), "vbr": True}], files=1),
    S("wav-ignores-bitrate", {"url": YT, "mode": "audio", "audio_format": "wav", "audio_quality": "128"},
      [{"ext": "wav", "streams": ["audio:pcm_s16le"], "bitrate": (1000, 3000)}], files=1),
    # -- sprint 2: FLAC / M4A / Opus -----------------------------------------
    S("flac", {"url": YT, "mode": "audio", "audio_format": "flac"},
      [{"ext": "flac", "streams": ["audio:flac"]}], files=1),
    S("m4a-192", {"url": YT, "mode": "audio", "audio_format": "m4a", "audio_quality": "192"},
      [{"ext": "m4a", "streams": ["audio:aac"], "bitrate": (170, 215), "no_note": True}], files=1),
    S("m4a-best", {"url": YT, "mode": "audio", "audio_format": "m4a"},
      [{"ext": "m4a", "streams": ["audio:aac"], "bitrate": (225, 285)}], files=1),
    S("opus-copy-note", {"url": YT, "mode": "audio", "audio_format": "opus", "audio_quality": "128"},
      [{"ext": "opus", "streams": ["audio:opus"], "note": "bitrate not applied"}], files=1),
    S("archive-flac", {"url": ARCHIVE, "mode": "audio", "audio_format": "flac"},
      [{"ext": "flac", "streams": ["audio:flac"]}], files=1),
    S("archive-mp3-copy", {"url": ARCHIVE, "mode": "audio", "audio_format": "mp3"},
      [{"ext": "mp3", "streams": ["audio:mp3"], "note": "already MP3"}], files=1),
    # -- sprint 3: keep original ---------------------------------------------
    S("mp3-keep", {"url": YT, "mode": "audio", "audio_format": "mp3", "keep_original": True},
      [{"ext": "mp3", "streams": ["audio:mp3"],
        "task_files": [{"role": "original", "ext": "webm", "streams": ["audio:opus"]},
                       {"role": "converted", "ext": "mp3", "streams": ["audio:mp3"]}]}], files=2),
    S("separate-flac-keep", {"url": YT, "mode": "separate", "quality": "480", "audio_format": "flac",
                             "keep_original": True},
      [{"streams": ["video:*"]}, {"ext": "flac", "streams": ["audio:flac"]}], files=3),
    S("archive-mp3-keep-nothing", {"url": ARCHIVE, "mode": "audio", "audio_format": "mp3", "keep_original": True},
      [{"ext": "mp3", "note": "no separate original"}], files=1),
    # -- sprint 4: cover art + tags ------------------------------------------
    S("mp3-embed", {"url": YT, "mode": "audio", "audio_format": "mp3", "embed": True},
      [{"ext": "mp3", "streams": ["audio:mp3"], "cover": True, "tags": ["title", "artist"], "no_note": True,
        "chapters": 3}],
      files=1),
    S("m4a-embed", {"url": YT, "mode": "audio", "audio_format": "m4a", "embed": True},
      # the fixture video has 3 YouTube chapters; MP4 stores them as a text data track
      [{"ext": "m4a", "streams": ["audio:aac", "data:bin_data"], "cover": "mp4tagger", "tags": ["title", "artist"],
        "chapters": 3}], files=1),
    S("wav-embed-tags-only", {"url": YT, "mode": "audio", "audio_format": "wav", "embed": True},
      [{"ext": "wav", "cover": False, "tags": ["title", "artist"], "note": "can't hold cover art"}], files=1),
    S("flac-embed", {"url": YT, "mode": "audio", "audio_format": "flac", "embed": True},
      [{"ext": "flac", "cover": "mutagen", "tags": ["title", "artist"]}], files=1),
    S("opus-embed", {"url": YT, "mode": "audio", "audio_format": "opus", "embed": True},
      [{"ext": "opus", "cover": "mutagen", "tags": ["title", "artist"]}], files=1),
    S("native-embed-tags-only", {"url": YT, "mode": "audio", "embed": True},
      [{"cover": False, "tags": ["title"], "note": "only embedded when converting"}], files=1),
    S("separate-mp3-keep-embed", {"url": YT, "mode": "separate", "quality": "480", "audio_format": "mp3",
                                  "keep_original": True, "embed": True},
      [{"streams": ["video:*"]}, {"ext": "mp3", "cover": True, "tags": ["title"]}], files=3),
    # -- sprint 5: playlists --------------------------------------------------
    S("playlist-mp3-range", {"url": PLAYLIST, "mode": "audio", "audio_format": "mp3",
                             "audio_quality": "128", "embed": True, "playlist": True, "items": "3-4"},
      [{"ext": "mp3", "tags": ["title", "track", "album"]}, {"ext": "mp3", "tags": ["track"]}],
      files=2, paths=[f"{PL_TITLE}/003 - .*\\.audio\\.mp3", f"{PL_TITLE}/004 - .*\\.audio\\.mp3"]),
    S("playlist-off-takes-one-video", {"url": PL_ITEM_URL, "mode": "audio"},
      [{"streams": ["audio:*"]}], files=1),
    S("playlist-bad-items-rejected", {"url": PLAYLIST, "playlist": True, "items": "1;rm -rf /"},
      [], http=400),

    S("bad-bitrate-rejected", {"url": YT, "mode": "audio", "audio_format": "mp3", "audio_quality": "999"},
      [], http=400),
]


def wait_job(port, job_id, until, timeout=600):
    """Poll one job until `until(job)` is true; returns the job or raises."""
    end = time.time() + timeout
    while time.time() < end:
        _, job = api(port, "GET", f"/api/jobs/{job_id}")
        if until(job):
            return job
        time.sleep(1)
    raise TimeoutError(f"job {job_id} never satisfied {until.__name__}: state={job.get('state')}")


def terminal(job):
    return job["state"] in TERMINAL


def rerun_skips_existing(port, scdir):
    """Same playlist item twice with skip_existing: second run downloads nothing."""
    body = {"url": PLAYLIST, "mode": "audio", "audio_format": "mp3", "playlist": True,
            "items": "4", "skip_existing": True, "outdir": str(scdir)}
    errs = []
    status, job = api(port, "POST", "/api/jobs", body)
    first = wait_job(port, job["id"], terminal)
    if first["state"] != "done":
        return [f"first run {first['state']}: {first.get('error')}"]
    files = sorted(p.name for p in scdir.rglob("*.mp3"))

    status, job2 = api(port, "POST", "/api/jobs", body)
    second = wait_job(port, job2["id"], terminal)
    if second["state"] != "done":
        errs.append(f"second run {second['state']}: {second.get('error')}")
    states = [t["state"] for t in second["tasks"]]
    if states != ["skipped"]:
        errs.append(f"second run task states {states} != ['skipped']")
    elif "Already downloaded" not in " ".join(second["tasks"][0].get("notes") or []):
        errs.append("no 'already downloaded' note on the skipped task")
    if sorted(p.name for p in scdir.rglob("*.mp3")) != files:
        errs.append("second run changed the files on disk")
    archives = [p.name for p in scdir.rglob(".yt-gui-archive-*")]
    if archives != [".yt-gui-archive-audio-mp3.txt"]:
        errs.append(f"archive files {archives}")
    return errs


def cancel_mid_playlist(port, scdir):
    """Cancel after the first item finishes: the rest stop, the finished file stays."""
    body = {"url": PLAYLIST, "mode": "audio", "playlist": True, "outdir": str(scdir)}
    errs = []
    _, job = api(port, "POST", "/api/jobs", body)

    def first_done(j):
        return any(t["state"] in ("done", "failed") for t in j["tasks"])

    wait_job(port, job["id"], first_done)
    api(port, "POST", f"/api/jobs/{job['id']}/cancel")
    final = wait_job(port, job["id"], terminal, timeout=120)
    if final["state"] != "cancelled":
        errs.append(f"state {final['state']} != cancelled")
    states = [t["state"] for t in final["tasks"]]
    if states[0] != "done":
        errs.append(f"first item {states[0]} != done")
    if any(s not in ("cancelled", "queued", "skipped") for s in states[1:]):
        errs.append(f"later items not stopped: {states[1:]}")
    if len(list(scdir.rglob("*.webm"))) + len(list(scdir.rglob("*.m4a"))) < 1:
        errs.append("the finished item's file is missing")
    leftover = subprocess.run(["pgrep", "-af", "yt-dlp"], capture_output=True, text=True).stdout
    if str(scdir) in leftover:
        errs.append(f"yt-dlp still running: {leftover.strip()}")
    return errs


def restart_keeps_history(port, scdir):
    """A finished job, a restart, then the same job still listed - and retryable."""
    errs = []
    state = scdir / "state.json"
    p2 = free_port()
    log = open(scdir / "server.log", "w")
    srv = start_server(p2, scdir, log, state)
    try:
        for _ in range(60):
            try:
                api(p2, "GET", "/api/config")
                break
            except OSError:
                time.sleep(0.2)
        _, job = api(p2, "POST", "/api/jobs", {"url": YT, "mode": "audio", "audio_format": "mp3",
                                               "outdir": str(scdir / "dl")})
        done = wait_job(p2, job["id"], terminal)
        if done["state"] != "done":
            return [f"first run {done['state']}: {done.get('error')}"]
        path = done["tasks"][0]["filepath"]
    finally:
        srv.terminate()
        srv.wait(timeout=10)

    srv = start_server(p2, scdir, log, state)
    try:
        for _ in range(60):
            try:
                _, snap = api(p2, "GET", "/api/jobs")
                break
            except OSError:
                time.sleep(0.2)
        jobs = snap["jobs"]
        if len(jobs) != 1 or jobs[0]["id"] != job["id"]:
            return [f"history not restored: {[j.get('id') for j in jobs]}"]
        back = jobs[0]
        if back["state"] != "done":
            errs.append(f"restored state {back['state']} != done")
        if back["tasks"][0]["filepath"] != path:
            errs.append("restored task lost its filepath")
        if back["tasks"][0]["missing"]:
            errs.append("existing file reported as missing")

        Path(path).unlink()
        srv.terminate(); srv.wait(timeout=10)
        srv = start_server(p2, scdir, log, state)
        for _ in range(60):
            try:
                _, snap = api(p2, "GET", "/api/jobs")
                break
            except OSError:
                time.sleep(0.2)
        if not snap["jobs"][0]["tasks"][0]["missing"]:
            errs.append("deleted file not flagged missing after restart")

        status, retried = api(p2, "POST", f"/api/jobs/{job['id']}/retry", {})
        if status != 201:
            errs.append(f"retry HTTP {status}: {retried}")
        else:
            again = wait_job(p2, retried["id"], terminal)
            if again["state"] != "done":
                errs.append(f"retried job {again['state']}: {again.get('error')}")
            elif not Path(again["tasks"][0]["filepath"]).exists():
                errs.append("retried job produced no file")

        _, cleared = api(p2, "POST", "/api/jobs/clear", {})
        _, snap = api(p2, "GET", "/api/jobs")
        if snap["jobs"]:
            errs.append(f"clear left {len(snap['jobs'])} jobs")
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        log.close()
    return errs


def kill_leaves_interrupted(port, scdir):
    """SIGKILL mid-download: the job comes back as interrupted, then retries fine."""
    errs = []
    state = scdir / "state.json"
    p2 = free_port()
    log = open(scdir / "server.log", "w")
    srv = start_server(p2, scdir, log, state)
    try:
        for _ in range(60):
            try:
                api(p2, "GET", "/api/config")
                break
            except OSError:
                time.sleep(0.2)
        _, job = api(p2, "POST", "/api/jobs", {"url": PLAYLIST, "mode": "audio", "playlist": True,
                                               "outdir": str(scdir / "dl")})
        wait_job(p2, job["id"], lambda j: any(t["state"] == "downloading" for t in j["tasks"]), timeout=180)
        time.sleep(2)  # let the state writer flush
    finally:
        srv.kill()
        srv.wait(timeout=10)
    subprocess.run(["pkill", "-f", f"-I .* {PLAYLIST}"], capture_output=True)

    srv = start_server(p2, scdir, log, state)
    try:
        for _ in range(60):
            try:
                _, snap = api(p2, "GET", "/api/jobs")
                break
            except OSError:
                time.sleep(0.2)
        if not snap["jobs"]:
            return ["nothing restored after the kill"]
        back = snap["jobs"][0]
        if back["state"] != "interrupted":
            errs.append(f"state {back['state']} != interrupted")
        if not any(t["state"] == "interrupted" for t in back["tasks"]):
            errs.append(f"no interrupted task: {[t['state'] for t in back['tasks']]}")
        if "stopped" not in (back.get("error") or ""):
            errs.append(f"error text: {back.get('error')!r}")
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        log.close()
    return errs


def corrupt_state_still_starts(port, scdir):
    state = scdir / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{ this is not json")
    p2 = free_port()
    log = open(scdir / "server.log", "w")
    srv = start_server(p2, scdir, log, state)
    errs = []
    try:
        for _ in range(60):
            try:
                _, snap = api(p2, "GET", "/api/jobs")
                break
            except OSError:
                time.sleep(0.2)
        else:
            return ["server did not start with a corrupt state file"]
        if snap["jobs"]:
            errs.append("expected an empty job list")
        if not list(scdir.glob("state.bad-*")):
            errs.append(f"no .bad-* backup: {[p.name for p in scdir.iterdir()]}")
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        log.close()
    return errs


SCENARIOS += [
    {"name": "playlist-rerun-skips", "custom": rerun_skips_existing},
    {"name": "playlist-cancel", "custom": cancel_mid_playlist},
    {"name": "restart-keeps-history", "custom": restart_keeps_history},
    {"name": "kill-leaves-interrupted", "custom": kill_leaves_interrupted},
    {"name": "corrupt-state-still-starts", "custom": corrupt_state_still_starts},
]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

def start_server(port, root: Path, log, state_file: Path = None):
    argv = [sys.executable, str(SERVER), "--port", str(port), "--root", str(root),
            "--outdir", str(root / "default"),
            "--state-file", str(state_file) if state_file else "none"]
    return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def ffprobe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-show_chapters",
                          "-of", "json", path],
                         capture_output=True, text=True)
    return json.loads(out.stdout or "{}")


def is_cover(stream) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def check_file(path: str, exp: dict) -> list[str]:
    errs = []
    p = Path(path)
    if not p.is_file():
        return [f"missing file {path}"]
    if "ext" in exp and p.suffix.lstrip(".") != exp["ext"]:
        errs.append(f"ext {p.suffix} != .{exp['ext']}")
    info = ffprobe(path)
    streams = info.get("streams") or []
    real = [s for s in streams if not is_cover(s)]
    if "streams" in exp:
        got = sorted(f"{s.get('codec_type')}:{s.get('codec_name')}" for s in real)
        want = exp["streams"]
        ok = len(got) == len(want)
        pool = list(got)
        for w in want:
            wt, wc = w.split(":")
            match = next((g for g in pool if g.split(":")[0] == wt and wc in ("*", g.split(":")[1])), None)
            if match is None:
                ok = False
            else:
                pool.remove(match)
        if not ok:
            errs.append(f"streams {got} != {want}")
    if "bitrate" in exp:
        audio = next((s for s in real if s.get("codec_type") == "audio"), {})
        br = audio.get("bit_rate") or (info.get("format") or {}).get("bit_rate")
        kbps = int(br) / 1000 if br else None
        lo, hi = exp["bitrate"]
        if kbps is None or not lo <= kbps <= hi:
            errs.append(f"bitrate {kbps} kbps not in [{lo}, {hi}]")
    if "vbr" in exp:
        head = p.read_bytes()[:65536]
        vbr = b"Xing" in head
        cbr = b"Info" in head
        if (vbr, cbr) != (exp["vbr"], not exp["vbr"]):
            errs.append(f"LAME header Xing={vbr} Info={cbr}, expected {'VBR' if exp['vbr'] else 'CBR'}")
    if "cover" in exp:
        has = any(is_cover(s) for s in streams)
        want = {"mutagen": MUTAGEN, "mp4tagger": MP4TAGGER}.get(exp["cover"], exp["cover"])
        if has != want:
            errs.append(f"cover art present={has}, expected {want}")
    if "chapters" in exp and len(info.get("chapters") or []) != exp["chapters"]:
        errs.append(f"{len(info.get('chapters') or [])} chapters != {exp['chapters']}")
    if "tags" in exp:
        tags = {k.lower(): v for k, v in ((info.get("format") or {}).get("tags") or {}).items()}
        for s in real:
            tags.update({k.lower(): v for k, v in (s.get("tags") or {}).items()})
        for t in exp["tags"]:
            if not tags.get(t):
                errs.append(f"missing tag {t!r} (have {sorted(tags)})")
    return errs


def check_job(sc: dict, job: dict, scdir: Path) -> list[str]:
    errs = []
    want_state = sc.get("state", "done")
    if job["state"] != want_state:
        errs.append(f"state {job['state']!r} != {want_state!r}: {job.get('error')}")
        return errs
    tasks = job["tasks"]
    if "tasks" in sc and len(tasks) != len(sc["tasks"]):
        errs.append(f"{len(tasks)} tasks != {len(sc['tasks'])}")
    for i, (task, exp) in enumerate(zip(tasks, sc.get("tasks", []))):
        if want_state == "done" and not task.get("filepath"):
            errs.append(f"task {i} has no filepath")
            continue
        if task.get("filepath"):
            errs += [f"task {i} ({task['label']}): {e}" for e in check_file(task["filepath"], exp)]
        if "task_files" in exp:
            got = task.get("files") or []
            if [f["role"] for f in got] != [f["role"] for f in exp["task_files"]]:
                errs.append(f"task {i}: file roles {[f['role'] for f in got]} != {[f['role'] for f in exp['task_files']]}")
            else:
                for f, fexp in zip(got, exp["task_files"]):
                    errs += [f"task {i} {f['role']}: {e}" for e in check_file(f["path"], fexp)]
        notes = " | ".join(task.get("notes") or [])
        if "note" in exp and exp["note"] not in notes:
            errs.append(f"task {i}: note {exp['note']!r} not in {notes!r}")
        if exp.get("no_note") and notes:
            errs.append(f"task {i}: unexpected note {notes!r}")
    files = sorted(f for f in scdir.rglob("*") if f.is_file() and not f.name.startswith("."))
    if "paths" in sc:
        rel = [str(f.relative_to(scdir)) for f in files]
        if len(rel) != len(sc["paths"]) or not all(re.fullmatch(pat, r) for pat, r in zip(sc["paths"], rel)):
            errs.append(f"paths {rel} do not match {sc['paths']}")
    if "files" in sc and len(files) != sc["files"]:
        errs.append(f"{len(files)} files in dir != {sc['files']}: {[f.name for f in files]}")
    leftovers = [f.name for f in files if f.suffix in (".part", ".ytdl") or ".temp." in f.name]
    if leftovers:
        errs.append(f"leftover partial files: {leftovers}")
    if "check" in sc:
        errs += sc["check"](job, scdir, files)
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", action="append", default=[], help="only run scenarios containing this text")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    chosen = [s for s in SCENARIOS if not args.k or any(k in s["name"] for k in args.k)]
    if not chosen:
        sys.exit("no scenarios selected")

    tmp = Path(tempfile.mkdtemp(prefix="yt-gui-smoke-"))
    port = free_port()
    log = open(tmp / "server.log", "w")
    proc = start_server(port, tmp, log)
    results = []
    try:
        for _ in range(50):
            try:
                _, cfg = api(port, "GET", "/api/config")
                break
            except OSError:
                time.sleep(0.2)
        else:
            sys.exit(f"server did not start; see {tmp / 'server.log'}")
        global MUTAGEN
        global MP4TAGGER
        MUTAGEN = bool(cfg.get("mutagen"))
        MP4TAGGER = next(f["art"] for f in cfg["audio_formats"] if f["key"] == "m4a")
        print(f"yt-dlp {cfg.get('ytdlp_version')} · ffmpeg {'yes' if cfg.get('ffmpeg') else 'no'} "
              f"· mutagen {'yes' if MUTAGEN else 'no'} "
              f"· {len(chosen)} scenarios · {tmp}")

        started = {}
        customs = [sc for sc in chosen if "custom" in sc]
        for sc in chosen:
            if "custom" in sc:
                continue
            scdir = tmp / sc["name"]
            body = {**sc["request"], "outdir": str(scdir)}
            status, job = api(port, "POST", "/api/jobs", body)
            want = sc.get("http", 201)
            if status != want:
                results.append((sc["name"], [f"HTTP {status} != {want}: {job}"]))
            elif status != 201:
                results.append((sc["name"], []))
            else:
                started[job["id"]] = (sc, scdir)
                if "during" in sc:
                    sc["during"](port, job)

        deadline = time.time() + args.timeout
        pending = dict(started)
        while pending and time.time() < deadline:
            _, snap = api(port, "GET", "/api/jobs")
            for job in snap["jobs"]:
                if job["id"] in pending and job["state"] in TERMINAL:
                    sc, scdir = pending.pop(job["id"])
                    results.append((sc["name"], check_job(sc, job, scdir)))
                    print(f"  {'PASS' if not results[-1][1] else 'FAIL'}  {sc['name']}", flush=True)
            time.sleep(1)
        for sc, _ in pending.values():
            results.append((sc["name"], [f"timed out after {args.timeout}s"]))

        for sc in customs:
            scdir = tmp / sc["name"]
            scdir.mkdir(parents=True, exist_ok=True)
            try:
                errs = sc["custom"](port, scdir)
            except Exception as exc:  # noqa: BLE001 - reported as a failure
                errs = [f"{type(exc).__name__}: {exc}"]
            results.append((sc["name"], errs))
            print(f"  {'PASS' if not errs else 'FAIL'}  {sc['name']}", flush=True)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()

    print()
    failed = 0
    for name, errs in sorted(results):
        print(f"{'PASS' if not errs else 'FAIL'}  {name}")
        for e in errs:
            print(f"        {e}")
        failed += bool(errs)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    if args.keep or failed:
        print(f"artifacts: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
