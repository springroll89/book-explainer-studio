import tempfile
import unittest
from pathlib import Path

from bookflow.sound import default_gap_policy, generate
from bookflow.common import atomic_write, write_yaml


class SoundTests(unittest.TestCase):
    def test_default_gap_policy(self):
        self.assertEqual(default_gap_policy("ambience", "bed"), "duck")
        self.assertEqual(default_gap_policy("event", "story_event"), "gap")
        self.assertEqual(default_gap_policy("process", "reveal"), "partial_gap")
        self.assertEqual(default_gap_policy("design", "mood"), "duck")

    def test_generate_is_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            ep = Path(tmp) / "ep01"
            (ep / "production").mkdir(parents=True)
            atomic_write(ep / "draft_v1.md", "---\nepisode: 1\n---\n一句话。\n")
            write_yaml(ep / "production/sound_cues.yaml", {"episode": 1, "cues": []})
            result = generate(ep)
            self.assertFalse(result["executed"])
            self.assertTrue(result["requires_user_confirmation"])


if __name__ == "__main__":
    unittest.main()
