"""Imported user edits can carry a script confirmation only with intact local evidence."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow import feedback
from bookflow.approvals import (_spoken_snapshot, confirmation_state, read_log, reconcile_imported_edits,
                                record_assistant_edit, record_confirmation, record_user_edit_instruction)
from bookflow.common import ROOT, atomic_write, load_yaml, write_json, write_yaml
from bookflow.flow import derive, write
from bookflow.selftest import run as selftest_run


class ApprovalCarryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assertTrue(selftest_run(self.root)["passed"])
        self.project = self.root / "projects/selftest-fixture"
        self.final = self.project / "episodes/ep01/final.md"
        self.original = self.final.read_text(encoding="utf-8")
        self.copy = feedback.create_copy(self.final, self.final.parent / "human_edit/carry")
        self.edited = self.root / "user-edited.md"
        atomic_write(self.edited, Path(self.copy["markdown"]).read_text(encoding="utf-8")
                     .replace("手机显示五点四十", "手机显示五点半"))
        self.imported = feedback.import_edits(self.edited, Path(self.copy["baseline"]))
        self.round = Path(self.imported["round"])
        learning = load_yaml(self.round / "learning.yaml")
        learning["status"] = "reviewed"
        write_yaml(self.round / "learning.yaml", learning)

    def update_final(self, replacement="手机显示五点半"):
        atomic_write(self.final, self.original.replace("手机显示五点四十", replacement))

    def test_next_carries_matching_import_once_and_records_basis(self):
        self.update_final()
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "invalidated")
        before = len(read_log(self.project))
        state = write(self.project)
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        self.assertEqual(len(read_log(self.project)), before + 1)
        carried = read_log(self.project)[-1]
        self.assertEqual(carried["action"], "carry")
        self.assertEqual(carried["basis"]["round"], str(self.round.relative_to(self.project.resolve())))
        self.assertEqual(carried["basis"]["edited_file_sha256"], feedback.sha256_file(self.edited))
        self.assertEqual(carried["session"], "bookflow")
        self.assertEqual(state["stage"], "全季初稿")
        self.assertTrue(state["lesson_blocked"])
        self.assertIn("lessons triage", state["next_step"])
        write(self.project)
        self.assertEqual(len(read_log(self.project)), before + 1)

    def test_imported_round_cannot_carry_to_a_later_approval(self):
        report = json.loads((self.round / "diff.json").read_text(encoding="utf-8"))
        script_approvals = [row for row in read_log(self.project)
                            if row.get("gate") == "script" and 1 in row.get("episodes", [])]
        self.assertEqual(report["prior_approval_at"], script_approvals[-1]["at"])

        reapproved = record_confirmation(self.project, "script", "拍板文案", [1],
                                         verify_transcript=False)
        self.assertTrue(reapproved["passed"], reapproved)
        new_approval_at = [row for row in read_log(self.project)
                           if row.get("gate") == "script" and 1 in row.get("episodes", [])][-1]["at"]
        self.assertNotEqual(report["prior_approval_at"], new_approval_at)

        self.update_final()
        before = len(read_log(self.project))
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [])
        self.assertEqual(len(read_log(self.project)), before)
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "invalidated")

    def test_next_lists_stale_downstream_media_for_only_the_changed_episode(self):
        plan_path = self.project / "plan/episodes.yaml"
        plan = load_yaml(plan_path)
        rows = plan if isinstance(plan, list) else plan["episodes"]
        rows.append({**rows[0], "ep": 2})
        write_yaml(plan_path, plan)
        self.update_final()

        state = derive(self.project)
        self.assertEqual(len(state["downstream_impacts"]), 1)
        impact = state["downstream_impacts"][0]
        self.assertIn("第1集文案变化（1 处", impact)
        self.assertNotIn("第2集", impact)
        for label in ("声音设计清单", "配音段落", "音效", "混音", "字幕", "分镜", "画面", "成片"):
            self.assertIn(label, impact)

        command = subprocess.run([sys.executable, "-m", "bookflow", "next", str(self.project), "--read-only"],
                                 cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertIn("下游影响：", command.stdout)
        progress_state = write(self.project)
        self.assertTrue(progress_state["downstream_impacts"])
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        progress = (self.project / "进度.md").read_text(encoding="utf-8")
        self.assertIn("下游影响：", progress)
        self.assertIn("第1集文案变化", progress)
        self.assertNotIn("第2集", progress)

        from bookflow.produce import run as produce_run
        produced = produce_run(self.project, 1, test_mode=True, from_stage="cues")
        self.assertTrue(produced["passed"], produced)
        self.assertEqual(derive(self.project)["downstream_impacts"], [])

    def test_read_only_next_and_unreviewed_round_never_carry(self):
        self.update_final()
        before = len(read_log(self.project))
        derive(self.project)
        self.assertEqual(len(read_log(self.project)), before)
        learning = load_yaml(self.round / "learning.yaml")
        learning["status"] = "pending_semantic_review"
        write_yaml(self.round / "learning.yaml", learning)
        write(self.project)
        self.assertEqual(len(read_log(self.project)), before)
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "invalidated")

    def test_mismatch_revoke_and_tampered_import_do_not_carry(self):
        before = len(read_log(self.project))
        self.update_final("手机显示六点整")
        write(self.project)
        self.assertEqual(len(read_log(self.project)), before)
        self.update_final()
        atomic_write(self.round / "revised.txt", "伪造的改稿。")
        write(self.project)
        self.assertEqual(len(read_log(self.project)), before)
        self.assertTrue(record_confirmation(self.project, "script", "撤回文案", [1],
                                            verify_transcript=False)["passed"])
        write(self.project)
        self.assertEqual(read_log(self.project)[-1]["action"], "revoke")
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "revoked")

    def test_carried_approval_stales_if_edit_evidence_is_removed(self):
        self.update_final()
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [1])
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        (self.round / "edited.md").unlink()
        status = confirmation_state(self.project, "script", 1)
        self.assertEqual(status["state"], "invalidated")
        self.assertTrue(any(row["reason"] == "carry_evidence_invalid" for row in status["changed_files"]))

    def test_modified_baseline_blocks_cannot_carry(self):
        self.update_final()
        baseline_path = self.round / "baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["blocks"][0]["text"] = "伪造的原稿"
        write_json(baseline_path, baseline)
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [])
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "invalidated")
        write_json(baseline_path, [])
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [])

    def test_second_reviewed_edit_can_carry_from_first(self):
        self.update_final()
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [1])
        second_copy = feedback.create_copy(self.final, self.final.parent / "human_edit/carry2")
        second_edit = self.root / "user-edited-2.md"
        atomic_write(second_edit, Path(second_copy["markdown"]).read_text(encoding="utf-8")
                     .replace("手机显示五点半", "手机显示五点整"))
        second_round = Path(feedback.import_edits(second_edit, Path(second_copy["baseline"]))["round"])
        learning = load_yaml(second_round / "learning.yaml")
        learning["status"] = "reviewed"
        write_yaml(second_round / "learning.yaml", learning)
        atomic_write(self.final, self.final.read_text(encoding="utf-8")
                     .replace("手机显示五点半", "手机显示五点整"))
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [1])
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        self.assertEqual([row["action"] for row in read_log(self.project) if row["gate"] == "script"],
                         ["approve", "carry", "carry"])

    def test_explicit_instruction_carries_only_exact_requested_replacement(self):
        learning = load_yaml(self.round / "learning.yaml")
        learning["status"] = "pending_semantic_review"
        write_yaml(self.round / "learning.yaml", learning)
        self.update_final()
        quote = "把“手机显示五点四十”改为“手机显示五点半”"
        result = subprocess.run([sys.executable, "-m", "bookflow", "edit", "instruction",
                                 str(self.project), "1", "--quote", quote,
                                 "--replace", "手机显示五点四十", "手机显示五点半"],
                                cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(result.stdout)
        self.assertTrue(recorded["passed"], recorded)
        repeated = record_user_edit_instruction(self.project, 1, quote,
                                                [("手机显示五点四十", "手机显示五点半")],
                                                verify_transcript=False)
        self.assertEqual(recorded["record"]["id"], repeated["record"]["id"])
        state = write(self.project)
        carried = read_log(self.project)[-1]
        self.assertEqual(carried["basis"]["kind"], "user_instruction")
        self.assertEqual(state["stage"], "全季初稿")
        self.assertEqual(carried["basis"]["record_sha256"], feedback.sha256_file(Path(recorded["path"])))
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        Path(recorded["path"]).unlink()
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "invalidated")

    def test_instruction_with_extra_unrequested_change_is_rejected(self):
        self.update_final()
        extra = self.final.read_text(encoding="utf-8").replace("这十二分钟是谁留下的？", "这十二分钟怎么来的？")
        atomic_write(self.final, extra)
        quote = "把“手机显示五点四十”改为“手机显示五点半”"
        result = record_user_edit_instruction(self.project, 1, quote,
                                              [("手机显示五点四十", "手机显示五点半")],
                                              verify_transcript=False)
        self.assertFalse(result["passed"])
        self.assertFalse((self.project / "feedback/instructions").exists())

    def test_instruction_cannot_carry_a_whole_script_replacement(self):
        before = _spoken_snapshot(self.final)
        after = "全新整集口播稿。" * 80
        atomic_write(self.final, after)
        quote = f"整集替换为：{after}"
        result = record_user_edit_instruction(self.project, 1, quote, [(before, after)],
                                              verify_transcript=False)
        self.assertFalse(result["passed"])
        self.assertFalse((self.project / "feedback/instructions").exists())

    def test_instruction_quote_must_match_available_transcript_but_unavailable_only_warns(self):
        learning = load_yaml(self.round / "learning.yaml")
        learning["status"] = "pending_semantic_review"
        write_yaml(self.round / "learning.yaml", learning)
        config = load_yaml(self.project / "project.yaml")
        config["approvals"]["verify_transcript"] = True
        write_yaml(self.project / "project.yaml", config)
        self.update_final()
        quote = "把“手机显示五点四十”改为“手机显示五点半”"
        args = (self.project, 1, quote, [("手机显示五点四十", "手机显示五点半")])
        with patch("bookflow.approvals._session_user_message", return_value=("另一条用户消息", "")):
            denied = record_user_edit_instruction(*args)
        self.assertFalse(denied["passed"])
        with patch("bookflow.approvals._session_user_message", return_value=(None, "会话记录不可读")):
            accepted = record_user_edit_instruction(*args)
        self.assertTrue(accepted["passed"], accepted)
        self.assertEqual(accepted["record"]["transcript_check"], "unavailable")
        self.assertTrue(accepted["warnings"])

    def test_instruction_carry_can_be_followed_by_another_instruction(self):
        learning = load_yaml(self.round / "learning.yaml")
        learning["status"] = "pending_semantic_review"
        write_yaml(self.round / "learning.yaml", learning)
        self.update_final()
        first_quote = "把“手机显示五点四十”改为“手机显示五点半”"
        first = record_user_edit_instruction(self.project, 1, first_quote,
                                              [("手机显示五点四十", "手机显示五点半")],
                                              verify_transcript=False)
        self.assertTrue(first["passed"], first)
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [1])
        middle = self.final.read_text(encoding="utf-8").replace("手机显示五点半", "手机显示五点整")
        atomic_write(self.final, middle)
        second_quote = "把“手机显示五点半”改为“手机显示五点整”"
        second = record_user_edit_instruction(self.project, 1, second_quote,
                                              [("手机显示五点半", "手机显示五点整")],
                                              verify_transcript=False)
        self.assertTrue(second["passed"], second)
        self.assertEqual(reconcile_imported_edits(self.project, [1]), [1])
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        self.assertEqual([row["action"] for row in read_log(self.project) if row["gate"] == "script"],
                         ["approve", "carry", "carry"])

    def test_small_assistant_change_is_listed_for_review_in_next_and_progress(self):
        old = "这十二分钟是谁留下的？"
        new = "这十二分钟是谁留下的："
        atomic_write(self.final, self.original.replace(old, new))
        command = subprocess.run(
            [sys.executable, "-m", "bookflow", "edit", "assistant-change", str(self.project), "1",
             "--replace", old, new, "--risk-review", "只调整提问标点，不改变人物身份、情节事实或结尾。",
             "--no-identity-change", "--no-plot-fact-change", "--no-ending-change"],
            cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(command.returncode, 0, command.stderr)
        result = json.loads(command.stdout)
        self.assertTrue(result["passed"], result)
        self.assertLessEqual(result["change_ratio"], 0.03)
        before_log = len(read_log(self.project))
        before_evidence = Path(result["path"]).read_bytes()
        confirmation = confirmation_state(self.project, "script", 1)
        self.assertEqual(confirmation["state"], "passed")
        self.assertTrue(confirmation["changed_files"])
        readonly = derive(self.project)
        self.assertTrue(readonly["change_pending"])
        self.assertFalse(any("文案确认" in item for item in readonly["needs_you"]))
        self.assertTrue(readonly["downstream_impacts"])
        self.assertEqual(len(read_log(self.project)), before_log)
        self.assertEqual(Path(result["path"]).read_bytes(), before_evidence)
        state = write(self.project)
        self.assertIn("待过目", " ".join(state["change_pending"]))
        self.assertIn("改动待过目：", (self.project / "进度.md").read_text(encoding="utf-8"))
        approved = record_confirmation(self.project, "script", "拍板文案", [1], verify_transcript=False)
        self.assertTrue(approved["passed"], approved)
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        self.assertEqual(derive(self.project)["change_pending"], [])

    def test_minor_evidence_does_not_cover_later_unrecorded_change_or_revoke(self):
        old, new = "这十二分钟是谁留下的？", "这十二分钟是谁留下的："
        edited = self.original.replace(old, new)
        atomic_write(self.final, edited)
        registered = record_assistant_edit(
            self.project, 1, [(old, new)], "仅改标点。",
            no_identity_change=True, no_plot_fact_change=True, no_ending_change=True)
        self.assertTrue(registered["passed"], registered)
        atomic_write(self.final, edited.replace("手机显示五点四十", "手机显示六点四十"))
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "invalidated")
        atomic_write(self.final, edited)
        self.assertTrue(record_confirmation(self.project, "script", "撤回文案", [1],
                                            verify_transcript=False)["passed"])
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "revoked")

    def test_assistant_edit_over_threshold_or_risk_flag_is_not_minor(self):
        old = "手机显示五点四十，墙上的大钟却指向五点五十二。"
        new = "此时手机显示五点四十，而墙上的大钟却指向五点五十二，两个时间之间存在十二分钟差异。"
        atomic_write(self.final, self.original.replace(old, new))
        over = record_assistant_edit(
            self.project, 1, [(old, new)], "润色句子。",
            no_identity_change=True, no_plot_fact_change=True, no_ending_change=True)
        self.assertFalse(over["passed"])
        unsafe = record_assistant_edit(
            self.project, 1, [(old, new)], "可能改变情节事实。",
            no_identity_change=True, no_plot_fact_change=False, no_ending_change=True)
        self.assertFalse(unsafe["passed"])
        self.assertFalse((self.project / "feedback/assistant_changes").exists())

    def test_assistant_change_threshold_is_configurable_and_tamper_invalidates_review(self):
        old = "这十二分钟是谁留下的？"
        new = "这十二分钟是谁留下的："
        atomic_write(self.final, self.original.replace(old, new))
        config = load_yaml(self.project / "project.yaml")
        config["approvals"]["minor_change_ratio"] = 0.001
        write_yaml(self.project / "project.yaml", config)
        args = (self.project, 1, [(old, new)], "只调整标点。")
        flags = {"no_identity_change": True, "no_plot_fact_change": True, "no_ending_change": True}
        self.assertFalse(record_assistant_edit(*args, **flags)["passed"])
        config["approvals"]["minor_change_ratio"] = 0.03
        write_yaml(self.project / "project.yaml", config)
        registered = record_assistant_edit(*args, **flags)
        self.assertTrue(registered["passed"], registered)
        path = Path(registered["path"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["risk_review"]["note"] = "被篡改"
        write_json(path, record)
        state = confirmation_state(self.project, "script", 1)
        self.assertEqual(state["state"], "invalidated")
        self.assertEqual(state["change_pending"], [])


if __name__ == "__main__":
    unittest.main()
