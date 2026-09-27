"""Dialogue lines are voiced by their characters; subtitles stay per sentence."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from bookflow import voice_script
from bookflow.approvals import record_confirmation
from bookflow.common import atomic_write, load_yaml, sha256_file, write_json, write_yaml
from bookflow.produce import check
from bookflow.selftest import run as selftest_run
from bookflow.sentences import generate
from bookflow.voice_plan import plan
from bookflow.voice_stage import advance

DIALOGUE = "\n\n她伸出手：“钥匙留下。”他把手缩回去：“不行。”\n\n院长说那是他们的“家”。\n"


class FakeDoubao:
    def __init__(self, audio):
        self.audio = audio
        self.submissions = []
        self.units = {}

    def submit(self, *, text, speaker, request_id, resource_id, model):
        self.submissions.append({"text": text, "speaker": speaker})
        return f"task-{len(self.submissions)}"

    def query(self, *, task_id, request_id, resource_id):
        unit = self.units[task_id]
        sentences = [{"text": row["text"], "startTime": index + 0.1, "endTime": index + 0.8}
                     for index, row in enumerate(unit["sentences"])]
        return {"state": "done", "task_id": task_id, "audio_url": "https://example.com/a.mp3",
                "billable_chars": len(unit["text"]), "sentences": sentences}

    def download_audio(self, url):
        return self.audio


class MultiVoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.assertTrue(selftest_run(root)["passed"])
        self.project = root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        self.production = self.epdir / "production"
        self.final = self.epdir / "final.md"
        atomic_write(self.final, self.final.read_text(encoding="utf-8").rstrip("\n") + DIALOGUE)
        generate(self.final, None)
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [1],
                                            verify_transcript=False)["passed"])
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("sound_design", {}).setdefault("pricing", {}).update(
            narration_model_per_10k_chars=100, sfx_model_per_minute=0)
        write_yaml(config_path, config)
        write_yaml(self.project / "production/voice_cast.yaml", {
            "revision": 1, "engine": {"model": "seed-tts-2.0-standard", "resource_id": "seed-tts-2.0",
                                      "sample_rate": 24000},
            "narrator": {"voice_id": "v-narrator", "status": "confirmed"},
            "characters": {"P01": {"name": "劳拉", "voice_id": "v-laura", "status": "confirmed"},
                           "P02": {"name": "阿尔贝托", "voice_pool": "secondary_male", "status": "confirmed"}},
            "shared_voice_pools": {"secondary_male": {"voice_id": "v-pool", "status": "confirmed"}}})
        cues = self.production / "sound_cues.yaml"
        write_json(self.production / "manifest.json", {"mode": "real", "charges": [], "stages": {
            "cues": {"status": "done", "inputs": [{"path": str(path.relative_to(self.project)),
                      "sha256": sha256_file(path)} for path in (self.final, self.epdir / "final.sentences.json")],
                     "outputs": [{"path": str(cues.relative_to(self.epdir)), "sha256": sha256_file(cues)}],
                     "cost_cny": 0, "charge_ids": []}}})
        for name in ("voice.wav", "timing.json", "voice_segments.json"):
            path = self.production / name
            if path.exists():
                path.unlink()

    def label(self, speakers):
        created = voice_script.scaffold(self.project, 1)
        self.assertEqual(created["quotes"], 3)
        path = voice_script.script_path(self.epdir)
        data = load_yaml(path)
        self.assertEqual([row["text"] for row in data["quotes"]], ["钥匙留下。", "不行。", "家"])
        for row, speaker in zip(data["quotes"], speakers):
            row["speaker"] = speaker
        write_yaml(path, data)

    def test_season_check_lists_missing_scripts_and_voices(self):
        before = voice_script.season(self.project, [1])
        self.assertEqual(before["missing_scripts"], [1])
        self.label(["P01", "P09", "narrator"])
        gap = voice_script.season(self.project, [1])
        self.assertFalse(gap["passed"])
        self.assertEqual([row["id"] for row in gap["needs_voice"]], ["P09"])
        self.assertEqual([row["id"] for row in gap["reused"]], ["P01"])
        self.assertEqual([row["id"] for row in gap["cast_without_lines"]], ["P02"])
        updated = voice_script.set_voice(self.project, "P09", voice_id="v-new", name="新人物")
        self.assertEqual(updated["revision"], 2)
        self.assertTrue(voice_script.season(self.project, [1])["passed"])

    def test_script_is_bound_to_final_text(self):
        self.label(["P01", "P02", "narrator"])
        atomic_write(self.final, self.final.read_text(encoding="utf-8").replace("不行。", "不给。"))
        state = voice_script.check_episode(self.project, 1)
        self.assertEqual(state["state"], "invalid")
        self.assertTrue(any("当前 final.md" in error for error in state["errors"]))

    def test_units_split_quotes_and_timing_rejoins_sentences(self):
        self.label(["P01", "P02", "narrator"])
        planned = plan(self.epdir)
        self.assertTrue(planned["multi_voice"])
        dialogue = [row for row in planned["paragraphs"] if row["speaker"] != "v-narrator"]
        self.assertEqual([(row["speaker"], row["text"]) for row in dialogue],
                         [("v-laura", "钥匙留下。"), ("v-pool", "不行。")])
        scare = next(row for row in planned["paragraphs"] if "家" in row["text"])
        self.assertEqual(scare["speaker"], "v-narrator")
        audio = self.production.parent.parent.parent / "fake.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "30", "-y", str(audio)], check=True, capture_output=True)
        fake = FakeDoubao(audio.read_bytes())
        result = None
        for _ in range(4 * len(planned["paragraphs"]) + 2):
            current = plan(self.epdir)
            pending = next((row for row in current["paragraphs"] if not row["cached"]), None)
            if pending is not None:
                fake.units[f"task-{len(fake.submissions) + 1}"] = pending
            result = advance(self.project, self.epdir, client=fake)
            if result.get("executed") == ["voice"]:
                break
        self.assertEqual(result.get("executed"), ["voice"], result)
        self.assertIn({"text": "钥匙留下。", "speaker": "v-laura"}, fake.submissions)
        self.assertIn({"text": "不行。", "speaker": "v-pool"}, fake.submissions)
        timing = load_yaml(self.production / "timing.json")
        stable = load_yaml(self.epdir / "final.sentences.json")["sentences"]
        self.assertEqual([(row["id"], row["text"]) for row in timing["sentences"]],
                         [(row["id"], row["text"]) for row in stable])
        self.assertTrue(check(self.project, 1, until="voice")["stages"][1]["ready"])
        inputs = {row["path"] for row in load_yaml(self.production / "manifest.json")["stages"]["voice"]["inputs"]}
        self.assertIn("episodes/ep01/production/voice_script.yaml", inputs)

    def test_new_character_elsewhere_keeps_this_episode_cache_key(self):
        self.label(["P01", "P02", "narrator"])
        before = plan(self.epdir)["cast_sha256"]
        voice_script.set_voice(self.project, "P31", voice_id="v-other", name="别集人物")
        self.assertEqual(plan(self.epdir)["cast_sha256"], before)

    def test_unlabelled_dialogue_blocks_synthesis(self):
        voice_script.scaffold(self.project, 1)
        with self.assertRaisesRegex(ValueError, "voices check"):
            plan(self.epdir)


if __name__ == "__main__":
    unittest.main()
