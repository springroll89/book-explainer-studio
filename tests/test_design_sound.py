"""Style, character-sheet and per-episode sound confirmations; audition and casting sheets."""
import tempfile
import unittest
from pathlib import Path

from bookflow import voice_script
from bookflow.approvals import confirmation_state, parse_confirmation, record_confirmation
from bookflow.audition import build, sheet_state
from bookflow.common import atomic_write, load_yaml, write_yaml
from bookflow.flow import derive
from bookflow.guard import check as guard_check, required_confirmations
from bookflow.selftest import run as selftest_run
from bookflow.sentences import generate


class ParseTests(unittest.TestCase):
    def test_new_passphrases(self):
        for quote, gate in (("拍板画风", "style"), ("拍板定妆。", "characters"), ("拍版声音", "sound")):
            with self.subTest(quote=quote):
                self.assertEqual(parse_confirmation(quote)["gate"], gate)
        self.assertEqual(parse_confirmation("拍板声音，除第3集")["exclude"], [3])
        self.assertEqual(parse_confirmation("撤回定妆")["action"], "revoke")
        for quote in ("画风可以", "拍板画风和定妆", "拍板声音吧"):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                parse_confirmation(quote)

    def test_visual_action_requires_design_and_sound(self):
        needed = required_confirmations("visual", 2)
        for item in (("style", None), ("characters", None), ("sound", 2), ("sample", 1)):
            self.assertIn(item, needed)
        self.assertNotIn(("sound", 2), required_confirmations("media-generate", 2))


class FixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assertTrue(selftest_run(Path(temporary.name))["passed"])
        self.project = Path(temporary.name) / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"

    def test_changed_character_sheet_reopens_design_and_blocks_visuals(self):
        self.assertEqual(confirmation_state(self.project, "characters")["state"], "passed")
        self.assertTrue(guard_check(self.project, "visual", 1)["passed"])
        atomic_write(self.project / "visual/P01.png", "changed")
        self.assertEqual(confirmation_state(self.project, "characters")["state"], "invalidated")
        state = derive(self.project)
        self.assertEqual(state["stage"], "设计确认")
        self.assertIn("定妆交付物变动", " ".join(state["blockers"]))
        blocked = guard_check(self.project, "visual", 1)
        self.assertFalse(blocked["passed"])
        self.assertIn("定妆", " ".join(blocked["errors"]))

    def test_audition_sheet_is_bound_to_the_mix(self):
        result = build(self.project, 1)
        self.assertTrue(result["passed"], result)
        text = (self.epdir / "production/audition_sheet.md").read_text(encoding="utf-8")
        self.assertIn("音效时间线（0 条）", text)
        self.assertEqual(sheet_state(self.project, 1)["state"], "current")
        mix = self.epdir / "production/final_mix.wav"
        mix.write_bytes(mix.read_bytes() + b"\0")
        self.assertEqual(sheet_state(self.project, 1)["state"], "stale")
        self.assertEqual(confirmation_state(self.project, "sound", 1)["state"], "invalidated")
        refused = record_confirmation(self.project, "sound", "拍板声音", [1], verify_transcript=False)
        self.assertFalse(refused["passed"])
        self.assertIn("试听单", refused["errors"][0])

    def test_audition_lists_effects_and_dialogue(self):
        timing_path = self.epdir / "production/timing_actual.json"
        timing = load_yaml(timing_path)
        first = timing["sentences"][0]
        timing["cue_timeline"] = [{"cue_id": "C01", "start_sec": 65.25, "end_sec": 67.0, "policy": "gap",
                                   "sentence_id": first["id"], "placement": "before", "asset_scope": "project"}]
        write_yaml(self.epdir / "production/sound_cues.yaml", {"cues": [
            {"cue_id": "C01", "description": "木门吱呀打开", "sound_class": "event"}]})
        from bookflow.common import write_json
        write_json(timing_path, timing)
        result = build(self.project, 1)
        self.assertTrue(result["passed"], result)
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("| 1 | 01:05.2–01:07.0 | 事件音 | 木门吱呀打开（C01） | 句前：", text)

    def test_casting_sheet_lists_profile_lines_and_confusable_characters(self):
        final = self.epdir / "final.md"
        atomic_write(final, final.read_text(encoding="utf-8").rstrip("\n")
                     + "\n\n她说：“钥匙留下。”他答：“不行，今天不行。”\n")
        generate(final, None)
        write_yaml(self.project / "analysis/characters.yaml", {"characters": [
            {"id": "P01", "name": "劳拉", "gender": "女", "age_group": "青年", "personality": "冷静", "summary": "女警"},
            {"id": "P02", "name": "阿尔贝托", "gender": "男", "age_group": "中年"},
            {"id": "P03", "name": "莉娜", "gender": "女", "age_group": "青年", "personality": "急躁",
             "summary": "记者"}]})
        write_yaml(self.project / "production/voice_cast.yaml", {"revision": 3, "narrator": {
            "voice_id": "v-n", "status": "confirmed"}, "characters": {
            "P01": {"name": "劳拉", "voice_id": "v-l", "status": "confirmed"}}})
        voice_script.scaffold(self.project, 1)
        path = voice_script.script_path(self.epdir)
        data = load_yaml(path)
        for row, speaker in zip(data["quotes"], ("P01", "P02")):
            row["speaker"] = speaker
        write_yaml(path, data)
        sheet = voice_script.casting_sheet(self.project, [1])
        rows = {row["id"]: row for row in sheet["rows"]}
        self.assertEqual(set(rows), {"P01", "P02"})
        self.assertEqual(rows["P01"]["voice"], "v-l（已确认）")
        self.assertEqual(rows["P02"]["samples"], ["不行，今天不行。"])
        self.assertIn("P02", sheet["warnings"][0])  # personality and summary missing
        text = Path(sheet["path"]).read_text(encoding="utf-8")
        self.assertIn("| P02 | 阿尔贝托 | 男 | 中年 | 未填 | 未填 | 1 | 1 | 未定 | — |", text)
        season = voice_script.season(self.project, [1])
        self.assertEqual(next(row for row in season["needs_voice"] if row["id"] == "P02")["age_group"], "中年")
        # Same episode, gender and age group → flagged as easy to confuse.
        data["quotes"][1]["speaker"] = "P03"
        write_yaml(path, data)
        rows = {row["id"]: row for row in voice_script.casting_sheet(self.project, [1])["rows"]}
        self.assertEqual(rows["P01"]["confusable"], ["P03"])


if __name__ == "__main__":
    unittest.main()
