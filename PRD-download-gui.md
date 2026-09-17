# PRD (Draft): Local URL Downloader Web GUI

**Status:** Draft
**Date:** 2026-09-15
**Depends on:** `yt-dlp` (system-installed), `ffmpeg` (for merge mode)

## 1. Summary

A locally-run web GUI that wraps `yt-dlp`. Given any supported URL, the user chooses
what to download — audio only, video only, both as separate files, or a single merged
file — and where to save it. All downloads execute on the user's own machine.

## 2. Goals

- One field for the URL, one control for output mode, one control for output path.
- Correctly produce: merged file, separate video+audio files, or audio-only file.
- Live progress per file; clear errors (unsupported site, no formats, ffmpeg missing).
- Zero-install for the user beyond `yt-dlp` + `ffmpeg`.

## 3. Non-Goals (v1)

- Multi-URL / playlist batches, cookies/login, scheduled or headless operation.
- Subtitles, thumbnails, SponsorBlock, trimming, format remux/convert beyond container choice.
- Multi-user / network-accessible deployment (local-only; see §9).
- Mobile layout beyond basic responsiveness.

## 4. Users & Use Case

A power user with `yt-dlp` already installed who wants to stop hand-typing `-f`/`-S`
combinations. Typical session: paste URL → pick mode → pick folder → download → verify.

## 5. Core Functionality

### 5.1 Inputs

| Control | Details |
|---|---|
| URL | Single URL, any site `yt-dlp` supports. |
| Mode | One of: **Merged** (default), **Separate** (one video + one audio file), **Audio only**, **Video only**. |
| Quality | `best` / `1080p` / `720p` / `480p` / `audio-only: best audio`. Ignored for audio-only mode. |
| Output path | Directory (or full filename). Defaults to a local `downloads/` dir. Must be an absolute, writable, local path. |

### 5.2 Behavior by mode

| Mode | yt-dlp strategy | Resulting files |
|---|---|---|
| Merged | `-f "bestvideo[height<=H]+bestaudio/best"` | 1 file, video+audio |
| Separate | Two independent `-f` runs: `bestvideo[height<=H]` and `bestaudio` (never combined, so no implicit merge) | `*.video.<ext>` + `*.audio.<ext>` |
| Audio only | `-f bestaudio` | 1 audio file |
| Video only | `-f bestvideo[height<=H]` | 1 video-only file |

- Filenames use the video's title/ID, sanitized to filesystem-safe characters.
- Merged mode requires `ffmpeg` present; the app detects this at startup and disables
  the option with an explanatory message when absent.
- No re-compression/remux unless user picks a container preference (v1: keep native
  container; expose `--remux` only if trivial).

### 5.3 Progress & state

- Per-job card: state (`queued`, `probing formats`, `downloading`, `merging`, `done`, `failed`),
  progress bar + speed + ETA per file (separate mode shows 2 bars).
- Jobs run server-side; UI updates via polling or SSE (no websockets required).
- Jobs survive page refresh while the server is up. "Cancel" kills the process group.

### 5.4 Error surface

- Unsupported/unresolvable URL → clear message from yt-dlp, surfaced verbatim-ish.
- Output path missing → create it; not writable → error with path echoed back.
- Geo-block / age-restriction / bot-check → show yt-dlp's error and a hint (cookies, VPN).

## 6. Architecture

Single local server process + static frontend.

```
browser ──HTTP──▶ local server (Python, stdlib http.server or FastAPI)
                      ├── POST /api/jobs      {url, mode, quality, outdir}
                      ├── GET  /api/jobs      list + states
                      ├── GET  /api/jobs/{id} state, progress, files
                      ├── POST /api/jobs/{id}/cancel
                      └── spawns:  yt-dlp -j (probe) then per-mode yt-dlp invocations
                                   (subprocess, argv list — never shell strings)
```

- **Probe step:** run `yt-dlp -J` (JSON, no download) first to validate the URL, get
  title, and verify requested formats exist before committing to a job.
- **Spawn:** build yt-dlp argv as a list (no shell interpolation — the URL is user input).
- **Progress:** parse yt-dlp stdout lines (`--progress` / `-v`) in the server loop;
  expose via SSE at ~1 Hz.
- **Frontend:** single static page (vanilla JS or a minimal framework), no build step
  required for v1.

## 7. Acceptance Criteria

1. Paste a YouTube URL → mode Merged → file contains both video and audio streams.
2. Same URL → mode Separate → exactly two files: video stream only, audio stream only
   (verified by `ffprobe`).
3. Audio only → single audio file; Video only → single video-only file.
4. Invalid output path → job fails with the path in the error; valid but new path → created.
5. `yt-dlp` exits non-zero (bad URL) → job state `failed` with readable message; server keeps running.
6. Cancel mid-download → process killed, no orphan files larger than the partial, state `failed/cancelled`.
7. All of the above reproducible on 2 different sites (e.g. YouTube + a non-YouTube extractor).

## 8. Non-Functional

- **Performance:** server overhead negligible; one job = one/two yt-dlp processes.
- **Concurrency:** v1 allows concurrent jobs, capped at a small N (e.g. 3) to avoid
  saturating disk/network.
- **Robustness:** server restart loses job states (acceptable; files on disk remain).
- **Portability:** Linux/macOS; no packaging needed for v1 (`pip install`-free or single
  dependency server).

## 9. Security (local-only assumptions made explicit)

- Bind to `127.0.0.1` only; show the printed URL (`http://localhost:<port>`) at startup.
- Output path validated: absolute, resolves within an allowed root (default: user home),
  no `..` traversal outside it, must be a directory-or-file path the OS allows.
- yt-dlp invoked with `argv` list; URL passed as a single arg — no shell metacharacter expansion.
- No auth (local single-user). Documenting this: anyone on the machine can reach the port.
- Arbitrary-code path: yt-dlp supports Python extractor plugins — run with the system
  `yt-dlp` install, not user-supplied code.

## 10. Open Questions

- Should "Separate" also offer a combined filename stem so the pair is visually obvious? (Leaning yes: `<title>.video.mp4` / `<title>.audio.m4a`.)
- Keep jobs in memory only, or persist a small JSON so a restart can show "unknown" old jobs?
- Offer `-S` format-sort customization (codec preference) later, or keep the quality preset only?

## 11. Suggested Milestones

| M | Deliverable |
|---|---|
| M0 | Server skeleton: POST job → spawn yt-dlp (merged only) → poll state. |
| M1 | All 4 modes + quality preset + output-path validation. |
| M2 | Frontend: form, job cards, progress, cancel, error display. |
| M3 | Hardening: concurrency cap, path whitelist, ffprobe-free error hints, README. |
