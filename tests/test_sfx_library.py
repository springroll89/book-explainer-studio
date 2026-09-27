"""Isolated shared sound library checks; no paid generation or real approvals."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow import sfx_library, sound
from bookflow.__main__ import parser
from bookflow.common import atomic_write, load_yaml, sha256_file, write_yaml
from bookflow.selftest import run as selftest_run


class SoundLibraryTests(unittest.TestCase):
    def test_accepted_asset_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / "音效库"
            storage = library / "storage"
            storage.mkdir(parents=True)
            audio = storage / "tone.wav"
            audio.write_bytes(b"fixture-audio")
            (library / "ambience").symlink_to(storage, target_is_directory=True)
            write_yaml(library / "index.yaml", [{"id": "SFX-0001", "file": "ambience/tone.wav",
                        "sha256": sha256_file(audio), "status": "accepted", "class": "ambience",
                        "tags": ["雨"], "used_in": []}])
            self.assertIsNone(sfx_library.accepted_asset("SFX-0001", library=library))
            index = library / "index.yaml"
            index.rename(storage / "index.yaml")
            index.symlink_to(storage / "index.yaml")
            with self.assertRaisesRegex(ValueError, "符号链接"):
                sfx_library.search(["雨"], library=library)

    def test_add_requires_real_binding_for_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects/book"
            write_yaml(project / "project.yaml", {"book": {"title": "测试"}})
            source = project / "episodes/ep01/production/sfx.wav"
            # A structurally valid WAV, but no episode confirmation or stage manifest.
            import wave
            source.parent.mkdir(parents=True)
            with wave.open(str(source), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(8000)
                stream.writeframes(b"\0\0" * 800)
            library = root / "音效库"
            with self.assertRaisesRegex(ValueError, "确认"):
                sfx_library.add(project, 1, source, desc="雨声", tags=["雨"],
                                sound_class="ambience", status="accepted", library=library)
            self.assertFalse((library / "index.yaml").exists())

    def test_fixture_reuse_estimate_and_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = selftest_run(root)
            self.assertTrue(result["passed"], result)
            project = root / "projects/selftest-fixture"
            epdir = project / "episodes/ep01"
            library = root / "音效库"
            source = epdir / "production/sfx.wav"
            added = sfx_library.add(project, "ep01", source, desc="雨打窗户", tags=["雨", "窗户"],
                                    sound_class="ambience", status="accepted", cost_cny=2.0,
                                    library=library)
            self.assertEqual(added["id"], "SFX-0001")
            self.assertEqual(len(sfx_library.search(["雨"], library=library)["items"]), 1)
            duplicate = sfx_library.add(project, 1, source, desc="重复", tags=["雨"],
                                        sound_class="ambience", status="accepted", library=library)
            self.assertEqual(duplicate["status"], "warning")
            self.assertEqual(len(load_yaml(library / "index.yaml")), 1)

            # Only an explicit accepted ID changes the cost count; a suggestion does not.
            sheet = epdir / "production/sound_cues.yaml"
            data = load_yaml(sheet)
            data["cues"] = [{"cue_id": "C01", "description": "雨打窗户", "sound_class": "ambience",
                             "function": "bed", "duration_sec": 60, "status": "planned"}]
            write_yaml(sheet, data)
            with patch.object(sfx_library, "location", return_value=library):
                estimate = sound.estimate(epdir)
                self.assertTrue((epdir / "production/_reports/sound_estimate.json").is_file())
                self.assertFalse((epdir / "production/sound_estimate.json").exists())
                self.assertEqual(estimate["library_candidates"]["C01"], ["SFX-0001"])
                self.assertEqual(estimate["new_assets"], 1)
                data["cues"][0]["asset_id"] = "SFX-0001"
                write_yaml(sheet, data)
                estimate = sound.estimate(epdir)
                self.assertEqual(estimate["new_assets"], 0)
                self.assertEqual(estimate["reusable_assets"], 1)

                asset = library / load_yaml(library / "index.yaml")[0]["file"]
                atomic_write(asset, "changed")
                self.assertEqual(sfx_library.search(["雨"], library=library)["items"], [])
                with self.assertRaisesRegex(ValueError, "哈希失效"):
                    sound.estimate(epdir)
            self.assertEqual(sfx_library.stats(library=library)["by_status"]["accepted"], 1)

    def test_cli_parses_library_commands(self):
        cli = parser()
        self.assertEqual(cli.parse_args(["sfx", "search", "雨", "窗户"]).terms, ["雨", "窗户"])
        self.assertEqual(cli.parse_args(["sfx", "stats"]).action, "stats")


if __name__ == "__main__":
    unittest.main()
