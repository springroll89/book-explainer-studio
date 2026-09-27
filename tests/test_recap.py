"""Legacy recap migration must be explicit, lossless, and honest about review."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bookflow.__main__ import dispatch, parser
from bookflow.approvals import confirmation_state, record_confirmation
from bookflow.common import atomic_write, load_yaml, sha256_file, write_json, write_yaml
from bookflow.recap import check, context, entry_sha256, inspect, migrate, snapshot_final, update_working
from bookflow.review import evaluate


class RecapMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "projects/book"
        write_yaml(self.project / "project.yaml", {"book": {"title": "测试书"}})
        generation = "a" * 64
        write_json(self.project / "source/current.json", {"generation": generation})
        write_json(self.project / "source/imports" / generation / "manifest.json", {"generation": generation})
        write_json(self.project / "source/imports" / generation / "chapters.json", [])
        atomic_write(self.project / "source/imports" / generation / "paragraphs.jsonl", "")
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": 1}, {"ep": 2}]})
        draft = self.project / "episodes/ep01/draft_v1.md"
        atomic_write(draft, "---\nepisode: 1\n---\n门开了。")
        self.working = self.project / "episodes/working_continuity.yaml"
        write_yaml(self.working, {"episodes": [{"ep": 1, "draft_path": "episodes/ep01/draft_v1.md",
            "draft_sha256": sha256_file(draft), "source_generation": generation,
            "revealed": ["门开了"], "identities_known": {"P01": "来客"}, "recap_points": ["门开了"]}]})
        self.ledger = self.project / "series_ledger.yaml"
        write_yaml(self.ledger, {"episodes": [{"ep": 1, "final_sha256": "b" * 64,
            "source_generation": generation, "revealed": ["旧定稿记录"]}]})

    def test_preview_is_read_only_and_write_preserves_both_sources(self):
        before = (self.working.read_bytes(), self.ledger.read_bytes())
        preview = migrate(self.project)
        recap = self.project / "episodes/recap.yaml"
        self.assertTrue(preview["passed"])
        self.assertFalse(preview["written"])
        self.assertFalse(recap.exists())
        self.assertEqual(preview["episodes"], [1, 2])
        self.assertEqual(preview["stale_episodes"], [1])
        self.assertEqual(preview["missing_episodes"], [2])
        result = migrate(self.project, write=True)
        self.assertTrue(result["written"])
        data = load_yaml(recap)
        self.assertEqual(data["migration_state_at_creation"], "needs_semantic_review")
        first = data["episodes"][0]
        self.assertEqual(first["candidates"]["working"]["legacy_entry"]["revealed"], ["门开了"])
        self.assertEqual(first["candidates"]["final"]["legacy_entry"]["revealed"], ["旧定稿记录"])
        self.assertTrue(first["candidates"]["working"]["migration_snapshot"]["file_current"])
        self.assertTrue(first["candidates"]["working"]["migration_snapshot"]["dependencies_current"])
        self.assertFalse(first["candidates"]["final"]["migration_snapshot"]["file_current"])
        self.assertEqual(first["semantic_review"]["status"], "pending")
        self.assertIsNone(first["selected_basis"])
        self.assertEqual((self.working.read_bytes(), self.ledger.read_bytes()), before)
        saved = recap.read_bytes()
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            migrate(self.project, write=True)
        self.assertEqual(recap.read_bytes(), saved)

    def test_superseded_draft_is_not_current(self):
        atomic_write(self.project / "episodes/ep01/draft_v2.md", "新版稿。")
        report = migrate(self.project, write=True)
        self.assertEqual(report["stale_episodes"], [1])
        candidate = load_yaml(self.project / "episodes/recap.yaml")["episodes"][0]["candidates"]["working"]
        self.assertFalse(candidate["migration_snapshot"]["file_current"])

    def test_unsafe_legacy_path_is_not_read(self):
        data = load_yaml(self.working)
        data["episodes"][0]["draft_path"] = "../../private.txt"
        write_yaml(self.working, data)
        migrate(self.project, write=True)
        candidate = load_yaml(self.project / "episodes/recap.yaml")["episodes"][0]["candidates"]["working"]
        self.assertIsNone(candidate["migration_snapshot"]["file_sha256"])
        self.assertFalse(candidate["migration_snapshot"]["file_current"])

    def test_changed_prior_draft_marks_later_dependency_stale(self):
        first = self.project / "episodes/ep01/draft_v1.md"
        second = self.project / "episodes/ep02/draft_v1.md"
        atomic_write(second, "---\nepisode: 2\n---\n他走了。")
        data = load_yaml(self.working)
        data["episodes"].append({"ep": 2, "draft_path": "episodes/ep02/draft_v1.md",
            "draft_sha256": sha256_file(second), "source_generation": "a" * 64,
            "dependency_hashes": {"1": sha256_file(first)}})
        write_yaml(self.working, data)
        atomic_write(first, first.read_text(encoding="utf-8") + "\n又开了一次。")
        report = migrate(self.project, write=True)
        self.assertIn(2, report["stale_episodes"])
        candidate = load_yaml(self.project / "episodes/recap.yaml")["episodes"][1]["candidates"]["working"]
        self.assertTrue(candidate["migration_snapshot"]["file_current"])
        self.assertFalse(candidate["migration_snapshot"]["dependencies_current"])

    def test_working_only_project_can_stage_recap(self):
        self.ledger.unlink()
        result = migrate(self.project, write=True)
        self.assertTrue(result["written"])
        first = load_yaml(self.project / "episodes/recap.yaml")["episodes"][0]
        self.assertEqual(list(first["candidates"]), ["working"])
        self.assertTrue(self.working.is_file())

    def test_missing_source_generation_leaves_legacy_files_unchanged(self):
        before = self.working.read_bytes()
        (self.project / "source/current.json").unlink()
        with self.assertRaisesRegex(ValueError, "原文批次"):
            migrate(self.project, write=True)
        self.assertEqual(self.working.read_bytes(), before)
        self.assertFalse((self.project / "episodes/recap.yaml").exists())

    def test_incomplete_source_batch_cannot_stage_recap(self):
        (self.project / "source/imports" / ("a" * 64) / "manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "原文批次文件不完整"):
            migrate(self.project, write=True)
        self.assertFalse((self.project / "episodes/recap.yaml").exists())

    def test_mismatched_source_manifest_cannot_stage_recap(self):
        write_json(self.project / "source/imports" / ("a" * 64) / "manifest.json",
                   {"generation": "b" * 64})
        with self.assertRaisesRegex(ValueError, "当前指针不一致"):
            migrate(self.project, write=True)
        self.assertFalse((self.project / "episodes/recap.yaml").exists())

    def test_malformed_legacy_record_cannot_create_recap(self):
        write_yaml(self.working, {"episodes": [{"ep": 1}, {"ep": 1}]})
        with self.assertRaisesRegex(ValueError, "重复集号"):
            migrate(self.project, write=True)
        self.assertFalse((self.project / "episodes/recap.yaml").exists())

    def test_cli_preview_does_not_mutate_project(self):
        args = parser().parse_args(["recap", "migrate", str(self.project)])
        result = dispatch(args)
        self.assertFalse(result["written"])
        self.assertFalse((self.project / "episodes/recap.yaml").exists())

    def reviewed_working_recap(self):
        working = load_yaml(self.working)
        working["episodes"][0].update(threads_setup=[], threads_payoff=[], open_questions=[])
        write_yaml(self.working, working)
        migrate(self.project, write=True)
        path = self.project / "episodes/recap.yaml"
        data = load_yaml(path)
        row = data["episodes"][0]
        candidate = row["candidates"]["working"]
        row["selected_basis"] = "working"
        row["semantic_review"] = {
            "status": "completed", "reviewer": "fixture-agent",
            "note": "已逐句核对测试稿的开门线索与人物指代；这是隔离测试记录。",
            "ending_summary": "门被打开。", "unresolved": [],
            "reviewed_file_sha256": candidate["migration_snapshot"]["file_sha256"],
            "reviewed_entry_sha256": entry_sha256(candidate["legacy_entry"]),
        }
        write_yaml(path, data)
        return path

    def test_pending_and_reviewed_recap_context(self):
        migrate(self.project, write=True)
        self.assertFalse(check(self.project, required_episodes=[1])["passed"])
        (self.project / "episodes/recap.yaml").unlink()
        self.reviewed_working_recap()
        self.assertTrue(check(self.project, required_episodes=[1])["passed"])
        prior = context(self.project, 2)
        self.assertTrue(prior["passed"], prior)
        self.assertEqual(prior["episodes"][0]["actual"]["revealed"], ["门开了"])
        self.assertEqual(prior["episodes"][0]["file_sha256"],
                         sha256_file(self.project / "episodes/ep01/draft_v1.md"))
        self.assertEqual(prior["episodes"][0]["ending_summary"], "门被打开。")
        fingerprints = inspect(self.project)
        self.assertEqual(fingerprints["episodes"][0]["candidates"]["working"]["entry_sha256"],
                         entry_sha256(load_yaml(self.working)["episodes"][0]))
        self.assertNotIn("门开了", str(fingerprints))
        self.assertEqual(parser().parse_args(["recap", "check", str(self.project)]).action, "check")
        self.assertEqual(parser().parse_args(["recap", "context", str(self.project), "2"]).ep, 2)
        self.assertEqual(parser().parse_args(["recap", "inspect", str(self.project)]).action, "inspect")

    def test_draft_review_does_not_bypass_pending_or_stale_recap(self):
        config = load_yaml(self.project / "project.yaml")
        config["drafting"] = {"mode": "full_season_review"}
        write_yaml(self.project / "project.yaml", config)
        second = self.project / "episodes/ep02/draft_v1.md"
        atomic_write(second, "---\nepisode: 2\n---\n第二集。")
        first = self.project / "episodes/ep01/draft_v1.md"
        report = {"draft_sha256": sha256_file(second), "source_generation": "a" * 64,
                  "dependency_hashes": {"1": sha256_file(first)}}
        migrate(self.project, write=True)
        pending = evaluate(self.project, 2, second, report)
        self.assertFalse(pending["passed"])
        self.assertIn("尚未选择有效", "；".join(pending["errors"]))
        (self.project / "episodes/recap.yaml").unlink()
        self.reviewed_working_recap()
        reviewed = evaluate(self.project, 2, second, report)
        self.assertFalse(any("前情" in error or "工作稿" in error for error in reviewed["errors"]), reviewed)
        atomic_write(first, first.read_text(encoding="utf-8") + "\n新句。")
        stale = evaluate(self.project, 2, second, report)
        self.assertIn("失效", "；".join(stale["errors"]))

    def test_review_becomes_stale_after_draft_or_entry_change(self):
        recap = self.reviewed_working_recap()
        draft = self.project / "episodes/ep01/draft_v1.md"
        atomic_write(draft, draft.read_text(encoding="utf-8") + "\n又来了一人。")
        self.assertFalse(check(self.project, required_episodes=[1])["passed"])
        atomic_write(draft, "---\nepisode: 1\n---\n门开了。")
        data = load_yaml(recap)
        data["episodes"][0]["candidates"]["working"]["legacy_entry"]["revealed"] = ["被改动的结论"]
        write_yaml(recap, data)
        result = check(self.project, required_episodes=[1])
        self.assertFalse(result["passed"])
        self.assertIn("复核记录已失效", "；".join(result["errors"]))

    def test_unresolved_issue_or_changed_source_manifest_blocks_recap(self):
        recap = self.reviewed_working_recap()
        data = load_yaml(recap)
        data["episodes"][0]["semantic_review"]["unresolved"] = ["人物指代待核"]
        write_yaml(recap, data)
        self.assertFalse(check(self.project, required_episodes=[1])["passed"])
        data["episodes"][0]["semantic_review"]["unresolved"] = []
        write_yaml(recap, data)
        write_json(self.project / "source/imports" / ("a" * 64) / "manifest.json",
                   {"generation": "b" * 64})
        result = check(self.project, required_episodes=[1])
        self.assertFalse(result["passed"])
        self.assertIn("批次清单", "；".join(result["errors"]))

    def test_final_candidate_cannot_pass_without_script_confirmation(self):
        final = self.project / "episodes/ep01/final.md"
        atomic_write(final, "---\nepisode: 1\nstatus: final\n---\n门开了。")
        ledger = load_yaml(self.ledger)
        ledger["episodes"][0].update(final_sha256=sha256_file(final),
            threads_setup=[], threads_payoff=[], identities_known={}, open_questions=[], recap_points=[])
        write_yaml(self.ledger, ledger)
        migrate(self.project, write=True)
        recap = self.project / "episodes/recap.yaml"
        data = load_yaml(recap)
        row = data["episodes"][0]
        candidate = row["candidates"]["final"]
        row["selected_basis"] = "final"
        row["semantic_review"] = {"status": "completed", "reviewer": "fixture-agent",
            "note": "已核对测试稿。", "ending_summary": "门被打开。", "unresolved": [],
            "reviewed_file_sha256": candidate["migration_snapshot"]["file_sha256"],
            "reviewed_entry_sha256": entry_sha256(candidate["legacy_entry"])}
        write_yaml(recap, data)
        result = check(self.project, required_episodes=[1])
        self.assertFalse(result["passed"])
        self.assertIn("文案确认", "；".join(result["errors"]))
        self.assertFalse((self.project / "approvals").exists())

    def test_final_candidate_tracks_test_confirmation_and_revoke(self):
        final = self.project / "episodes/ep01/final.md"
        atomic_write(final, "---\nepisode: 1\nstatus: final\n---\n门开了。")
        ledger = load_yaml(self.ledger)
        ledger["episodes"][0].update(final_sha256=sha256_file(final),
            threads_setup=[], threads_payoff=[], identities_known={}, open_questions=[], recap_points=[])
        write_yaml(self.ledger, ledger)
        migrate(self.project, write=True)
        path = self.project / "episodes/recap.yaml"
        data = load_yaml(path)
        row = data["episodes"][0]
        candidate = row["candidates"]["final"]
        row["selected_basis"] = "final"
        row["semantic_review"] = {"status": "completed", "reviewer": "fixture-agent",
            "note": "已核对隔离测试稿。", "ending_summary": "门被打开。", "unresolved": [],
            "reviewed_file_sha256": candidate["migration_snapshot"]["file_sha256"],
            "reviewed_entry_sha256": entry_sha256(candidate["legacy_entry"])}
        write_yaml(path, data)
        approved = record_confirmation(self.project, "script", "拍板文案", [1],
                                       session="recap-test-fixture", verify_transcript=False)
        self.assertTrue(approved["passed"], approved)
        self.assertTrue(check(self.project, required_episodes=[1])["passed"])
        revoked = record_confirmation(self.project, "script", "撤回文案", [1],
                                      session="recap-test-fixture", verify_transcript=False)
        self.assertTrue(revoked["passed"], revoked)
        self.assertFalse(check(self.project, required_episodes=[1])["passed"])

    @staticmethod
    def actual(reveal="门开了"):
        return {"revealed": [reveal], "threads_setup": [], "threads_payoff": [],
                "identities_known": {}, "open_questions": [], "recap_points": [reveal]}

    def test_new_project_writes_recap_without_legacy_files_and_keeps_review_on_rerun(self):
        self.working.unlink()
        self.ledger.unlink()
        draft = self.project / "episodes/ep01/draft_v1.md"
        actual_path = self.project / "episodes/ep01/actual.yaml"
        write_yaml(actual_path, self.actual())
        args = parser().parse_args(["recap", "update-working", str(self.project), "1",
                                    "--draft", str(draft), "--actual", str(actual_path)])
        self.assertTrue(dispatch(args)["written"])
        path = self.project / "episodes/recap.yaml"
        data = load_yaml(path)
        self.assertEqual(data["episodes"][0]["candidates"]["working"]["entry"]["revealed"], ["门开了"])
        self.assertEqual(data["episodes"][0]["semantic_review"]["status"], "pending")
        fingerprint = inspect(self.project)["episodes"][0]["candidates"]["working"]
        data["episodes"][0]["semantic_review"] = {
            "status": "completed", "reviewer": "fixture-agent", "note": "已核对隔离测试稿。",
            "ending_summary": "门被打开。", "unresolved": [],
            "reviewed_file_sha256": fingerprint["file_sha256"],
            "reviewed_entry_sha256": fingerprint["entry_sha256"]}
        write_yaml(path, data)
        before = path.read_bytes()
        self.assertFalse(dispatch(args)["written"])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(check(self.project, required_episodes=[1])["passed"])
        self.assertFalse(self.working.exists())
        self.assertFalse(self.ledger.exists())

    def test_new_draft_archives_prior_recap_and_requires_fresh_review(self):
        self.working.unlink()
        self.ledger.unlink()
        first = self.project / "episodes/ep01/draft_v1.md"
        update_working(self.project, 1, first, self.actual())
        path = self.project / "episodes/recap.yaml"
        data = load_yaml(path)
        data["episodes"][0]["semantic_review"]["status"] = "completed"
        write_yaml(path, data)
        second = self.project / "episodes/ep01/draft_v2.md"
        atomic_write(second, "---\nepisode: 1\n---\n门又关了。")
        changed = update_working(self.project, 1, second, self.actual("门又关了"))
        self.assertTrue(changed["written"])
        row = load_yaml(path)["episodes"][0]
        self.assertEqual(row["history"][0]["candidates"]["working"]["entry"]["revealed"], ["门开了"])
        self.assertEqual(row["history"][0]["semantic_review"]["status"], "completed")
        self.assertEqual(row["semantic_review"]["status"], "pending")
        self.assertEqual(row["candidates"]["working"]["entry"]["draft_sha256"], sha256_file(second))
        self.assertFalse(check(self.project, required_episodes=[1])["passed"])
        self.assertTrue(first.is_file())

    def test_parallel_draft_requires_later_dependency_refresh(self):
        self.working.unlink()
        self.ledger.unlink()
        second = self.project / "episodes/ep02/draft_v1.md"
        atomic_write(second, "---\nepisode: 2\n---\n他走了。")
        first_write = update_working(self.project, 2, second, self.actual("他走了"))
        self.assertEqual(first_write["pending_dependencies"], [1])
        self.assertFalse(check(self.project, required_episodes=[2])["passed"])
        first = self.project / "episodes/ep01/draft_v1.md"
        update_working(self.project, 1, first, self.actual())
        refreshed = update_working(self.project, 2, second, self.actual("他走了"))
        self.assertTrue(refreshed["written"])
        row = load_yaml(self.project / "episodes/recap.yaml")["episodes"][1]
        self.assertEqual(row["candidates"]["working"]["entry"]["dependency_hashes"]["1"], sha256_file(first))
        self.assertEqual(len(row["history"]), 1)

    def test_rejected_working_inputs_leave_existing_recap_unchanged(self):
        self.working.unlink()
        self.ledger.unlink()
        draft = self.project / "episodes/ep01/draft_v1.md"
        update_working(self.project, 1, draft, self.actual())
        path = self.project / "episodes/recap.yaml"
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "实际前情"):
            update_working(self.project, 1, draft, {"revealed": ["门开了"]})
        outside = self.project / "episodes/ep02/draft_v1.md"
        atomic_write(outside, "---\nepisode: 2\n---\n他走了。")
        with self.assertRaisesRegex(ValueError, "本集目录"):
            update_working(self.project, 1, outside, self.actual())
        write_json(self.project / "source/imports" / ("a" * 64) / "manifest.json", {"generation": "b" * 64})
        with self.assertRaisesRegex(ValueError, "批次清单"):
            update_working(self.project, 1, draft, self.actual())
        self.assertEqual(path.read_bytes(), before)

    def test_working_writer_rejects_symlinked_episode_directory(self):
        external = self.project.parent.parent / "external-episode"
        external.mkdir()
        draft = external / "draft_v1.md"
        atomic_write(draft, "---\nepisode: 2\n---\n他走了。")
        episode_link = self.project / "episodes/ep02"
        episode_link.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "本集目录"):
            update_working(self.project, 2, episode_link / "draft_v1.md", self.actual("他走了"))
        self.assertFalse((self.project / "episodes/recap.yaml").exists())

    def reviewed_direct_working(self, ep: int, draft: Path, actual: dict):
        update_working(self.project, ep, draft, actual)
        path = self.project / "episodes/recap.yaml"
        data = load_yaml(path)
        row = next(item for item in data["episodes"] if item["ep"] == ep)
        fingerprint = next(item for item in inspect(self.project)["episodes"] if item["ep"] == ep)["candidates"]["working"]
        row["semantic_review"] = {"status": "completed", "reviewer": "fixture-agent",
                                  "note": "已逐句核对隔离测试稿。", "ending_summary": "本集结束。",
                                  "unresolved": [], "reviewed_file_sha256": fingerprint["file_sha256"],
                                  "reviewed_entry_sha256": fingerprint["entry_sha256"]}
        write_yaml(path, data)

    def test_confirmed_identical_final_carries_review_and_revoke_invalidates_it(self):
        self.working.unlink()
        self.ledger.unlink()
        draft = self.project / "episodes/ep01/draft_v1.md"
        self.reviewed_direct_working(1, draft, self.actual())
        final = self.project / "episodes/ep01/final.md"
        atomic_write(final, "---\nepisode: 1\nstatus: final\n---\n门开了。")
        recap = self.project / "episodes/recap.yaml"
        before = recap.read_bytes()
        with self.assertRaisesRegex(ValueError, "文案确认"):
            snapshot_final(self.project, 1)
        self.assertEqual(recap.read_bytes(), before)
        approved = record_confirmation(self.project, "script", "拍板文案", [1],
                                       session="recap-test-fixture", verify_transcript=False)
        self.assertTrue(approved["passed"], approved)
        result = snapshot_final(self.project, 1)
        self.assertTrue(result["review_carried"])
        row = load_yaml(recap)["episodes"][0]
        self.assertEqual(row["candidates"]["final"]["entry"]["final_sha256"], sha256_file(final))
        self.assertIn("carried_from_identical_spoken", row["semantic_review"])
        self.assertEqual(row["history"][0]["selected_basis"], "working")
        self.assertTrue(check(self.project, required_episodes=[1])["passed"])
        self.assertEqual(context(self.project, 2)["episodes"][0]["basis"], "final")
        saved = recap.read_bytes()
        self.assertFalse(snapshot_final(self.project, 1)["written"])
        self.assertEqual(recap.read_bytes(), saved)
        atomic_write(final, final.read_text(encoding="utf-8").replace(
            "status: final\n", "status: final\nfixture_note: metadata-only\n", 1))
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        refreshed = snapshot_final(self.project, 1)
        self.assertTrue(refreshed["review_carried"])
        self.assertIn("carried_from_identical_final_spoken",
                      load_yaml(recap)["episodes"][0]["semantic_review"])
        self.assertTrue(check(self.project, required_episodes=[1])["passed"])
        revoked = record_confirmation(self.project, "script", "撤回文案", [1],
                                      session="recap-test-fixture", verify_transcript=False)
        self.assertTrue(revoked["passed"], revoked)
        self.assertFalse(check(self.project, required_episodes=[1])["passed"])
        with self.assertRaisesRegex(ValueError, "文案确认"):
            snapshot_final(self.project, 1)

    def test_changed_final_needs_explicit_actual_and_fresh_review(self):
        self.working.unlink()
        self.ledger.unlink()
        draft = self.project / "episodes/ep01/draft_v1.md"
        self.reviewed_direct_working(1, draft, self.actual())
        final = self.project / "episodes/ep01/final.md"
        atomic_write(final, "---\nepisode: 1\nstatus: final\n---\n门关了。")
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [1],
                                            session="recap-test-fixture", verify_transcript=False)["passed"])
        recap = self.project / "episodes/recap.yaml"
        before = recap.read_bytes()
        with self.assertRaisesRegex(ValueError, "纯口播"):
            snapshot_final(self.project, 1)
        self.assertEqual(recap.read_bytes(), before)
        args = parser().parse_args(["recap", "snapshot-final", str(self.project), "1", "--actual",
                                    str(self.project / "episodes/ep01/final_actual.yaml")])
        write_yaml(args.actual, self.actual("门关了"))
        self.assertTrue(dispatch(args)["written"])
        row = load_yaml(recap)["episodes"][0]
        self.assertEqual(row["semantic_review"]["status"], "pending")
        self.assertFalse(check(self.project, required_episodes=[1])["passed"])
        self.assertFalse(dispatch(args)["written"])
        atomic_write(final, "---\nepisode: 1\nstatus: final\n---\n门又开了。")
        with self.assertRaisesRegex(ValueError, "文案确认"):
            snapshot_final(self.project, 1, self.actual("门又开了"))

    def test_later_final_snapshot_tracks_prior_final_file_hash(self):
        self.working.unlink()
        self.ledger.unlink()
        first = self.project / "episodes/ep01/draft_v1.md"
        self.reviewed_direct_working(1, first, self.actual())
        first_final = self.project / "episodes/ep01/final.md"
        atomic_write(first_final, "---\nepisode: 1\nstatus: final\n---\n门开了。")
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [1],
                                            session="recap-test-fixture", verify_transcript=False)["passed"])
        snapshot_final(self.project, 1)
        second = self.project / "episodes/ep02/draft_v1.md"
        atomic_write(second, "---\nepisode: 2\n---\n他走了。")
        self.reviewed_direct_working(2, second, self.actual("他走了"))
        second_final = self.project / "episodes/ep02/final.md"
        atomic_write(second_final, "---\nepisode: 2\nstatus: final\n---\n他走了。")
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [2],
                                            session="recap-test-fixture", verify_transcript=False)["passed"])
        snapshot_final(self.project, 2)
        row = load_yaml(self.project / "episodes/recap.yaml")["episodes"][1]
        self.assertEqual(row["candidates"]["final"]["entry"]["dependency_hashes"]["1"], sha256_file(first_final))
        self.assertTrue(check(self.project, required_episodes=[1, 2])["passed"])
        atomic_write(first_final, first_final.read_text(encoding="utf-8") + "\n<!-- 仅测试元数据 -->\n")
        self.assertFalse(check(self.project, required_episodes=[2])["passed"])

    def test_later_final_snapshot_does_not_read_symlinked_prior_final(self):
        self.working.unlink()
        self.ledger.unlink()
        second = self.project / "episodes/ep02/draft_v1.md"
        atomic_write(second, "---\nepisode: 2\n---\n他走了。")
        self.reviewed_direct_working(2, second, self.actual("他走了"))
        second_final = self.project / "episodes/ep02/final.md"
        atomic_write(second_final, "---\nepisode: 2\nstatus: final\n---\n他走了。")
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [2],
                                            session="recap-test-fixture", verify_transcript=False)["passed"])
        external = self.project.parent.parent / "outside-final.md"
        atomic_write(external, "外部文件，不应读取。")
        prior = self.project / "episodes/ep01/final.md"
        prior.symlink_to(external)
        result = snapshot_final(self.project, 2, self.actual("他走了"))
        self.assertEqual(result["pending_dependencies"], [1])
        entry = load_yaml(self.project / "episodes/recap.yaml")["episodes"][0]["candidates"]["final"]["entry"]
        self.assertEqual(entry["dependency_hashes"], {})
        self.assertFalse(check(self.project, required_episodes=[2])["passed"])


if __name__ == "__main__":
    unittest.main()
