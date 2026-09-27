"""The test-mode pipeline must resume by evidence and never touch real books."""
import json
import tempfile
import unittest
from pathlib import Path

from bookflow.__main__ import dispatch, parser
from bookflow.approvals import record_confirmation
from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.flow import _media_ready, derive
from bookflow.produce import STAGES, check, run
from bookflow.selftest import run as selftest_run


class ProduceTests(unittest.TestCase):
    def fixture(self, root):
        report = selftest_run(root)
        self.assertTrue(report["passed"], report)
        return root / "projects/selftest-fixture"

    def test_rerun_skips_unchanged_stages_and_repairs_changed_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            episode = project / "episodes/ep01"
            video = episode / "production/final.mp4"
            baseline_hash = sha256_file(video)
            baseline_mtime = video.stat().st_mtime_ns
            repeated = run(project, "ep01", test_mode=True, until="render")
            self.assertTrue(repeated["passed"], repeated)
            self.assertEqual(repeated["executed"], [])
            self.assertEqual(repeated["skipped"], list(STAGES))
            self.assertEqual(video.stat().st_mtime_ns, baseline_mtime)
            voice = episode / "production/voice.wav"
            voice.write_bytes(b"corrupted fixture output")
            status = check(project, "ep01")
            self.assertFalse(status["stages"][1]["ready"])
            self.assertEqual(derive(project)["stage"], "声音")
            repaired = run(project, "ep01", test_mode=True)
            self.assertEqual(repaired["executed"], ["voice"])
            self.assertEqual(sha256_file(video), baseline_hash)
            self.assertEqual(video.stat().st_mtime_ns, baseline_mtime)
            self.assertTrue(all(row["ready"] for row in check(project, "ep01")["stages"]))

    def test_input_hash_change_invalidates_voice_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            cast_path = project / "production/voice_cast.yaml"
            from bookflow.common import load_yaml
            cast = load_yaml(cast_path)
            cast["version"] = 2
            write_yaml(cast_path, cast)
            rows = check(project, "ep01")["stages"]
            self.assertFalse(rows[1]["ready"])
            result = run(project, "ep01", test_mode=True)
            self.assertEqual(result["executed"], ["voice"])
            config_path = project / "project.yaml"
            config = load_yaml(config_path)
            config.setdefault("sound_design", {})["budget_per_episode"] = 30
            write_yaml(config_path, config)
            self.assertTrue(check(project, "ep01")["stages"][1]["ready"])

    def test_two_sentence_edit_regenerates_only_its_paragraph_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            final = project / "episodes/ep01/final.md"
            original = final.read_text(encoding="utf-8")
            first = original.replace("五点五十二。〔据", "五点五十二。窗外还没有亮。〔据", 1)
            self.assertNotEqual(first, original)
            final.write_text(first, encoding="utf-8")
            self.assertTrue(record_confirmation(project, "script", "拍板文案", [1],
                                                session="selftest-fixture", verify_transcript=False)["passed"])
            self.assertTrue(run(project, "ep01", test_mode=True, until="voice")["passed"])
            manifest_path = project / "episodes/ep01/production/voice_segments.json"
            before = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"]
            self.assertEqual(len(before), 2)
            second = first.replace("五点四十，", "五点四十一，", 1).replace("窗外还没有亮。", "窗外依旧没有亮。", 1)
            final.write_text(second, encoding="utf-8")
            self.assertTrue(record_confirmation(project, "script", "拍板文案", [1],
                                                session="selftest-fixture", verify_transcript=False)["passed"])
            rerun = run(project, "ep01", test_mode=True, until="voice")
            self.assertEqual(rerun["executed"], ["cues", "voice"])
            after = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"]
            self.assertNotEqual(after[0]["text_sha256"], before[0]["text_sha256"])
            self.assertNotEqual(after[0]["generated_at"], before[0]["generated_at"])
            self.assertEqual(after[1:], before[1:])
            self.assertTrue(check(project, "ep01", until="voice")["stages"][-1]["ready"])
            downstream = run(project, "ep01", test_mode=True, until="storyboard")
            self.assertEqual(downstream["executed"], ["mix", "subs", "storyboard"])

    def test_voice_cast_change_regenerates_all_paragraphs(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            manifest_path = project / "episodes/ep01/production/voice_segments.json"
            before = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"]
            cast_path = project / "production/voice_cast.yaml"
            from bookflow.common import load_yaml
            cast = load_yaml(cast_path)
            cast["version"] = 2
            write_yaml(cast_path, cast)
            self.assertTrue(run(project, "ep01", test_mode=True, until="voice")["passed"])
            after = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"]
            self.assertEqual(len(after), len(before))
            self.assertTrue(all(new["voice_cast_sha256"] != old["voice_cast_sha256"]
                                for old, new in zip(before, after)))

    def test_test_pipeline_uses_configured_voice_cache_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            config_path = project / "project.yaml"
            config = load_yaml(config_path)
            config.setdefault("voice_production", {}).update(
                segments="production/cache/paragraphs.json",
                parts_dir="production/cache/parts")
            write_yaml(config_path, config)

            result = run(project, "ep01", test_mode=True, from_stage="cues", until="voice")

            self.assertTrue(result["passed"], result)
            episode = project / "episodes/ep01"
            self.assertTrue((episode / "production/cache/paragraphs.json").is_file())
            self.assertTrue(list((episode / "production/cache/parts").glob("*.wav")))
            self.assertTrue(check(project, "ep01", until="voice")["stages"][-1]["ready"])

    def test_corrupted_paragraph_cache_is_detected_and_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            production = project / "episodes/ep01/production"
            manifest_path = production / "voice_segments.json"
            before = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"]
            (production / "voice_parts/para-0001.wav").write_bytes(b"corrupted fixture segment")
            self.assertFalse(check(project, "ep01", until="voice")["stages"][-1]["ready"])
            result = run(project, "ep01", test_mode=True, until="voice")
            self.assertEqual(result["executed"], ["voice"])
            after = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"]
            self.assertNotEqual(after[0]["generated_at"], before[0]["generated_at"])
            self.assertEqual(after[1:], before[1:])
            self.assertTrue(check(project, "ep01", until="voice")["stages"][-1]["ready"])

    def test_from_voice_forces_bounded_downstream_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            result = run(project, "ep01", test_mode=True, from_stage="voice", until="mix")
            self.assertTrue(result["passed"], result)
            self.assertEqual(result["executed"], ["voice", "sfx", "mix"])

    def test_real_project_cannot_turn_on_test_mode_with_a_config_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "projects/book"
            write_yaml(project / "project.yaml", {"book": {"title": "测试书"},
                                                  "approvals": {"test_fixture_only": True}})
            final = project / "episodes/ep01/final.md"
            final.parent.mkdir(parents=True)
            final.write_text("测试稿\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "隔离示范项目"):
                run(project, "ep01", test_mode=True)
            self.assertFalse((final.parent / "production").exists())
            write_json(final.parent / "production/manifest.json", {"mode": "test",
                       "test_fixture_only": True, "stages": {"voice": {"status": "done"}}})
            self.assertFalse(_media_ready(project, 1, "audio"))
            self.assertFalse(check(project, 1, until="voice")["stages"][-1]["ready"])

    def test_check_is_read_only_and_cli_accepts_required_forms(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "projects/book"
            write_yaml(project / "project.yaml", {"book": {"title": "测试书"}})
            final = project / "episodes/ep01/final.md"
            final.parent.mkdir(parents=True)
            final.write_text("测试稿\n", encoding="utf-8")
            result = check(project, "ep01")
            self.assertEqual(result["status"], "warning")
            self.assertFalse((final.parent / "production").exists())
            args = parser().parse_args(["produce", str(project), "ep01", "--test-mode", "--until", "mix"])
            self.assertEqual((args.command, args.until), ("produce", "mix"))
            args = parser().parse_args(["produce", "check", str(project), "ep01"])
            self.assertEqual((args.project, args.check_episode), ("check", "ep01"))
            self.assertEqual(dispatch(args)["status"], "warning")

    def test_real_manifest_needs_verified_snapshots_not_done_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            epdir = project / "episodes/ep01"
            manifest_path = epdir / "production/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("test_fixture_only")
            manifest["mode"] = "real"
            write_json(manifest_path, manifest)
            self.assertFalse(_media_ready(project, 1, "audio"))
            final = epdir / "final.md"
            cast = project / "production/voice_cast.yaml"
            cues = epdir / "production/sound_cues.yaml"
            inputs = {"cues": (final, epdir / "final.sentences.json"), "voice": (final, cast), "sfx": (cues,),
                      "mix": (epdir / "production/voice.wav", epdir / "production/sfx.wav"),
                      "subs": (epdir / "production/final_mix.wav", epdir / "production/timing_actual.json"),
                      "storyboard": (epdir / "production/timing_actual.json",),
                      "images": (epdir / "production/storyboard.yaml",),
                      "render": (epdir / "production/final_mix.wav", epdir / "production/subtitles.srt",
                                 epdir / "production/placeholder.png")}
            for stage, record in manifest["stages"].items():
                paths = inputs[stage]
                record["inputs"] = [{"path": str(path.relative_to(project)), "sha256": sha256_file(path)}
                                    for path in paths]
            write_json(manifest_path, manifest)
            self.assertTrue(_media_ready(project, 1, "audio"))
            self.assertTrue(_media_ready(project, 1, "visual"))
            manifest["stages"]["mix"]["inputs"] = [
                {"path": str(final.relative_to(project)), "sha256": sha256_file(final)}]
            write_json(manifest_path, manifest)
            self.assertEqual({row["stage"]: row for row in check(project, 1)["stages"]}["mix"]["reason"],
                             "unlinked_inputs")
            manifest["stages"]["mix"]["inputs"] = [
                {"path": str(path.relative_to(project)), "sha256": sha256_file(path)}
                for path in inputs["mix"]]
            write_json(manifest_path, manifest)
            final.write_text(final.read_text(encoding="utf-8") + "\n变化", encoding="utf-8")
            rows = {row["stage"]: row for row in check(project, 1)["stages"]}
            self.assertFalse(rows["voice"]["ready"])
            self.assertEqual(rows["mix"]["reason"], "upstream_changed")
            self.assertFalse(_media_ready(project, 1, "visual"))

    def test_real_manifest_rejects_broken_output_and_accepts_archived_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.fixture(Path(temporary))
            epdir = project / "episodes/ep01"
            final = epdir / "final.md"
            voice = epdir / "production/voice.wav"
            cast = project / "production/voice_cast.yaml"
            from bookflow.media_manifest import real_stage_fresh
            record = {"status": "done", "inputs": [
                {"path": str(path.relative_to(project)), "sha256": sha256_file(path)}
                for path in (final, cast)],
                "outputs": [{"path": "production/voice.wav", "sha256": sha256_file(voice)},
                            {"path": "production/timing.json",
                             "sha256": sha256_file(epdir / "production/timing.json")}]}
            self.assertTrue(real_stage_fresh(epdir, "voice", record))
            record["outputs"][0]["sha256"] = "0" * 64
            self.assertFalse(real_stage_fresh(epdir, "voice", record))
            record["outputs"][0]["sha256"] = sha256_file(voice)
            record["outputs"][0]["path"] = "../ep01/production/voice.wav"
            self.assertFalse(real_stage_fresh(epdir, "voice", record))
            record["outputs"][0]["path"] = "production/voice.wav"
            from bookflow.archive import _title
            cloud_root = Path(temporary) / "fake-cloud"
            cloud = cloud_root / "书籍讲解" / _title(project) / "ep01/production/voice.wav"
            cloud.parent.mkdir(parents=True)
            cloud.write_bytes(voice.read_bytes())
            voice.unlink()
            write_yaml(project / "archive/ep01.yaml", {"project": str(project.resolve()), "episode": 1,
                                                       "archive_root": str(cloud_root),
                                                       "status": "complete", "files": [{
                "source": "episodes/ep01/production/voice.wav", "target": str(cloud),
                "sha256": record["outputs"][0]["sha256"], "size": cloud.stat().st_size,
                "removed": True}]})
            from bookflow.archive import archived_digest
            self.assertEqual(archived_digest(project, "episodes/ep01/production/voice.wav"),
                             record["outputs"][0]["sha256"])
            self.assertTrue(real_stage_fresh(epdir, "voice", record))


if __name__ == "__main__":
    unittest.main()
