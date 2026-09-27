"""Story mode relaxes lecture-shaped gates without relaxing provenance or facts."""
import tempfile
import unittest
from pathlib import Path

from bookflow.__main__ import dispatch, new_project, parser
from bookflow.common import atomic_write, load_yaml, sha256_file, write_json, write_yaml
from bookflow.exporting import export_episode
from bookflow.planning import check_plan
from bookflow.quality import audit_story_season, lint, verify_quotes
from bookflow.review import evaluate
from bookflow.selftest import run as selftest_run


class StoryProfileTests(unittest.TestCase):
    def test_story_audit_can_select_current_drafts_before_final_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"}})
            write_yaml(project / "plan/episodes.yaml", {"episodes": [{"ep": 1}, {"ep": 2}]})
            for ep in (1, 2):
                folder = project / "episodes" / f"ep{ep:02d}"
                atomic_write(folder / "draft_v1.md", "二〇二四年，他回来了。\n")
                atomic_write(folder / "final.md", ("2024年" if ep == 1 else "二〇二四年") + "，他回来了。\n")
            final_rules = {item["rule"] for item in audit_story_season(project)["items"]}
            draft_rules = {item["rule"] for item in audit_story_season(project, prefer_final=False)["items"]}
        self.assertIn("year_style", final_rules)
        self.assertNotIn("year_style", draft_rules)

    def test_new_fiction_uses_story_and_nonfiction_keeps_explainer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for slug, genre, expected in (("story-demo", "suspense", "story"),
                                           ("science-demo", "popsci", "explainer")):
                new_project(slug, slug, "测试作者", genre, root=root)
                config = load_yaml(root / "projects" / slug / "project.yaml")
                self.assertEqual(config["profile"], expected)
                self.assertEqual(config["producers"]["render"], "ffmpeg")
            new_project("narrative-history", "史料叙事", "测试作者", "history", root=root, profile="story")
            self.assertEqual(load_yaml(root / "projects/narrative-history/project.yaml")["profile"], "story")

    def test_story_plan_allows_missing_takeaways_but_explainer_does_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(selftest_run(root)["passed"])
            project = root / "projects/selftest-fixture"
            plan = load_yaml(project / "plan/episodes.yaml")
            plan["episodes"][0].pop("takeaways")
            plan["episodes"][0].pop("target_chars")
            write_yaml(project / "plan/episodes.yaml", plan)
            self.assertEqual(check_plan(project)["errors"], [])
            preview = export_episode(project / "episodes/ep01/final.md", preview=True)
            self.assertEqual(preview["status"], "preview_awaiting_human")
            page = (project / "episodes/ep01/preview/index.html").read_text(encoding="utf-8")
            self.assertIn("本集问题与线索", page)
            self.assertNotIn("这一集听完，带走什么", page)
            config = load_yaml(project / "project.yaml")
            config["profile"] = "explainer"
            write_yaml(project / "project.yaml", config)
            self.assertTrue(any("takeaways" in item for item in check_plan(project)["errors"]))

    def test_story_lint_does_not_require_hook_and_flags_meta_outside_dialogue(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"},
                                                   "format": {"episode_minutes": [0.001, 0.002]}})
            draft = project / "episodes/ep01/draft_v1.md"
            atomic_write(draft, "“我们知道吗？”〔据 p00001〕\n上一集他没有回答。\n")
            result = lint(draft)
            self.assertTrue(result["passed"], result)
            self.assertNotIn("hook", [item["rule"] for item in result["items"]])
            self.assertIn("meta_narration", [item["rule"] for item in result["items"]])
            self.assertGreater(result["stats"]["dialogue_ratio"], 0)
            self.assertTrue(any(item["rule"] == "duration" and item["level"] == "warn"
                                for item in result["items"]))
            atomic_write(draft, "他没有回答。\n")
            low_dialogue = lint(draft)
            self.assertTrue(low_dialogue["passed"])
            self.assertTrue(any(item["rule"] == "dialogue_ratio" and item["level"] == "warn"
                                for item in low_dialogue["items"]))

    def test_reconstructed_dialogue_needs_same_paragraph_evidence_not_exact_quote(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            generation = "a" * 64
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"}})
            write_json(project / "source/current.json", {"generation": generation})
            atomic_write(project / "source/imports" / generation / "paragraphs.jsonl",
                         '{"id":"p00001","chapter":"ch01","text":"他拦住她，想问她为什么离开。"}\n')
            draft = project / "episodes/ep01/final.md"
            atomic_write(draft, "他拦住她。“你现在就要走？”〔据 p00001〕\n")
            self.assertTrue(verify_quotes(draft)["passed"])
            atomic_write(draft, "他拦住她。“你现在就要走？”\n")
            self.assertTrue(any("场景对白所在段缺少" in item for item in verify_quotes(draft)["errors"]))
            atomic_write(draft, "他拦住她。“你现在就要走？”〔据 p00002〕\n")
            self.assertFalse(verify_quotes(draft)["passed"])

    def test_story_review_requires_fact_report_but_not_listener_or_core_depth(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            generation = "b" * 64
            config_path = project / "project.yaml"
            write_yaml(config_path, {"profile": "story", "book": {"title": "夹具"}})
            write_json(project / "source/current.json", {"generation": generation})
            write_yaml(project / "plan/episodes.yaml", {"episodes": [{"ep": 1, "takeaways": []}]})
            draft = project / "episodes/ep01/final.md"
            atomic_write(draft, "---\nstatus: final\n---\n他看着空房间，没有回答。\n")
            meta = {"draft_sha256": sha256_file(draft), "source_generation": generation,
                    "dependency_hashes": {}}
            fact_path = draft.parent / "review/fact.yaml"
            write_yaml(fact_path, {**meta, "findings": []})
            report = {**meta, "findings": [], "reviewers": {
                "fact": {"status": "completed", "report": "review/fact.yaml"}}}
            self.assertTrue(evaluate(project, 1, draft, report)["passed"])
            write_yaml(fact_path, {**meta, "findings": [{"id": "F1", "severity": "P0", "category": "fact",
                                                          "message": "人物身份未核实", "resolved": False}]})
            self.assertFalse(evaluate(project, 1, draft, report)["passed"])
            write_yaml(fact_path, {**meta, "findings": []})
            listener_path = draft.parent / "review/listener.yaml"
            write_yaml(listener_path, {**meta, "findings": []})
            report["reviewers"]["listener"] = {"status": "completed", "independent": False,
                                                  "report": "review/listener.yaml"}
            self.assertFalse(evaluate(project, 1, draft, report)["passed"])
            report["reviewers"].pop("listener")
            write_yaml(config_path, {"profile": "explainer", "book": {"title": "夹具"}})
            self.assertFalse(evaluate(project, 1, draft, report)["passed"])

    def test_book_lint_rule_defaults_to_origin_episode_and_needs_evidence_to_expand(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"},
                                                   "format": {"episode_minutes": [0.001, 1]}})
            drafts = []
            for ep in (1, 2):
                draft = project / "episodes" / f"ep{ep:02d}" / "draft_v1.md"
                atomic_write(draft, f"---\nepisode: {ep}\n---\n他又抬头看了一眼。\n")
                drafts.append(draft)
            rule = {"id": "local-phrase", "origin_ep": 1, "level": "error", "max": 0,
                    "patterns": ["又抬头"]}
            rules_path = project / "feedback/lint_rules.yaml"
            write_yaml(rules_path, {"rules": [rule]})
            self.assertFalse(lint(drafts[0])["passed"])
            self.assertTrue(lint(drafts[1])["passed"])
            rule["applies_to"] = "all"
            write_yaml(rules_path, {"rules": [rule]})
            result = lint(drafts[1])
            self.assertFalse(result["passed"])
            self.assertTrue(any(item["rule"] == "book_lint_rules" for item in result["items"]))
            rule["scope_evidence"] = "只改这一集"
            write_yaml(rules_path, {"rules": [rule]})
            self.assertTrue(any("全季 lint 规则" in item["message"] for item in lint(drafts[1])["items"]))
            rule["scope_evidence"] = "用户明确说全季都要检查这一句式"
            write_yaml(rules_path, {"rules": [rule]})
            result = lint(drafts[1])
            self.assertFalse(result["passed"])
            self.assertTrue(any(item["rule"] == "phrase:local-phrase" for item in result["items"]))
            rule["patterns"] = ["["]
            write_yaml(rules_path, {"rules": [rule]})
            self.assertTrue(any("无效正则" in item["message"] for item in lint(drafts[1])["items"]))

    def test_story_lint_warns_on_repeated_long_descriptive_address(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"}})
            draft = project / "episodes/ep01/draft_v1.md"
            atomic_write(draft, "那个戴着黑帽子的男人走了。那个戴着黑帽子的男人回来了。"
                         "那个戴着黑帽子的男人又走了。\n")
            result = lint(draft)
            self.assertTrue(any(item["rule"] == "long_descriptor" and "3 次" in item["message"]
                                for item in result["items"]), result)

    def test_story_season_audit_checks_years_clue_terms_and_uniform_length(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"}})
            write_yaml(project / "plan/episodes.yaml", {"episodes": [
                {"ep": 1, "threads_setup": ["T1"]},
                {"ep": 2},
                {"ep": 3, "threads_payoff": ["T1"]}]})
            write_yaml(project / "analysis/threads.yaml", {"threads": [
                {"id": "T1", "spoken_key": "红围巾"}]})
            for ep, body in ((1, "2024年，他看见红围巾。"),
                             (2, "二〇二四年，他又等了一天。"),
                             (3, "2024年，他回来找人了。")):
                atomic_write(project / "episodes" / f"ep{ep:02d}" / "draft_v1.md", body + "\n")
            result = audit_story_season(project)
            self.assertFalse(result["passed"])
            rules = {item["rule"] for item in result["items"]}
            self.assertIn("year_style", rules)
            self.assertIn("spoken_key", rules)
            self.assertEqual(result["stats"]["episodes_checked"], 3)
            self.assertIn("episode_chars_cv", result["stats"])
            atomic_write(project / "episodes/ep02/draft_v1.md", "2024年，他又等了一天。\n")
            atomic_write(project / "episodes/ep03/final.md", "2024年，他带着红围巾回来。\n")
            repaired = audit_story_season(project)
            self.assertTrue(repaired["passed"], repaired)
            self.assertNotIn("year_style", {item["rule"] for item in repaired["items"]})
            self.assertNotIn("spoken_key", {item["rule"] for item in repaired["items"]})
            self.assertTrue(any(row["path"].endswith("ep03/final.md") for row in repaired["episodes"]))

    def test_story_season_audit_reports_missing_episode_without_fabricating_distribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"}})
            write_yaml(project / "plan/episodes.yaml", {"episodes": [{"ep": 1}, {"ep": 2}, {"ep": 3}]})
            atomic_write(project / "episodes/ep01/draft_v1.md", "他来了。\n")
            result = audit_story_season(project)
            self.assertFalse(result["passed"])
            self.assertNotIn("episode_chars_cv", result["stats"])
            self.assertEqual(result["stats"]["episodes_checked"], 1)

    def test_story_season_audit_warns_on_uniform_length_and_cli_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_yaml(project / "project.yaml", {"profile": "story", "book": {"title": "夹具"}})
            write_yaml(project / "plan/episodes.yaml", {"episodes": [{"ep": 1}, {"ep": 2}, {"ep": 3}]})
            for ep in (1, 2, 3):
                atomic_write(project / "episodes" / f"ep{ep:02d}" / "draft_v1.md", "他开门，屋里没有人。\n")
            before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
            result = dispatch(parser().parse_args(["story-check", str(project)]))
            self.assertTrue(result["passed"])
            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["stats"]["episode_chars_cv"], 0)
            self.assertTrue(any(item["rule"] == "episode_length_distribution" for item in result["items"]))
            self.assertEqual(before, {path: path.read_bytes() for path in project.rglob("*") if path.is_file()})

    def test_story_audit_can_diagnose_legacy_profile_without_changing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            config = project / "project.yaml"
            write_yaml(config, {"book": {"title": "旧项目"}})
            write_yaml(project / "plan/episodes.yaml", {"episodes": [{"ep": 1}]})
            atomic_write(project / "episodes/ep01/draft_v1.md", "他来了。\n")
            before = config.read_bytes()
            result = audit_story_season(project)
            self.assertEqual(result["project_profile"], "explainer")
            self.assertEqual(config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
