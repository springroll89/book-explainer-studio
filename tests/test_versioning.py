"""Studio version tracking must not fabricate legacy creation history."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow import __version__
from bookflow.__main__ import new_project
from bookflow.common import load_yaml, write_yaml
from bookflow.doctor import check as doctor_check
from bookflow.flow import write as next_write


class VersioningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_new_project_records_creation_and_current_version(self):
        project = Path(new_project("test-book", "测试书", "作者", "suspense", root=self.root)["project"])
        config = load_yaml(project / "project.yaml")
        self.assertEqual(config["studio_created_version"], __version__)
        self.assertEqual(config["studio_version"], __version__)

    def test_next_updates_only_current_version_and_preserves_yaml_comment(self):
        project = Path(new_project("test-book", "测试书", "作者", "suspense", root=self.root)["project"])
        config_path = project / "project.yaml"
        config_path.write_text(config_path.read_text(encoding="utf-8") + "# 用户手写说明\n", encoding="utf-8")
        with patch("bookflow.versioning.__version__", "9.9.9"):
            next_write(project)
            first = config_path.read_bytes()
            next_write(project)
            self.assertEqual(config_path.read_bytes(), first)
        config = load_yaml(config_path)
        self.assertEqual(config["studio_created_version"], __version__)
        self.assertEqual(config["studio_version"], "9.9.9")
        self.assertIn("# 用户手写说明", config_path.read_text(encoding="utf-8"))

    def test_legacy_project_keeps_creation_version_unknown(self):
        project = self.root / "projects/legacy"
        write_yaml(project / "project.yaml", {"book": {"title": "旧书"}})
        self.assertTrue(any(row["id"] == "studio_version" and row["status"] == "warning"
                            for row in doctor_check(project)["checks"]))
        with patch("bookflow.versioning.__version__", "9.9.9"):
            next_write(project)
        config = load_yaml(project / "project.yaml")
        self.assertEqual(config["studio_version"], "9.9.9")
        self.assertNotIn("studio_created_version", config)

    def test_invalid_yaml_is_not_rewritten_by_next(self):
        project = self.root / "projects/broken"
        config_path = project / "project.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("book: [\n", encoding="utf-8")
        before = config_path.read_bytes()
        next_write(project)
        self.assertEqual(config_path.read_bytes(), before)

    def test_version_update_keeps_crlf_and_other_fields(self):
        project = self.root / "projects/crlf"
        config_path = project / "project.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_bytes(b'book:\r\n  title: test\r\nstudio_version: "0.1.0"\r\n# note\r\n')
        with patch("bookflow.versioning.__version__", "9.9.9"):
            next_write(project)
        content = config_path.read_bytes()
        self.assertIn(b'studio_version: "9.9.9"\r\n# note\r\n', content)
        self.assertNotIn(b'\n# note\n', content)


if __name__ == "__main__":
    unittest.main()
