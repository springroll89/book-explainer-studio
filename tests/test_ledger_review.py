import copy
import tempfile
import unittest
from pathlib import Path

from bookflow import ledger, review
from bookflow.common import atomic_write, load_yaml, parse_draft, sha256_file, write_json, write_yaml
from bookflow.quality import lint, verify_quotes


class LedgerReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.cfg = {
            "review": {"hook_fulfil_min": 0.6, "max_p1": 3},
            "format": {"episode_minutes": [0.01, 1]},
            "quote": {"max_ratio": 0.5},
        }
        self.generation = "a" * 64
        write_yaml(self.project / "project.yaml", self.cfg)
        write_json(self.project / "source" / "current.json", {"generation": self.generation})
        atomic_write(self.project / "source" / "imports" / self.generation / "paragraphs.jsonl",
                     '{"id":"p00001","chapter":"ch01","text":"他把灯关了，又打开了。他站在门口，没有离开。"}\n')
        write_yaml(self.project / "plan" / "episodes.yaml", {"episodes": [
            {"ep": ep, "takeaways": [{"id": f"K{ep}", "text": "作者如何通过动作塑造人物"}]}
            for ep in range(1, 4)
        ]})
        write_yaml(self.project / "series_ledger.yaml", {"episodes": [self.entry(ep) for ep in range(1, 3)]})
        self.paths = {}
        for ep in range(1, 3):
            final = self.project / "episodes" / f"ep{ep:02d}" / "final.md"
            final.parent.mkdir(parents=True)
            final.write_text("---\nstatus: final\n---\n〔据 p00001〕他为什么把灯又打开？[钩子]\n〔延伸〕我觉得这个动作说明他还在犹豫。\n", encoding="utf-8")
            self.paths[ep] = final

    @staticmethod
    def entry(ep):
        return {"ep": ep, **{key: kind() for key, kind in ledger.REQUIRED.items()}}

    def report(self, ep):
        draft = self.paths[ep]
        parsed = parse_draft(draft.read_text(encoding="utf-8"))
        dependencies = {str(previous): sha256_file(self.paths[previous]) for previous in range(1, ep)}
        core = [{"id": f"K{ep}", "delivered": True, "supported": True}]
        listener = {
            "engaged": [{"sentence_id": parsed["hooks"][0]["sentence_id"], "trigger": parsed["sentences"][0]["text"], "why": "想知道开灯的原因"}],
            "dropoff": [],
            "takeaway": "人物的动作可以表达犹豫",
        }
        roles = {}
        for role in ("fact", "listener", "deai"):
            role_path = draft.parent / "review" / f"{role}.yaml"
            original = {"draft_sha256": sha256_file(draft), "source_generation": self.generation, "dependency_hashes": dependencies, "findings": []}
            if role == "fact":
                original["core_takeaways"] = core
            if role == "listener":
                original["listener"] = listener
            write_yaml(role_path, original)
            roles[role] = {"status": "completed", "independent": True, "report": f"review/{role}.yaml"}
        return {
            "draft_sha256": sha256_file(draft),
            "source_generation": self.generation,
            "dependency_hashes": dependencies,
            "core_takeaways": core,
            "findings": [],
            "listener": listener,
            "reviewers": roles,
        }

    def save_review(self, ep, report=None):
        path = self.paths[ep].parent / "review" / "summary.yaml"
        write_yaml(path, report or self.report(ep))
        return path

    def stamp(self, ep):
        result = ledger.stamp(self.project, ep, "测试确认者", self.save_review(ep))
        self.assertTrue(result["passed"], result)
        return result

    def test_missing_approval_does_not_stamp(self):
        result = ledger.stamp(self.project, 1, " ", self.save_review(1))
        self.assertFalse(result["passed"])
        self.assertNotIn("final_sha256", ledger._load(self.project)["episodes"][0])

    def test_missing_review_cannot_stamp(self):
        result = ledger.stamp(self.project, 1, "测试确认者", self.project / "missing.yaml")
        self.assertFalse(result["passed"])

    def test_hard_error_cannot_be_bypassed_by_valid_review(self):
        final = self.paths[1]
        final.write_text(final.read_text(encoding="utf-8") + "<!-- TODO: 确认人物身份 -->\n", encoding="utf-8")
        review_path = self.save_review(1)
        self.assertTrue(review.evaluate(self.project, 1, final, load_yaml(review_path))["passed"])
        self.assertFalse(lint(final)["passed"])
        result = ledger.stamp(self.project, 1, "测试确认者", review_path)
        self.assertFalse(result["passed"])
        self.assertTrue(any("硬指标检查未通过" in error for error in result["errors"]))
        self.assertNotIn("final_sha256", ledger._load(self.project)["episodes"][0])

    def test_wrong_quote_cannot_be_bypassed_by_valid_review(self):
        final = self.paths[1]
        final.write_text(final.read_text(encoding="utf-8") + "〔引 p00001〕「他摔坏了玻璃。」\n", encoding="utf-8")
        review_path = self.save_review(1)
        self.assertTrue(lint(final)["passed"])
        self.assertFalse(verify_quotes(final)["passed"])
        result = ledger.stamp(self.project, 1, "测试确认者", review_path)
        self.assertFalse(result["passed"])
        self.assertTrue(any("引文与来源检查未通过" in error for error in result["errors"]))
        self.assertNotIn("final_sha256", ledger._load(self.project)["episodes"][0])

    def test_review_hash_and_source_generation_are_required(self):
        for key in ("draft_sha256", "source_generation"):
            with self.subTest(key=key):
                report = self.report(1)
                report[key] = "old"
                self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])

    def test_core_depth_cannot_use_p1_allowance(self):
        report = self.report(1)
        self.assertTrue(review.evaluate(self.project, 1, self.paths[1], report)["passed"])
        report["findings"] = [{"id": "D1", "severity": "P1", "category": "depth", "message": "只记住了情节", "resolved": False}]
        self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])
        report["findings"] = []
        for field in ("delivered", "supported"):
            broken = copy.deepcopy(report)
            broken["core_takeaways"][0][field] = False
            self.assertFalse(review.evaluate(self.project, 1, self.paths[1], broken)["passed"])
        report["core_takeaways"] = []
        self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])

    def test_changed_prior_stamp_does_not_clear_later_review_requirement(self):
        self.stamp(1)
        self.stamp(2)
        self.assertTrue(ledger.context(self.project, 3)["passed"])
        self.paths[1].write_text(self.paths[1].read_text(encoding="utf-8") + "这次他终于出门了。\n", encoding="utf-8")
        ledger.mark_stale(self.project, 1, "改了结尾")
        result = self.stamp(1)
        self.assertEqual(result["affected_episodes"], [2])
        self.assertFalse(ledger.context(self.project, 3)["passed"])
        self.assertTrue(ledger._load(self.project)["episodes"][1]["requires_review"])
        self.stamp(2)
        self.assertTrue(ledger.context(self.project, 3)["passed"])

    def test_unannounced_prior_change_is_detected_after_restamp(self):
        self.stamp(1)
        self.stamp(2)
        self.paths[1].write_text(self.paths[1].read_text(encoding="utf-8") + "门外已经没有人。\n", encoding="utf-8")
        self.assertFalse(ledger.context(self.project, 3)["passed"])
        self.stamp(1)
        self.assertFalse(ledger.context(self.project, 3)["passed"])

    def test_old_review_cannot_clear_dependency_requirement(self):
        self.stamp(1)
        self.stamp(2)
        old_review = self.paths[2].parent / "review" / "summary.yaml"
        self.paths[1].write_text(self.paths[1].read_text(encoding="utf-8") + "他把钥匙留在门口。\n", encoding="utf-8")
        self.stamp(1)
        result = ledger.stamp(self.project, 2, "测试确认者", old_review)
        self.assertFalse(result["passed"])
        self.assertTrue(any("第 1 集当前定稿" in error for error in result["errors"]))
        self.assertFalse(ledger.context(self.project, 3)["passed"])

    def test_incomplete_or_shared_context_listener_cannot_pass(self):
        report = self.report(1)
        report["reviewers"]["fact"]["status"] = "pending"
        self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])
        report = self.report(1)
        report["reviewers"]["listener"]["independent"] = False
        self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])

    def test_missing_predecessor_blocks_context(self):
        self.assertFalse(ledger.context(self.project, 2)["passed"])

    def test_long_paragraph_hook_matches_exact_sentence(self):
        text = "他在门口站着。" * 20 + "可门究竟是谁打开的？[钩子]"
        parsed = parse_draft(text)
        sid = parsed["hooks"][0]["sentence_id"]
        result = review.align_hooks(parsed, {"engaged": [{"sentence_id": sid, "trigger": "可门究竟是谁打开的？", "why": "想知道是谁开门"}]}, self.cfg)
        self.assertEqual(result["fulfil_rate"], 1.0)
        self.assertFalse(result["fake"])
        self.assertFalse(result["invalid_responses"])

    def test_listener_unknown_sentence_blocks_review(self):
        report = self.report(1)
        report["listener"]["engaged"][0]["sentence_id"] = "s999"
        self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])

    def test_stale_listener_trigger_cannot_match_current_hook(self):
        report = self.report(1)
        report["listener"]["engaged"][0]["trigger"] = "上一版的不同原句。"
        role_path = self.paths[1].parent / "review" / "listener.yaml"
        original = load_yaml(role_path)
        original["listener"] = report["listener"]
        write_yaml(role_path, original)
        result = review.evaluate(self.project, 1, self.paths[1], report)
        self.assertFalse(result["passed"])
        self.assertEqual(result["hook_alignment"]["fulfil_rate"], 0.0)
        self.assertEqual(result["hook_alignment"]["invalid_responses"][0]["reason"], "trigger_mismatch")

    def test_missing_dropoff_trigger_is_not_accepted(self):
        parsed = parse_draft("他还在犹豫。[钩子]")
        result = review.align_hooks(parsed, {"dropoff": [{"sentence_id": "s001", "why": "没听懂"}]}, self.cfg)
        self.assertEqual(result["invalid_responses"][0]["reason"], "trigger_mismatch")

    def test_summary_cannot_hide_original_p0_or_core_failure(self):
        for failure in ("P0", "core"):
            with self.subTest(failure=failure):
                report = self.report(1)
                fact_path = self.paths[1].parent / "review" / "fact.yaml"
                fact = load_yaml(fact_path)
                if failure == "P0":
                    fact["findings"] = [{"id": "F1", "severity": "P0", "category": "fidelity", "message": "叙述人物身份错误", "resolved": False}]
                    report["findings"] = [{**fact["findings"][0], "resolved": True}]
                else:
                    fact["core_takeaways"] = []
                write_yaml(fact_path, fact)
                self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])

    def test_role_report_version_and_listener_content_cannot_be_replaced_by_summary(self):
        report = self.report(1)
        role_path = self.paths[1].parent / "review" / "listener.yaml"
        original = load_yaml(role_path)
        original["draft_sha256"] = "old-draft"
        write_yaml(role_path, original)
        result = review.evaluate(self.project, 1, self.paths[1], report)
        self.assertFalse(result["passed"])
        self.assertTrue(any("listener 原始报告缺少当前稿件指纹" in error for error in result["errors"]))
        report = self.report(1)
        report["listener"]["takeaway"] = "汇总自行添加的收获"
        self.assertFalse(review.evaluate(self.project, 1, self.paths[1], report)["passed"])

    def test_changed_original_review_invalidates_approved_ledger(self):
        self.stamp(1)
        self.assertTrue(ledger.context(self.project, 2)["passed"])
        fact_path = self.paths[1].parent / "review" / "fact.yaml"
        fact_path.write_text(fact_path.read_text(encoding="utf-8") + "new_issue: true\n", encoding="utf-8")
        self.assertFalse(ledger.context(self.project, 2)["passed"])


if __name__ == "__main__":
    unittest.main()
