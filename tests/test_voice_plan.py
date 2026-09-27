"""Real-voice planning is read-only and never mistakes a bad part for paid cache."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from bookflow.common import sha256_file, write_json, write_yaml
from bookflow.sentences import generate
from bookflow.voice_plan import plan


class VoicePlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "book"
        self.epdir = self.project / "episodes/ep01"
        self.epdir.mkdir(parents=True)
        self.final = self.epdir / "final.md"
        self.final.write_text("第一句。第二句。\n\n第三句。第四句。\n", encoding="utf-8")
        generate(self.final)
        write_yaml(self.project / "project.yaml", {"producers": {"tts": "doubao_tts"}})
        self.cast = self.project / "production/voice_cast.yaml"
        write_yaml(self.cast, {"revision": 1, "narrator": {"voice_id": "test-voice", "status": "confirmed"}})

    def _cache(self):
        initial = plan(self.epdir)
        parts = self.epdir / "production/voice_parts"
        parts.mkdir(parents=True)
        rows = []
        for paragraph in initial["paragraphs"]:
            audio = parts / paragraph["path"]
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                            "-t", "1", "-y", str(audio)], check=True, capture_output=True)
            measured = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "default=nw=1:nk=1", str(audio)],
                                      check=True, capture_output=True)
            duration = float(measured.stdout.decode().strip())
            count = len(paragraph["sentences"])
            timing = [{**sentence, "startTime": round(i * duration / count, 3),
                       "endTime": round((i + 1) * duration / count, 3)}
                      for i, sentence in enumerate(paragraph["sentences"])]
            rows.append({"path": paragraph["path"], "status": "done", "sha256": sha256_file(audio),
                         "duration_sec": duration, "sentences": timing,
                         "charge_id": f"voice-{paragraph['index']}",
                         "text_sha256": paragraph["text_sha256"],
                         "voice_cast_sha256": initial["cast_sha256"],
                         "config_sha256": initial["config_sha256"]})
        write_json(self.epdir / "production/voice_segments.json", {"mode": "real", "segments": rows})
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "stages": {}, "charges": [
            {"id": row["charge_id"], "stage": "voice", "cost_cny": 0.01} for row in rows]})
        return rows

    def test_two_sentence_edit_only_reserves_affected_paragraph(self):
        rows = self._cache()
        self.assertEqual(plan(self.epdir)["new_chars"], 0)
        self.final.write_text("第一句改了。第二句也改了。\n\n第三句。第四句。\n", encoding="utf-8")
        generate(self.final, self.final, write_mapping=False)
        revised = plan(self.epdir)
        self.assertEqual([row["cached"] for row in revised["paragraphs"]], [False, True])
        self.assertEqual(revised["new_chars"], len(revised["paragraphs"][0]["text"]))
        self.assertEqual(rows[1]["sha256"], revised["paragraphs"][1]["cached_record"]["sha256"])

    def test_changed_cast_or_audio_invalidates_cache(self):
        rows = self._cache()
        (self.epdir / "production/voice_parts" / rows[0]["path"]).write_bytes(b"not an mp3")
        self.assertEqual([row["cached"] for row in plan(self.epdir)["paragraphs"]], [False, True])
        write_yaml(self.cast, {"revision": 2, "narrator": {"voice_id": "test-voice", "status": "confirmed"}})
        self.assertEqual(plan(self.epdir)["reused_paragraphs"], 0)

    def test_fixture_or_malformed_cache_never_counts_as_real(self):
        self._cache()
        manifest = self.epdir / "production/voice_segments.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["mode"] = "test"
        write_json(manifest, data)
        self.assertEqual(plan(self.epdir)["reused_paragraphs"], 0)
        data["mode"] = "real"
        data["segments"].append(data["segments"][0])
        write_json(manifest, data)
        with self.assertRaisesRegex(ValueError, "重复"):
            plan(self.epdir)

    def test_cache_without_charge_or_with_changed_config_is_not_reused(self):
        rows = self._cache()
        manifest = self.epdir / "production/manifest.json"
        write_json(manifest, {"mode": "real", "stages": {}, "charges": []})
        self.assertEqual(plan(self.epdir)["reused_paragraphs"], 0)
        write_json(manifest, {"mode": "real", "stages": {}, "charges": [
            {"id": row["charge_id"], "stage": "voice", "cost_cny": 0.01} for row in rows]})
        project_config = self.project / "project.yaml"
        write_yaml(project_config, {"producers": {"tts": "doubao_tts"},
                                    "sound_design": {"narration": {"speed": 5}}})
        self.assertEqual(plan(self.epdir)["reused_paragraphs"], 0)

    def test_versioned_part_preserves_and_reuses_previous_script(self):
        original_rows = self._cache()
        original = self.final.read_text(encoding="utf-8")
        self.final.write_text("第一句改了。第二句也改了。\n\n第三句。第四句。\n", encoding="utf-8")
        generate(self.final, self.final, write_mapping=False)
        changed = plan(self.epdir)["paragraphs"][0]
        source = self.epdir / "production/voice_parts" / original_rows[0]["path"]
        versioned = "para-0001-" + "a" * 16 + ".mp3"
        target = source.parent / versioned
        target.write_bytes(source.read_bytes())
        revised = {**original_rows[0], "path": versioned, "sha256": sha256_file(target),
                   "text_sha256": changed["text_sha256"], "sentences": [
                       {**sentence, "startTime": i * 0.4, "endTime": (i + 1) * 0.4}
                       for i, sentence in enumerate(changed["sentences"])], "charge_id": "voice-new"}
        parts_manifest = self.epdir / "production/voice_segments.json"
        write_json(parts_manifest, {"mode": "real", "segments": [*original_rows, revised]})
        write_json(self.epdir / "production/manifest.json", {"mode": "real", "stages": {}, "charges": [
            {"id": row["charge_id"], "stage": "voice", "cost_cny": 0.01} for row in original_rows] + [
            {"id": "voice-new", "stage": "voice", "cost_cny": 0.01}]})
        current = plan(self.epdir)
        self.assertEqual(current["paragraphs"][0]["cached_record"]["path"], versioned)
        self.final.write_text(original, encoding="utf-8")
        generate(self.final, self.final, write_mapping=False)
        self.assertEqual(plan(self.epdir)["paragraphs"][0]["cached_record"]["path"], original_rows[0]["path"])
        self.assertTrue(source.is_file())
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
