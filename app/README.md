# yt-dlp downloader — local web GUI

A single local Python process that serves a one-page GUI and drives the system
`yt-dlp`. Implements [`PRD-download-gui.md`](../PRD-download-gui.md).

## Requirements

- Python 3.11+ (stdlib only — nothing to `pip install`)
- `yt-dlp` on `PATH`
- `ffmpeg` on `PATH` (only for **Merged** mode and MP3/WAV conversion; the UI disables those if it's missing)
- `ffprobe` on `PATH` for MP3/WAV conversion (ships with ffmpeg)

## Run

```sh
python3 app/server.py                      # http://localhost:8723
python3 app/server.py --port 9000 \
                      --outdir ~/Videos \  # default download folder
                      --root ~ \           # allowed root for output paths
                      --max-concurrent 3
```

The server binds `127.0.0.1`. **There is no authentication** — any user on this
machine can reach the port.

## Modes

| Mode | Format selector | Output |
|---|---|---|
| Merged | `bestvideo[height<=H]+bestaudio/best[height<=H]/best` | `<title> [<id>].<ext>` — one file, both streams |
| Separate | two independent runs: `bestvideo[height<=H]`, then `bestaudio` | `<title> [<id>].video.<ext>` + `<title> [<id>].audio.<ext>` |
| Audio only | `bestaudio` | `<title> [<id>].audio.<ext>` |
| Video only | `bestvideo[height<=H]` | `<title> [<id>].video.<ext>` |

Separate mode runs two *independent* yt-dlp invocations, so yt-dlp never has both
streams in one job and cannot implicitly merge them. Containers are left native —
nothing is re-encoded or remuxed, unless an audio format is chosen (below).

## Audio format

Applies to **Audio only** and the audio file in **Separate** mode; ignored otherwise.

| Choice | yt-dlp flags | Output |
|---|---|---|
| Original | — | native stream (`.webm`/`.m4a`), no re-encode |
| MP3 | `-x --audio-format mp3 --audio-quality 0` | `<title> [<id>].audio.mp3`, LAME VBR V0 (~245 kbps) |
| WAV | `-x --audio-format wav` | `<title> [<id>].audio.wav`, 16-bit PCM |

yt-dlp downloads the best audio stream, runs ffmpeg, and deletes the source file.
The task shows `converting` while ffmpeg runs. Converting to MP3 from Opus/AAC is
lossy-to-lossy; pick Original if you want the untouched stream.

The PRD lists `audio-only: best audio` as a quality value; here that is the
**Audio only** mode instead, and the quality control is disabled while it's selected.
Same result, one less way to express the same thing.

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/config` | ffmpeg presence, yt-dlp version, default/allowed paths, concurrency cap |
| `POST` | `/api/jobs` | `{url, mode, quality, audio_format, outdir}` → job (`audio_format`: `native`/`mp3`/`wav`), or `400` with a readable `error` |
| `GET` | `/api/jobs` | `{version, jobs: [...]}` newest first |
| `GET` | `/api/jobs/{id}` | one job |
| `POST` | `/api/jobs/{id}/cancel` | SIGTERMs the process group |
| `POST` | `/api/validate-path` | `{outdir}` → `{ok, path}` / `{ok: false, error}` |
| `GET` | `/api/events` | SSE, ~1 Hz, full state snapshot when it changes |

## How it works

1. **Probe** — `yt-dlp -J --no-playlist` validates the URL and yields the title.
   A conservative format check rejects obviously impossible requests (e.g. no video
   format at or below the chosen height) before any bytes move. Extractors that
   leave `vcodec`/`acodec`/`height` unset are not second-guessed.
2. **Download** — one `yt-dlp` process per output file, built as an argv list; the
   URL is always a single argument after `--`, so no shell metacharacter can expand.
   Progress comes from `--progress-template` markers parsed off stdout; the final
   path from `-O after_move:`. For merged downloads (two streams, one process) byte
   counts accumulate across streams, so the totals reflect the finished file.
3. **Cancel** — each process is spawned in its own session and cancelled with
   `killpg(SIGTERM)`. A `.part` file may remain; nothing larger.

## Security

- Binds `127.0.0.1`; requests with a non-local `Host` header are rejected (blunt
  DNS-rebinding guard).
- Output paths must be absolute, must resolve inside `--root` (default `$HOME`),
  and must be a writable directory; missing directories are created.
- `yt-dlp` is invoked as an argv list, never through a shell.
- The system `yt-dlp` is used, so no user-supplied extractor plugin code is run.

## Known limits (v1, per PRD §3)

- One URL per job; playlist URLs are rejected with an explanatory message.
- No cookies/login, subtitles, thumbnails, SponsorBlock, trimming or remuxing.
- Job state lives in memory: a server restart clears the list, files stay on disk.
- In **Merged** mode the percentage steps back once when yt-dlp moves from the
  video stream to the audio stream — the second stream's size isn't known until it
  starts. Byte counts stay correct.

## Verified against the PRD's acceptance criteria

Checked on 2026-09-16 with yt-dlp 2026.08.19 (YouTube + archive.org):

1. Merged → one `.webm`/`.mkv`, `ffprobe` shows video + audio. ✅
2. Separate → exactly two files; `ffprobe`: `av1,video` / `opus,audio`. ✅
3. Audio only → one audio-stream file; Video only → one video-stream file. ✅
4. Path outside `--root`, relative path, non-writable → rejected with the path
   echoed; a new nested path is created. ✅
5. Bad URL → job `failed` with yt-dlp's message; server keeps serving. ✅
6. Cancel mid-download → process group gone, state `cancelled`, only a `.part` left. ✅
7. Same flows on archive.org (its formats are single-stream, so Separate mode there
   fails with yt-dlp's "Requested format is not available" plus a hint). ✅
