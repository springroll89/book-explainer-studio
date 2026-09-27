"""The stage check composes existing validators without granting approvals."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from bookflow import archive
from bookflow.__main__ import dispatch, parser
from bookflow.approvals import record_confirmation
from bookflow.common import ROOT, atomic_write, load_yaml, write_yaml
from bookflow.flow import derive
from bookflow.recap import inspect as inspect_recap
from bookflow.selftest import run as selftest_run


class CheckCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assertTrue(selftest_run(self.root)["passed"])
        self.project = self.root / "projects/selftest-fixture"

    def check(self):
        return dispatch(parser().parse_args(["check", str(self.project)]))

    def test_text_error_is_reported_without_rewriting_project(self):
        draft = self.project / "episodes/ep01/draft_v1.md"
        atomic_write(draft, draft.read_text(encoding="utf-8") + "\nTODO：待核实。\n")
        tracked = [self.project / name for name in ("state.json", "进度.md", "episodes/recap.yaml")]
        before = {path: path.read_bytes() for path in tracked}
        result = self.check()
        self.assertEqual(result["stage"], "全季初稿")
        self.assertFalse(result["passed"])
        self.assertTrue(any("draft_v1.md" in error and "待核实" in error for error in result["errors"]))
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        completed = subprocess.run([sys.executable, "-m", "bookflow", "check", str(self.project)],
                                   cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 1)
        self.assertFalse((self.root / "lessons/inbox.yaml").exists())
        self.assertFalse(json.loads(completed.stdout)["passed"])

    def test_coverage_refresh_preserves_separate_human_review(self):
        machine = self.project / "analysis/coverage_machine.json"
        machine.unlink()
        review = self.project / "analysis/coverage_review.yaml"
        before = review.read_bytes()
        result = self.check()
        self.assertEqual(result["stage"], "拆书")
        self.assertTrue(machine.is_file())
        self.assertEqual(review.read_bytes(), before)
        self.assertIn(str(machine.resolve()), result["artifacts"])
        self.assertIn("coverage", [row["id"] for row in result["checks"]])

    def test_human_sample_gate_and_unknown_budget_remain_visible(self):
        revoked = record_confirmation(self.project, "sample", "撤回样片", [1],
                                      session="selftest-fixture", verify_transcript=False)
        self.assertTrue(revoked["passed"], revoked)
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("sound_design", {})["pricing"] = {  # defaults now carry real prices
            "narration_model_per_10k_chars": None, "sfx_model_per_minute": None, "currency": "CNY"}
        write_yaml(config_path, config)
        manifest = self.project / "episodes/ep01/production/manifest.json"
        before = manifest.read_bytes()
        result = self.check()
        self.assertEqual(result["stage"], "样片确认")
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "error")
        self.assertTrue(any("单价" in error for error in result["errors"]))
        self.assertIn("样片确认", result["needs_you"])
        self.assertEqual(manifest.read_bytes(), before)

    def test_changed_media_is_not_reported_ready(self):
        (self.project / "episodes/ep01/production/voice.wav").write_bytes(b"changed fixture")
        result = self.check()
        self.assertEqual(result["stage"], "声音")
        self.assertFalse(result["passed"])
        self.assertTrue(any("voice" in error for error in result["errors"]))

    def test_final_stage_checks_current_review_and_requires_recap_snapshot(self):
        recap_path = self.project / "episodes/recap.yaml"
        data = load_yaml(recap_path)
        working = inspect_recap(self.project)["episodes"][0]["candidates"]["working"]
        data["episodes"][0]["selected_basis"] = "working"
        data["episodes"][0]["semantic_review"] = {
            "status": "completed", "reviewer": "selftest-fixture", "test_fixture_only": True,
            "note": "隔离夹具重新核对工作稿", "ending_summary": "树影造成错觉", "unresolved": [],
            "reviewed_file_sha256": working["file_sha256"],
            "reviewed_entry_sha256": working["entry_sha256"],
        }
        write_yaml(recap_path, data)
        result = self.check()
        self.assertEqual(result["stage"], "文案确认")
        self.assertFalse(result["passed"])
        self.assertTrue(any(row["id"] == "review_ep01" and row["passed"] for row in result["checks"]))
        self.assertTrue(any("定稿前情快照" in error for error in result["errors"]))
        summary = self.project / "episodes/ep01/review/summary_fixture.yaml"
        report = load_yaml(summary)
        report["draft_sha256"] = "0" * 64
        write_yaml(summary, report)
        stale = self.check()
        self.assertTrue(any(row["id"] == "review_ep01" and not row["passed"] for row in stale["checks"]))

    def test_malformed_archive_manifest_is_an_actionable_error(self):
        write_yaml(self.project / "archive/ep01.yaml", ["invalid"])
        result = self.check()
        self.assertEqual(result["stage"], "归档")
        self.assertFalse(result["passed"])
        self.assertTrue(any("archive/ep01.yaml" in error for error in result["errors"]))

    def test_incomplete_delivery_package_is_not_complete(self):
        (self.project / "episodes/ep01/deliver/voiceover.txt").unlink()
        result = self.check()
        self.assertEqual(result["stage"], "归档")
        self.assertFalse(result["passed"])
        self.assertTrue(any("deliver/voiceover.txt" in error for error in result["errors"]))

    def test_archived_target_disappearance_fails_without_hydrating_files(self):
        cloud = self.root / "check-cloud"
        cloud.mkdir()
        completed = archive.run(self.project, 1, archive_root=cloud,
                                upload_check=lambda _: True, eviction_check=lambda _: True)
        self.assertEqual(completed["status"], "success")
        args = parser().parse_args(["check", str(self.project)])
        healthy = dispatch(args)
        self.assertEqual(healthy["stage"], "归档")
        self.assertTrue(healthy["passed"], healthy["errors"])
        manifest = yaml.safe_load((self.project / "archive/ep01.yaml").read_text(encoding="utf-8"))
        Path(manifest["files"][0]["target"]).unlink()
        broken = dispatch(args)
        self.assertFalse(broken["passed"])
        self.assertTrue(any("目标位置" in error for error in broken["errors"]), broken)

    def test_duplicate_completed_archive_row_blocks_check_and_next(self):
        cloud = self.root / "duplicate-cloud"
        cloud.mkdir()
        self.assertEqual(archive.run(self.project, 1, archive_root=cloud,
                         upload_check=lambda _: True, eviction_check=lambda _: True)["status"], "success")
        path = self.project / "archive/ep01.yaml"
        manifest = load_yaml(path)
        manifest["files"].append(dict(manifest["files"][0]))
        write_yaml(path, manifest)
        result = self.check()
        self.assertFalse(result["passed"])
        self.assertTrue(any("重复登记" in error for error in result["errors"]), result)
        state = derive(self.project)
        self.assertEqual(state["stage"], "归档")
        self.assertIn("重复登记", state["blockers"][0])


if __name__ == "__main__":
    unittest.main()
