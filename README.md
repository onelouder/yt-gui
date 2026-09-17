# yt-gui

A small local web GUI for [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a URL,
choose what you want (merged video, separate video and audio, audio only, or video
only), choose a folder, and download. Everything runs on your own machine.

- **Standard library only:** one Python file for the server and a static page. Nothing to `pip install`, no build step.
- **Four download modes** plus a quality cap (best / 1080p / 720p / 480p).
- **Audio conversion** to **MP3**, **M4A**, **Opus** (choice of bitrate), **FLAC** or **WAV** using ffmpeg, or keep the original stream.
- **Live progress** for each file (percent, speed, ETA) over Server-Sent Events, and a cancel button.
- **Clear errors**, with hints for bot checks, age limits, geo-blocking and missing ffmpeg.
- **Safe by default:** binds to `127.0.0.1`, keeps output paths under an allowed root, and never passes input through a shell.

## Requirements

- Python 3.11+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp#installation) on `PATH`
- [`ffmpeg`](https://ffmpeg.org/) and `ffprobe` on `PATH` (needed for Merged mode and
  MP3/WAV conversion; the UI disables those options when they're missing)

## Quick start

```sh
git clone https://github.com/onelouder/yt-gui.git
cd yt-gui
python3 app/server.py
```

Open <http://localhost:8723>.

```sh
python3 app/server.py --port 9000 \
                      --outdir ~/Music \      # default download folder
                      --root ~ \              # output paths must be inside this
                      --max-concurrent 3
```

## Modes

| Mode | Result |
|---|---|
| Merged | One file with video and audio (needs ffmpeg) |
| Separate | `<title> [<id>].video.<ext>` and `<title> [<id>].audio.<ext>`, never merged |
| Audio only | `<title> [<id>].audio.<ext>` |
| Video only | `<title> [<id>].video.<ext>`, no audio track |

**Audio format** applies to Audio only and to the audio file in Separate mode:

| Choice | Output |
|---|---|
| Original | The source stream as-is (`.webm` / `.m4a`), no re-encoding |
| MP3 | Best (LAME VBR V0, about 245 kbps), or 320 / 192 / 128 kbps |
| M4A | AAC. Best (256 kbps), or 320 / 192 / 128 kbps |
| Opus | Best (160 kbps), or 320 / 192 / 128 kbps |
| FLAC | Lossless |
| WAV | 16-bit PCM, uncompressed |

If the source already uses the chosen codec, the stream is copied without re-encoding
and the job card says so.

## Security

This is a **single-user local tool with no authentication**. Any user on the same
machine can reach the port. The server:

- binds to `127.0.0.1` and rejects requests with a non-local `Host` header
- only writes to absolute paths inside `--root` (default: your home directory)
- runs `yt-dlp` with an argument list, and always passes the URL as a single argument after `--`

Don't expose it to a network.

## Documentation

- [`app/README.md`](app/README.md): how it works, the HTTP API, known limits
- [`PRD-download-gui.md`](PRD-download-gui.md): the original product requirements
- [`SPRINT-PLAN-v2.md`](SPRINT-PLAN-v2.md): roadmap for bitrate choice, FLAC/M4A/Opus,
  keeping originals, cover art and tags, playlists, and saved job history

## Legal

yt-gui is a front end for yt-dlp and doesn't download anything by itself. Only
download content you have the right to download, and follow the terms of the sites
you use.

## License

[MIT](LICENSE) © 2026 Jason Wells
