"""Offline unit tests: argv building, request validation, progress parsing.

Run: python3 -m unittest discover app/tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._saved = (server.ALLOWED_ROOT, server.DEFAULT_OUTDIR, server.FFMPEG, server.FFPROBE, server.YTDLP,
                       server.ATOMICPARSLEY)
        server.ALLOWED_ROOT = self.root
        server.DEFAULT_OUTDIR = self.root / "dl"
        server.FFMPEG = "/usr/bin/ffmpeg"
        server.FFPROBE = "/usr/bin/ffprobe"
        server.YTDLP = "/usr/bin/yt-dlp"
        self._mutagen = server.MUTAGEN
        server.MUTAGEN = False  # never shell out to detect it in unit tests

    def tearDown(self):
        (server.ALLOWED_ROOT, server.DEFAULT_OUTDIR, server.FFMPEG, server.FFPROBE, server.YTDLP,
         server.ATOMICPARSLEY) = self._saved
        server.MUTAGEN = self._mutagen
        self._tmp.cleanup()

    def job(self, **kw):
        data = {"url": URL, "outdir": str(self.root / "out")}
        data.update(kw)
        return server.new_job(data)

    def argvs(self, job):
        return [server.task_argv(job, step) for step in job.plan]


class PlanTests(Base):
    def test_modes_task_keys(self):
        expect = {
            "merged": ["merged"],
            "separate": ["video", "audio"],
            "audio": ["audio"],
            "video": ["video"],
        }
        for mode, keys in expect.items():
            with self.subTest(mode=mode):
                self.assertEqual([t.key for t in self.job(mode=mode).tasks], keys)

    def test_url_is_last_after_double_dash(self):
        for mode in server.MODES:
            for argv in self.argvs(self.job(mode=mode)):
                self.assertEqual(argv[-2:], ["--", URL])

    def test_quality_filters_video_selector(self):
        job = self.job(mode="video", quality="720")
        self.assertEqual(job.plan[0].fmt, "bestvideo[height<=720]")
        job = self.job(mode="merged", quality="480")
        self.assertIn("bestvideo[height<=480]+bestaudio", job.plan[0].fmt)

    def test_native_audio_has_no_extract(self):
        argv = self.argvs(self.job(mode="audio"))[0]
        self.assertNotIn("-x", argv)

    def test_convert_formats_add_extract(self):
        for fmt in ("mp3", "wav"):
            with self.subTest(fmt=fmt):
                argv = self.argvs(self.job(mode="audio", audio_format=fmt))[0]
                i = argv.index("--audio-format")
                self.assertIn("-x", argv)
                self.assertEqual(argv[i + 1], fmt)

    def test_separate_converts_only_audio(self):
        video, audio = self.argvs(self.job(mode="separate", audio_format="mp3"))
        self.assertNotIn("-x", video)
        self.assertIn("-x", audio)

    def test_audio_format_ignored_for_video_modes(self):
        for mode in ("merged", "video"):
            with self.subTest(mode=mode):
                job = self.job(mode=mode, audio_format="mp3")
                self.assertEqual(job.audio_format, "native")
                self.assertTrue(all("-x" not in a for a in self.argvs(job)))


class BitrateTests(Base):
    def quality_arg(self, **kw):
        argv = self.argvs(self.job(mode="audio", **kw))[0]
        return argv[argv.index("--audio-quality") + 1] if "--audio-quality" in argv else None

    def test_mp3_bitrates(self):
        self.assertEqual(self.quality_arg(audio_format="mp3"), "0")
        for q in ("320", "192", "128"):
            with self.subTest(q=q):
                self.assertEqual(self.quality_arg(audio_format="mp3", audio_quality=q), f"{q}K")

    def test_bitrate_ignored_where_meaningless(self):
        for fmt in ("wav", "native"):
            with self.subTest(fmt=fmt):
                job = self.job(mode="audio", audio_format=fmt, audio_quality="320")
                self.assertEqual(job.audio_quality, "best")
                self.assertIsNone(self.quality_arg(audio_format=fmt, audio_quality="320"))

    def test_unknown_bitrate_rejected(self):
        with self.assertRaises(ValueError):
            self.job(mode="audio", audio_format="mp3", audio_quality="999")

    def test_labels(self):
        self.assertEqual(self.job(mode="audio", audio_format="mp3", audio_quality="192").to_dict()["audio_label"], "MP3 192k")
        self.assertEqual(self.job(mode="audio", audio_format="wav").to_dict()["audio_label"], "WAV")


class FormatTests(Base):
    def argv(self, **kw):
        return self.argvs(self.job(mode="audio", **kw))[0]

    def opt(self, argv, name):
        return argv[argv.index(name) + 1] if name in argv else None

    def test_every_convert_format(self):
        expect = {"mp3": "0", "m4a": "256K", "opus": "160K", "flac": None, "wav": None}
        for key, best in expect.items():
            with self.subTest(fmt=key):
                argv = self.argv(audio_format=key)
                self.assertEqual(self.opt(argv, "--audio-format"), key)
                self.assertEqual(self.opt(argv, "--audio-quality"), best)

    def test_lossless_flags(self):
        for key in ("flac", "wav"):
            f = server.AUDIO_FORMATS[key]
            self.assertTrue(f.lossless)
            self.assertFalse(f.bitrate_ok)
        self.assertFalse(server.AUDIO_FORMATS["native"].bitrate_ok)

    def test_codec_family(self):
        cf = server.codec_family
        self.assertEqual(cf("opus", "webm"), "opus")
        self.assertEqual(cf("mp4a.40.2", "m4a"), "aac")
        self.assertEqual(cf(None, "mp3"), "mp3")
        self.assertEqual(cf("NA", "m4a"), "aac")
        self.assertIsNone(cf("none", "webm"))

    def test_source_notes(self):
        step = self.job(mode="audio", audio_format="opus", audio_quality="128").plan[0]
        self.assertIn("bitrate not applied", server.source_note(step, "opus", "webm"))
        self.assertIsNone(server.source_note(step, "mp4a.40.2", "m4a"))
        mp3 = self.job(mode="audio", audio_format="mp3").plan[0]
        self.assertEqual(server.source_note(mp3, "NA", "mp3"), "Source is already MP3: copied without re-encoding.")
        wav = self.job(mode="audio", audio_format="wav").plan[0]
        self.assertIsNone(server.source_note(wav, "pcm_s16le", "wav"))
        native = self.job(mode="audio").plan[0]
        self.assertIsNone(server.source_note(native, "opus", "webm"))


class KeepOriginalTests(Base):
    def test_flag_only_when_converting(self):
        argv = self.argvs(self.job(mode="audio", audio_format="mp3", keep_original=True))[0]
        self.assertIn("-k", argv)
        for kw in ({"mode": "audio"}, {"mode": "merged", "audio_format": "mp3"}):
            with self.subTest(**kw):
                job = self.job(keep_original=True, **kw)
                self.assertFalse(job.keep_original)
                self.assertTrue(all("-k" not in a for a in self.argvs(job)))

    def test_separate_keeps_only_audio(self):
        video, audio = self.argvs(self.job(mode="separate", audio_format="flac", keep_original=True))
        self.assertNotIn("-k", video)
        self.assertIn("-k", audio)

    def test_rejects_non_bool(self):
        with self.assertRaises(ValueError):
            self.job(mode="audio", audio_format="mp3", keep_original="yes")

    def _files(self, names, source, final, keep=True):
        for n in names:
            (self.root / n).write_bytes(b"x")
        step = self.job(mode="audio", audio_format="mp3", keep_original=keep).plan[0]
        step.task.source_path = str(self.root / source)
        step.task.filepath = str(self.root / final)
        return server.collect_files(step, step.task), step.task.notes

    def test_collect_both(self):
        files, notes = self._files(["a.webm", "a.mp3"], "a.webm", "a.mp3")
        self.assertEqual([f["role"] for f in files], ["original", "converted"])
        self.assertEqual(notes, [])

    def test_collect_copy_in_place(self):
        files, notes = self._files(["a.mp3"], "a.mp3", "a.mp3")
        self.assertEqual(len(files), 1)
        self.assertIn("no separate original", notes[0])

    def test_collect_orig_rename(self):
        files, notes = self._files(["a.orig.m4a", "a.m4a"], "a.m4a", "a.m4a")
        self.assertEqual([f["path"] for f in files], [str(self.root / "a.orig.m4a"), str(self.root / "a.m4a")])
        self.assertEqual(notes, [])


class EmbedTests(Base):
    def run_embed(self, fmt, mode="audio"):
        job = self.job(mode=mode, audio_format=fmt, embed=True)
        return self.argvs(job)[-1], job.tasks[-1].notes

    def test_art_where_ffmpeg_can(self):
        for fmt in ("mp3",):
            with self.subTest(fmt=fmt):
                argv, notes = self.run_embed(fmt)
                self.assertIn("--embed-metadata", argv)
                self.assertIn("--embed-thumbnail", argv)
                self.assertEqual(argv[argv.index("--convert-thumbnails") + 1], "jpg")
                self.assertEqual(notes, [])

    def test_tags_only_where_art_would_fail(self):
        server.ATOMICPARSLEY = None
        expect = {"wav": "can't hold cover art", "opus": "mutagen", "flac": "mutagen", "m4a": "AtomicParsley",
                  "native": "only embedded when converting"}
        for fmt, text in expect.items():
            with self.subTest(fmt=fmt):
                argv, notes = self.run_embed(fmt)
                self.assertIn("--embed-metadata", argv)
                self.assertNotIn("--embed-thumbnail", argv)
                self.assertIn(text, notes[0])

    def test_keep_original_skips_thumbnail_conversion(self):
        argv = self.argvs(self.job(mode="audio", audio_format="mp3", embed=True, keep_original=True))[0]
        self.assertIn("--embed-thumbnail", argv)
        self.assertNotIn("--convert-thumbnails", argv)

    def test_atomicparsley_enables_m4a_art(self):
        server.ATOMICPARSLEY = "/usr/bin/AtomicParsley"
        argv, notes = self.run_embed("m4a")
        self.assertIn("--embed-thumbnail", argv)
        self.assertEqual(notes, [])

    def test_hint_for_embed_failure(self):
        hint = server.hint_for("ERROR: Postprocessing: Unable to embed using ffprobe & ffmpeg; Conversion failed!")
        self.assertIn("Cover art", hint)

    def test_mutagen_enables_opus_flac_art(self):
        server.MUTAGEN = True
        for fmt in ("opus", "flac", "m4a"):
            with self.subTest(fmt=fmt):
                argv, notes = self.run_embed(fmt)
                self.assertIn("--embed-thumbnail", argv)
                self.assertEqual(notes, [])

    def test_only_audio_steps(self):
        video, audio = self.argvs(self.job(mode="separate", audio_format="mp3", embed=True))
        self.assertNotIn("--embed-metadata", video)
        self.assertIn("--embed-metadata", audio)
        job = self.job(mode="merged", embed=True)
        self.assertFalse(job.embed)
        self.assertNotIn("--embed-metadata", self.argvs(job)[0])

    def test_rejects_non_bool(self):
        with self.assertRaises(ValueError):
            self.job(mode="audio", embed=1)


class PlaylistTests(Base):
    def entries(self, n=3, start=1):
        return {"_type": "playlist", "title": "Mix", "playlist_count": n,
                "requested_entries": list(range(start, start + n)),
                "entries": [{"id": f"v{i}", "title": f"Song {i}"} for i in range(start, start + n)]}

    def plan(self, info=None, **kw):
        kw.setdefault("mode", "audio")
        job = self.job(playlist=True, **kw)
        job.plan = server.playlist_plan(job, server.playlist_entries(job, info or self.entries()))
        job.tasks = [s.task for s in job.plan]
        return job

    def test_no_tasks_before_the_probe(self):
        job = self.job(playlist=True, mode="audio")
        self.assertEqual((job.plan, job.tasks), ([], []))

    def test_single_video_url_with_playlist_ticked(self):
        real = server.probe
        server.probe = lambda job, flat=False: {"_type": "video", "title": "One", "formats": []}
        try:
            job = self.job(playlist=True, mode="audio")
            server.prepare(job)
        finally:
            server.probe = real
        self.assertFalse(job.playlist)
        self.assertEqual([t.key for t in job.tasks], ["audio"])
        self.assertEqual(job.title, "One")

    def test_items_validation(self):
        for good in ("1-10", "1,4,7-9", "3"):
            self.assertEqual(self.job(playlist=True, items=good).items, good)
        for bad in ("1;rm -rf /", "abc", "1-", "-2", "1..3", "1 2"):
            with self.subTest(items=bad), self.assertRaises(ValueError):
                self.job(playlist=True, items=bad)

    def test_items_ignored_without_playlist(self):
        job = self.job(items="1-3", skip_existing=True)
        self.assertEqual((job.items, job.skip_existing), ("", False))

    def test_one_step_per_entry_with_indices(self):
        job = self.plan(self.entries(3, start=5))
        self.assertEqual([t.key for t in job.tasks], ["5:audio", "6:audio", "7:audio"])
        self.assertEqual(job.tasks[0].label, "005 · Song 5")
        for i, step in zip((5, 6, 7), job.plan):
            argv = server.task_argv(job, step)
            self.assertNotIn("--no-playlist", argv)
            self.assertEqual(argv[argv.index("-I") + 1], str(i))
            self.assertIn("--yes-playlist", argv)
            self.assertIn("%(playlist_index)03d", step.outtmpl)

    def test_separate_makes_two_tasks_per_entry(self):
        job = self.plan(self.entries(2), mode="separate")
        self.assertEqual([t.key for t in job.tasks], ["1:video", "1:audio", "2:video", "2:audio"])
        self.assertTrue(job.tasks[0].label.endswith("Video"))

    def test_archive_per_kind(self):
        job = self.plan(self.entries(1), mode="separate", audio_format="mp3", skip_existing=True)
        archives = [Path(s.archive).name for s in job.plan]
        self.assertEqual(archives, [".yt-gui-archive-video.txt", ".yt-gui-archive-audio-mp3.txt"])
        job = self.plan(self.entries(1))
        self.assertIsNone(job.plan[0].archive)

    def test_track_tags_when_embedding(self):
        job = self.plan(self.entries(1), audio_format="mp3", embed=True)
        argv = server.task_argv(job, job.plan[0])
        self.assertIn("%(playlist_index)s:%(track_number)s", argv)
        self.assertIn("%(playlist_title)s:%(album)s", argv)

    def test_cap_and_empty(self):
        job = self.job(playlist=True)
        with self.assertRaises(RuntimeError) as cm:
            server.playlist_entries(job, self.entries(server.MAX_PLAYLIST_ITEMS + 1))
        self.assertIn("above the", str(cm.exception))
        with self.assertRaises(RuntimeError):
            server.playlist_entries(job, {"entries": []})

    def test_one_bad_item_leaves_the_rest(self):
        job = self.plan(self.entries(3))
        real = server.run_task

        def fake(j, step):
            if step.index == 2:
                step.task.state = "failed"
                step.task.error = "boom"
                raise RuntimeError("boom")
            step.task.state = "done"

        server.run_task = fake
        try:
            server.run_plan(job)
        finally:
            server.run_task = real
        self.assertEqual(job.state, "partial")
        self.assertEqual([t.state for t in job.tasks], ["done", "failed", "done"])
        self.assertIn("1 of 3", job.error)

    def test_all_bad_items_fail_the_job(self):
        job = self.plan(self.entries(2))

        def fake(j, step):
            raise RuntimeError("boom")

        real, server.run_task = server.run_task, fake
        try:
            with self.assertRaises(RuntimeError):
                server.run_plan(job)
        finally:
            server.run_task = real


class ValidationTests(Base):
    def assertRejected(self, **kw):
        with self.assertRaises(ValueError):
            self.job(**kw)

    def test_bad_inputs(self):
        self.assertRejected(url="")
        self.assertRejected(url="ftp://example.com/x")
        self.assertRejected(url="file:///etc/passwd")
        self.assertRejected(mode="bogus")
        self.assertRejected(quality="4320")
        self.assertRejected(audio_format="flac-ish")

    def test_outdir_rules(self):
        self.assertRejected(outdir="relative/path")
        self.assertRejected(outdir="/etc")
        job = self.job(outdir=str(self.root / "a" / "b"))
        self.assertTrue(Path(job.outdir).is_dir())

    def test_conversion_needs_ffmpeg(self):
        server.FFPROBE = None
        self.assertRejected(mode="audio", audio_format="mp3")
        self.job(mode="audio")  # native still fine

    def test_merged_needs_ffmpeg(self):
        server.FFMPEG = None
        self.assertRejected(mode="merged")


class ProgressTests(unittest.TestCase):
    def test_monotonic_across_streams(self):
        t = server.Task("merged", "x")
        server.parse_progress(t, "downloading|500|1000|NA|10|5")
        server.parse_progress(t, "finished|1000|1000|NA|NA|NA")
        self.assertEqual(t.downloaded, 1000)
        server.parse_progress(t, "downloading|100|400|NA|10|5")  # second stream starts
        self.assertEqual(t.downloaded, 1100)
        self.assertEqual(t.total, 1400)

    def test_na_fields(self):
        t = server.Task("audio", "x")
        server.parse_progress(t, "downloading|NA|NA|NA|NA|NA")
        self.assertIsNone(t.percent)
        server.parse_progress(t, "short")  # malformed: ignored


if __name__ == "__main__":
    unittest.main()
