"""Real queue orchestration is exercised without launching Codex or Doubao."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.common import load_yaml, write_yaml
from bookflow.jobs import _sfx_until_ready as _REAL_SFX, _voice_until_ready, run, summary


class RealJobRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "projects/demo"
        self.project.mkdir(parents=True)
        write_yaml(self.project / "project.yaml", {"title": "测试"})
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": 1}]})
        write_yaml(self.project / "production/voice_cast.yaml", {"version": 1})
        write_yaml(self.project / "analysis/characters.yaml", {"characters": []})
        final = self.project / "episodes/ep01/final.md"
        final.parent.mkdir(parents=True)
        final.write_text("测试定稿。\n", encoding="utf-8")
        # Paid SFX generation has its own tests below; these cover the card runner.
        sfx = patch("bookflow.jobs._sfx_until_ready", return_value={"passed": True})
        sfx.start()
        self.addCleanup(sfx.stop)

    def test_sfx_loop_generates_until_bound_and_stops_on_block(self):
        steps = [{"passed": True, "progress": "generated"}, {"passed": True, "progress": "generated"},
                 {"passed": True, "summary": "已绑定已验收音效"}]
        with patch("bookflow.sfx_stage.advance", side_effect=steps) as advance:
            self.assertEqual(_REAL_SFX(self.project, 1), {"passed": True})
        self.assertEqual(advance.call_count, 3)
        with patch("bookflow.sfx_stage.advance",
                   return_value={"passed": False, "summary": "音效生成超出本集费用上限"}):
            blocked = _REAL_SFX(self.project, 1)
        self.assertEqual((blocked["passed"], blocked["needs_you"]), (False, True))
        self.assertIn("费用上限", blocked["reason"])

    @staticmethod
    def _media(*, until=None, cues_ready=True):
        stages = ["cues", "voice", "sfx", "mix", "subs"]
        if until == "cues":
            stages = stages[:1]
        return {"stages": [{"stage": name, "ready": cues_ready if name == "cues" else True,
                             "reason": "fresh" if cues_ready else "missing_or_changed"}
                            for name in stages]}

    def test_serial_real_worker_requires_verified_cues_budget_and_media(self):
        media_calls = 0

        def media(project, ep, *, until=None):
            nonlocal media_calls
            if until == "cues":
                media_calls += 1
                return self._media(until=until, cues_ready=media_calls > 1)
            return self._media(until=until)

        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", side_effect=media), \
             patch("bookflow.cost.estimate_episode", return_value={"passed": True}), \
             patch("bookflow.job_runner.invoke_cues", return_value={"passed": True}) as cues, \
             patch("bookflow.job_runner.invoke_audio", return_value={"passed": True}) as audio:
            result = run(self.project, "audio")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["completed"], ["audio-ep01"])
        cues.assert_called_once()
        audio.assert_called_once()
        self.assertEqual(summary(self.project)["counts"]["audio"]["done"], 1)

    def test_three_cards_run_serially_and_stop_at_first_failed_child(self):
        for ep in (2, 3):
            final = self.project / "episodes" / f"ep{ep:02d}" / "final.md"
            final.parent.mkdir(parents=True)
            final.write_text(f"测试第{ep}集。\n", encoding="utf-8")
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": ep} for ep in (1, 2, 3)]})
        seen = []

        def worker(project, ep, card):
            seen.append(ep)
            return {"passed": ep != 2, "reason": "模拟第二集失败"}

        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", return_value=self._media()), \
             patch("bookflow.cost.estimate_episode", return_value={"passed": True}), \
             patch("bookflow.job_runner.invoke_audio", side_effect=worker):
            result = run(self.project, "audio")
        self.assertEqual(seen, [1, 2])
        self.assertEqual(result["completed"], ["audio-ep01"])
        self.assertEqual(result["cards"][0]["status"], "failed")
        state = summary(self.project)["counts"]["audio"]
        self.assertEqual((state["done"], state["failed"], state["todo"]), (1, 1, 1))

    def test_guard_stops_before_model_or_paid_stage(self):
        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": False, "errors": ["G3 未通过"]}), \
             patch("bookflow.produce.check", return_value=self._media(until="cues", cues_ready=False)), \
             patch("bookflow.job_runner.invoke_cues") as cues, \
             patch("bookflow.job_runner.invoke_audio") as audio:
            result = run(self.project, "audio")
        self.assertEqual(result["cards"][0]["status"], "needs_you")
        cues.assert_not_called()
        audio.assert_not_called()

    def test_budget_stops_after_existing_cues_before_audio_session(self):
        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", return_value=self._media(until="cues")), \
             patch("bookflow.cost.estimate_episode", return_value={
                 "passed": False, "blockers": ["预计超过每集费用上限"]}), \
             patch("bookflow.job_runner.invoke_cues") as cues, \
             patch("bookflow.job_runner.invoke_audio") as audio:
            result = run(self.project, "audio")
        self.assertEqual(result["cards"][0]["status"], "needs_you")
        self.assertIn("费用", result["summary"])
        cues.assert_not_called()
        audio.assert_not_called()

    def test_missing_voice_is_handled_by_parent_not_agent(self):
        def media(project, ep, *, until=None):
            result = self._media(until=until)
            if until == "voice":
                result["stages"][-1] = {"stage": "voice", "ready": False,
                                         "reason": "missing_or_changed"}
            return result

        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", side_effect=media), \
             patch("bookflow.cost.estimate_episode", return_value={"passed": True}), \
             patch("bookflow.voice_stage.advance", return_value={"passed": False,
                   "summary": "模拟配音阻断"}) as parent_voice, \
             patch("bookflow.job_runner.invoke_audio") as audio:
            result = run(self.project, "audio")
        self.assertEqual(result["cards"][0]["status"], "failed")
        self.assertIn("模拟配音阻断", result["summary"])
        parent_voice.assert_called_once()
        audio.assert_not_called()

    def test_parent_voice_budget_block_becomes_needs_you(self):
        def media(project, ep, *, until=None):
            result = self._media(until=until)
            if until == "voice":
                result["stages"][-1] = {"stage": "voice", "ready": False,
                                         "reason": "missing_or_changed"}
            return result

        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", side_effect=media), \
             patch("bookflow.cost.estimate_episode", return_value={"passed": True}), \
             patch("bookflow.voice_stage.advance", return_value={
                 "passed": False, "progress": "budget_blocked", "summary": "模拟费用上限"}), \
             patch("bookflow.job_runner.invoke_audio") as audio:
            result = run(self.project, "audio")
        self.assertEqual(result["cards"][0]["status"], "needs_you")
        self.assertIn("费用上限", result["summary"])
        audio.assert_not_called()

    def test_waiting_provider_card_resumes_without_reclaiming_new_work(self):
        def media(project, ep, *, until=None):
            result = self._media(until=until)
            if until == "voice" and not resumed[0]:
                result["stages"][-1] = {"stage": "voice", "ready": False,
                                         "reason": "missing_or_changed"}
            return result

        resumed = [False]
        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", side_effect=media), \
             patch("bookflow.cost.estimate_episode", return_value={"passed": True}), \
             patch("bookflow.jobs._voice_until_ready", return_value={
                 "passed": False, "waiting": True, "reason": "原任务仍在运行"}) as voice, \
             patch("bookflow.job_runner.invoke_audio", return_value={"passed": True}) as audio:
            first = run(self.project, "audio")
            self.assertEqual(first["cards"][0]["status"], "doing")
            self.assertEqual(voice.call_count, 1)
            audio.assert_not_called()
            resumed[0] = True
            second = run(self.project, "audio")
        self.assertEqual(second["status"], "success", second)
        self.assertEqual(second["completed"], ["audio-ep01"])
        self.assertEqual(voice.call_count, 1)
        audio.assert_called_once()

    def test_parent_voice_loop_only_advances_reported_existing_state(self):
        checked = 0

        def media(project, ep, *, until=None):
            nonlocal checked
            checked += 1
            result = self._media(until="voice")
            result["stages"][-1]["ready"] = checked >= 4
            return result

        advances = [{"passed": False, "progress": "submitted"},
                    {"passed": False, "progress": "running"},
                    {"passed": False, "progress": "part_saved"}]
        with patch("bookflow.produce.check", side_effect=media), \
             patch("bookflow.voice_stage.advance", side_effect=advances) as adapter, \
             patch("bookflow.jobs.time.sleep") as pause:
            result = _voice_until_ready(self.project, 1, timeout_sec=30)
        self.assertTrue(result["passed"], result)
        self.assertEqual(adapter.call_count, 3)
        self.assertEqual(pause.call_count, 2)

    def test_model_completion_without_verified_cue_manifest_is_failure(self):
        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", return_value=self._media(until="cues", cues_ready=False)), \
             patch("bookflow.job_runner.invoke_cues", return_value={"passed": True}), \
             patch("bookflow.job_runner.invoke_audio") as audio:
            result = run(self.project, "audio")
        self.assertEqual(result["cards"][0]["status"], "failed")
        self.assertIn("cues 未通过", result["summary"])
        audio.assert_not_called()

    def test_shared_table_change_during_child_stops_without_marking_done(self):
        def mutate(*args):
            write_yaml(self.project / "production/voice_cast.yaml", {"version": 2})
            return {"passed": True}

        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", return_value=self._media(until="cues")), \
             patch("bookflow.cost.estimate_episode", return_value={"passed": True}), \
             patch("bookflow.job_runner.invoke_audio", side_effect=mutate):
            result = run(self.project, "audio")
        self.assertEqual(result["cards"][0]["status"], "failed")
        self.assertIn("共享只读文件", result["summary"])
        self.assertEqual(load_yaml(self.project / "production/voice_cast.yaml")["version"], 2)

    def test_concurrent_task_card_change_is_reported_without_overwrite(self):
        def mutate(project, ep, card_path):
            current = load_yaml(card_path)
            current["claimed_by"] = {"session": "another-session", "at": current["updated_at"]}
            write_yaml(card_path, current)
            return {"passed": True}

        with patch("bookflow.jobs.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}), \
             patch("bookflow.guard.check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.produce.check", return_value=self._media(until="cues")), \
             patch("bookflow.cost.estimate_episode", return_value={"passed": True}), \
             patch("bookflow.job_runner.invoke_audio", side_effect=mutate):
            result = run(self.project, "audio")
        self.assertEqual(result["status"], "error")
        self.assertIn("未覆盖现有文件", result["summary"])
        card = load_yaml(self.project / "jobs/audio-ep01.yaml")
        self.assertEqual(card["claimed_by"]["session"], "another-session")


if __name__ == "__main__":
    unittest.main()
