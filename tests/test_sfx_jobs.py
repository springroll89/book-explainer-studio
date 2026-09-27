"""Paid sound requests are journaled without calling a real provider."""
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.cost import estimate_episode
from bookflow.selftest import run as selftest_run
from bookflow.sfx_jobs import read, reserve, settle, transition


class SfxJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.assertTrue(selftest_run(root)["passed"])
        self.project = root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("sound_design", {}).setdefault("pricing", {}).update(
            narration_model_per_10k_chars=0, sfx_model_per_minute=0)
        write_yaml(config_path, config)
        self.cue = {"cue_id": "C01", "description": "木门吱呀打开", "sound_class": "event",
                    "function": "reveal", "duration_sec": 0.1, "status": "planned"}
        write_yaml(self.epdir / "production/sound_cues.yaml", {"cues": [self.cue]})
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "charges": [],
                                                             "stages": {"cues": {}, "voice": {}}})

    def _reserve(self):
        with patch("bookflow.sfx_jobs.guard_check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.sfx_jobs.real_stage_fresh", return_value=True), \
             patch("bookflow.sfx_jobs.estimate_episode", return_value={"passed": True}):
            return reserve(self.epdir, "C01")

    def test_reserve_before_submit_and_query_only_original_task(self):
        first = self._reserve()
        self.assertEqual(first["action"], "submit")
        identity = first["job"]["request_id"]
        self.assertEqual(read(self.epdir)["jobs"][0]["status"], "submitting")
        self.assertEqual(self._reserve()["action"], "blocked")
        transition(self.epdir, identity, status="running", task_id="provider-task-1")
        again = self._reserve()
        self.assertEqual(again["action"], "query")
        self.assertEqual(again["job"]["task_id"], "provider-task-1")
        self.assertEqual(len(read(self.epdir)["jobs"]), 1)
        self.assertFalse(estimate_episode(self.project, 1)["passed"])
        self.assertTrue(any("未结清的豆包音效任务" in row
                            for row in estimate_episode(self.project, 1)["blockers"]))

    def test_unknown_or_changed_cue_never_creates_second_paid_request(self):
        first = self._reserve()
        identity = first["job"]["request_id"]
        transition(self.epdir, identity, status="unknown")
        self.cue["description"] = "另一种门声"
        write_yaml(self.epdir / "production/sound_cues.yaml", {"cues": [self.cue]})
        self.assertEqual(self._reserve()["action"], "blocked")
        self.assertEqual(len(read(self.epdir)["jobs"]), 1)
        with self.assertRaisesRegex(ValueError, "状态转移"):
            transition(self.epdir, identity, status="running", task_id="new-task")

    def test_synchronous_completion_can_reach_settlement_without_task_id(self):
        identity = self._reserve()["job"]["request_id"]
        transition(self.epdir, identity, status="provider_done")
        transition(self.epdir, identity, status="provider_done")
        self.assertEqual(self._reserve()["action"], "settle")
        self.assertEqual(len(read(self.epdir)["jobs"]), 1)

    def test_guard_and_budget_block_before_request_id(self):
        with patch("bookflow.sfx_jobs.guard_check", return_value={"passed": False, "errors": ["未确认"]}):
            self.assertEqual(reserve(self.epdir, "C01")["action"], "guard_blocked")
        with patch("bookflow.sfx_jobs.guard_check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.sfx_jobs.real_stage_fresh", return_value=True), \
             patch("bookflow.sfx_jobs.estimate_episode", return_value={"passed": False, "blockers": ["超额"]}):
            self.assertEqual(reserve(self.epdir, "C01")["action"], "budget_blocked")
        self.assertEqual(read(self.epdir)["jobs"], [])

    def test_invalid_existing_journal_blocks_new_request(self):
        path = self.epdir / "production/sfx_jobs.json"
        write_json(path, {"schema": 1, "mode": "real", "jobs": [{"request_id": "old",
                    "status": "unexpected", "cue_id": "C01"}]})
        with self.assertRaisesRegex(ValueError, "格式无效|未知状态"):
            self._reserve()
        self.assertEqual(len(load_yaml(path)["jobs"]), 1)

    def test_settlement_rejects_test_manifest(self):
        identity = self._reserve()["job"]["request_id"]
        transition(self.epdir, identity, status="provider_done")
        manifest = load_yaml(self.epdir / "production/manifest.json")
        manifest["mode"] = "test"
        manifest["charges"] = [{"id": identity, "stage": "sfx", "cost_cny": 0}]
        write_json(self.epdir / "production/manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "收费"):
            settle(self.epdir, identity)

    def test_settle_requires_matching_charge_and_verified_local_audio(self):
        identity = self._reserve()["job"]["request_id"]
        transition(self.epdir, identity, status="running", task_id="provider-task-1")
        transition(self.epdir, identity, status="provider_done")
        with self.assertRaisesRegex(ValueError, "收费"):
            settle(self.epdir, identity)
        audio = self.epdir / "production/sfx_generated/C01.wav"
        audio.parent.mkdir(parents=True)
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b"\0\0" * 4800)
        sheet = load_yaml(self.epdir / "production/sound_cues.yaml")
        sheet["cues"][0].update(status="asset_ready", asset_id="production/sfx_generated/C01.wav",
                                 asset_sha256=sha256_file(audio), asset_charge_id=identity,
                                 asset_origin="generated")
        write_yaml(self.epdir / "production/sound_cues.yaml", sheet)
        manifest = load_yaml(self.epdir / "production/manifest.json")
        manifest["charges"] = [{"id": identity, "stage": "sfx", "cost_cny": 0.15}]
        write_json(self.epdir / "production/manifest.json", manifest)
        sheet["cues"][0]["asset_origin"] = "user_supplied"
        write_yaml(self.epdir / "production/sound_cues.yaml", sheet)
        with self.assertRaisesRegex(ValueError, "素材"):
            settle(self.epdir, identity)
        sheet["cues"][0]["asset_origin"] = "generated"
        write_yaml(self.epdir / "production/sound_cues.yaml", sheet)
        manifest["charges"].append({"id": identity, "stage": "voice", "cost_cny": 0.1})
        write_json(self.epdir / "production/manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "唯一"):
            settle(self.epdir, identity)
        manifest["charges"].pop()
        write_json(self.epdir / "production/manifest.json", manifest)
        self.assertEqual(settle(self.epdir, identity)["status"], "done")
        self.assertEqual(settle(self.epdir, identity)["status"], "done")
        audio.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "哈希"):
            settle(self.epdir, identity)


if __name__ == "__main__":
    unittest.main()
