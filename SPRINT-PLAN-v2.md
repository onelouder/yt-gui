# Sprint Plan: v2 Audio & Batch Features

**Date:** 2026-09-17
**Builds on:** `PRD-download-gui.md` (v1) and the MP3/WAV option added 2026-09-17
**Scope:** six features, one sprint each, plus a Sprint 0 that sets up testing.

| # | Feature | Size | Sprint |
|---|---|---|---|
| — | Test harness + baseline | S | 0 |
| 1 | MP3 bitrate choice (V0 / 320 / 192 / 128) | S | 1 |
| 2 | More formats: FLAC, M4A, Opus | S | 2 |
| 3 | Keep the original alongside the converted file | S–M | 3 |
| 4 | Embed thumbnail + tags | M | 4 |
| 5 | Playlist downloads | L | 5 |
| 6 | Keep jobs after a server restart | M | 6 |

Sizes: S ≈ half a day, M ≈ 1 day, L ≈ 2–3 days.

The order follows dependencies. Sprints 1–4 share one code path (the audio
postprocessing argv), so each builds on the one before. Playlists (5) change the
Job/Task shape, so persistence (6) goes last and saves the final shape instead of
needing a migration later.

> PRD §3 lists playlists and thumbnails as v1 non-goals. Update the PRD with a v2
> section as part of Sprint 0 so it doesn't contradict this plan.

---

## Final API shape (set now, filled in one sprint at a time)

`POST /api/jobs` gains these fields. Every one is optional and its default matches
today's behavior, so old clients keep working.

| Field | Values | Default | Sprint |
|---|---|---|---|
| `audio_format` | `native` `mp3` `wav` `flac` `m4a` `opus` | `native` | done / 2 |
| `audio_quality` | `best` `320` `192` `128` | `best` | 1 |
| `keep_original` | bool | `false` | 3 |
| `embed` | bool (tags + cover art) | `false` | 4 |
| `playlist` | bool | `false` | 5 |

The server stays the source of truth. It rejects unknown values with `400`, turns
options off where they don't apply (for example `audio_quality` on WAV), and
`/api/config` publishes which combinations are allowed so the UI never has to
guess.

---

## Validation gate (runs after every sprint)

A sprint is done only when **all** of these pass:

1. **Unit tests** (`python3 -m unittest discover app/tests`). Offline, fast. They
   cover argv building, request validation, and progress parsing.
2. **Smoke tests** (`python3 app/tests/smoke.py`). Start a real server on a free
   port with a temp output folder, submit scenario jobs, wait, then check the
   output files with `ffprobe`. **All earlier sprints' scenarios run again**, so
   this doubles as a regression check.
3. **Manual UI checklist** for the sprint (listed below), run in a browser.
4. **README** updated (options table, API table, known limits).
5. **Commit**, tagged `v2-sprint-N`, so any sprint can be rolled back on its own.

Test media: YouTube `jNQXAC9IVRw` ("Me at the zoo": 19 s, stable, public). Also one
archive.org item, which keeps PRD criterion 7 (two extractors). Sprint 5 adds a
small public playlist, chosen and recorded in `app/tests/fixtures.json` at the
start of that sprint.

---

## Sprint 0: Test harness + baseline

**Goal:** make "validate after each phase" a single command.

- `git init` the project (only `app/`, the docs, and `.gitignore`; ignore
  `downloads/`, `*.m4a`, `*.mp4`, `.venv/`, `yt-dlp/`) and commit the current
  state as `v2-baseline`.
- `app/tests/test_server.py`: stdlib `unittest`, covering:
  - `build_plan` for every mode × audio format (task keys, format selector, extra argv)
  - `create_job` validation (bad mode, format, URL, or path → `ValueError`), with
    job threads stubbed out
  - `parse_progress` monotonic totals across two streams
- `app/tests/smoke.py`: stdlib only. Launches `server.py` on a free port with `--root $TMP`,
  runs a list of `{request, expect}` scenarios, and polls `/api/jobs` until each is
  terminal. `expect` holds the final state, the file count, the extension per task,
  and ffprobe `codec_name` / `codec_type` / `bit_rate` range / tags / attached_pic.
  Prints a PASS/FAIL table and exits non-zero on any failure. Takes `-k <name>` to
  run a subset.
- Refactor to allow testing: move the argv construction in `run_task` into a pure
  `task_argv(job, step) -> list[str]` so the unit tests can check it without
  spawning anything.
- Add the v2 section to the PRD.

