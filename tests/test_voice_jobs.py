"""A persistent request record prevents accidental paid resubmission."""
import json
import tempfile
import unittest
from pathlib import Path

from bookflow.common import load_yaml, write_json, write_yaml
from bookflow.cost import estimate_episode
from bookflow.selftest import run as selftest_run
from bookflow.voice_jobs import read, reserve, transition
from bookflow.voice_plan import plan


class VoiceJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assertTrue(selftest_run(self.root)["passed"])
        self.project = self.root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        project_yaml = self.project / "project.yaml"
        config = load_yaml(project_yaml)
        config.setdefault("sound_design", {}).setdefault("pricing", {}).update(
            narration_model_per_10k_chars=0, sfx_model_per_minute=0)
        write_yaml(project_yaml, config)
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "charges": [], "stages": {}})
        self.planned = plan(self.epdir)

    def _reserve_first(self):
        return reserve(self.epdir, self.planned["paragraphs"][0],
                       cast_sha=self.planned["cast_sha256"], config_sha=self.planned["config_sha256"])

    def test_persist_before_submit_and_query_same_task(self):
        first = self._reserve_first()
        self.assertEqual(first["action"], "submit")
        request_id = first["job"]["request_id"]
        self.assertEqual(read(self.epdir)["jobs"][0]["status"], "submitting")
        self.assertEqual(self._reserve_first()["action"], "blocked")
        self.assertFalse(estimate_episode(self.project, 1)["passed"])
        transition(self.epdir, request_id, status="running", task_id="task-123")
        resumed = self._reserve_first()
        self.assertEqual(resumed["action"], "query")
        self.assertEqual(resumed["job"]["task_id"], "task-123")
        self.assertEqual(len(read(self.epdir)["jobs"]), 1)
        other = reserve(self.epdir, self.planned["paragraphs"][1],
                        cast_sha=self.planned["cast_sha256"], config_sha=self.planned["config_sha256"])
        self.assertEqual(other["action"], "blocked")

    def test_unknown_result_cannot_be_reset_or_resubmitted(self):
        first = self._reserve_first()
        request_id = first["job"]["request_id"]
        transition(self.epdir, request_id, status="unknown")
        self.assertEqual(self._reserve_first()["action"], "blocked")
        with self.assertRaisesRegex(ValueError, "状态转移"):
            transition(self.epdir, request_id, status="running", task_id="new-task")
        self.assertEqual(len(read(self.epdir)["jobs"]), 1)

    def test_invalid_journal_blocks_budget(self):
        path = self.epdir / "production/voice_jobs.json"
        write_json(path, {"mode": "real", "jobs": [{"request_id": "same", "status": "running"},
                                                     {"request_id": "same", "status": "running"}]})
        with self.assertRaisesRegex(ValueError, "重复"):
            estimate_episode(self.project, 1)
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["jobs"]), 2)

    def test_over_budget_creates_no_request_id(self):
        project_yaml = self.project / "project.yaml"
        config = load_yaml(project_yaml)
        config["cost"] = {"per_episode_cny": 0.01}
        config["sound_design"]["pricing"]["narration_model_per_10k_chars"] = 1000
        write_yaml(project_yaml, config)
        planned = plan(self.epdir)
        attempt = reserve(self.epdir, planned["paragraphs"][0],
                          cast_sha=planned["cast_sha256"], config_sha=planned["config_sha256"])
        self.assertEqual(attempt["action"], "budget_blocked")
        self.assertEqual(read(self.epdir)["jobs"], [])


if __name__ == "__main__":
    unittest.main()
