"""Existing human cue sheets become real media evidence only after validation."""
import tempfile
import unittest
from pathlib import Path

from bookflow.approvals import record_confirmation
from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.media_manifest import real_stage_fresh
from bookflow.produce import check as produce_check, run as produce_run
from bookflow.selftest import run as selftest_run


class CuesStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.assertTrue(selftest_run(root)["passed"])
        self.project = root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        self.final = self.epdir / "final.md"
        self.sidecar = self.epdir / "final.sentences.json"
        self.cue_path = self.epdir / "production/sound_cues.yaml"
        self.manifest_path = self.epdir / "production/manifest.json"
        self.sentences = load_yaml(self.sidecar)["sentences"]
        self.cue = {"cue_id": "C01", "sound_class": "event", "function": "transition",
                    "description": "钟声", "gap_policy": "duck", "duration_sec": 0.5,
                    "level_db": -18, "anchor": {"sentence_id": self.sentences[-1]["id"]},
                    "placement": "before", "status": "planned"}
        self._save()
        write_json(self.manifest_path, {"mode": "real", "charges": [], "stages": {}})

    def _save(self):
        write_yaml(self.cue_path, {"draft_sha256": sha256_file(self.final), "cues": [self.cue]})

    def test_binds_without_overwriting_human_sheet_and_skips_unchanged(self):
        from bookflow.cues_stage import bind_existing
        original = sha256_file(self.cue_path)
        result = bind_existing(self.epdir)
        self.assertTrue(result["passed"], result)
        self.assertEqual(sha256_file(self.cue_path), original)
        record = load_yaml(self.manifest_path)["stages"]["cues"]
        self.assertTrue(real_stage_fresh(self.epdir, "cues", record))
        self.assertEqual(record["cost_cny"], 0)
        self.assertEqual(bind_existing(self.epdir)["executed"], [])
        self.assertTrue({"episodes/ep01/final.md", "episodes/ep01/final.sentences.json"}
                        <= {row["path"] for row in record["inputs"]})
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("format", {})["speech_rate_cpm"] = 300
        write_yaml(config_path, config)
        row = produce_check(self.project, 1, until="cues")["stages"][0]
        self.assertEqual((row["ready"], row["reason"]), (False, "config_changed"))

    def test_invalid_anchor_and_stale_sidecar_write_nothing(self):
        from bookflow.cues_stage import bind_existing
        self.cue["anchor"]["sentence_id"] = "s999"
        self._save()
        original = sha256_file(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "锚点"):
            bind_existing(self.epdir)
        self.assertEqual(sha256_file(self.manifest_path), original)
        self.cue["anchor"]["sentence_id"] = self.sentences[-1]["id"]
        self._save()
        sidecar = load_yaml(self.sidecar)
        sidecar["draft_sha256"] = "0" * 64
        write_json(self.sidecar, sidecar)
        with self.assertRaisesRegex(ValueError, "句子表"):
            bind_existing(self.epdir)
        self.assertEqual(sha256_file(self.manifest_path), original)

    def test_opening_five_seconds_and_blank_sheet_do_not_pass_silently(self):
        from bookflow.cues_stage import bind_existing
        self.cue["anchor"]["sentence_id"] = self.sentences[0]["id"]
        self._save()
        with self.assertRaisesRegex(ValueError, "前5秒"):
            bind_existing(self.epdir)
        self.cue["status"] = "on_hold"
        self._save()
        self.assertTrue(bind_existing(self.epdir)["passed"])
        write_yaml(self.cue_path, {"draft_sha256": sha256_file(self.final), "cues": []})
        with self.assertRaisesRegex(ValueError, "无音效理由"):
            bind_existing(self.epdir)

    def test_public_produce_accepts_passphrase_confirmations(self):
        # Only approvals/log.yaml exists here (no legacy G1-G3 files); the guard must read it.
        self.assertFalse(list((self.project / "approvals").glob("G*.yaml")))
        result = produce_run(self.project, 1, until="cues")
        self.assertTrue(result["passed"], result)
        self.assertIn("cues", load_yaml(self.manifest_path)["stages"])

    def test_public_produce_still_requires_media_guard(self):
        self.assertTrue(record_confirmation(self.project, "plan", "撤回方案",
                                            verify_transcript=False)["passed"])
        result = produce_run(self.project, 1, until="cues")
        self.assertFalse(result["passed"])
        self.assertIn("守卫", result["summary"])
        self.assertNotIn("cues", load_yaml(self.manifest_path)["stages"])


if __name__ == "__main__":
    unittest.main()
