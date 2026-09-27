"""Environment and project health checks must be actionable and secret-safe."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.__main__ import new_project, parser
from bookflow.common import write_json, write_yaml
from bookflow.doctor import check
from bookflow.flow import derive


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "projects/book"
        write_yaml(self.project / "project.yaml", {"book": {"title": "测试书"},
                                                   "genre": {"primary": "suspense"}})

    def test_light_check_blocks_invalid_project_before_source_stage(self):
        write_yaml(self.project / "project.yaml", {"book": {"title": ""}})
        report = check(self.project, full=False)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("project.yaml" in error for error in report["errors"]))
        state = derive(self.project)
        self.assertEqual(state["stage"], "建项目")
        self.assertTrue(state["next_actions"])

    def test_malformed_yaml_stays_actionable_in_next(self):
        (self.project / "project.yaml").write_text("book: [\n", encoding="utf-8")
        report = check(self.project, full=False)
        self.assertEqual(report["status"], "error")
        state = derive(self.project)
        self.assertEqual(state["stage"], "建项目")
        self.assertIn("project.yaml", " ".join(state["blockers"]))

    def test_full_check_reports_missing_ffmpeg_without_exposing_key(self):
        with patch("bookflow.doctor.shutil.which", return_value=None), \
             patch.dict(os.environ, {"DOUBAO_API_KEY": "top-secret-canary"}):
            report = check(self.project, full=True)
        self.assertFalse(report["passed"])
        self.assertIn("ffmpeg", " ".join(report["errors"]))
        self.assertIn("ffprobe", " ".join(report["errors"]))
        self.assertIn("安装", " ".join(report["next_actions"]))
        self.assertNotIn("top-secret-canary", json.dumps(report, ensure_ascii=False))

    def test_full_check_detects_incomplete_source_batch(self):
        generation = "a" * 64
        write_json(self.project / "source/current.json", {"generation": generation})
        report = check(self.project, full=True)
        self.assertFalse(report["passed"])
        self.assertIn("原文批次缺少", " ".join(report["errors"]))
        self.assertTrue(any(item["id"] == "source_batch" for item in report["checks"]))

    def test_new_runs_light_doctor_and_cli_exposes_doctor(self):
        args = parser().parse_args(["doctor", str(self.project)])
        self.assertEqual(args.command, "doctor")
        with patch("bookflow.__main__.ROOT", self.root):
            created = new_project("fresh", "新书", "作者", "suspense")
        self.assertTrue(created["doctor"]["passed"])
        project = Path(created["project"])
        self.assertEqual(derive(project)["stage"], "导入原文")
        self.assertEqual(created["state"]["stage"], "导入原文")
        self.assertFalse((project / "status.yaml").exists())
        self.assertEqual(json.loads((project / "state.json").read_text(encoding="utf-8"))["stage"], "导入原文")
        self.assertTrue((project / "进度.md").is_file())


if __name__ == "__main__":
    unittest.main()
