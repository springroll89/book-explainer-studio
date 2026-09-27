"""Selected real SFX assets can be bound without paid generation or approvals."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow import sfx_library
from bookflow.approvals import record_confirmation
from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.media_manifest import real_stage_fresh
from bookflow.produce import check as produce_check, run as produce_run
from bookflow.cost import estimate_episode
from bookflow.sound import check as sound_check, estimate_data
from bookflow.selftest import run as selftest_run
from bookflow.sfx_stage import bind_existing
from bookflow.sfx_jobs import reserve, settle, transition
from bookflow.adapters.ffmpeg_audio import inputs as mix_inputs


class SfxStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.assertTrue(selftest_run(root)["passed"])
        self.project = root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        self.production = self.epdir / "production"
        self.final = self.epdir / "final.md"
        self.cues = self.production / "sound_cues.yaml"
        self.source = self.production / "sfx.wav"
        self.library = root / "音效库"
        accepted = sfx_library.add(self.project, 1, self.source, desc="短促的雨声",
                                   tags=["雨", "窗"], sound_class="ambience", status="accepted",
                                   library=self.library)
        self.asset_id = accepted["id"]
        sentence = load_yaml(self.epdir / "final.sentences.json")["sentences"][-1]
        self.cue = {"cue_id": "C01", "description": "短促的雨声", "tags": ["雨"],
                    "sound_class": "ambience", "function": "bed", "gap_policy": "duck",
                    "duration_sec": 0.5, "level_db": -20, "anchor": {"sentence_id": sentence["id"]},
                    "placement": "before", "status": "planned"}
        self._save_cue()
        cast = self.project / "production/voice_cast.yaml"
        self.manifest_path = self.production / "manifest.json"
        write_json(self.manifest_path, {"mode": "real", "charges": [], "stages": {
            "cues": {"status": "done", "inputs": [self._row(path, self.project)
                                                     for path in (self.final, self.epdir / "final.sentences.json")],
                     "outputs": [self._row(self.cues, self.epdir)], "charge_ids": [], "cost_cny": 0},
            "voice": {"status": "done", "inputs": [self._row(path, self.project)
                                                    for path in (self.final, cast)],
                      "outputs": [self._row(path, self.epdir) for path in
                                  (self.production / "voice.wav", self.production / "timing.json")],
                      "charge_ids": [], "cost_cny": 0}}})

    @staticmethod
    def _row(path, root):
        return {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}

    def _save_cue(self):
        write_yaml(self.cues, {"cues": [self.cue]})

    def test_selected_accepted_asset_creates_real_binding_for_mixer(self):
        self.cue.update(status="reused", asset_id=self.asset_id)
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        result = bind_existing(self.epdir)
        self.assertTrue(result["passed"], result)
        manifest = load_yaml(self.manifest_path)
        record = manifest["stages"]["sfx"]
        self.assertEqual(record["cost_cny"], 0)
        self.assertEqual(record["charge_ids"], [])
        self.assertTrue(real_stage_fresh(self.epdir, "sfx", record))
        self.assertEqual(len(mix_inputs(self.epdir)["effects"]), 1)
        repeated = bind_existing(self.epdir)
        self.assertEqual(repeated["executed"], [])
        self.assertEqual(load_yaml(self.manifest_path)["charges"], [])
        entry = self.library / load_yaml(self.library / "index.yaml")[0]["file"]
        entry.write_bytes(b"tampered")
        self.assertFalse(real_stage_fresh(self.epdir, "sfx", record))

    def test_unselected_cue_reports_candidate_without_writing(self):
        before = sha256_file(self.manifest_path)
        result = bind_existing(self.epdir)
        self.assertFalse(result["passed"])
        self.assertIn(self.asset_id, result["candidates"]["C01"])
        self.assertEqual(sha256_file(self.manifest_path), before)
        self.assertFalse((self.production / "sfx_bindings.json").exists())

    def test_local_asset_requires_explicit_hash_and_is_not_overwritten(self):
        self.cue.update(status="asset_ready", asset_id="production/sfx.wav")
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "写入当前文件哈希"):
            bind_existing(self.epdir)
        self.assertNotIn("sfx", load_yaml(self.manifest_path)["stages"])
        self.cue["asset_sha256"] = sha256_file(self.source)
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "user_supplied"):
            bind_existing(self.epdir)
        self.cue["asset_origin"] = "user_supplied"
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        audio_hash = sha256_file(self.source)
        self.assertTrue(bind_existing(self.epdir)["passed"])
        self.assertEqual(sha256_file(self.source), audio_hash)

    def test_local_paid_asset_requires_existing_charge_and_keeps_its_cost(self):
        self.cue.update(status="asset_ready", asset_id="production/sfx.wav",
                        asset_sha256=sha256_file(self.source), asset_charge_id="sfx-existing-1")
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "缺少对应"):
            bind_existing(self.epdir)
        manifest["charges"].append({"id": "sfx-existing-1", "stage": "sfx", "cost_cny": 1.25})
        write_json(self.manifest_path, manifest)
        self.assertTrue(bind_existing(self.epdir)["passed"])
        stage = load_yaml(self.manifest_path)["stages"]["sfx"]
        self.assertEqual(stage["charge_ids"], ["sfx-existing-1"])
        self.assertEqual(stage["cost_cny"], 1.25)

    def test_journaled_paid_asset_cannot_bind_before_settlement(self):
        with patch("bookflow.sfx_jobs.guard_check", return_value={"passed": True, "errors": []}), \
             patch("bookflow.sfx_jobs.estimate_episode", return_value={"passed": True}):
            job = reserve(self.epdir, "C01")["job"]
        request_id = job["request_id"]
        transition(self.epdir, request_id, status="provider_done")
        self.cue.update(status="asset_ready", asset_id="production/sfx.wav",
                        asset_sha256=sha256_file(self.source), asset_charge_id=request_id,
                        asset_origin="generated")
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        manifest["charges"].append({"id": request_id, "stage": "sfx", "cost_cny": 0.25})
        write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "未结清"):
            bind_existing(self.epdir)
        self.assertEqual(settle(self.epdir, request_id)["status"], "done")
        self.assertTrue(bind_existing(self.epdir)["passed"])
        self.cue["description"] = "已修改的另一种雨声"
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "原始输入已变化"):
            bind_existing(self.epdir)

    def test_binding_uses_configured_cue_sheet_path(self):
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("mix", {})["cues"] = "production/custom_cues.yaml"
        write_yaml(config_path, config)
        self.cues = self.production / "custom_cues.yaml"
        self.cue.update(status="reused", asset_id=self.asset_id)
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        self.assertEqual(estimate_data(self.epdir)["cue_count"], 1)
        self.assertTrue(sound_check(self.epdir)["passed"])
        self.assertTrue(bind_existing(self.epdir)["passed"])
        record = load_yaml(self.manifest_path)["stages"]["sfx"]
        self.assertTrue(real_stage_fresh(self.epdir, "sfx", record))
        self.assertEqual(len(mix_inputs(self.epdir)["effects"]), 1)
        self.assertEqual(estimate_episode(self.project, 1)["new_sfx"], 0)

    def test_binding_cannot_replace_cue_source(self):
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config["sfx_production"] = {"binding_output": "production/custom_cues.json"}
        config.setdefault("mix", {})["cues"] = "production/custom_cues.json"
        write_yaml(config_path, config)
        self.cues = self.production / "custom_cues.json"
        self.cue.update(status="reused", asset_id=self.asset_id)
        write_json(self.cues, {"cues": [self.cue]})
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        cue_hash = sha256_file(self.cues)
        with self.assertRaisesRegex(ValueError, "不能覆盖人工 cue 表"):
            bind_existing(self.epdir)
        self.assertEqual(sha256_file(self.cues), cue_hash)

    def test_public_produce_remains_guarded_before_any_binding(self):
        self.cue.update(status="reused", asset_id=self.asset_id)
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        self.assertTrue(record_confirmation(self.project, "plan", "撤回方案",
                                            verify_transcript=False)["passed"])
        result = produce_run(self.project, 1, until="sfx")
        self.assertFalse(result["passed"])
        self.assertIn("守卫", result["summary"])
        self.assertFalse((self.production / "sfx_bindings.json").exists())
        self.assertNotIn("sfx", load_yaml(self.manifest_path)["stages"])

    def test_binding_is_invalidated_by_configuration_and_does_not_overwrite_unowned_file(self):
        self.cue.update(status="reused", asset_id=self.asset_id)
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        binding = self.production / "sfx_bindings.json"
        binding.write_text("manual binding\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            bind_existing(self.epdir)
        self.assertEqual(binding.read_text(encoding="utf-8"), "manual binding\n")
        binding.unlink()
        self.assertTrue(bind_existing(self.epdir)["passed"])
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config["sfx_production"] = {"binding_output": "production/new_bindings.json"}
        write_yaml(config_path, config)
        stage = {row["stage"]: row for row in produce_check(self.project, 1, until="sfx")["stages"]}["sfx"]
        self.assertFalse(stage["ready"])
        self.assertEqual(stage["reason"], "config_changed")

    def test_interrupted_binding_resumes_without_reselecting_or_charging(self):
        self.cue.update(status="reused", asset_id=self.asset_id)
        self._save_cue()
        manifest = load_yaml(self.manifest_path)
        manifest["stages"]["cues"]["outputs"] = [self._row(self.cues, self.epdir)]
        write_json(self.manifest_path, manifest)
        real_write = write_json
        manifest_writes = 0

        def fail_final_manifest(path, data):
            nonlocal manifest_writes
            if Path(path).resolve() == self.manifest_path.resolve():
                manifest_writes += 1
                if manifest_writes == 2:
                    raise OSError("simulated interruption")
            return real_write(path, data)

        with patch("bookflow.sfx_stage.write_json", side_effect=fail_final_manifest):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                bind_existing(self.epdir)
        pending = load_yaml(self.manifest_path)
        self.assertIn("sfx_binding_pending", pending)
        self.assertTrue((self.production / "sfx_bindings.json").is_file())
        self.assertTrue(bind_existing(self.epdir)["passed"])
        finished = load_yaml(self.manifest_path)
        self.assertNotIn("sfx_binding_pending", finished)
        self.assertEqual(finished["charges"], [])


if __name__ == "__main__":
    unittest.main()
