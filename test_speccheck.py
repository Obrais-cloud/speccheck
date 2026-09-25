#!/usr/bin/env python3
"""Unit tests for speccheck's evaluation logic (runs without ffmpeg).

  python3 -m unittest -q test_speccheck
"""
import subprocess
import sys
import unittest
from pathlib import Path

import speccheck as sc

HERE = Path(__file__).resolve().parent


class FpsParsing(unittest.TestCase):
    def test_common_rates(self):
        self.assertEqual(sc._fps("25/1"), 25.0)
        self.assertEqual(sc._fps("30000/1001"), 29.97)
        self.assertEqual(sc._fps("24/1"), 24.0)

    def test_degenerate(self):
        self.assertIsNone(sc._fps("0/0"))
        self.assertIsNone(sc._fps(None))
        self.assertIsNone(sc._fps("garbage"))


class Operators(unittest.TestCase):
    def test_eq_numeric_with_tolerance(self):
        self.assertTrue(sc._passes(25.0, "eq", 25, 0.01))
        self.assertTrue(sc._passes(24.999, "eq", 25, 0.01))
        self.assertFalse(sc._passes(30, "eq", 25, 0.01))

    def test_eq_string_caseless(self):
        self.assertTrue(sc._passes("ProRes", "eq", "prores", 0))
        self.assertFalse(sc._passes("h264", "eq", "prores", 0))

    def test_in(self):
        self.assertTrue(sc._passes("h264", "in", ["h264", "hevc"], 0))
        self.assertFalse(sc._passes("prores", "in", ["h264", "hevc"], 0))

    def test_range_min_max(self):
        self.assertTrue(sc._passes(-23, "range", [-24, -22], 0))
        self.assertFalse(sc._passes(-21, "range", [-24, -22], 0))
        self.assertTrue(sc._passes(-1.5, "max", -1.0, 0))   # true peak below ceiling
        self.assertFalse(sc._passes(-0.5, "max", -1.0, 0))
        self.assertTrue(sc._passes(1080, "min", 720, 0))

    def test_none_never_passes(self):
        self.assertFalse(sc._passes(None, "eq", 25, 0))


class DottedGet(unittest.TestCase):
    def test_nested(self):
        d = {"video": {"width": 1920}, "container": "mov"}
        self.assertEqual(sc._get(d, "video.width"), 1920)
        self.assertEqual(sc._get(d, "container"), "mov")
        self.assertIsNone(sc._get(d, "video.missing"))
        self.assertIsNone(sc._get(d, "audio.channels"))  # whole branch absent


class CheckStatuses(unittest.TestCase):
    SPEC = {
        "requirements": {
            "codec":   {"field": "video.codec", "op": "in", "expected": ["prores"], "severity": "fail"},
            "fps":     {"field": "video.fps", "op": "eq", "expected": 25, "tolerance": 0.01, "severity": "fail"},
            "pixfmt":  {"field": "video.pix_fmt", "op": "in", "expected": ["yuv422p10le"], "severity": "warn"},
            "missing": {"field": "loudness.integrated_lufs", "op": "range", "expected": [-24, -22], "severity": "fail"},
        }
    }

    def test_pass_fail_warn_and_missing(self):
        data = {"video": {"codec": "h264", "fps": 25.0, "pix_fmt": "yuv420p"}}
        by = {r["check"]: r["status"] for r in sc.check(data, self.SPEC)}
        self.assertEqual(by["codec"], "FAIL")    # wrong codec, severity fail
        self.assertEqual(by["fps"], "PASS")      # exact
        self.assertEqual(by["pixfmt"], "WARN")   # wrong pix_fmt, severity warn
        self.assertEqual(by["missing"], "WARN")  # unmeasured -> warn, not fail

    def test_report_exit_code(self):
        data = {"video": {"codec": "prores", "fps": 25.0, "pix_fmt": "yuv422p10le"},
                "loudness": {"integrated_lufs": -23}}
        rc = sc.report("x.mov", self.SPEC, sc.check(data, self.SPEC))
        self.assertEqual(rc, 0)  # all pass


class Presets(unittest.TestCase):
    def test_all_presets_load(self):
        for p in sorted((HERE / "specs").glob("*.yaml")):
            spec = sc.load_spec(str(p))
            self.assertIn("requirements", spec)
            for name, req in spec["requirements"].items():
                self.assertIn("field", req, f"{p.name}:{name}")
                self.assertIn(req.get("op", "eq"),
                              {"eq", "in", "range", "min", "max"}, f"{p.name}:{name}")


class Faststart(unittest.TestCase):
    def test_detection(self):
        s = HERE / "samples"
        if not (s / "web-ok.mp4").exists() or not (s / "bad.mp4").exists():
            self.skipTest("run ./gen-samples.sh first")
        self.assertIs(sc.mp4_faststart(str(s / "web-ok.mp4")), True)
        self.assertIs(sc.mp4_faststart(str(s / "bad.mp4")), False)

    def test_non_mp4_is_none(self):
        self.assertIsNone(sc.mp4_faststart(str(HERE / "speccheck.py")))  # not ISO-BMFF


class SpecAll(unittest.TestCase):
    def test_find_videos_and_all_specs(self):
        vids = sc.find_videos(HERE / "samples")
        if not vids:
            self.skipTest("run ./gen-samples.sh first")
        self.assertTrue(all(v.suffix.lower() in sc.VIDEO_EXTS for v in vids))
        self.assertFalse(any(v.name.startswith("._") for v in vids))
        specs = sc.all_specs()
        self.assertGreaterEqual(len(specs), 4)
        self.assertTrue(all("requirements" in s for _, s in specs))

    def test_evaluate_file_against_all(self):
        sample = HERE / "samples" / "web-ok.mp4"
        if not sample.exists() or subprocess.run(["which", "ffprobe"],
                                                 capture_output=True).returncode != 0:
            self.skipTest("no sample or ffprobe")
        rows = sc.evaluate_file(sample, sc.all_specs(), no_loudness=True)
        self.assertEqual(len(rows), len(sc.all_specs()))
        # a clean 1080p H.264 clip should satisfy at least one spec with no fails
        self.assertTrue(any(r["fails"] == 0 for r in rows))

    def test_all_cli_on_folder(self):
        folder = HERE / "samples"
        if not sc.find_videos(folder) or subprocess.run(["which", "ffprobe"],
                                                        capture_output=True).returncode != 0:
            self.skipTest("no samples or ffprobe")
        r = subprocess.run([sys.executable, str(HERE / "speccheck.py"), str(folder),
                            "--all", "--no-loudness"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("match at least one spec", r.stdout)


class CliSmoke(unittest.TestCase):
    """End-to-end against the generated sample, only if it exists + ffprobe present."""
    def test_good_sample_passes(self):
        sample = HERE / "samples" / "good.mov"
        if not sample.exists() or subprocess.run(["which", "ffprobe"],
                                                 capture_output=True).returncode != 0:
            self.skipTest("no sample or ffprobe")
        r = subprocess.run([sys.executable, str(HERE / "speccheck.py"), str(sample),
                            "--spec", "festival-prores", "--no-loudness"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
