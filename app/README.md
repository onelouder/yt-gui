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
| MP3 | `-x --audio-format mp3 --audio-quality <q>` | `.audio.mp3` — Best = LAME VBR V0 (~245 kbps) |
| M4A | `-x --audio-format m4a --audio-quality <q>` | `.audio.m4a` (AAC) — Best = 256 kbps |
| Opus | `-x --audio-format opus --audio-quality <q>` | `.audio.opus` — Best = 160 kbps |
| FLAC | `-x --audio-format flac` | `.audio.flac`, lossless |
| WAV | `-x --audio-format wav` | `.audio.wav`, 16-bit PCM |

**Bitrate** applies to lossy formats (MP3, M4A, Opus): **Best** as listed above, or a
fixed **320 / 192 / 128 kbps** (`--audio-quality 320K` etc., passed to ffmpeg as
`-b:a`). It is ignored for Original, FLAC and WAV.

**Stream copy:** when the source already uses the target codec (YouTube audio is
usually Opus; archive.org often serves MP3), yt-dlp copies the stream instead of
re-encoding it, so no quality is lost and the bitrate isn't applied. yt-dlp prints the
source codec (`-O video:%(acodec)s`), and the task shows a note when this happens.

**Keep original file** (`-k`) leaves the downloaded source next to the converted file,
e.g. `….audio.webm` + `….audio.mp3`. yt-dlp reports the pre-conversion path
(`-O post_process:%(filepath)s`) and the task lists both files with their role. When
there's nothing separate to keep (the source already had the target codec and file
type), the task says so.

**Embed cover art & tags** (`--embed-metadata`, plus `--embed-thumbnail
--convert-thumbnails jpg` where it works) writes title, artist, date, description
and chapters, and the video thumbnail as cover art. yt-dlp *fails the job* when it can't
embed art, so the server only asks for art where it will succeed:

| Format | Cover art | Needs |
|---|---|---|
| MP3 | yes | ffmpeg |
| M4A | yes | mutagen or AtomicParsley. yt-dlp's ffmpeg fallback breaks on ffmpeg 9, whose `ipod` muxer rejects JPEG covers |
| Opus, FLAC | yes | mutagen, imported by yt-dlp's own Python (`pacman -S python-mutagen`) |
| WAV | no | — tags only |
| Original | no | — tags only (the native `.webm` can't hold art) |

Where art isn't possible, the job still runs with tags only and the task says why. The
server detects mutagen at startup from `yt-dlp -v`'s "Optional libraries" line; the
top bar shows `mutagen ok` / `no mutagen`. With **Keep original** on, the thumbnail
isn't pre-converted (yt-dlp's `-k` would keep the intermediate image), so the cover is
PNG instead of JPEG.

Without Keep original, yt-dlp downloads the best audio stream, runs ffmpeg, and deletes the source file.
Task states while yt-dlp postprocesses: `merging`, `converting`, `tagging`, `embedding art`. Converting to MP3 from Opus/AAC is
lossy-to-lossy; pick Original if you want the untouched stream.

The PRD lists `audio-only: best audio` as a quality value; here that is the
**Audio only** mode instead, and the quality control is disabled while it's selected.
Same result, one less way to express the same thing.

## Playlists

Off by default: a `watch?v=…&list=…` URL still downloads the single video. Tick
**Download whole playlist** and each item is downloaded by its own yt-dlp run:

- **Probe** — `yt-dlp -J --flat-playlist --yes-playlist [-I items]` lists the entries
  cheaply; `requested_entries` gives their real positions.
- **Run** — one process per item, `--yes-playlist -I <n>` against the *playlist* URL, so
  `%(playlist_title)s` and `%(playlist_index)s` stay available. Files land in
  `<playlist title>/001 - <title> [<id>].<ext>`.
- **Failures are per item.** A private, deleted or geo-blocked entry marks that item
  failed and the rest continue; the job ends `done`, `partial`, or `failed` if every
  item failed.
- **Items** accepts `1-10`, `1,4,7-9` or `3` (validated against `^\d+(-\d+)?(,…)*$`).
- **Cap:** `--max-playlist-items` (default 200) — above it the job fails and suggests a range.
- **Skip items already downloaded** adds `--download-archive`, one file per output kind
  (`.yt-gui-archive-audio-mp3.txt`, `.yt-gui-archive-video.txt`, …) inside the output
  folder, so Separate mode and re-runs in another format aren't skipped by each other.
  A skipped item shows as `skipped` with a note.
- **With tags on**, `--parse-metadata` maps `playlist_index` to the track number and
  `playlist_title` to the album.
- A playlist job holds one concurrency slot and runs its items one after another;
  cancelling stops the current item and skips the rest.

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/config` | ffmpeg presence, yt-dlp version, default/allowed paths, concurrency cap |
| `POST` | `/api/jobs` | `{url, mode, quality, audio_format, audio_quality, keep_original, embed, playlist, items, skip_existing, outdir}` → job (`audio_format`: `native`/`mp3`/`m4a`/`opus`/`flac`/`wav`; `audio_quality`: `best`/`320`/`192`/`128`), or `400` with a readable `error` |
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

## Tests

```sh
python3 -m unittest discover app/tests   # offline, < 1 s
python3 app/tests/smoke.py               # live: real downloads, checked with ffprobe
python3 app/tests/smoke.py -k mp3        # only scenarios whose name contains "mp3"
node app/tests/ui_check.mjs              # headless Chromium UI checks (Node 22+, chromium)
node app/tests/ui_check.mjs --shot       # ...and save a screenshot per check
```

The live tests start their own server on a free port with a temp folder, so they
don't touch a running instance. Test URLs are in `app/tests/fixtures.json`.

## Security

- Binds `127.0.0.1`; requests with a non-local `Host` header are rejected (blunt
  DNS-rebinding guard).
- Output paths must be absolute, must resolve inside `--root` (default `$HOME`),
  and must be a writable directory; missing directories are created.
- `yt-dlp` is invoked as an argv list, never through a shell.
- The system `yt-dlp` is used, so no user-supplied extractor plugin code is run.

## Known limits (v1, per PRD §3)

- One URL per job (a playlist counts as one job).
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
