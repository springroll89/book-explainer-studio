"""The shared rule index must point to one existing owner per theme."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs/RULES_INDEX.md"


class RulesIndexTests(unittest.TestCase):
    def test_every_theme_has_one_existing_repository_destination(self):
        text = INDEX.read_text(encoding="utf-8")
        rows = re.findall(r"^\| ([^|]+) \| \[([^]]+)\]\(([^)]+)\) \|$", text, re.M)
        table_lines = [line for line in text.splitlines()
                       if line.startswith("| ") and not line.startswith(("| 主题", "|---"))]
        self.assertEqual(len(rows), len(table_lines))
        self.assertGreaterEqual(len(rows), 10)
        themes = [theme for theme, _, _ in rows]
        self.assertEqual(len(themes), len(set(themes)))
        for _, _, target in rows:
            destination = (INDEX.parent / target).resolve()
            self.assertTrue(destination.is_relative_to(ROOT), target)
            self.assertTrue(destination.is_file(), target)

    def test_index_does_not_duplicate_visual_thresholds(self):
        text = INDEX.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\d+\s*[–-]\s*\d+\s*(?:秒|个镜头|张)")
        self.assertNotIn("短段保留", (ROOT / "docs/WORKFLOW.md").read_text(encoding="utf-8"))
        self.assertNotIn("短段并记录理由", (ROOT / "docs/书籍精讲工作室说明书.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
