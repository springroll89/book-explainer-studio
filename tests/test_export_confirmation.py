"""The isolated fixture proves formal export uses the four-confirmation protocol."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.approvals import confirmation_state, record_confirmation
from bookflow.common import load_yaml, write_yaml
from bookflow.exporting import export_episode
from bookflow.selftest import run as selftest_run


class ExportConfirmationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        result = selftest_run(root)
        self.assertTrue(result["passed"], result)
        self.project = root / "projects/selftest-fixture"
        self.final = self.project / "episodes/ep01/final.md"
        self.review = self.final.parent / "review/summary.yaml"
        write_yaml(self.review, {"test_fixture_only": True})
        write_yaml(self.project / "release/compliance.yaml", {
            "test_fixture_only": True,
            "copyright": {"status": "test", "note": "isolated fixture"},
            "ai_content_label": {"explicit_label": "test", "platform_setting": "test", "checked_rules": "test"},
            "likeness_and_assets": {"note": "isolated fixture"},
            "platform": {"name": "test", "rules_checked_at": "test"}, "reviewer": "fixture",
        })
        approved = record_confirmation(self.project, "release", "拍板成片", [1],
                                       session="selftest-fixture", verify_transcript=False)
        self.assertTrue(approved["passed"], approved)

    def _export(self):
        with patch("bookflow.review.evaluate", return_value={"passed": True, "errors": [], "warnings": []}):
            return export_episode(self.final, preview=False, review_path=self.review)

    def test_valid_script_and_release_enable_export(self):
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        self.assertEqual(confirmation_state(self.project, "release", 1)["state"], "passed")
        result = self._export()
        self.assertEqual(result["status"], "delivered")
        self.assertTrue((self.final.parent / "deliver/index.html").is_file())
        self.assertIn("前情快照：已复核", (self.final.parent / "deliver/index.html").read_text(encoding="utf-8"))

    def test_revoked_release_blocks_export(self):
        before = (self.final.parent / "deliver/index.html").read_bytes()
        revoked = record_confirmation(self.project, "release", "撤回成片", [1],
                                      session="selftest-fixture", verify_transcript=False)
        self.assertTrue(revoked["passed"], revoked)
        with self.assertRaisesRegex(ValueError, "成片确认"):
            self._export()
        self.assertEqual((self.final.parent / "deliver/index.html").read_bytes(), before)

    def test_changed_script_blocks_export(self):
        original = self.final.read_text(encoding="utf-8")
        self.final.write_text(original + "\n有人又敲了一次门。\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "文案确认"):
            self._export()

    def test_invalid_recap_blocks_export_without_legacy_fallback(self):
        delivered = self.final.parent / "deliver/index.html"
        before = delivered.read_bytes()
        recap_path = self.project / "episodes/recap.yaml"
        data = load_yaml(recap_path)
        data["episodes"][0]["semantic_review"]["status"] = "pending"
        write_yaml(recap_path, data)
        with patch("bookflow.ledger.context", side_effect=AssertionError("must not fall back")):
            with self.assertRaisesRegex(ValueError, "前情表定稿快照"):
                self._export()
        self.assertEqual(delivered.read_bytes(), before)

    def test_project_without_recap_keeps_legacy_export_path(self):
        (self.project / "episodes/recap.yaml").unlink()
        with patch("bookflow.ledger.context", return_value={"passed": True, "errors": []}) as legacy:
            self.assertEqual(self._export()["status"], "delivered")
        legacy.assert_called_once_with(self.project.resolve(), 2)
        self.assertIn("旧账本：已确认", (self.final.parent / "deliver/index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