**Validate**
- Smoke tests: the existing modes (merged, separate, audio, video) plus native / MP3
  / WAV all pass.
- A deliberately broken expectation makes the smoke run fail (proves the harness
  catches failures).

---

## Sprint 1: MP3 bitrate choice

**Goal:** the user picks MP3 quality instead of always getting V0.

**Server**
- Add `AUDIO_QUALITIES = {"best": "0", "320": "320K", "192": "192K", "128": "128K"}`,
  passed as `--audio-quality`.
  - `best` = LAME VBR V0. The fixed bitrates are CBR (yt-dlp turns values over 10
    into `-b:a <n>k`).
- Only applies to lossy targets. For `native` and `wav`, force `best` and leave the
  flag out.
- `Job.to_dict` gains `audio_quality`. The job subtitle shows "MP3 320k".

**UI**
- A "Bitrate" select next to Audio format. Enabled only when the format is lossy.
  The label shows "Best (VBR ~245k)".
- "Use these settings" restores it.

**Risks:** none worth noting. yt-dlp's `_quality_args` handles this directly.

**Validate**
- Smoke: MP3 at 320, 192 and 128 → ffprobe `bit_rate` within ±2% of the target.
  MP3 best → bit_rate > 128 kbps and varies (VBR).
- Unit: `{"audio_format":"wav","audio_quality":"320"}` → argv has no
  `--audio-quality`. `audio_quality:"999"` → 400.
- Manual: the Bitrate select turns on/off as format and mode change, and the job
  card shows the bitrate.

---

## Sprint 2: FLAC, M4A, Opus

**Goal:** more target formats without if-chains everywhere.

**Server**
- Replace `AUDIO_FORMATS` / `AUDIO_FORMAT_LABELS` with one spec table:
  ```python
  AudioFormat(key, label, ytdlp_codec, ext, lossless, bitrate_ok, art_ok)
  mp3  → ("mp3",  "mp3",  lossless=False, bitrate_ok=True,  art_ok=True)
  m4a  → ("m4a",  "m4a",  lossless=False, bitrate_ok=True,  art_ok=True)
  opus → ("opus", "opus", lossless=False, bitrate_ok=True,  art_ok=True)   # needs mutagen for art
  flac → ("flac", "flac", lossless=True,  bitrate_ok=False, art_ok=True)   # needs mutagen for art
  wav  → ("wav",  "wav",  lossless=True,  bitrate_ok=False, art_ok=False)
  ```
  `/api/config` publishes the table. The UI builds its options from it instead of
  hard-coding them.
- **Opus quirk:** yt-dlp maps VBR numbers (0–10) to settings only for
  lame/vorbis/aac. For opus, `best` does nothing. Map Opus `best` → `160K` so the
  setting always means something.
- **Same-codec quirk:** if the source is already Opus (typical on YouTube) or AAC,
  yt-dlp copies the stream instead of re-encoding, so the bitrate is ignored. Say so
  in the hint ("copied without re-encoding if the source is already Opus"). Don't
  fight it; it's the better outcome.

**UI:** options come from config, and a hint explains lossless vs lossy.

**Validate**
- Smoke: FLAC → `flac` codec, lossless. M4A 192 → `aac`, ~192k. Opus → `opus` codec
  in `.opus`. The WAV and MP3 scenarios from Sprint 1 still pass.
- Smoke on archive.org (MP3 source) → FLAC works (checks a non-YouTube source codec).
- Unit: the spec table drives `bitrate_ok` / the lossless rules, and there's a test
  per format.
- Manual: the dropdown lists all six formats, and FLAC/WAV disable Bitrate.

---

## Sprint 3: Keep original

**Goal:** optionally keep the untouched source stream next to the converted file.

**Server**
- `keep_original: true` → add `-k` (`--keep-video`, which also keeps the `-x`
  source).
- **Report both files.** `-O after_move:` prints only the final path. Add
  `-O video:@@O@@%(filename)s` (the planned download name, printed before
  download) and store both. `Task.filepath` becomes `Task.files: list[{path, role}]`
  with role `original` / `converted`. Keep `filepath` in `to_dict` as the last file
  for backward compatibility.
- **Collision guard:** if the source extension equals the target (e.g. m4a → m4a
  when YouTube serves AAC), yt-dlp overwrites in place and nothing is kept. We only
  learn the source format after the probe, so check in `check_formats`, which already
  has `info`:
  - predict the `bestaudio` ext
  - if it matches the target, keep the job going, drop `-k`, and record a job
    `notice` ("source was already M4A; nothing to keep")
