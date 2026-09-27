"""Queue claims and acceptance operate only on isolated test projects."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bookflow.__main__ import dispatch, parser
from bookflow.approvals import record_confirmation
from bookflow.common import ROOT, atomic_write, latest_draft, load_yaml, sha256_file, write_yaml
from bookflow.flow import derive, write as flow_write
from bookflow.jobs import PROMPT, _draft_baseline, claim, done, plan, run, summary
from bookflow.selftest import run as selftest_run


class JobTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        report = selftest_run(root)
        self.assertTrue(report["passed"], report)
        project = root / "projects/selftest-fixture"
        episode_plan = load_yaml(project / "plan/episodes.yaml")
        template = episode_plan["episodes"][0]
        for ep in (2, 3):
            episode_plan["episodes"].append({**template, "ep": ep, "title_working": f"测试第 {ep} 集"})
            final = project / "episodes" / f"ep{ep:02d}" / "final.md"
            final.parent.mkdir(parents=True)
            fixture = (ROOT / "demos/fixtures/ep01_draft.md").read_text(encoding="utf-8")
            final.write_text(fixture.replace("episode: 1", f"episode: {ep}", 1), encoding="utf-8")
        write_yaml(project / "plan/episodes.yaml", episode_plan)
        accepted_plan = record_confirmation(project, "plan", "拍板方案",
                                            session="selftest-fixture", verify_transcript=False)
        self.assertTrue(accepted_plan["passed"], accepted_plan)
        accepted = record_confirmation(project, "script", "拍板文案", [2, 3],
                                       session="selftest-fixture", verify_transcript=False)
        self.assertTrue(accepted["passed"], accepted)
        return project

    def test_three_episode_audio_run_resumes_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            first = plan(project, "audio")
            self.assertEqual(len(first["changed"]), 3)
            held = claim(project, "audio", "manual-fixture")
            self.assertEqual(held["cards"][0]["id"], "audio-ep01")
            self.assertEqual(claim(project, "audio", "manual-fixture")["cards"][0]["id"], "audio-ep01")
            self.assertEqual(done(project, "audio-ep01", "manual-fixture")["cards"][0]["status"], "done")
            output = run(project, "audio", test_mode=True)
            self.assertEqual(output["completed"], ["audio-ep02", "audio-ep03"])
            self.assertTrue(all(summary(project)["counts"]["audio"][key] == expected
                                for key, expected in {"done": 3, "failed": 0, "needs_you": 0}.items()))
            voice = project / "episodes/ep02/production/voice.wav"
            modified = voice.stat().st_mtime_ns
            repeated = run(project, "audio", test_mode=True)
            self.assertEqual(repeated["completed"], [])
            self.assertEqual(voice.stat().st_mtime_ns, modified)
            self.assertTrue((project / "jobs/_prompt.md").is_file())

    def test_changed_one_final_requeues_one_card_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            self.assertEqual(run(project, "audio", test_mode=True)["status"], "success")
            final = project / "episodes/ep02/final.md"
            final.write_text(final.read_text(encoding="utf-8").replace("title_working:", "fixture_note: changed\ntitle_working:", 1),
                             encoding="utf-8")
            changed = plan(project, "audio")
            self.assertEqual(changed["changed"], ["audio-ep02"])
            self.assertEqual(len(changed["preserved"]), 2)
            self.assertEqual(len(list((project / "jobs/history").glob("audio-ep02-*.yaml"))), 1)
            before = run(project, "audio", test_mode=True)
            self.assertEqual(before["completed"], ["audio-ep02"])
            self.assertEqual(summary(project)["counts"]["audio"]["done"], 3)

    def test_corrupted_output_requeues_done_card_even_with_same_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            self.assertEqual(run(project, "audio", test_mode=True)["status"], "success")
            voice = project / "episodes/ep02/production/voice.wav"
            voice.write_bytes(b"corrupted fixture output")
            replanned = plan(project, "audio")
            self.assertEqual(replanned["changed"], ["audio-ep02"])
            self.assertEqual(run(project, "audio", test_mode=True)["completed"], ["audio-ep02"])

    def test_unaccepted_work_fails_and_needs_you_surfaces_in_next(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            plan(project, "audio")
            got = claim(project, "audio", "test-session")
            self.assertEqual(got["cards"][0]["id"], "audio-ep01")
            unfinished = done(project, "audio-ep01", "test-session")
            self.assertEqual(unfinished["cards"][0]["status"], "done")
            got = claim(project, "audio", "test-session")
            self.assertEqual(got["cards"][0]["id"], "audio-ep02")
            unfinished = done(project, "audio-ep02", "test-session")
            self.assertEqual(unfinished["cards"][0]["status"], "failed")
            self.assertIn("cues", unfinished["cards"][0]["failure_reason"])
            card = load_yaml(project / "jobs/audio-ep03.yaml")
            card["status"] = "needs_you"
            card["needs_you"] = ["测试新角色需要分配音色"]
            write_yaml(project / "jobs/audio-ep03.yaml", card)
            state = derive(project)
            self.assertTrue(any("测试新角色" in item for item in state["needs_you"]))
            self.assertEqual(state["queue"]["audio"]["needs_you"], 1)

    def test_cli_and_real_mode_do_not_run_paid_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            args = parser().parse_args(["jobs", "plan", str(project), "--stage", "audio"])
            self.assertEqual(dispatch(args)["changed"], ["audio-ep01", "audio-ep02", "audio-ep03"])
            self.assertEqual(parser().parse_args(["jobs", "run", str(project),
                                                   "--stage", "draft"]).stage, "draft")
            args = parser().parse_args(["produce", "check", str(project), "ep01", "--until", "subs"])
            self.assertEqual(len(dispatch(args)["stages"]), 5)
            before = summary(project)["counts"]["audio"]
            blocked = run(project, "audio")
            self.assertEqual(blocked["status"], "error")
            self.assertEqual(summary(project)["counts"]["audio"], before)

    def test_foreign_claim_and_existing_failure_stop_serial_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            plan(project, "audio")
            claimed = claim(project, "audio", "other-session")
            self.assertEqual(claimed["cards"][0]["id"], "audio-ep01")
            self.assertEqual(run(project, "audio", test_mode=True)["status"], "warning")
            self.assertEqual(summary(project)["counts"]["audio"]["todo"], 2)
            claimed = claim(project, "audio", "other-session")
            self.assertEqual(claimed["cards"][0]["id"], "audio-ep01")
            card_path = project / "jobs/audio-ep01.yaml"
            card = load_yaml(card_path)
            card.update(status="failed", claimed_by=None, failure_reason="测试失败")
            write_yaml(card_path, card)
            self.assertEqual(run(project, "audio", test_mode=True)["status"], "warning")
            self.assertEqual(summary(project)["counts"]["audio"]["todo"], 2)

    def test_next_points_to_claimable_card_at_audio_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(selftest_run(root)["passed"])
            project = root / "projects/selftest-fixture"
            plan(project, "audio")
            (project / "episodes/ep01/production/voice.wav").write_bytes(b"changed test output")
            state = derive(project)
            self.assertEqual(state["stage"], "声音")
            self.assertIn("jobs claim", state["next_step"])
            self.assertIn("audio-ep01", state["next_step"])

    def test_expired_claim_is_released_for_next_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            plan(project, "audio")
            claim(project, "audio", "stale-session")
            path = project / "jobs/audio-ep01.yaml"
            card = load_yaml(path)
            card["claimed_by"]["at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            write_yaml(path, card)
            next_card = claim(project, "audio", "new-session")["cards"][0]
            self.assertEqual(next_card["id"], "audio-ep01")
            self.assertEqual(next_card["claimed_by"]["session"], "new-session")

    def test_draft_cards_track_scoped_recap_without_binding_mutable_legacy_continuity(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            first = plan(project, "draft")
            self.assertEqual(len(first["changed"]), 3)
            card = load_yaml(project / "jobs/draft-ep02.yaml")
            self.assertIn("current", card["inputs"])
            self.assertIn("project", card["inputs"])
            self.assertEqual(card["inputs"]["recap_context"]["status"], "reviewed_prior")
            self.assertNotIn("working_continuity", card["inputs"])
            self.assertNotIn("recap_context", load_yaml(project / "jobs/draft-ep01.yaml")["inputs"])
            self.assertIn("按分集计划", load_yaml(project / "jobs/draft-ep03.yaml")["do"])
            self.assertNotIn("墙钟疑问", str(card))
            write_yaml(project / "episodes/working_continuity.yaml", {"episodes": []})
            self.assertEqual(plan(project, "draft")["changed"], [])
            recap_path = project / "episodes/recap.yaml"
            recap_data = load_yaml(recap_path)
            recap_data["episodes"][0]["semantic_review"]["ending_summary"] += " 测试版更新。"
            write_yaml(recap_path, recap_data)
            changed = plan(project, "draft")
            self.assertEqual(changed["changed"], ["draft-ep02", "draft-ep03"])
            self.assertEqual(changed["preserved"], ["draft-ep01"])
            source = project / "source/current.json"
            source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assertEqual(len(plan(project, "draft")["changed"]), 3)

    def test_draft_card_rejects_changed_prior_recap_before_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            plan(project, "draft")
            self.assertEqual(claim(project, "draft", "fixture-one")["cards"][0]["episode"], 1)
            self.assertEqual(claim(project, "draft", "fixture-two")["cards"][0]["episode"], 2)
            recap_path = project / "episodes/recap.yaml"
            data = load_yaml(recap_path)
            data["episodes"][0]["semantic_review"]["ending_summary"] += " 新结尾。"
            write_yaml(recap_path, data)
            rejected = done(project, "draft-ep02", "fixture-two")
            self.assertEqual(rejected["cards"][0]["status"], "failed")
            self.assertIn("输入已变化", rejected["summary"])

    def test_planning_upgrades_version_before_child_next_reads_shared_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(selftest_run(Path(temporary))["passed"])
            project = Path(temporary) / "projects/selftest-fixture"
            config_path = project / "project.yaml"
            config = load_yaml(config_path)
            config["studio_version"] = "0.1.0"
            write_yaml(config_path, config)
            plan(project, "audio")
            self.assertEqual(load_yaml(config_path)["studio_version"], "0.2.0-dev.1")
            self.assertIn("next <项目> --read-only --json", (project / "jobs/_prompt.md").read_text(encoding="utf-8"))
            card = load_yaml(project / "jobs/audio-ep01.yaml")
            self.assertEqual(card["inputs"]["project"]["sha256"], sha256_file(config_path))
            before = sha256_file(config_path)
            flow_write(project)
            self.assertEqual(sha256_file(config_path), before)

    def test_planning_updates_only_unmodified_legacy_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(selftest_run(Path(temporary))["passed"])
            project = Path(temporary) / "projects/selftest-fixture"
            prompt_path = project / "jobs/_prompt.md"
            old_prompt = PROMPT.replace("next <项目> --read-only --json", "next <项目>")
            atomic_write(prompt_path, old_prompt)
            plan(project, "audio")
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), PROMPT)
            atomic_write(prompt_path, "用户自定义任务提示词\n")
            plan(project, "audio")
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), "用户自定义任务提示词\n")

    def test_draft_card_cannot_be_done_when_draft_guard_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            plan(project, "draft")
            claimed = claim(project, "draft", "manual-fixture")["cards"][0]
            self.assertEqual(claimed["id"], "draft-ep01")
            with patch("bookflow.guard.check", return_value={"passed": False,
                  "errors": ["原文批次失效"]}):
                result = done(project, "draft-ep01", "manual-fixture")
            self.assertEqual(result["cards"][0]["status"], "failed")
            self.assertIn("原文批次失效", result["summary"])

    def test_real_draft_queue_serially_creates_versions_and_edit_packages_without_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            called = []
            template = (ROOT / "demos/fixtures/ep01_draft.md").read_text(encoding="utf-8")

            def worker(project_path, ep, card_path):
                called.append(ep)
                epdir = project_path / "episodes" / f"ep{ep:02d}"
                old = latest_draft(epdir)
                version = int(old.stem.split("_v")[-1]) + 1 if old else 1
                (epdir / f"draft_v{version}.md").write_text(
                    template.replace("episode: 1", f"episode: {ep}", 1), encoding="utf-8")
                return {"passed": True}

            with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
                 patch("bookflow.produce._test_fixture", return_value=False), \
                 patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
                 patch("bookflow.job_runner.invoke_draft", side_effect=worker):
                result = run(project, "draft")
            self.assertEqual(called, [1, 2, 3])
            self.assertEqual(result["completed"], ["draft-ep01", "draft-ep02", "draft-ep03"])
            self.assertIn("连续性", result["summary"])
            for ep in (1, 2, 3):
                card = load_yaml(project / "jobs" / f"draft-ep{ep:02d}.yaml")
                draft = project / card["output_draft"]
                self.assertTrue(draft.is_file())
                self.assertTrue((draft.parent / "human_edit" / f"v{draft.stem.split('_v')[-1]}" / "baseline.json").is_file())
            with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
                 patch("bookflow.produce._test_fixture", return_value=False), \
                 patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
                 patch("bookflow.job_runner.invoke_draft") as worker_again:
                repeated = run(project, "draft")
            self.assertEqual(repeated["completed"], [])
            worker_again.assert_not_called()

    def test_draft_queue_stops_on_guard_failure_before_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(selftest_run(Path(temporary))["passed"])
            project = Path(temporary) / "projects/selftest-fixture"
            with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
                 patch("bookflow.produce._test_fixture", return_value=False), \
                 patch("bookflow.guard.check", return_value={"passed": False,
                       "errors": ["原文批次失效"]}), \
                 patch("bookflow.job_runner.invoke_draft") as worker:
                result = run(project, "draft")
            self.assertEqual(result["cards"][0]["status"], "needs_you")
            worker.assert_not_called()

    def test_draft_queue_creates_planned_episode_directory_only_after_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(selftest_run(Path(temporary))["passed"])
            project = Path(temporary) / "projects/selftest-fixture"
            plan_data = load_yaml(project / "plan/episodes.yaml")
            plan_data["episodes"].append({**plan_data["episodes"][0], "ep": 2})
            write_yaml(project / "plan/episodes.yaml", plan_data)
            second = project / "episodes/ep02"
            self.assertFalse(second.exists())
            template = (ROOT / "demos/fixtures/ep01_draft.md").read_text(encoding="utf-8")

            def worker(project_path, ep, card_path):
                epdir = project_path / "episodes" / f"ep{ep:02d}"
                self.assertTrue(epdir.is_dir())
                old = latest_draft(epdir)
                version = int(old.stem.split("_v")[-1]) + 1 if old else 1
                (epdir / f"draft_v{version}.md").write_text(
                    template.replace("episode: 1", f"episode: {ep}", 1), encoding="utf-8")
                return {"passed": True}

            with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
                 patch("bookflow.produce._test_fixture", return_value=False), \
                 patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
                 patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
                 patch("bookflow.job_runner.invoke_draft", side_effect=worker):
                result = run(project, "draft")
            self.assertEqual(result["completed"], ["draft-ep01", "draft-ep02"], result)
            self.assertTrue((second / "draft_v1.md").is_file())

    def test_draft_queue_resumes_new_version_without_second_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(selftest_run(Path(temporary))["passed"])
            project = Path(temporary) / "projects/selftest-fixture"
            plan(project, "draft")
            session = "bookflow-codex-draft-runner"
            card = claim(project, "draft", session)["cards"][0]
            _draft_baseline(project, card, session, fresh=True)
            epdir = project / "episodes/ep01"
            source = latest_draft(epdir)
            new = epdir / f"draft_v{int(source.stem.split('_v')[-1]) + 1}.md"
            new.write_bytes(source.read_bytes())
            with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
                 patch("bookflow.produce._test_fixture", return_value=False), \
                 patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
                 patch("bookflow.job_runner.invoke_draft") as worker:
                result = run(project, "draft")
            self.assertEqual(result["completed"], ["draft-ep01"])
            worker.assert_not_called()

    def test_draft_queue_rejects_overwrite_of_existing_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(selftest_run(Path(temporary))["passed"])
            project = Path(temporary) / "projects/selftest-fixture"

            def worker(project_path, ep, card_path):
                old = latest_draft(project_path / "episodes/ep01")
                old.write_text(old.read_text(encoding="utf-8") + "\n覆盖旧稿。", encoding="utf-8")
                return {"passed": True}

            with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
                 patch("bookflow.produce._test_fixture", return_value=False), \
                 patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
                 patch("bookflow.job_runner.invoke_draft", side_effect=worker):
                result = run(project, "draft")
            self.assertEqual(result["cards"][0]["status"], "failed")
            self.assertIn("修改或删除了已有初稿", result["summary"])

    def test_draft_queue_rejects_wrong_episode_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(selftest_run(Path(temporary))["passed"])
            project = Path(temporary) / "projects/selftest-fixture"

            def worker(project_path, ep, card_path):
                epdir = project_path / "episodes/ep01"
                old = latest_draft(epdir)
                version = int(old.stem.split("_v")[-1]) + 1
                (epdir / f"draft_v{version}.md").write_text(
                    "---\nepisode: 2\n---\n〔据 p00002〕这不是第一集。", encoding="utf-8")
                return {"passed": True}

            with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
                 patch("bookflow.produce._test_fixture", return_value=False), \
                 patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
                 patch("bookflow.job_runner.invoke_draft", side_effect=worker):
                result = run(project, "draft")
            self.assertEqual(result["cards"][0]["status"], "failed")
            self.assertIn("元数据集号", result["summary"])


if __name__ == "__main__":
    unittest.main()
