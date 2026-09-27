"""The stage guard reads the four passphrase confirmations, not legacy terminal gates."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.approvals import confirmation_state, migrate_legacy, parse_confirmation, record_confirmation
from bookflow.common import write_yaml
from bookflow.flow import _pending_acknowledged, _review_suffix
from bookflow.guard import check, required_confirmations
from bookflow.selftest import run as selftest_run


class RequiredConfirmationTests(unittest.TestCase):
    def test_actions_map_to_new_confirmations(self):
        self.assertEqual(required_confirmations("draft", 5, batch=True), [("plan", None)])
        self.assertEqual(required_confirmations("draft", 2), [("plan", None)])
        self.assertEqual(required_confirmations("draft", 3), [("plan", None), ("script", 1)])
        self.assertEqual(required_confirmations("review", 3), [("plan", None)])
        self.assertEqual(required_confirmations("sound-plan", 1), [("plan", None), ("script", 1)])
        self.assertEqual(required_confirmations("media-generate", 4),
                         [("plan", None), ("script", 4), ("sample", 1)])
        self.assertEqual(required_confirmations("export-deliver", 4), [("script", 4), ("release", 4)])
        self.assertEqual(required_confirmations("unknown-action", 1), [])


class GuardProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assertTrue(selftest_run(Path(temporary.name))["passed"])
        self.project = Path(temporary.name) / "projects/selftest-fixture"

    def test_passphrase_only_project_passes_text_and_media_guards(self):
        self.assertEqual([p.name for p in (self.project / "approvals").glob("*.yaml")], ["log.yaml"])
        for action in ("draft", "review", "sound-plan", "visual", "media-generate", "export-deliver"):
            with self.subTest(action=action):
                result = check(self.project, action, 1)
                self.assertTrue(result["passed"], result)

    def test_revoked_plan_blocks_drafting_and_media(self):
        self.assertTrue(record_confirmation(self.project, "plan", "撤回方案", verify_transcript=False)["passed"])
        for action in ("draft", "media-generate"):
            with self.subTest(action=action):
                result = check(self.project, action, 1)
                self.assertFalse(result["passed"])
                self.assertIn("方案确认未通过（当前状态：revoked）", result["errors"])

    def test_unmigrated_legacy_records_stop_until_migrated_and_log_is_kept(self):
        write_yaml(self.project / "approvals/G1-20260101T000000Z.yaml",
                   {"gate": "G1", "ep": None, "object_files": [], "object_sha256": "old",
                    "approved_at": "2026-01-01T00:00:00+00:00"})
        blocked = check(self.project, "sound-plan", 1)
        self.assertFalse(blocked["passed"])
        self.assertTrue(any("migrate-approvals" in error for error in blocked["errors"]), blocked)
        migrated = migrate_legacy(self.project)
        self.assertTrue(migrated["passed"], migrated)
        self.assertEqual(migrated["legacy_records"], 1)
        self.assertTrue((self.project / "approvals/log.yaml").is_file())
        self.assertFalse((self.project / "approvals/legacy/log.yaml").exists())
        self.assertEqual(confirmation_state(self.project, "plan")["state"], "passed")
        self.assertTrue(check(self.project, "sound-plan", 1)["passed"])


class PassphraseNormalizationTests(unittest.TestCase):
    def test_full_width_digits_and_punctuation(self):
        self.assertEqual(parse_confirmation("拍板文案除５")["exclude"], [5])
        self.assertEqual(parse_confirmation("拍板文案，除第１２集；")["exclude"], [12])
        with self.assertRaises(ValueError):
            parse_confirmation("拍板文案除０")


class PendingReviewTests(unittest.TestCase):
    item = {"episode": 3, "created_at": "2026-09-27T02:00:00+00:00", "text": "第3集改动待过目：甲→乙"}

    def test_review_suffix(self):
        self.assertEqual(_review_suffix([]), "")
        self.assertEqual(_review_suffix(["甲", "乙"]), "；并一并过目：甲；乙")

    def test_later_release_confirmation_acknowledges_minor_edit(self):
        states = {"release": {"state": "passed", "at": "2026-09-27T03:00:00+00:00"}}
        with patch("bookflow.flow.confirmation_state", side_effect=lambda p, g, ep=None: states.get(g, {"state": "pending"})):
            self.assertTrue(_pending_acknowledged(Path("."), self.item))

    def test_earlier_or_missing_confirmation_keeps_edit_pending(self):
        earlier = {"state": "passed", "at": "2026-09-27T01:00:00+00:00"}
        with patch("bookflow.flow.confirmation_state", return_value=earlier):
            self.assertFalse(_pending_acknowledged(Path("."), self.item))
        with patch("bookflow.flow.confirmation_state", return_value={"state": "pending"}):
            self.assertFalse(_pending_acknowledged(Path("."), self.item))
        # A sample confirmation only covers episode 1.
        sample_only = lambda p, g, ep=None: ({"state": "passed", "at": "2026-09-27T03:00:00+00:00"}
                                             if g == "sample" else {"state": "pending"})
        with patch("bookflow.flow.confirmation_state", side_effect=sample_only):
            self.assertFalse(_pending_acknowledged(Path("."), self.item))
            self.assertTrue(_pending_acknowledged(Path("."), {**self.item, "episode": 1}))


if __name__ == "__main__":
    unittest.main()
