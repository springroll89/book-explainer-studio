"""Real local mix/subtitles with generated tones; no paid voice or SFX API."""
import json
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from bookflow.adapters.ffmpeg_audio import inputs as mix_inputs
from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.produce import check, run
from bookflow.selftest import run as selftest_run


def _tone(path: Path, seconds: float, frequency: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        frames = (int(6000 * math.sin(2 * math.pi * frequency * index / 48000))
                  for index in range(round(seconds * 48000)))
        stream.writeframes(b"".join(struct.pack("<h", sample) for sample in frames))


def _energy_at(path: Path, second: float, frequency: int = 880) -> float:
    result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-ss", str(second),
                             "-t", "0.2", "-i", str(path), "-ac", "1", "-ar", "48000",
                             "-f", "s16le", "pipe:1"], capture_output=True, check=True)
    samples = struct.unpack(f"<{len(result.stdout) // 2}h", result.stdout)
    return abs(sum(sample * math.sin(2 * math.pi * frequency * index / 48000)
                   for index, sample in enumerate(samples)) / len(samples))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本机 FFmpeg")
class FFmpegAudioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        report = selftest_run(root / "fixture")
        self.assertTrue(report["passed"], report)
        self.project = root / "fixture/projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        self.production = self.epdir / "production"
        self.final = self.epdir / "final.md"
        self.voice = self.production / "voice.wav"
        self.timing = self.production / "timing.json"
        self.cues = self.production / "sound_cues.yaml"
        self.asset = self.production / "sfx/effect.wav"
        _tone(self.voice, 12, 440)
        _tone(self.asset, 0.8, 880)
        timing = load_yaml(self.timing)
        timing["audio_sha256"] = sha256_file(self.voice)
        timing["source"] = "timestamps"
        write_json(self.timing, timing)
        last_id = timing["sentences"][-1]["id"]
        write_yaml(self.cues, {"draft_sha256": sha256_file(self.final), "cues": [{
            "cue_id": "C01-01", "status": "asset_ready", "anchor": {"sentence_id": last_id},
            "placement": "after", "gap_policy": "gap", "duration_sec": 0.8,
            "pre_pad_sec": 0.2, "post_pad_sec": 0.2, "level_db": -6,
            "asset_id": "production/sfx/effect.wav", "asset_sha256": sha256_file(self.asset)}]})
        manifest_path = self.production / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("test_fixture_only")
        manifest["mode"] = "real"
        sources = {
            "cues": [self.final, self.epdir / "final.sentences.json"],
            "voice": [self.final, self.project / "production/voice_cast.yaml"],
            "sfx": [self.cues],
        }
        for stage, paths in sources.items():
            record = manifest["stages"][stage]
            record["inputs"] = [{"path": str(path.relative_to(self.project)),
                                 "sha256": sha256_file(path)} for path in paths]
            record["charge_ids"] = []
            record["outputs"] = [{"path": row["path"],
                                  "sha256": sha256_file(self.epdir / row["path"])}
                                 for row in record["outputs"]]
        manifest["stages"]["sfx"]["outputs"].append(
            {"path": str(self.asset.relative_to(self.epdir)), "sha256": sha256_file(self.asset)})
        write_json(manifest_path, manifest)
        self.assertTrue(check(self.project, 1, until="sfx")["stages"][-1]["ready"])

    def test_mix_and_subtitles_use_real_sfx_and_final_script(self):
        mixed = run(self.project, 1, from_stage="mix")
        self.assertTrue(mixed["passed"], mixed)
        self.assertEqual(mixed["probe"]["sfx_tracks"], 1)
        self.assertTrue(-16 <= mixed["probe"]["integrated_lufs"] <= -14)
        self.assertLessEqual(mixed["probe"]["true_peak_dbtp"], -1)
        actual = load_yaml(self.production / "timing_actual.json")
        original = load_yaml(self.timing)
        self.assertAlmostEqual(actual["sentences"][-1]["endTime"],
                               original["sentences"][-1]["endTime"])
        self.assertAlmostEqual(actual["duration_sec"], 13.2, delta=0.15)
        report = load_yaml(self.production / "_reports/mix_qc.json")
        self.assertAlmostEqual(report["cue_timeline"][0]["start_sec"], 8.2, delta=0.3)
        self.assertGreater(_energy_at(self.production / "final_mix.wav", 8.3),
                           _energy_at(self.production / "final_mix.wav", 7.0) + 100)
        captions = run(self.project, 1, from_stage="subs")
        self.assertTrue(captions["passed"], captions)
        text = (self.production / "subtitles.srt").read_text(encoding="utf-8")
        for row in load_yaml(self.final.with_suffix(".sentences.json"))["sentences"]:
            self.assertIn(row["text"], text)
        self.assertTrue(check(self.project, 1, until="subs")["stages"][-1]["ready"])

    def test_gap_moves_next_sentence_and_bounded_run_reaches_subs(self):
        original = load_yaml(self.timing)["sentences"]
        candidates = [(index, row) for index, row in enumerate(original[:-1]) if row["endTime"] >= 5]
        self.assertTrue(candidates)
        index, anchor = candidates[0]
        sheet = load_yaml(self.cues)
        sheet["cues"][0]["anchor"]["sentence_id"] = anchor["id"]
        write_yaml(self.cues, sheet)
        plan = mix_inputs(self.epdir)
        self.assertAlmostEqual(plan["actual"][index]["endTime"], anchor["endTime"])
        self.assertAlmostEqual(plan["actual"][index + 1]["startTime"],
                               original[index + 1]["startTime"] + 1.2)
        manifest_path = self.production / "manifest.json"
        manifest = load_yaml(manifest_path)
        manifest["stages"]["sfx"]["inputs"][0]["sha256"] = sha256_file(self.cues)
        manifest["stages"]["cues"]["outputs"][0]["sha256"] = sha256_file(self.cues)
        write_json(manifest_path, manifest)
        result = run(self.project, 1, from_stage="mix", until="subs")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["executed"], ["mix", "subs"])
        self.assertTrue(check(self.project, 1, until="subs")["stages"][-1]["ready"])

    def test_duck_and_partial_gap_mix_audio_without_fake_sfx_count(self):
        sheet = load_yaml(self.cues)
        cue = sheet["cues"][0]
        cue.update(gap_policy="duck", pre_pad_sec=0, post_pad_sec=0)
        write_yaml(self.cues, sheet)
        manifest_path = self.production / "manifest.json"
        manifest = load_yaml(manifest_path)
        manifest["stages"]["sfx"]["inputs"][0]["sha256"] = sha256_file(self.cues)
        manifest["stages"]["cues"]["outputs"][0]["sha256"] = sha256_file(self.cues)
        write_json(manifest_path, manifest)
        duck = run(self.project, 1, from_stage="mix")
        self.assertTrue(duck["passed"], duck)
        self.assertEqual(duck["probe"]["sfx_tracks"], 1)
        self.assertAlmostEqual(duck["probe"]["duration_sec"], 12, delta=0.15)
        self.assertGreater(_energy_at(self.production / "final_mix.wav", 8.2), 1)

        cue.update(gap_policy="partial_gap", pre_pad_sec=0.2, post_pad_sec=0.2,
                   peak_window={"start": 0.2, "end": 0.6})
        write_yaml(self.cues, sheet)
        manifest = load_yaml(manifest_path)
        manifest["stages"]["sfx"]["inputs"][0]["sha256"] = sha256_file(self.cues)
        manifest["stages"]["cues"]["outputs"][0]["sha256"] = sha256_file(self.cues)
        write_json(manifest_path, manifest)
        partial = run(self.project, 1, from_stage="mix")
        self.assertTrue(partial["passed"], partial)
        self.assertEqual(partial["probe"]["sfx_tracks"], 1)
        self.assertAlmostEqual(partial["probe"]["duration_sec"], 12.8, delta=0.15)

    def test_selected_accepted_library_asset_is_hash_bound_even_if_cue_is_planned(self):
        from bookflow.media_manifest import real_stage_fresh

        library = self.project.parent.parent / "音效库"
        asset = library / "effect.wav"
        _tone(asset, 0.8, 880)
        index = library / "index.yaml"
        write_yaml(index, [{"id": "SFX-0001", "file": "effect.wav", "sha256": sha256_file(asset),
                            "status": "accepted", "class": "event", "tags": [], "used_in": []}])
        sheet = load_yaml(self.cues)
        sheet["cues"][0].update(asset_id="SFX-0001", status="planned")
        sheet["cues"][0].pop("asset_sha256")
        write_yaml(self.cues, sheet)
        manifest_path = self.production / "manifest.json"
        manifest = load_yaml(manifest_path)
        manifest["stages"]["sfx"]["inputs"][0]["sha256"] = sha256_file(self.cues)
        manifest["stages"]["cues"]["outputs"][0]["sha256"] = sha256_file(self.cues)
        write_json(manifest_path, manifest)
        with (patch("bookflow.sfx_library.location", return_value=library),
              patch("bookflow.adapters.ffmpeg_audio.location", return_value=library)):
            result = run(self.project, 1, from_stage="mix")
            self.assertTrue(result["passed"], result)
            record = load_yaml(manifest_path)["stages"]["mix"]
            self.assertIn("index.yaml", [row["path"] for row in record["inputs"]
                                          if row.get("scope") == "sfx_library"])
            rows = load_yaml(index)
            rows[0]["status"] = "rejected"
            write_yaml(index, rows)
            self.assertFalse(real_stage_fresh(self.epdir, "mix", record))

    def test_opening_sfx_and_estimated_timing_are_rejected(self):
        sheet = load_yaml(self.cues)
        sheet["cues"][0]["anchor"]["sentence_id"] = load_yaml(self.timing)["sentences"][0]["id"]
        sheet["cues"][0]["placement"] = "before"
        write_yaml(self.cues, sheet)
        with self.assertRaisesRegex(ValueError, "开场前5秒"):
            mix_inputs(self.epdir)
        sheet["cues"][0]["anchor"]["sentence_id"] = load_yaml(self.timing)["sentences"][-1]["id"]
        sheet["cues"][0]["placement"] = "after"
        write_yaml(self.cues, sheet)
        timing = load_yaml(self.timing)
        timing["source"] = "estimate"
        write_json(self.timing, timing)
        manifest_path = self.production / "manifest.json"
        manifest = load_yaml(manifest_path)
        for row in manifest["stages"]["voice"]["outputs"]:
            if row["path"] == "production/timing.json":
                row["sha256"] = sha256_file(self.timing)
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "实测句子时间"):
            mix_inputs(self.epdir)

    def test_manual_mix_and_subtitles_are_not_overwritten(self):
        output = self.production / "final_mix.wav"
        output.write_bytes(b"manual mix")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            run(self.project, 1, from_stage="mix")
        self.assertEqual(output.read_bytes(), b"manual mix")
        output.unlink()
        self.assertTrue(run(self.project, 1, from_stage="mix")["passed"])
        captions = self.production / "subtitles.srt"
        captions.write_text("人工修订字幕", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            run(self.project, 1, from_stage="subs")
        self.assertEqual(captions.read_text(encoding="utf-8"), "人工修订字幕")
