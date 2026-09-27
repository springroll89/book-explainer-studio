"""Status must agree with content confirmations, including old-book fixtures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.approvals import _digest_paths, gate_state, record_confirmation
from bookflow.common import atomic_write, write_yaml
from bookflow.states import derive


class StatusConfirmationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        write_yaml(self.project / "project.yaml", {"approvals": {"test_fixture_only": True}})
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": 1}, {"ep": 2}]})
        self.final = self.project / "episodes/ep01/final.md"
        atomic_write(self.final, "---\nstatus: final\nepisode: 1\n---\n门已经开了。\n")

    def confirm(self, kind, quote):
        result = record_confirmation(self.project, kind, quote, [1],
                                     session="selftest-fixture", verify_transcript=False)
        self.assertTrue(result["passed"], result)

    def test_final_without_draft_uses_all_four_current_confirmations(self):
        atomic_write(self.project / "analysis/book_brief.md", "测试夹具")
        write_yaml(self.project / "analysis/characters.yaml", {"characters": []})
        for name in ("final_mix.wav", "subtitles.srt", "storyboard.yaml", "final.mp4"):
            atomic_write(self.final.parent / "production" / name, "test_fixture_only")
        write_yaml(self.project / "release/compliance.yaml", {"test_fixture_only": True})
        for kind, label in (("plan", "方案"), ("script", "文案"), ("sample", "样片"), ("release", "成片")):
            self.confirm(kind, "拍板" + label)
        result = derive(self.project)
        self.assertEqual(result["confirmations"], {
            "plan": "passed", "style": "pending", "characters": "pending", "sample": "passed",
            "script": {"1": "passed", "2": "pending"}, "sound": {"1": "pending", "2": "pending"},
            "release": {"1": "passed", "2": "pending"}})
        self.assertEqual(result["episodes"][0]["status"], "approved")
        self.assertIn("待文案确认：第 2 集", result["awaiting_human"])
        self.confirm("release", "撤回成片")
        revoked = derive(self.project)
        self.assertEqual(revoked["confirmations"]["release"]["1"], "revoked")
        self.assertIn("needs_recheck", revoked["episodes"][0]["flags"])

    def test_legacy_approval_does_not_approve_script_or_restore_revoke(self):
        files, digest = _digest_paths(self.project, "G4", 1)
        write_yaml(self.project / "approvals/G4-fixture.yaml", {
            "gate": "G4", "ep": 1, "object_files": files, "object_sha256": digest,
            "approved_at": "2026-01-01T00:00:00+00:00", "test_fixture_only": True})
        self.assertEqual(gate_state(self.project, "G4", 1), "passed")
        result = derive(self.project)
        self.assertEqual(result["confirmations"]["script"]["1"], "pending")
        self.assertEqual(result["episodes"][0]["status"], "final_candidate")
        self.confirm("script", "拍板文案")
        self.confirm("script", "撤回文案")
        result = derive(self.project)
        self.assertEqual(result["confirmations"]["script"]["1"], "revoked")
        self.assertNotIn(result["episodes"][0]["status"], ("approved", "ledgered"))

    def test_newer_episode_drafting_follows_first_script_confirmation(self):
        atomic_write(self.project / "episodes/ep03/draft_v1.md", "---\nepisode: 3\n---\n他走了。")
        self.assertIn("on_hold", derive(self.project)["episodes"][-1]["flags"])
        self.confirm("script", "拍板文案")
        self.assertNotIn("on_hold", derive(self.project)["episodes"][-1]["flags"])
        self.confirm("script", "撤回文案")
        self.assertIn("on_hold", derive(self.project)["episodes"][-1]["flags"])

    def test_season_status_prefers_recap_and_never_falls_back_when_invalid(self):
        write_yaml(self.project / "project.yaml", {"drafting": {"mode": "full_season_review"},
                                                   "approvals": {"test_fixture_only": True}})
        for ep in (1, 2):
            atomic_write(self.project / f"episodes/ep{ep:02d}/draft_v1.md", "门开了。")
        write_yaml(self.project / "episodes/recap.yaml", {"episodes": []})
        with patch("bookflow.continuity.context", side_effect=AssertionError("no legacy fallback")):
            self.assertFalse(derive(self.project)["season_drafts"]["continuity_complete"])
            with patch("bookflow.recap.check", return_value={"passed": True}):
                self.assertTrue(derive(self.project)["season_drafts"]["continuity_complete"])


if __name__ == "__main__":
    unittest.main()