- Only valid when a conversion happens. With `native`, the flag is ignored.

**UI:** a "Keep original" checkbox, enabled only when the format ≠ Original. The
task shows both paths, labelled. The notice appears as a muted line, not an error.

**Validate**
- Smoke: MP3 + keep → two files: `.audio.webm` (opus) and `.audio.mp3` (mp3), both
  listed in `task.files`. MP3 without keep → one file (regression).
- Smoke: archive.org MP3 source → MP3 + keep → one file plus a notice (collision path).
- Separate + MP3 + keep → three files total.
- Manual: the checkbox enables/disables correctly, and the card shows both files.

---

## Sprint 4: Embed thumbnail + tags

**Goal:** converted audio shows artwork, title and artist in music players.

**Server**
- `embed: true` → `--embed-metadata --embed-thumbnail --convert-thumbnails jpg`.
  Metadata alone also works for `native`.
- **Hard constraint:** yt-dlp *raises* (the job fails) when embedding art in WAV
  ("Supported filetypes for thumbnail embedding are: mp3, mkv/mka,
  ogg/opus/flac, m4a/mp4/m4v/mov"). So:
  - WAV → metadata only
  - `native` → art only if the predicted ext is m4a, otherwise metadata only
  - the job gets a `notice` either way
- **Dependency:** art for Opus/FLAC needs the `mutagen` Python module *inside the
  yt-dlp install*. It's missing here (`import mutagen` fails). Detect it at startup
  by parsing the `Optional libraries:` line from `yt-dlp -v --version`, publish
  `mutagen: bool` in config, and without it use metadata only for opus/flac plus a
  notice. Document `pacman -S python-mutagen` in the README.
- Progress: the PP marker already carries the postprocessor name. Map it to
  friendlier task states: `ExtractAudio` → converting, `EmbedThumbnail` →
  embedding art, `Metadata` → tagging.
- Artist: YouTube's `uploader` becomes `artist` through yt-dlp's default metadata
  mapping. No custom `--parse-metadata` in this sprint (possible follow-up).

**UI:** an "Embed cover art & tags" checkbox. Where art can't be embedded, the hint
says "tags only for WAV", and a chip in the top bar shows whether mutagen is
available.

**Validate**
- Smoke: MP3 + embed → ffprobe shows a `mjpeg` stream with `disposition:attached_pic=1`
  and non-empty `title`/`artist` tags. The thumbnail `.jpg`/`.webp` is **not** left
  in the folder.
- Smoke: WAV + embed → job `done` (not failed), with a notice and no attached pic.
- Smoke: FLAC + embed → attached pic if mutagen is present, otherwise done with a
  notice. Run it both ways: before and after installing `python-mutagen`.
- Smoke: M4A + embed → attached pic (`covr`).
- Manual: open the MP3 in a player (e.g. `mpv --no-video` or a file manager) and
  the artwork shows.

---

## Sprint 5: Playlists

**Goal:** a playlist URL downloads every item, for example a whole album to MP3.

**Design decisions**
- **Opt-in.** A `playlist: false` default keeps the current `--no-playlist`
  behavior, including for `watch?v=X&list=Y` URLs, which stay a single video.
- **Probe:** `yt-dlp -J --flat-playlist --yes-playlist` → entry count, titles and
  ids, without resolving every video (fast).
- **Run:** one yt-dlp process **per item**, `--yes-playlist -I <n>` against the
  playlist URL (not the item URL). This keeps `playlist_title` / `playlist_index`
  available for filenames and for track-number tags. It also reuses `run_task`,
  cancel and progress parsing as they are. Cost: yt-dlp re-reads the playlist page
  for each item. That's acceptable for a local tool; if it proves slow, switch to
  one process with `%(info.playlist_index)s` in the progress template.
- **Tasks:** one Task per entry, created after the probe (the plan is built lazily).
  In Separate mode each entry has two tasks.
- **Output:** `%(playlist_title)s/%(playlist_index)03d - %(title)s [%(id)s].audio.%(ext)s`,
  a subfolder per playlist.
- **Tags:** with `embed`, add `--parse-metadata "playlist_index:%(track_number)s"`
  and `--parse-metadata "playlist_title:%(album)s"`.
- **Failures:** one bad item (private, deleted or geo-blocked) marks that task
  failed and the run moves on. The job ends `done`, `partial` (some failed) or
  `failed` (all failed).
- **Guards:**
  - an entry cap (default 200, `--max-playlist-items` flag); over the cap →
    400-style error suggesting a range
  - an optional `items` range field passed to `-I` (e.g. `1-20`), validated with a
    strict regex
  - an optional `--download-archive <outdir>/.yt-dl-archive` so re-running a
    playlist skips items already downloaded
- **Concurrency:** a playlist job holds one slot and runs its items one after
  another. Cancel stops the current item and skips the rest.

**UI:** a "Download whole playlist" checkbox and an optional "Items" field. The job
card shows an overall "12 / 40 · 1 failed" bar. The item list is collapsed by
default, with failed items always visible, so a 200-item card stays usable.

**Validate**
- Smoke (fixture playlist, 3–5 short items): MP3 → N files in the playlist
  subfolder, numbered `001…`, with track-number tags matching the index.
- Smoke: a `watch?v=…&list=…` URL with `playlist:false` → exactly one file
  (regression).
- Smoke: `items:"2-3"` → exactly 2 files.
- Smoke: running the same playlist again with the archive on → every item is
  skipped, and the job is `done` quickly.
- Smoke: a playlist containing an unavailable item (or a simulated one via a bad id
  in a unit-level fake) → `partial`, and the other files are present.
- Smoke: cancel after item 1 finishes → item 1 file exists, the rest `cancelled`,
  and no yt-dlp processes remain (`pgrep`).
- Unit: over the cap → error. Bad `items` string (`1;rm`) → 400.
- Manual: a 40+ item playlist card stays readable, and the counts update live.

---

## Sprint 6: Keep jobs after restart

**Goal:** a restart no longer wipes the job list, and interrupted jobs can be retried.

**Server**
- State file: `${XDG_STATE_HOME:-~/.local/state}/yt-dl-gui/jobs.json`
  (`--state-file` flag), with a `schema: 1` field.
- Writes: `bump()` sets a dirty flag, and a writer thread saves at most every 2 s,
  plus once on shutdown. The save is atomic (tmp file + `os.replace`). Only
  `to_dict()` data is saved, never process handles.
- Loading at startup:
  - jobs that were `queued` / `probing` / `downloading` / `converting` become
    `interrupted`
  - their active tasks also become `interrupted`
  - each task file gets `exists: bool` from a stat check
- Corrupt or unreadable file → rename it to `jobs.json.bad-<timestamp>`, start
  empty, and log a warning. It must never block startup.
- History cap: newest 200 jobs.
- New endpoints:
  - `POST /api/jobs/{id}/retry` → a new job with the same settings. yt-dlp resumes
    existing `.part` files, and the playlist archive skips finished items.
  - `DELETE /api/jobs/{id}`, and `POST /api/jobs/clear` (removes finished/failed
    jobs from the list; files are never touched)

**UI:** `interrupted` state styling, a Retry button, a "Clear finished" button, and
a "file missing" marker.

**Validate**
- Smoke: finish a job → restart the server → the job is still listed as `done` with
  the same files.
- Smoke: start a long download → `kill -9` the server → restart → job
  `interrupted`. Retry → it finishes, and the log shows yt-dlp resuming (`Resuming
  download at byte`).
- Smoke: write garbage to the state file → the server starts, the list is empty,
  and a `.bad-*` backup exists.
- Smoke: delete a downloaded file → restart → the task shows `exists:false`.
- Unit: the 200-job cap. Only terminal jobs are cleared.
- Manual: Retry/Clear buttons work, and a page refresh after restart shows history.

---

## Cross-cutting risks

| Risk | Mitigation |
|---|---|
| YouTube bot-checks / rate limits during repeated smoke runs | Short test media, `-k` subsets while iterating, full run only at the gate. Fall back to archive.org scenarios if YouTube blocks. |
| System `yt-dlp` updates change behavior between sprints | Record `yt-dlp --version` in each smoke report. Pin the version noted in the README. |
| Option combinations multiply (6 formats × 4 bitrates × keep × embed × modes) | The spec table plus server-side normalization is the single source of truth. The unit tests go through the full matrix, and the smoke tests cover one real case per rule. |
| Lossy-to-lossy conversion surprises users | The hint text states it, and Original stays the default. |
| Playlist jobs hold a slot for a long time | Documented. Future option: a separate cap for playlist jobs. |

## Out of scope (noted for later)

Custom `--parse-metadata` rules, square-cropping cover art, a separate concurrency
cap for playlists, cookies/login, trimming.
