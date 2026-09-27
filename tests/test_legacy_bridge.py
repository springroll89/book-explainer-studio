"""Bringing a book made with the old tools onto the new flow without redoing work."""
import tempfile
import unittest
from pathlib import Path

from bookflow.__main__ import dispatch, parser
from bookflow.approvals import confirmation_state, record_confirmation
from bookflow.common import atomic_write, load_yaml, write_yaml
from bookflow.finalize import freeze_many
from bookflow.legacy_media import adopt
from bookflow.produce import check, run


class LegacyBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "projects/book"
        write_yaml(self.project / "project.yaml", {"book": {"title": "测试书"}})
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": 1}, {"ep": 2}]})
        for ep in (1, 2):
            for version, body in ((1, "初稿。"), (2, "定稿正文。第二句。")):
                atomic_write(self.project / f"episodes/ep{ep:02d}/draft_v{version}.md",
                             f"---\nepisode: {ep}\nstatus: preview\n---\n{body}\n")

    def test_freeze_latest_draft_and_keep_confirmation_stable(self):
        result = freeze_many(self.project, [1, 2])
        self.assertTrue(result["passed"], result)
        final = self.project / "episodes/ep01/final.md"
        text = final.read_text(encoding="utf-8")
        self.assertIn("status: final", text)
        self.assertIn("frozen_from: draft_v2.md", text)
        self.assertTrue((final.parent / "final.sentences.json").is_file())
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", verify_transcript=False)["passed"])
        again = freeze_many(self.project, [1, 2])
        self.assertEqual([row["status"] for row in again["episodes"]], ["unchanged", "unchanged"])
        self.assertEqual(final.read_text(encoding="utf-8"), text)
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")

    def test_freeze_conflict_needs_replace_and_keeps_old_final(self):
        freeze_many(self.project, [1])
        conflict = freeze_many(self.project, [1], source="1")
        self.assertFalse(conflict["passed"])
        self.assertIn("--replace", conflict["errors"][0])
        replaced = freeze_many(self.project, [1], source="1", replace=True)
        self.assertTrue(replaced["passed"], replaced)
        kept = self.project / "episodes/ep01" / replaced["episodes"][0]["previous_final"]
        self.assertIn("定稿正文", kept.read_text(encoding="utf-8"))

    def test_freeze_rejects_wrong_episode_and_needs_single_episode_for_version(self):
        atomic_write(self.project / "episodes/ep02/draft_v3.md", "---\nepisode: 1\n---\n错集。\n")
        wrong = freeze_many(self.project, [2])
        self.assertFalse(wrong["passed"])
        self.assertIn("不一致", wrong["errors"][0])
        with self.assertRaisesRegex(ValueError, "单集"):
            freeze_many(self.project, [1, 2], source="1")

    def adopt_files(self):
        folder = self.project / "episodes/ep01/production/av1_build_v3"
        for name in ("mix.wav", "sub.srt", "board.json", "ep01.mp4"):
            atomic_write(folder / name, name)
        return {"mix": "production/av1_build_v3/mix.wav", "subtitles": "production/av1_build_v3/sub.srt",
                "storyboard": "production/av1_build_v3/board.json", "video": "production/av1_build_v3/ep01.mp4"}

    def test_adopted_episode_counts_as_made_and_binds_sample_confirmation(self):
        freeze_many(self.project, [1])
        files = self.adopt_files()
        result = adopt(self.project, 1, files=files, note="旧流程 V3，用户已审看")
        self.assertTrue(result["passed"], result)
        self.assertIn("拍板样片", result["next_actions"][0])
        report = check(self.project, 1)
        self.assertTrue(all(row["ready"] for row in report["stages"]), report)
        self.assertTrue(record_confirmation(self.project, "sample", "拍板样片", [1],
                                            verify_transcript=False)["passed"])
        self.assertEqual(confirmation_state(self.project, "sample", 1)["state"], "passed")
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [1],
                                            verify_transcript=False)["passed"])
        self.assertEqual(run(self.project, 1)["summary"], "本集为已收编的旧流程成品，无需重做")

    def test_changed_script_or_file_stales_adoption_and_is_never_overwritten(self):
        freeze_many(self.project, [1])
        adopt(self.project, 1, files=self.adopt_files(), note="旧流程 V3")
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [1],
                                            verify_transcript=False)["passed"])
        atomic_write(self.project / "episodes/ep01/production/av1_build_v3/ep01.mp4", "换过的视频")
        stale = check(self.project, 1)
        self.assertEqual(stale["stages"][0]["reason"], "legacy_video_changed")
        refused = run(self.project, 1)
        self.assertFalse(refused["passed"])
        self.assertIn("不会覆盖", refused["summary"])

    def test_adoption_refuses_outside_files_and_new_pipeline_manifest(self):
        freeze_many(self.project, [1])
        files = self.adopt_files()
        outside = self.project / "outside.mp4"
        atomic_write(outside, "x")
        with self.assertRaisesRegex(ValueError, "本集目录内"):
            adopt(self.project, 1, files={**files, "video": str(outside)}, note="旧流程")
        write_yaml(self.project / "episodes/ep01/production/manifest.json",
                   {"mode": "real", "charges": [], "stages": {"cues": {"status": "done"}}})
        with self.assertRaisesRegex(ValueError, "新流水线"):
            adopt(self.project, 1, files=files, note="旧流程")

    def test_cli_parses_new_commands(self):
        args = parser().parse_args(["final", "freeze", str(self.project), "--eps", "1-2"])
        self.assertTrue(dispatch(args)["passed"])
        adopt_args = parser().parse_args(["adopt-legacy", str(self.project), "--ep", "1", "--note", "旧流程",
                                          *sum(([f"--{k}", v] for k, v in self.adopt_files().items()), [])])
        self.assertTrue(dispatch(adopt_args)["passed"])
        voices = parser().parse_args(["voices", "check", str(self.project)])
        self.assertEqual(voices.action, "check")
        self.assertEqual(load_yaml(self.project / "episodes/ep01/production/approval_assets.yaml")["assets"]["video"],
                         "production/av1_build_v3/ep01.mp4")


if __name__ == "__main__":
    unittest.main()
