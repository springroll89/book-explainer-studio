"""End-to-end real voice state machine with a fake paid provider only."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.approvals import record_confirmation
from bookflow.guard import check as guard_check
from bookflow.produce import check, run
from bookflow.selftest import run as selftest_run
from bookflow.voice_jobs import read as read_jobs, reserve, settle, transition
from bookflow.voice_plan import plan
from bookflow.voice_stage import _assemble, _charge, _preflight_storage, _save_part, _settings
from bookflow.adapters.doubao_voice import ProviderUncertain


class FakeDoubao:
    def __init__(self, paragraphs, audio):
        self.paragraphs = paragraphs
        self.audio = audio
        self.submissions = []
        self.queries = []

    def submit(self, *, text, speaker, request_id, resource_id, model):
        number = len(self.submissions)
        self.submissions.append({"text": text, "request_id": request_id})
        return f"task-{number + 1}"

    def query(self, *, task_id, request_id, resource_id):
        self.queries.append(task_id)
        number = int(task_id.split("-")[1]) - 1
        paragraph = self.paragraphs[number]
        sentences = [{"text": row["text"], "startTime": index + 0.1,
                      "endTime": index + 0.8}
                     for index, row in enumerate(paragraph["sentences"])]
        return {"state": "done", "task_id": task_id, "audio_url": "https://example.com/audio.mp3",
                "billable_chars": len(paragraph["text"]), "sentences": sentences}

    def download_audio(self, url):
        return self.audio


class VoiceStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.assertTrue(selftest_run(root)["passed"])
        self.project = root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        self.production = self.epdir / "production"
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("sound_design", {}).setdefault("pricing", {}).update(
            narration_model_per_10k_chars=100, sfx_model_per_minute=0)
        write_yaml(config_path, config)
        cast = self.project / "production/voice_cast.yaml"
        write_yaml(cast, {"revision": 1, "engine": {"model": "seed-tts-2.0-standard",
                     "resource_id": "seed-tts-2.0", "sample_rate": 24000},
                     "narrator": {"voice_id": "test-speaker", "status": "confirmed"}})
        final = self.epdir / "final.md"
        cues = self.production / "sound_cues.yaml"
        write_json(self.production / "manifest.json", {"mode": "real", "charges": [], "stages": {
            "cues": {"status": "done", "inputs": [{"path": str(path.relative_to(self.project)),
                      "sha256": sha256_file(path)} for path in
                     (final, self.epdir / "final.sentences.json")],
                     "outputs": [{"path": str(cues.relative_to(self.epdir)), "sha256": sha256_file(cues)}],
                     "cost_cny": 0, "charge_ids": []}}})
        for name in ("voice.wav", "timing.json", "voice_segments.json"):
            (self.production / name).unlink()
        self.paragraphs = plan(self.epdir)["paragraphs"]
        self.assertGreater(len(self.paragraphs), 1)
        duration = max(len(row["sentences"]) for row in self.paragraphs) + 2
        audio = root / "fake-provider.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", str(duration), "-y", str(audio)], check=True, capture_output=True)
        self.fake = FakeDoubao(self.paragraphs, audio.read_bytes())

    def test_guard_and_explicit_paid_switch(self):
        self.assertTrue(record_confirmation(self.project, "plan", "撤回方案",
                                            verify_transcript=False)["passed"])
        guard = guard_check(self.project, "media-generate", 1)
        self.assertFalse(guard["passed"], guard)
        with patch("bookflow.voice_stage.DoubaoVoiceClient", return_value=self.fake):
            safe = run(self.project, 1, until="voice")
            guarded = run(self.project, 1, until="voice", allow_paid=True)
        self.assertFalse(safe["passed"])
        self.assertFalse(guarded["passed"])
        self.assertIn("守卫", guarded["summary"])
        self.assertEqual(self.fake.submissions, [])
        self.assertFalse((self.production / "voice_jobs.json").exists())

    def _complete_parts(self):
        settings = _settings(self.epdir)
        _preflight_storage(self.epdir, settings)
        planned = plan(self.epdir)
        for index, paragraph in enumerate(planned["paragraphs"]):
            state = reserve(self.epdir, paragraph, cast_sha=planned["cast_sha256"],
                            config_sha=planned["config_sha256"])
            self.assertEqual(state["action"], "submit", state)
            job = state["job"]
            transition(self.epdir, job["request_id"], status="running", task_id=f"task-{index + 1}")
            transition(self.epdir, job["request_id"], status="provider_done")
            response = self.fake.query(task_id=f"task-{index + 1}", request_id="local-test",
                                       resource_id="seed-tts-2.0")
            amount = _charge(self.epdir, job, response["billable_chars"])
            _save_part(self.epdir, settings, paragraph, job, response, self.fake, amount)
            settle(self.epdir, job["request_id"])
        return settings

    def test_journal_and_local_assembly_use_immutable_charges(self):
        settings = self._complete_parts()
        self.assertEqual(len(read_jobs(self.epdir)["jobs"]), len(self.paragraphs))
        self.assertTrue(all(row["status"] == "done" for row in read_jobs(self.epdir)["jobs"]))
        self.assertTrue(all(row["cached"] for row in plan(self.epdir)["paragraphs"]))
        _assemble(self.epdir, settings, plan(self.epdir))
        rows = check(self.project, 1, until="voice")["stages"]
        self.assertTrue(rows[-1]["ready"], rows)
        manifest = load_yaml(self.production / "manifest.json")
        self.assertEqual(len(manifest["charges"]), len(self.paragraphs))
        self.assertEqual(len(manifest["stages"]["voice"]["charge_ids"]), len(self.paragraphs))
        timing = load_yaml(self.production / "timing.json")
        stable = load_yaml(self.epdir / "final.sentences.json")["sentences"]
        self.assertEqual([(row["id"], row["text"]) for row in timing["sentences"]],
                         [(row["id"], row["text"]) for row in stable])
        repeated = _assemble(self.epdir, settings, plan(self.epdir))
        self.assertEqual(repeated["executed"], ["voice"])
        self.assertEqual(len(load_yaml(self.production / "manifest.json")["charges"]), len(self.paragraphs))

    def test_configured_cache_paths_are_used_by_planning_settlement_and_assembly(self):
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("voice_production", {}).update(
            segments="production/cache/paragraphs.json",
            parts_dir="production/cache/parts")
        write_yaml(config_path, config)

        settings = self._complete_parts()

        self.assertEqual(settings["segments"], self.production / "cache/paragraphs.json")
        self.assertEqual(settings["parts"], self.production / "cache/parts")
        self.assertTrue(settings["segments"].is_file())
        self.assertTrue(all(path.is_file() for path in settings["parts"].glob("*.mp3")))
        _assemble(self.epdir, settings, plan(self.epdir))
        voice_check = check(self.project, 1, until="voice")
        self.assertTrue(voice_check["stages"][-1]["ready"], voice_check["stages"])

    def test_cache_paths_cannot_escape_production(self):
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("voice_production", {})["parts_dir"] = "../outside"
        write_yaml(config_path, config)
        with self.assertRaisesRegex(ValueError, "相对路径"):
            _settings(self.epdir)

    def test_assembly_recovers_after_partial_swap(self):
        settings = self._complete_parts()
        original_replace = os.replace
        failed = False

        def fail_timing_once(source, target):
            nonlocal failed
            if Path(target) == settings["timing"] and not failed:
                failed = True
                raise OSError("simulated interruption")
            return original_replace(source, target)

        with patch("bookflow.voice_stage.os.replace", side_effect=fail_timing_once):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                _assemble(self.epdir, settings, plan(self.epdir))
        self.assertTrue(failed)
        manifest = load_yaml(self.production / "manifest.json")
        self.assertIn("voice_assembly_pending", manifest)
        self.assertTrue(settings["output"].is_file())
        _preflight_storage(self.epdir, settings)
        _assemble(self.epdir, settings, plan(self.epdir))
        manifest = load_yaml(self.production / "manifest.json")
        self.assertNotIn("voice_assembly_pending", manifest)
        self.assertEqual(len(manifest["charges"]), len(self.paragraphs))

    def test_existing_unowned_voice_blocks_before_paid_submit(self):
        (self.production / "voice.wav").write_bytes(b"manual-audio")
        with self.assertRaisesRegex(ValueError, "付费前停止"):
            _preflight_storage(self.epdir, _settings(self.epdir))
        self.assertFalse((self.production / "voice_jobs.json").exists())
        self.assertEqual(self.fake.submissions, [])

    def test_download_failure_keeps_original_task_and_charge(self):
        planned = plan(self.epdir)
        paragraph = planned["paragraphs"][0]
        state = reserve(self.epdir, paragraph, cast_sha=planned["cast_sha256"],
                        config_sha=planned["config_sha256"])
        job = state["job"]
        transition(self.epdir, job["request_id"], status="running", task_id="task-1")
        transition(self.epdir, job["request_id"], status="provider_done")
        response = self.fake.query(task_id="task-1", request_id="local-test", resource_id="seed-tts-2.0")
        amount = _charge(self.epdir, job, response["billable_chars"])
        with patch.object(self.fake, "download_audio", side_effect=ProviderUncertain("offline")):
            with self.assertRaises(ProviderUncertain):
                _save_part(self.epdir, _settings(self.epdir), paragraph, job, response, self.fake, amount)
        self.assertEqual(read_jobs(self.epdir)["jobs"][0]["status"], "provider_done")
        retry = reserve(self.epdir, paragraph, cast_sha=planned["cast_sha256"],
                        config_sha=planned["config_sha256"])
        self.assertEqual((retry["action"], retry["job"]["request_id"]), ("query", job["request_id"]))
        _charge(self.epdir, job, response["billable_chars"])
        self.assertEqual(len(load_yaml(self.production / "manifest.json")["charges"]), 1)


if __name__ == "__main__":
    unittest.main()
