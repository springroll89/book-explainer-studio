"""The real queue adapter is tested with a fake process, never a model call."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.job_runner import _run_cli, invoke_audio, invoke_cues, invoke_draft


class JobRunnerTests(unittest.TestCase):
    @staticmethod
    def _completed(output: str, code: int = 0):
        def fake(command, prompt, stream, **kwargs):
            stream.write(output)
            return code
        return fake

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "projects/book"
        self.project.mkdir(parents=True)
        (self.project / "project.yaml").write_text("title: test\n", encoding="utf-8")
        self.epdir = self.project / "episodes/ep01"
        self.epdir.mkdir(parents=True)
        self.card = self.project / "jobs/audio-ep01.yaml"
        self.card.parent.mkdir()
        self.card.write_text("id: audio-ep01\n", encoding="utf-8")

    def test_fresh_sandboxed_cue_session_has_no_provider_key_or_book_text_in_prompt(self):
        events = "\n".join(json.dumps(row) for row in (
            {"type": "thread.started", "thread_id": "test-thread"}, {"type": "turn.completed"}))
        with patch.dict(os.environ, {"ARK_API_KEY": "private-test-value",
                                          "VOLC_ACCESS_KEY": "private-test-value",
                                          "DOUBAO_API_KEY": "private-test-value"}), \
             patch("bookflow.job_runner.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.job_runner._run_cli", side_effect=self._completed(events)) as worker:
            result = invoke_cues(self.project, 1, self.card)
        self.assertTrue(result["passed"])
        args, kwargs = worker.call_args
        self.assertEqual(args[0][1:3], ["exec", "--ephemeral"])
        self.assertIn("workspace-write", args[0])
        self.assertIn("--json", args[0])
        self.assertEqual(kwargs["cwd"], self.epdir.resolve())
        self.assertNotIn("ARK_API_KEY", kwargs["environment"])
        self.assertNotIn("VOLC_ACCESS_KEY", kwargs["environment"])
        self.assertNotIn("DOUBAO_API_KEY", kwargs["environment"])
        self.assertNotIn("private-test-value", args[1])
        self.assertIn("只处理 cues", args[1])
        self.assertIn("--read-only", args[1])

    def test_local_audio_session_does_not_receive_paid_key(self):
        events = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.completed"}'
        with patch.dict(os.environ, {"DOUBAO_API_KEY": "private-test-value"}), \
             patch("bookflow.job_runner.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.job_runner._run_cli", side_effect=self._completed(events)) as worker:
            self.assertTrue(invoke_audio(self.project, 1, self.card)["passed"])
        kwargs = worker.call_args.kwargs
        self.assertNotIn("DOUBAO_API_KEY", kwargs["environment"])
        self.assertNotIn("private-test-value", worker.call_args.args[1])
        self.assertIn("不要使用 --allow-paid", worker.call_args.args[1])
        self.assertIn("--read-only", worker.call_args.args[1])

    def test_draft_session_uses_guard_and_cannot_receive_paid_key(self):
        card = self.project / "jobs/draft-ep01.yaml"
        card.write_text("id: draft-ep01\n", encoding="utf-8")
        events = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.completed"}'
        with patch.dict(os.environ, {"DOUBAO_API_KEY": "private-test-value"}), \
             patch("bookflow.job_runner.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.job_runner._run_cli", side_effect=self._completed(events)) as worker:
            self.assertTrue(invoke_draft(self.project, 1, card)["passed"])
        self.assertNotIn("DOUBAO_API_KEY", worker.call_args.kwargs["environment"])
        self.assertIn("guard", worker.call_args.args[1])
        self.assertIn("draft-episode/SKILL.md", worker.call_args.args[1])
        self.assertIn("不得修改或覆盖已有稿件", worker.call_args.args[1])
        self.assertIn("--read-only", worker.call_args.args[1])

    def test_path_escape_and_missing_card_do_not_start_process(self):
        with patch("bookflow.job_runner._run_cli") as worker:
            with self.assertRaisesRegex(ValueError, "不属于指定书目"):
                invoke_cues(self.project, 1, self.project / "project.yaml")
            self.card.unlink()
            with self.assertRaisesRegex(ValueError, "找不到当前任务卡"):
                invoke_cues(self.project, 1, self.card)
            worker.assert_not_called()

    def test_failure_timeout_or_missing_completion_never_claims_success(self):
        with patch("bookflow.job_runner.shutil.which", return_value="/usr/bin/codex"), \
             patch("bookflow.job_runner._run_cli") as worker:
            worker.side_effect = self._completed('{"type":"turn.completed"}', 1)
            self.assertFalse(invoke_cues(self.project, 1, self.card)["passed"])
            worker.side_effect = self._completed('{"type":"thread.started","thread_id":"t"}')
            self.assertFalse(invoke_cues(self.project, 1, self.card)["passed"])
            worker.side_effect = self._completed('\n'.join((
                '{"type":"thread.started","thread_id":"t"}',
                '{"type":"turn.failed"}', '{"type":"turn.completed"}')))
            self.assertFalse(invoke_cues(self.project, 1, self.card)["passed"])
            worker.side_effect = subprocess.TimeoutExpired([], 5)
            self.assertFalse(invoke_cues(self.project, 1, self.card)["passed"])

    def test_timeout_kills_the_entire_child_process_group(self):
        process = unittest.mock.Mock(pid=12345, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired([], 5), (None, None)]
        with patch("bookflow.job_runner.subprocess.Popen", return_value=process) as popen, \
             patch("bookflow.job_runner.os.killpg") as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_cli(["codex", "exec"], "prompt", None, cwd=self.epdir,
                         environment={}, timeout_sec=5)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        kill.assert_called_once_with(12345, __import__("signal").SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
