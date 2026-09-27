"""Regression checks for per-book leakage into reusable studio files."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bookflow.common import atomic_write, write_yaml
from bookflow.hygiene import audit


class HygieneTests(unittest.TestCase):
    def test_tracked_shared_tree_is_clean(self):
        root = Path(__file__).resolve().parents[1]
        report = audit(root, tracked_only=True)
        self.assertTrue(report["passed"], report["findings"][:12])

    def test_document_examples_do_not_hide_registered_book_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atomic_write(root / "docs/guide.md", "示例：projects/my-book/。\n")
            self.assertTrue(audit(root)["passed"])
            write_yaml(root / "projects/book/project.yaml", {"book": {"title": "专属故事名"}})
            atomic_write(root / "docs/guide.md", "示例：projects/my-book/；真实：projects/book/。\n")
            report = audit(root)
            self.assertEqual([item["rule"] for item in report["findings"]], ["book_path"])

    def test_external_project_catalog_catches_private_names(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            studio = base / "studio"
            catalog = base / "private-projects"
            write_yaml(catalog / "book/project.yaml", {"book": {"title": "私有书名"}})
            write_yaml(catalog / "book/analysis/characters.yaml", {
                "characters": [{"name_zh": "私有人名"}]})
            atomic_write(studio / "docs/guide.md", "私有书名与私有人名不应出现在共享文档。\n")
            report = audit(studio, projects_dir=catalog)
            self.assertEqual([item["rule"] for item in report["findings"]], ["book_term"])
            self.assertNotIn("私有人名", str(report))

    def test_book_terms_and_hardcoded_paths_are_detected_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_yaml(root / "projects/book/project.yaml", {"book": {"title": "专属故事名"}})
            write_yaml(root / "projects/book/analysis/characters.yaml", {
                "characters": [{"name_zh": "专属人物名", "spoken_name": "人物简称"}]})
            atomic_write(root / "tools/render.py", "# 专属故事名、专属人物名\n"
                         "FILE = '/Users/example/projects/book/ep01_v6.mp3'\n"
                         "WINDOWS = 'C:\\\\Users\\\\example\\\\render.py'\n")
            report = audit(root)
            self.assertFalse(report["passed"])
            rules = {item["rule"] for item in report["findings"]}
            self.assertTrue({"book_term", "book_path", "absolute_path", "fixed_episode_asset"} <= rules)
            self.assertNotIn("专属故事名", str(report))
            self.assertNotIn("/Users/example", str(report))

    def test_length_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atomic_write(root / ".agents/skills/sample/SKILL.md", "line\n" * 81)
            atomic_write(root / "style/story_craft.md", "line\n" * 201)
            atomic_write(root / "AGENTS.md", "line\n" * 61)
            rules = {item["rule"] for item in audit(root)["findings"]}
            self.assertEqual(rules, {"skill_over_80_lines", "craft_over_200_lines", "agents_over_60_lines"})

    def test_tracked_audit_also_checks_new_rule_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "style/story_craft.md"
            atomic_write(target, "不要把专属书名写进共享规则。\n")
            write_yaml(root / "projects/book/project.yaml", {"book": {"title": "专属书名"}})
            with patch("bookflow.hygiene.subprocess.run", return_value=SimpleNamespace(stdout=b"")):
                report = audit(root, tracked_only=True, include_paths=(target,))
            self.assertEqual([item["rule"] for item in report["findings"]], ["book_term"])

    def test_local_document_links_exist_and_stay_inside_studio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "studio"
            atomic_write(root / "docs/guide.md",
                         "[存在](../style/rules.md#写法) [缺失](../roles/missing.md) "
                         "[网站](https://example.com/guide)\n")
            atomic_write(root / "style/rules.md", "# 写法\n")
            atomic_write(root / "README.md", "[入口](docs/guide.md)\n")
            atomic_write(Path(temporary) / "outside.md", "仓库之外\n")
            atomic_write(root / "docs/outside.md", "[越界](../../outside.md)\n")
            report = audit(root)
            self.assertEqual([(item["path"], item["rule"]) for item in report["findings"]],
                             [("docs/guide.md", "broken_doc_link"),
                              ("docs/outside.md", "doc_link_outside_repo")])

    def test_document_reference_link_is_checked_without_echoing_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_write(root / "docs/guide.md", "详见[说明][flow]。\n\n[flow]: MISSING_SECRET.md\n")
            report = audit(root)
            self.assertEqual([item["rule"] for item in report["findings"]], ["broken_doc_link"])
            self.assertNotIn("MISSING_SECRET", str(report))

    def test_current_skill_count_must_not_be_hardcoded_in_docs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_write(root / "docs/guide.md", "本文件是 9 个项目技能的说明。\n"
                         "2024 年首版为 31 项测试，这是历史记录。\n")
            self.assertEqual([item["rule"] for item in audit(root)["findings"]],
                             ["manual_current_count"])

    def test_duplicate_long_rule_across_wrapped_paragraphs_without_echoing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rule = "每次修改定稿后都必须重新核对来源段落，并保留用户亲自确认的最终版本，不得把机器检查误称为人工通过。"
            atomic_write(root / "docs/first.md", rule + "\n")
            atomic_write(root / "roles/second.md", rule[:18] + "\n" + rule[18:] + "\n")
            report = audit(root)
            self.assertEqual([item["rule"] for item in report["findings"]], ["duplicate_rule_text"])
            self.assertNotIn(rule, str(report))

    def test_duplicate_code_examples_are_not_reported_as_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = "python -m bookflow produce projects/sample ep01 --until render --test-mode"
            atomic_write(root / "docs/first.md", "```sh\n" + command + "\n```\n")
            atomic_write(root / "roles/second.md", "```sh\n" + command + "\n```\n")
            self.assertTrue(audit(root)["passed"])


if __name__ == "__main__":
    unittest.main()
