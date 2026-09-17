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
    S("bad-bitrate-rejected", {"url": YT, "mode": "audio", "audio_format": "mp3", "audio_quality": "999"},
      [], http=400),
]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

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
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
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
        if has != exp["cover"]:
            errs.append(f"cover art present={has}, expected {exp['cover']}")
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
    files = [f for f in scdir.rglob("*") if f.is_file() and not f.name.startswith(".")]
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
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port), "--root", str(tmp),
         "--outdir", str(tmp / "default")],
        stdout=log, stderr=subprocess.STDOUT,
    )
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
        print(f"yt-dlp {cfg.get('ytdlp_version')} · ffmpeg {'yes' if cfg.get('ffmpeg') else 'no'} "
              f"· {len(chosen)} scenarios · {tmp}")

        started = {}
        for sc in chosen:
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
