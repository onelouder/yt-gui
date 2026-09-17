# Sprint Plan: v2 Audio & Batch Features

**Date:** 2026-09-17 · **Status: all sprints delivered** the same day.
**Builds on:** `PRD-download-gui.md` (v1) and the MP3/WAV option added 2026-09-17
**Scope:** six features, one sprint each, plus a Sprint 0 that sets up testing.

Each sprint is one commit, tagged, and passed its gate before the next began. The
plan below is kept as written; **[What actually changed](#what-actually-changed)**
records where reality differed.

| # | Feature | Size | Sprint | Tag |
|---|---|---|---|---|
| — | Test harness + baseline | S | 0 | `v2-sprint-0` ✅ |
| 1 | MP3 bitrate choice (V0 / 320 / 192 / 128) | S | 1 | `v2-sprint-1` ✅ |
| 2 | More formats: FLAC, M4A, Opus | S | 2 | `v2-sprint-2` ✅ |
| 3 | Keep the original alongside the converted file | S–M | 3 | `v2-sprint-3` ✅ |
| 4 | Embed thumbnail + tags | M | 4 | `v2-sprint-4` ✅ |
| 5 | Playlist downloads | L | 5 | `v2-sprint-5` ✅ |
| 6 | Keep jobs after a server restart | M | 6 | `v2-sprint-6` ✅ |

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
3. **UI checks** — planned as a manual browser checklist, built instead as
   `node app/tests/ui_check.mjs`: it drives the real page in headless Chromium over
   the DevTools protocol (no npm dependencies), and `--shot` saves a screenshot per
   check. Each sprint's checklist became checks in that file.
4. **README** updated (options table, API table, known limits).
5. **Commit**, tagged `v2-sprint-N`, so any sprint can be rolled back on its own.

Test media: YouTube `jNQXAC9IVRw` ("Me at the zoo": 19 s, stable, public). Also one
archive.org item, which keeps PRD criterion 7 (two extractors). Sprint 5 adds a
small public playlist, chosen and recorded in `app/tests/fixtures.json` at the
start of that sprint — `PL6IaIsEjSbf96XFRuNccS_RuEXwNdsoEu` ("JODA15", 4 items), plus
archive.org `testmp3testfile` (12 s MP3) as the second extractor.

Final tally: 53 unit tests, 35 live scenarios, 14 UI checks.

---

## ✅ Sprint 0: Test harness + baseline

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

## ✅ Sprint 1: MP3 bitrate choice

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

## ✅ Sprint 2: FLAC, M4A, Opus

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

## ✅ Sprint 3: Keep original

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

## ✅ Sprint 4: Embed thumbnail + tags

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

## ✅ Sprint 5: Playlists

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

## ✅ Sprint 6: Keep jobs after restart

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

## What actually changed

Findings from building it, in the order they surfaced. The code and `app/README.md`
describe the shipped behavior; this section says where it departs from the plan above.

**Sprint 0 — a pre-existing bug.** The first live run failed on archive.org: **Audio
only** with Original format used `bestaudio`, which matches nothing when an extractor
leaves codecs unset. Fixed with a fallback selector that accepts a single file whose
extension looks like audio. This bug predates v2.

**Sprint 1/2 — the format spec table arrived one sprint early.** The bitrate rules
needed it, so `AudioFormat` landed in Sprint 1 rather than being written twice.

**Sprint 2 — "best" per encoder, and stream copies.** yt-dlp maps VBR numbers only for
LAME/vorbis/AAC, so Opus "best" means 160 kbps and M4A "best" 256 kbps. When the source
already uses the target codec (YouTube → Opus, archive.org → MP3), yt-dlp copies the
stream and the bitrate is ignored — the app now prints the source codec
(`-O video:%(acodec)s`) and says so on the task.

**Sprint 3 — no collision guard needed.** The plan assumed re-encoding to the same
extension would overwrite the source. It doesn't: yt-dlp either skips the conversion
(reporting "already in target format") or renames the source `<name>.orig.<ext>`. So
there is no prediction step in `check_formats`; the task simply reports the files it
found, and notes when there is no separate original to keep.

**Sprint 4 — two real defects, both caught by the tests.**
- ffmpeg 9 writes `.m4a` through its `ipod` muxer, which rejects JPEG covers, so
  yt-dlp's ffmpeg fallback for M4A art fails outright. M4A art now needs mutagen or
  AtomicParsley, like Opus and FLAC — the plan had assumed ffmpeg would cover it.
- `-k` keeps *every* intermediate, including the pre-conversion thumbnail, leaving a
  stray `.webp`. With Keep original on, `--convert-thumbnails` is skipped so yt-dlp
  converts internally and cleans up (the cover is PNG instead of JPEG).

Also: the fixture video has real YouTube chapters, which `--embed-metadata` embeds —
in MP4 that appears as an extra text track, so it is expected, not a fault.

**Sprint 5 — a placeholder bug the UI check found.** Playlist jobs were created with a
single placeholder task, so a card briefly showed a fake item before the probe replaced
the plan. Playlist jobs now start with no tasks. The card's auto-expand also overrode a
deliberate collapse; the user's choice now wins.

**Sprint 6 — SIGTERM was ignored.** Only Ctrl+C ran the shutdown path, so a normal stop
(`systemctl`, `kill`, a test harness) lost up to two seconds of history and left
finished jobs recorded as running. SIGTERM now takes the same exit path.

**Cover art with mutagen installed** was verified after the fact. `python-mutagen`
1.48.1 was installed later the same day, the server's detection picked it up, and the
13 conversion/tagging scenarios were re-run: all passed, this time *requiring* art in
Opus, FLAC and M4A. Checked independently with ffprobe (an `mjpeg` stream with
`attached_pic`) and with mutagen itself. So both paths are exercised: the earlier runs
covered the tags-only fallback, these cover the full path. See the Sprint 4 section of
`app/README.md` for what each format needs.

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
