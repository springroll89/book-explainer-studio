"""No-paid-call budget tests on an isolated selftest fixture."""
import tempfile
import subprocess
import unittest
from pathlib import Path

from bookflow import cost, produce, sfx_library
from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.selftest import run as selftest_run
from bookflow.voice_plan import plan as voice_plan


class CostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        result = selftest_run(self.root)
        self.assertTrue(result["passed"], result)
        self.project = self.root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"

    def _prices(self, *, voice=100, sfx=2, budget=20):
        path = self.project / "project.yaml"
        data = load_yaml(path)
        data["cost"] = {"per_episode_cny": budget}
        data.setdefault("sound_design", {}).setdefault("pricing", {}).update(
            narration_model_per_10k_chars=voice, sfx_model_per_minute=sfx)
        write_yaml(path, data)

    def _cue(self, identity=""):
        path = self.epdir / "production/sound_cues.yaml"
        data = load_yaml(path)
        data["cues"] = [{"cue_id": "C01", "description": "雨打窗户", "sound_class": "ambience",
                         "function": "bed", "duration_sec": 60, "status": "planned", "asset_id": identity}]
        write_yaml(path, data)

    def test_missing_price_blocks_real_preflight_without_mutating_media(self):
        self._cue()
        before = sha256_file(self.epdir / "production/final.mp4")
        estimate = cost.estimate_episode(self.project, 1)
        self.assertFalse(estimate["passed"])
        self.assertIsNone(estimate["total_estimated_cny"])
        self.assertTrue(any("单价" in item for item in estimate["blockers"]))
        result = produce.run(self.project, 1)
        self.assertFalse(result["passed"])
        self.assertEqual(result["budget"]["status"], "warning")
        self.assertEqual(sha256_file(self.epdir / "production/final.mp4"), before)

    def test_reuse_reduces_reservation_and_only_over_cap_requests_decision(self):
        self._prices(voice=100, sfx=2, budget=20)
        self._cue()
        fresh = cost.estimate_episode(self.project, 1)
        self.assertTrue(fresh["passed"], fresh)
        self.assertEqual(fresh["new_sfx"], 1)
        self.assertEqual(fresh["sfx_reserved_cny"], 2)
        self.assertFalse(fresh["over_budget"])
        added = sfx_library.add(self.project, 1, self.epdir / "production/sfx.wav", desc="雨打窗户",
                                tags=["雨", "窗户"], sound_class="ambience", status="accepted")
        self._cue(added["id"])
        reused = cost.estimate_episode(self.project, 1)
        self.assertTrue(reused["passed"], reused)
        self.assertEqual(reused["new_sfx"], 0)
        self.assertEqual(reused["sfx_reserved_cny"], 0)
        self.assertEqual(reused["estimated_sfx_saved_cny"], 2)
        self._prices(voice=100, sfx=2, budget=0.01)
        over = cost.estimate_episode(self.project, 1)
        self.assertTrue(over["over_budget"])
        self.assertIn("超过", "；".join(over["blockers"]))

    def test_valid_real_output_reserves_no_duplicate_voice_charge(self):
        self._prices(voice=None, sfx=None, budget=20)
        final = self.epdir / "final.md"
        voice = self.epdir / "production/voice.wav"
        timing = self.epdir / "production/timing.json"
        inputs = [{"path": str(path.relative_to(self.project)), "sha256": sha256_file(path)}
                  for path in (final, self.project / "production/voice_cast.yaml")]
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "charges": [
            {"id": "voice-001", "stage": "voice", "cost_cny": 1.5}], "stages": {
            "voice": {"status": "done", "input_sha256": produce._input_digest(final, "voice"),
                      "inputs": inputs,
                      "outputs": [{"path": "production/voice.wav", "sha256": sha256_file(voice)},
                                  {"path": "production/timing.json", "sha256": sha256_file(timing)}],
                      "cost_cny": 1.5, "charge_ids": ["voice-001"]}}})
        cached = cost.estimate_episode(self.project, 1)
        self.assertTrue(cached["passed"], cached)
        self.assertEqual(cached["cached_stages"], ["voice"])
        self.assertEqual(cached["actual_spent_cny"], 1.5)
        self.assertEqual(cached["voice_reserved_cny"], 0)
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "charges": [
            {"id": "voice-001", "stage": "voice", "cost_cny": 1.5}], "stages": {
            "voice": {"status": "submitted", "input_sha256": produce._input_digest(final, "voice"),
                      "cost_cny": 0, "charge_ids": []}}})
        pending = cost.estimate_episode(self.project, 1)
        self.assertFalse(pending["passed"])
        self.assertIn("不能重发", "；".join(pending["blockers"]))
        self.assertEqual(pending["actual_spent_cny"], 1.5)

    def test_real_paragraph_cache_only_reserves_missing_voice_text(self):
        self._prices(voice=100, sfx=2)
        self._cue()
        planned = voice_plan(self.epdir)
        self.assertGreater(len(planned["paragraphs"]), 1)
        first = planned["paragraphs"][0]
        parts = self.epdir / "production/voice_parts"
        audio = parts / first["path"]
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "10", "-y", str(audio)], check=True, capture_output=True)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", str(audio)], check=True, capture_output=True)
        duration = float(probe.stdout.decode().strip())
        count = len(first["sentences"])
        timing = [{**sentence, "startTime": round(i * duration / count, 3),
                   "endTime": round((i + 1) * duration / count, 3)}
                  for i, sentence in enumerate(first["sentences"])]
        write_json(self.epdir / "production/voice_segments.json", {"mode": "real", "segments": [{
            "path": first["path"], "status": "done", "sha256": sha256_file(audio),
            "duration_sec": duration, "sentences": timing,
            "charge_id": "voice-cached",
            "text_sha256": first["text_sha256"], "voice_cast_sha256": planned["cast_sha256"],
            "config_sha256": planned["config_sha256"]}]})
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "charges": [
            {"id": "voice-cached", "stage": "voice", "cost_cny": 0.01}], "stages": {}})
        estimate = cost.estimate_episode(self.project, 1)
        self.assertTrue(estimate["passed"], estimate)
        self.assertEqual(estimate["voice_reused_paragraphs"], 1)
        self.assertEqual(estimate["voice_new_billable_chars"], sum(
            len(row["text"]) for row in planned["paragraphs"][1:]))
        self.assertLess(estimate["voice_reserved_cny"], estimate["voice_billable_chars"] * 100 / 10000)

    def test_prior_attempt_cost_survives_stage_replacement(self):
        self._prices(voice=100, sfx=2, budget=20)
        self._cue()
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "charges": [
            {"id": "voice-old", "stage": "voice", "cost_cny": 3},
            {"id": "voice-new", "stage": "voice", "cost_cny": 1.5}], "stages": {
            "voice": {"status": "failed", "cost_cny": 1.5, "charge_ids": ["voice-new"]}}})
        estimate = cost.estimate_episode(self.project, 1)
        self.assertTrue(estimate["passed"], estimate)
        self.assertEqual(estimate["actual_spent_cny"], 4.5)
        self.assertGreater(estimate["voice_reserved_cny"], 0)
        self._prices(voice=100, sfx=2, budget=0.01)
        self.assertTrue(cost.estimate_episode(self.project, 1)["over_budget"])

    def test_legacy_or_inconsistent_charges_block_paid_retry(self):
        self._prices()
        self._cue()
        path = self.epdir / "production/manifest.json"
        write_json(path, {"mode": "real", "stages": {"voice": {"status": "failed", "cost_cny": 1}}})
        legacy = cost.estimate_episode(self.project, 1)
        self.assertFalse(legacy["passed"])
        self.assertEqual(legacy["actual_spent_cny"], 1)
        self.assertIn("历史账单", "；".join(legacy["blockers"]))
        write_json(path, {"mode": "real", "charges": [
            {"id": "voice-1", "stage": "voice", "cost_cny": 1}], "stages": {
            "voice": {"status": "failed", "cost_cny": 0, "charge_ids": ["voice-1"]}}})
        inconsistent = cost.estimate_episode(self.project, 1)
        self.assertFalse(inconsistent["passed"])
        self.assertIn("不一致", "；".join(inconsistent["blockers"]))
        write_json(path, {"mode": "real", "charges": [
            {"id": "voice-1", "stage": "voice", "cost_cny": 1},
            {"id": "voice-1", "stage": "voice", "cost_cny": 1}], "stages": {}})
        with self.assertRaisesRegex(ValueError, "重复的费用 id"):
            cost.estimate_episode(self.project, 1)

    def test_invalid_cue_duration_fails_closed(self):
        self._prices()
        self._cue()
        path = self.epdir / "production/sound_cues.yaml"
        data = load_yaml(path)
        data["cues"][0]["duration_sec"] = "not-a-duration"
        write_yaml(path, data)
        estimate = cost.estimate_episode(self.project, 1)
        self.assertFalse(estimate["passed"])
        self.assertIn("时长", "；".join(estimate["blockers"]))


if __name__ == "__main__":
    unittest.main()
