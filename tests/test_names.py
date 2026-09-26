import tempfile
import unittest
from pathlib import Path

from bookflow.common import atomic_write, write_yaml
from bookflow.names import change_plan, check_project, check_text, feedback_candidates
from bookflow.quality import lint


class NameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        write_yaml(self.project / "project.yaml", {"book": {"title": "测试"}})
        self.person = {"id": "P01", "name_de": "Example Person", "name_zh": "测试·新译名", "spoken_name": "新译名",
                       "aliases_zh": [{"text": "旧译名", "replacement": "新译名", "status": "deprecated"}]}
        self.save()

    def save(self, others=None):
        write_yaml(self.project / "analysis/characters.yaml", {"characters": [self.person, *(others or [])]})

    def test_current_files_block_old_name_but_history_is_preserved(self):
        for file in ("source/raw/book.txt", "episodes/ep01/draft_v1.md", "episodes/ep01/human_edit/v1/original.md", "feedback/rounds/one/revised.txt"):
            atomic_write(self.project / file, "旧译名")
        atomic_write(self.project / "episodes/ep01/draft_v2.md", "新译名")
        self.assertTrue(check_project(self.project)["passed"])
        atomic_write(self.project / "analysis/notes.md", "旧译名来了。")
        atomic_write(self.project / "episodes/ep01/preview/voiceover.txt", "旧译名来了。")
        report = check_project(self.project)
        self.assertEqual(len(report["findings"]), 2)
        self.assertFalse(report["passed"])

    def test_draft_lint_rejects_deprecated_name(self):
        draft = self.project / "episodes/ep01/draft_v1.md"
        atomic_write(draft, "旧译名来到门前。[钩子]")
        items = lint(draft)["items"]
        self.assertTrue(any(i["rule"] == "deprecated_name" and i["level"] == "error" for i in items))

    def test_new_name_containing_old_form_is_not_a_false_positive(self):
        self.person.update(name_zh="新译名字", spoken_name="新译名字", aliases_zh=[{"text": "新译名", "replacement": "新译名字", "status": "deprecated"}])
        self.save()
        self.assertEqual(check_text(self.project, "新译名字"), [])
        self.assertEqual(len(check_text(self.project, "新译名")), 1)

    def test_shared_alias_cannot_silently_merge_people(self):
        self.save([{"id": "P02", "name_zh": "某某·旧译名", "spoken_name": "另一个人"}])
        with self.assertRaisesRegex(ValueError, "消歧"):
            check_text(self.project, "旧译名")

    def test_aliases_must_point_to_current_name(self):
        self.person["aliases_zh"][0]["replacement"] = "中间译名"
        self.save()
        with self.assertRaisesRegex(ValueError, "中间跳转"):
            check_text(self.project, "旧译名")

    def test_change_plan_separates_editable_generated_and_evidence(self):
        atomic_write(self.project / "analysis/notes.md", "新译名")
        atomic_write(self.project / "episodes/ep01/draft_v1.md", "新译名")
        atomic_write(self.project / "episodes/ep01/draft_v2.md", "新译名")
        atomic_write(self.project / "episodes/ep01/preview/voiceover.txt", "新译名")
        atomic_write(self.project / "source/raw/book.txt", "新译名")
        plan = change_plan(self.project, "P01", "更正名")
        self.assertEqual(plan["mappings"]["测试·新译名"], "测试·更正名")
        editable = {x["path"] for x in plan["update_with_backup"]}
        self.assertIn("analysis/characters.yaml", editable)
        self.assertIn("episodes/ep01/draft_v2.md", editable)
        self.assertNotIn("episodes/ep01/draft_v1.md", editable)
        self.assertEqual(len(plan["regenerate"]), 1)
        self.assertEqual((self.project / "analysis/notes.md").read_text(), "新译名")
        self.assertEqual(len(plan["preserve_history"]), 2)

    def test_character_change_is_flagged_without_guessing_new_identity(self):
        blocks = [{"text": "今天新译名来了。"}]
        changes = [{"id": "c001", "before_range": [3, 4]}]
        candidates = feedback_candidates(self.project, blocks, changes)
        self.assertEqual(candidates[0]["entity_id"], "P01")
        self.assertEqual(candidates[0]["status"], "needs_identity_review")
        self.assertNotIn("replacement", candidates[0])

    def test_change_plan_rejects_other_persons_name(self):
        self.save([{"id": "P02", "name_zh": "另一个人", "spoken_name": "另一个人"}])
        with self.assertRaisesRegex(ValueError, "另一人物"):
            change_plan(self.project, "P01", "另一个人")


if __name__ == "__main__":
    unittest.main()
