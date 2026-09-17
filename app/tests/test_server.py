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
        self._saved = (server.ALLOWED_ROOT, server.DEFAULT_OUTDIR, server.FFMPEG, server.FFPROBE, server.YTDLP)
        server.ALLOWED_ROOT = self.root
        server.DEFAULT_OUTDIR = self.root / "dl"
        server.FFMPEG = "/usr/bin/ffmpeg"
        server.FFPROBE = "/usr/bin/ffprobe"
        server.YTDLP = "/usr/bin/yt-dlp"

    def tearDown(self):
        (server.ALLOWED_ROOT, server.DEFAULT_OUTDIR, server.FFMPEG, server.FFPROBE, server.YTDLP) = self._saved
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
