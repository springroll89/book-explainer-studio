"""Local lesson intake must preserve evidence without publishing book feedback."""
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from bookflow.__main__ import parser
from bookflow.common import write_yaml
from bookflow.lessons import _check_command, accept, apply, inbox, observe, propose, report, revert, safe_error, triage


class LessonsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "projects/book"
        write_yaml(self.project / "project.yaml", {"book": {"title": "测试书"}})

    def test_correction_records_exact_quote_and_duplicate_evidence_is_idempotent(self):
        quote = "这个不对，以后不要把角色的话当成事实错误。"
        first = observe(self.project, quote, "chat:test-message-1")
        self.assertEqual(first["status"], "success")
        again = observe(self.project, quote, "chat:test-message-1")
        self.assertEqual(again["status"], "warning")
        rows = inbox(self.project)["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quote"], quote)
        self.assertEqual(rows[0]["source"], "user_message")
        self.assertEqual(rows[0]["status"], "new")
        self.assertEqual(rows[0]["book"], "book")

    def test_non_correction_is_not_recorded(self):
        report = observe(self.project, "请继续写下一集。", "chat:test-message-2")
        self.assertEqual(report["status"], "warning")
        self.assertEqual(inbox(self.project)["items"], [])

    def test_future_books_pronunciation_rule_is_registered_for_triage(self):
        quote = "后续写稿的部分，这个规则也要写进去，后续的书都要这样不要用〇"
        report = observe(self.project, quote, "chat:test-future-books")
        self.assertTrue(report["passed"])
        self.assertEqual(inbox(self.project)["items"][0]["quote"], quote)
        self.assertEqual(triage(self.project)["items"][0]["suggested_scope"], "general_candidate")

    def test_triage_lists_candidates_without_claiming_approval(self):
        observe(self.project, "所有书以后都这样检查。", "chat:test-message-3")
        report = triage(self.project)
        self.assertEqual(report["status"], "warning")
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(report["items"][0]["suggested_scope"], "general_candidate")
        self.assertIn("待你", " ".join(report["next_actions"]))
        self.assertEqual(inbox(self.project)["items"][0]["status"], "new")

    def test_proposal_stays_new_until_user_accepts(self):
        item_id = observe(self.project, "所有书以后都这样检查。", "chat:test-message-4")["id"]
        proposal = propose(self.project, item_id, kind="general_craft",
                           destination="style/story_craft.md", before="旧规则", after="新规则",
                           check="tests/test_lessons.py", rationale="换书仍会发生")
        self.assertEqual(proposal["status"], "warning")
        self.assertEqual(inbox(self.project)["items"][0]["status"], "new")
        preview = triage(self.project)["items"][0]
        self.assertEqual(preview["proposal"]["after"], "新规则")
        self.assertEqual((preview["kind"], preview["destination"], preview["before"],
                          preview["after"], preview["check"]),
                         ("general_craft", "style/story_craft.md", "旧规则", "新规则", "tests/test_lessons.py"))
        denied = accept(self.project, "继续", verify_transcript=False)
        self.assertFalse(denied["passed"])
        accepted = accept(self.project, "可以", verify_transcript=False)
        self.assertTrue(accepted["passed"], accepted)
        self.assertEqual(inbox(self.project)["items"][0]["status"], "triaged")

    def test_accept_requires_readable_matching_user_message(self):
        item_id = observe(self.project, "每次都要复查。", "chat:test-message-6")["id"]
        propose(self.project, item_id, kind="process_bug", destination="bookflow/flow.py",
                before="旧流程", after="新流程", check="tests/test_flow.py", rationale="流程问题")
        with patch("bookflow.approvals._session_user_message", return_value=(None, "会话记录不可读")):
            missing = accept(self.project, "可以")
        self.assertFalse(missing["passed"])
        with patch("bookflow.approvals._session_user_message", return_value=("继续", "")):
            mismatch = accept(self.project, "可以")
        self.assertFalse(mismatch["passed"])
        self.assertEqual(inbox(self.project)["items"][0]["status"], "new")
        with patch("bookflow.approvals._session_user_message", return_value=("可以", "")):
            accepted = accept(self.project, "可以")
        self.assertTrue(accepted["passed"])

    def test_proposal_cannot_escape_workspace(self):
        item_id = observe(self.project, "别再这样写。", "chat:test-message-5")["id"]
        with self.assertRaisesRegex(ValueError, "归宿路径"):
            propose(self.project, item_id, kind="general_craft", destination="../../private.md",
                    before="旧", after="新", check="tests/test_lessons.py", rationale="跨书通用")

    def test_cli_exposes_lesson_commands(self):
        for action in ("inbox", "triage"):
            args = parser().parse_args(["lessons", action, str(self.project)])
            self.assertEqual((args.command, args.action), ("lessons", action))
        args = parser().parse_args(["lessons", "observe", str(self.project), "--quote", "不对", "--evidence", "chat:test"])
        self.assertEqual(args.quote, "不对")
        args = parser().parse_args(["lessons", "accept", str(self.project), "--quote", "可以"])
        self.assertEqual(args.action, "accept")
        args = parser().parse_args(["lessons", "apply", str(self.project), "L20260923-01"])
        self.assertEqual((args.action, args.lesson_id), ("apply", "L20260923-01"))
        args = parser().parse_args(["lessons", "report", str(self.project)])
        self.assertEqual(args.action, "report")
        args = parser().parse_args(["lessons", "revert", str(self.project), "L20260923-01",
                                    "--quote", "撤回 L20260923-01"])
        self.assertEqual(args.action, "revert")

    def test_script_error_redacts_credential_like_values(self):
        cleaned = safe_error("api_key=private-value Bearer abcdef123 /Users/tester/file")
        self.assertNotIn("private-value", cleaned)
        self.assertNotIn("abcdef123", cleaned)
        self.assertNotIn("tester", cleaned)

    def test_check_command_accepts_only_existing_local_unittest_modules(self):
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_story_profile.py").write_text("", encoding="utf-8")
        (tests / "test_lessons.py").write_text("", encoding="utf-8")
        check = ".venv/bin/python -m unittest tests.test_story_profile tests.test_lessons"
        self.assertEqual(_check_command(self.root, check),
                         [sys.executable, "-m", "unittest", "tests.test_story_profile", "tests.test_lessons"])
        self.assertIsNone(_check_command(self.root, check + " --failfast"))
        self.assertIsNone(_check_command(self.root, ".venv/bin/python -m unittest tests.test_missing"))
        self.assertIsNone(_check_command(self.root, "python -c print(1)"))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True, check=True).stdout.strip()

    def prepare_git(self):
        (self.root / ".gitignore").write_text("projects/\nlessons/inbox.yaml\nlessons/.inbox.lock\n", encoding="utf-8")
        target = self.root / "style/story_craft.md"
        target.parent.mkdir(parents=True)
        target.write_text("旧规则\n", encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text("# 变更记录\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        self.git("add", ".gitignore", "style/story_craft.md", "CHANGELOG.md")
        self.git("commit", "-qm", "Fixture baseline")
        return target

    def accepted_shared(self, *, check="tests/test_lessons.py"):
        lesson_id = observe(self.project, "以后请遵守这条规则。", "chat:apply-test")["id"]
        propose(self.project, lesson_id, kind="general_craft", destination="style/story_craft.md",
                before="旧规则", after="新规则", check=check, rationale="跨书通用")
        accept(self.project, "可以", verify_transcript=False)
        return lesson_id

    def test_apply_commits_only_shared_rule_and_changelog(self):
        target = self.prepare_git()
        lesson_id = self.accepted_shared()
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            result = apply(self.project, lesson_id)
        self.assertTrue(result["passed"], result)
        self.assertEqual(target.read_text(encoding="utf-8"), "新规则\n")
        self.assertEqual(inbox(self.project)["items"][0]["status"], "applied")
        self.assertEqual(inbox(self.project)["items"][0]["commit"], self.git("rev-parse", "HEAD"))
        self.assertIn(lesson_id, (self.root / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertEqual(set(self.git("show", "--format=", "--name-only", "HEAD").splitlines()),
                         {"style/story_craft.md", "CHANGELOG.md"})
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_apply_restores_files_when_check_fails(self):
        target = self.prepare_git()
        lesson_id = self.accepted_shared()
        with patch("bookflow.lessons._verify_workspace", return_value=["自检失败"]):
            result = apply(self.project, lesson_id)
        self.assertFalse(result["passed"])
        self.assertEqual(target.read_text(encoding="utf-8"), "旧规则\n")
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), "# 变更记录\n")
        self.assertEqual(inbox(self.project)["items"][0]["status"], "triaged")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_apply_does_not_commit_unrelated_changes_from_verification(self):
        target = self.prepare_git()
        lesson_id = self.accepted_shared()
        def altered_workspace(*_args):
            (self.root / ".gitignore").write_text("projects/\n# test touched this file\n", encoding="utf-8")
            return []
        with patch("bookflow.lessons._verify_workspace", side_effect=altered_workspace):
            with self.assertRaisesRegex(ValueError, "非本教训文件"):
                apply(self.project, lesson_id)
        self.assertEqual(target.read_text(encoding="utf-8"), "旧规则\n")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Fixture baseline")
        self.assertIn("# test touched this file", (self.root / ".gitignore").read_text(encoding="utf-8"))

    def test_apply_refuses_dirty_tree_and_unconfirmed_lesson(self):
        target = self.prepare_git()
        lesson_id = observe(self.project, "以后请遵守这条规则。", "chat:apply-test")["id"]
        propose(self.project, lesson_id, kind="general_craft", destination="style/story_craft.md",
                before="旧规则", after="新规则", check="tests/test_lessons.py", rationale="跨书通用")
        with self.assertRaisesRegex(ValueError, "已确认"):
            apply(self.project, lesson_id)
        accept(self.project, "可以", verify_transcript=False)
        target.write_text("用户尚未提交的改动\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未提交改动"):
            apply(self.project, lesson_id)
        self.assertEqual(target.read_text(encoding="utf-8"), "用户尚未提交的改动\n")

    def test_apply_never_commits_book_specific_rule(self):
        self.prepare_git()
        lesson_id = observe(self.project, "这一集改成短句。", "chat:book-only")["id"]
        propose(self.project, lesson_id, kind="book_style", destination="feedback/rules.yaml",
                before="旧句", after="新句", check="unverified", rationale="仅此书")
        accept(self.project, "可以", verify_transcript=False)
        result = apply(self.project, lesson_id)
        self.assertFalse(result["passed"])
        self.assertEqual(inbox(self.project)["items"][0]["status"], "triaged")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Fixture baseline")

    def test_report_lists_only_applied_unverified_rules_without_quotes(self):
        self.prepare_git()
        lesson_id = self.accepted_shared(check="unverified")
        self.assertEqual(report(self.project)["items"], [])
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            apply(self.project, lesson_id)
        rows = report(self.project)["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], lesson_id)
        self.assertEqual(rows[0]["check"], "unverified")
        self.assertEqual(rows[0]["reason"], "unverified")
        self.assertNotIn("以后请遵守", str(rows))

    def test_report_flags_missing_check_file(self):
        self.prepare_git()
        test_file = self.root / "tests/test_rule.py"
        test_file.parent.mkdir()
        test_file.write_text("# check fixture\n", encoding="utf-8")
        self.git("add", "tests/test_rule.py")
        self.git("commit", "-qm", "Add check fixture")
        lesson_id = self.accepted_shared(check="tests/test_rule.py")
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            apply(self.project, lesson_id)
        self.assertEqual(report(self.project)["items"], [])
        test_file.unlink()
        self.assertEqual(report(self.project)["items"][0]["reason"], "missing_check")

    def test_revert_restores_rule_and_commits_without_erasing_history(self):
        target = self.prepare_git()
        lesson_id = self.accepted_shared()
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            applied = apply(self.project, lesson_id)
            reverted = revert(self.project, lesson_id, f"撤回 {lesson_id}", verify_transcript=False)
        self.assertTrue(reverted["passed"], reverted)
        self.assertNotEqual(applied["commit"], reverted["revert_commit"])
        self.assertEqual(target.read_text(encoding="utf-8"), "旧规则\n")
        self.assertEqual(inbox(self.project)["items"][0]["status"], "rejected")
        self.assertEqual(report(self.project)["items"], [])
        self.assertEqual(set(self.git("show", "--format=", "--name-only", "HEAD").splitlines()),
                         {"style/story_craft.md", "CHANGELOG.md"})
        self.assertIn("撤回共享规则提交", (self.root / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_revert_requires_matching_latest_user_message(self):
        self.prepare_git()
        lesson_id = self.accepted_shared()
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            apply(self.project, lesson_id)
        with patch("bookflow.approvals._session_user_message", return_value=("继续", "")):
            with self.assertRaisesRegex(ValueError, "无法核对"):
                revert(self.project, lesson_id, f"撤回 {lesson_id}")
        self.assertEqual(inbox(self.project)["items"][0]["status"], "applied")

    def test_revert_refuses_later_rule_edits(self):
        target = self.prepare_git()
        lesson_id = self.accepted_shared()
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            apply(self.project, lesson_id)
        target.write_text("新规则\n后续改动\n", encoding="utf-8")
        self.git("add", "style/story_craft.md")
        self.git("commit", "-qm", "Later rule edit")
        with self.assertRaisesRegex(ValueError, "后又被修改"):
            revert(self.project, lesson_id, f"撤回 {lesson_id}", verify_transcript=False)
        self.assertEqual(target.read_text(encoding="utf-8"), "新规则\n后续改动\n")
        self.assertEqual(inbox(self.project)["items"][0]["status"], "applied")

    def test_revert_restores_files_when_check_fails(self):
        target = self.prepare_git()
        lesson_id = self.accepted_shared()
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            apply(self.project, lesson_id)
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        with patch("bookflow.lessons._verify_workspace", return_value=["回退检查失败"]):
            result = revert(self.project, lesson_id, f"撤回 {lesson_id}", verify_transcript=False)
        self.assertFalse(result["passed"])
        self.assertEqual(target.read_text(encoding="utf-8"), "新规则\n")
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), changelog)
        self.assertEqual(inbox(self.project)["items"][0]["status"], "applied")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_revert_removes_rule_file_created_by_lesson(self):
        self.prepare_git()
        target = self.root / "style/new_rule.md"
        lesson_id = observe(self.project, "以后增加这条规则。", "chat:new-rule")["id"]
        propose(self.project, lesson_id, kind="general_craft", destination="style/new_rule.md",
                before="", after="新增规则", check="unverified", rationale="跨书通用")
        accept(self.project, "可以", verify_transcript=False)
        with patch("bookflow.lessons._verify_workspace", return_value=[]):
            applied = apply(self.project, lesson_id)
            self.assertTrue(target.is_file())
            reverted = revert(self.project, lesson_id, f"撤回 {lesson_id}", verify_transcript=False)
        self.assertTrue(applied["passed"])
        self.assertTrue(reverted["passed"])
        self.assertFalse(target.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
