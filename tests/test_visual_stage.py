"""Agent-made storyboards and images are validated, then recorded for local rendering."""
import tempfile
import unittest
from pathlib import Path

from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.media_manifest import real_stage_fresh
from bookflow.selftest import run as selftest_run
from bookflow.visual_stage import bind_existing


def row(root: Path, path: Path) -> dict:
    return {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}


class VisualStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.assertTrue(selftest_run(root)["passed"])
        self.project = root / "projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        self.production = self.epdir / "production"
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config.setdefault("render", {})["title_card"] = {"required": False, "manifest": "production/title_card.yaml"}
        config.setdefault("visual_pacing", {}).setdefault("opening", {})["min_hard_changes"] = 0
        write_yaml(config_path, config)
        final, mix = self.epdir / "final.md", self.production / "final_mix.wav"
        write_json(self.production / "manifest.json", {"mode": "real", "charges": [], "stages": {
            "mix": {"status": "done", "inputs": [row(self.project, final)],
                    "outputs": [row(self.epdir, mix), row(self.epdir, self.production / "timing_actual.json")]},
            "subs": {"status": "done", "inputs": [row(self.project, final), row(self.project, mix)],
                     "outputs": [row(self.epdir, self.production / "subtitles.srt")]}}})

    def test_binds_storyboard_and_images_linked_to_current_mix(self):
        result = bind_existing(self.project, self.epdir)
        self.assertTrue(result["passed"], result)
        stages = load_yaml(self.production / "manifest.json")["stages"]
        self.assertTrue(real_stage_fresh(self.epdir, "storyboard", stages["storyboard"]))
        self.assertTrue(real_stage_fresh(self.epdir, "images", stages["images"]))
        self.assertIn(row(self.project, self.production / "final_mix.wav"), stages["storyboard"]["inputs"])
        self.assertEqual(bind_existing(self.project, self.epdir)["skipped"], ["storyboard", "images"])

    def test_pacing_failure_records_nothing(self):
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config["visual_pacing"]["opening"]["min_hard_changes"] = 6
        write_yaml(config_path, config)
        result = bind_existing(self.project, self.epdir)
        self.assertFalse(result["passed"])
        self.assertIn("节奏", result["summary"])
        self.assertNotIn("storyboard", load_yaml(self.production / "manifest.json")["stages"])

    def test_missing_image_is_reported_not_raised(self):
        (self.production / "placeholder.png").unlink()
        result = bind_existing(self.project, self.epdir)
        self.assertFalse(result["passed"])
        self.assertIn("图片", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
