"""Paid sound-effect generation: one journaled request per call, never resent."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.adapters.doubao_sfx import SfxProviderError
from bookflow.common import load_yaml, write_json, write_yaml
from bookflow.selftest import run as selftest_run
from bookflow.sfx_generate import generate_next
from bookflow.sfx_jobs import read
from bookflow.sfx_library import _duration


class FakeSfx:
    def __init__(self, audio: bytes, duration: float, fail: Exception | None = None):
        self.audio, self.duration, self.fail = audio, duration, fail
        self.calls = []

    def generate(self, *, prompt, request_id):
        self.calls.append(prompt)
        if self.fail:
            raise self.fail
        return {"audio": self.audio, "duration_sec": self.duration}


class SfxGenerateTests(unittest.TestCase):
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
            narration_model_per_10k_chars=2.8, sfx_model_per_minute=1.0)
        write_yaml(config_path, config)
        self.cue = {"cue_id": "C01", "description": "木门吱呀打开", "tags": ["门", "木门"],
                    "sound_class": "event", "function": "reveal", "duration_sec": 3, "status": "planned"}
        write_yaml(self.epdir / "production/sound_cues.yaml", {"cues": [self.cue]})
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "charges": [],
                                                             "stages": {"cues": {}, "voice": {}}})
        clip = root / "clip.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", "1", "-y", str(clip)], check=True, capture_output=True)
        self.audio = clip.read_bytes()
        patches = [patch("bookflow.sfx_jobs.guard_check", return_value={"passed": True, "errors": []}),
                   patch("bookflow.sfx_jobs.real_stage_fresh", return_value=True),
                   patch("bookflow.sfx_jobs.estimate_episode", return_value={"passed": True}),
                   patch("bookflow.cues_stage.bind_existing", return_value={"passed": True})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_generates_charges_marks_cue_and_settles(self):
        fake = FakeSfx(self.audio, 1.0)
        result = generate_next(self.project, self.epdir, client=fake)
        self.assertTrue(result["passed"], result)
        self.assertIn("时长约3秒", fake.calls[0])
        cue = load_yaml(self.epdir / "production/sound_cues.yaml")["cues"][0]
        self.assertEqual((cue["status"], cue["asset_origin"]), ("asset_ready", "generated"))
        asset = self.epdir / cue["asset_id"]
        self.assertTrue(asset.name.endswith("-loop.wav"))  # 1 s clip extended to the planned 3 s
        self.assertGreaterEqual(_duration(asset) + 0.05, 3)
        charges = load_yaml(self.epdir / "production/manifest.json")["charges"]
        self.assertEqual([(row["stage"], row["cost_cny"]) for row in charges], [("sfx", 0.02)])
        self.assertEqual(read(self.epdir)["jobs"][0]["status"], "done")
        again = generate_next(self.project, self.epdir, client=fake)
        self.assertEqual(again["summary"], "没有待生成的音效")
        self.assertEqual(len(fake.calls), 1)

    def test_uncertain_answer_locks_request_without_resend(self):
        fake = FakeSfx(self.audio, 1.0, fail=TimeoutError("network"))
        result = generate_next(self.project, self.epdir, client=fake)
        self.assertEqual(result["progress"], "provider_uncertain")
        self.assertEqual(read(self.epdir)["jobs"][0]["status"], "unknown")
        retry = generate_next(self.project, self.epdir, client=FakeSfx(self.audio, 1.0))
        self.assertEqual(retry["progress"], "request_unsettled")
        self.assertEqual(len(read(self.epdir)["jobs"]), 1)
        self.assertEqual(load_yaml(self.epdir / "production/manifest.json")["charges"], [])

    def test_local_client_error_does_not_reserve_a_request(self):
        for error in (SfxProviderError("missing credential"), ValueError("invalid config"),
                      OSError("unreadable config")):
            with self.subTest(error=type(error).__name__), \
                 patch("bookflow.adapters.doubao_sfx.DoubaoSfxClient", side_effect=error):
                result = generate_next(self.project, self.epdir)
                self.assertFalse(result["passed"])
                self.assertEqual(result["progress"], "client_not_ready")
                self.assertEqual(read(self.epdir)["jobs"], [])
                self.assertFalse((self.epdir / "production/sfx_jobs.json").exists())
        fake = FakeSfx(self.audio, 1.0)
        self.assertTrue(generate_next(self.project, self.epdir, client=fake)["passed"])
        self.assertEqual(len(fake.calls), 1)

    def test_provider_failure_is_recorded_without_automatic_resend(self):
        fake = FakeSfx(self.audio, 1.0, fail=SfxProviderError("HTTP 401 fixture rejection"))
        result = generate_next(self.project, self.epdir, client=fake)
        self.assertFalse(result["passed"])
        self.assertEqual(result["progress"], "provider_failed")
        self.assertEqual(read(self.epdir)["jobs"][0]["status"], "failed")
        self.assertEqual(load_yaml(self.epdir / "production/manifest.json")["charges"], [])
        retry_client = FakeSfx(self.audio, 1.0)
        retry = generate_next(self.project, self.epdir, client=retry_client)
        self.assertEqual(retry["progress"], "request_unsettled")
        self.assertEqual(retry_client.calls, [])
        self.assertEqual(len(read(self.epdir)["jobs"]), 1)

    def test_library_candidate_blocks_paid_generation_until_forced(self):
        found = {"items": [{"id": "SFX-0001"}]}
        with patch("bookflow.sfx_library.search", return_value=found):
            fake = FakeSfx(self.audio, 1.0)
            blocked = generate_next(self.project, self.epdir, client=fake)
            self.assertEqual(blocked["progress"], "library_candidates")
            self.assertEqual(fake.calls, [])
            self.cue["generate"] = True
            write_yaml(self.epdir / "production/sound_cues.yaml", {"cues": [self.cue]})
            self.assertTrue(generate_next(self.project, self.epdir, client=fake)["passed"])


if __name__ == "__main__":
    unittest.main()
