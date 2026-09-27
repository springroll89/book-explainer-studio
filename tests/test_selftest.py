"""The model-free rehearsal must never masquerade as a real approval or media test."""
import tempfile
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch

from bookflow.__main__ import parser
from bookflow.selftest import run


class SelftestTests(unittest.TestCase):
    def test_isolated_pipeline_renders_probeable_mp4_and_reaches_export_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = run(root)
            if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                self.assertFalse(report["passed"])
                self.assertIn("ffmpeg 和 ffprobe", report["summary"])
                return
            self.assertTrue(report["passed"], report)
            self.assertEqual(report["scope"], "text_media_fixture")
            self.assertEqual(report["status"], "warning")
            self.assertTrue(report["probe"]["subtitle_frame_visible"])
            self.assertAlmostEqual(report["probe"]["duration_sec"], 8.0, delta=0.35)
            self.assertTrue((root / "projects/selftest-fixture/episodes/ep01/preview/index.html").is_file())
            self.assertTrue((root / "projects/selftest-fixture/episodes/ep01/deliver/index.html").is_file())
            self.assertTrue((root / "projects/selftest-fixture/episodes/ep01/production/final.mp4").is_file())
            self.assertTrue((root / "projects/selftest-fixture/episodes/ep01/production/subtitles.srt").is_file())
            self.assertTrue((root / "projects/selftest-fixture/approvals/log.yaml").is_file())
            self.assertTrue((root / "projects/selftest-fixture/episodes/ep01/production/manifest.json").is_file())
            self.assertTrue((root / "archive-rehearsal/projects/selftest-fixture/archive/ep01.yaml").is_file())
            self.assertIn("test_sample+test_release+export_stage", report["checks"])
            self.assertIn("fixture_fact_review+recap+formal_format_export", report["checks"])
            self.assertIn("fixture_archive_upload_guard+restore_hash", report["checks"])
            self.assertIn("produce_test_mode+resume+skip+ffprobe", report["checks"])
            self.assertIn("foreign_source_ingest", report["checks"])
            self.assertIn("recap_working+fixture_semantic_review", report["checks"])
            self.assertIn("recap_final_snapshot+identical_spoken_review", report["checks"])
            self.assertTrue((root / "projects/selftest-fixture/episodes/recap.yaml").is_file())
            self.assertFalse((root / "projects/selftest-fixture/episodes/working_continuity.yaml").exists())
            self.assertFalse((root / "projects/selftest-fixture/series_ledger.yaml").exists())
            foreign_project = root / "projects/selftest-foreign-fixture"
            self.assertTrue((foreign_project / "source/current.json").is_file())
            self.assertFalse((foreign_project / "approvals/log.yaml").exists())

    def test_missing_ffmpeg_is_actionable_and_never_passes_as_text_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("bookflow.media_fixture.shutil.which", return_value=None):
                report = run(Path(temporary))
            self.assertFalse(report["passed"])
            self.assertIn("ffmpeg 和 ffprobe", report["summary"])
            self.assertTrue(report["next_actions"])

    def test_existing_project_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "projects").mkdir()
            report = run(root)
            self.assertFalse(report["passed"])
            self.assertEqual(report["status"], "error")
            self.assertIn("全新的临时目录", report["summary"])
            self.assertTrue(report["next_actions"])
            self.assertEqual(list((root / "projects").iterdir()), [])

    def test_cli_exposes_selftest(self):
        self.assertEqual(parser().parse_args(["selftest"]).command, "selftest")
        rules = (Path(__file__).resolve().parents[1] / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("./run.sh selftest", rules)
        self.assertIn("test_fixture_only", rules)


if __name__ == "__main__":
    unittest.main()
