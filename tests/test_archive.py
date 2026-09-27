"""Archive safety checks use only a fresh fixture and a fake cloud directory."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow import archive
from bookflow.approvals import confirmation_state
from bookflow.common import atomic_write, load_yaml, sha256_file, write_yaml
from bookflow.flow import derive
from bookflow.selftest import run as selftest_run


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        result = selftest_run(self.root / "studio")
        self.assertTrue(result["passed"], result)
        self.project = self.root / "studio/projects/selftest-fixture"
        self.epdir = self.project / "episodes/ep01"
        self.cloud = self.root / "fake-cloud"
        self.cloud.mkdir()
        atomic_write(self.epdir / "deliver/index.html", "isolated export fixture")

    def test_upload_pending_preserves_originals_and_restore_checks_hash(self):
        video = self.epdir / "production/final.mp4"
        before = sha256_file(video)
        first = archive.run(self.project, "ep01", archive_root=self.cloud,
                            upload_check=lambda path: False)
        self.assertEqual(first["status"], "awaiting_upload")
        self.assertEqual(derive(self.project)["stage"], "归档")
        self.assertTrue(video.is_file())
        manifest = load_yaml(self.project / "archive/ep01.yaml")
        self.assertGreater(len(manifest["files"]), 2)
        self.assertFalse(any(row["source"].endswith("sfx.wav") for row in manifest["files"]))
        self.assertTrue(all(Path(row["target"]).is_file() for row in manifest["files"]))

        second = archive.run(self.project, 1, archive_root=self.cloud,
                             upload_check=lambda path: True, eviction_check=lambda path: False)
        self.assertEqual(second["status"], "awaiting_eviction")
        self.assertEqual(second["bytes_freed"], 0)
        self.assertTrue(video.exists())
        self.assertEqual(load_yaml(self.project / "archive/ep01.yaml")["status"],
                         "uploaded_verified_awaiting_eviction")
        self.assertEqual(confirmation_state(self.project, "sample", 1)["state"], "passed")
        self.assertEqual(confirmation_state(self.project, "release", 1)["state"], "passed")

        real_sha256 = sha256_file

        def no_cloud_read(path):
            if Path(path).is_relative_to(self.cloud):
                raise AssertionError("online-only file was read")
            return real_sha256(path)

        with patch("bookflow.archive.sha256_file", side_effect=no_cloud_read):
            third = archive.run(self.project, "ep01", archive_root=self.cloud,
                                upload_check=lambda path: True, eviction_check=lambda path: True)
        self.assertEqual(third["status"], "success")
        self.assertFalse(video.exists())
        self.assertEqual(derive(self.project)["stage"], "归档")
        self.assertIn("无待办", derive(self.project)["next_step"])
        self.assertEqual(third["bytes_freed"], sum(row["size"] for row in manifest["files"]))
        restored = archive.restore(self.project, 1, only="video")
        self.assertIn(str(video.resolve()), restored["restored"])
        self.assertEqual(sha256_file(video), before)

    def test_cloud_copy_tamper_stops_before_removal(self):
        archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda path: False)
        manifest = load_yaml(self.project / "archive/ep01.yaml")
        target = Path(manifest["files"][0]["target"])
        atomic_write(target, "tampered")
        with self.assertRaisesRegex(ValueError, "哈希"):
            archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda path: True)
        self.assertTrue((self.epdir / "production/final.mp4").is_file())
        self.assertTrue(all((self.project / row["source"]).is_file() for row in manifest["files"]))

    def test_restore_never_overwrites_conflicting_local_file(self):
        archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda path: False)
        archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda path: True,
                    eviction_check=lambda path: True)
        video = self.epdir / "production/final.mp4"
        atomic_write(video, "local change")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            archive.restore(self.project, 1, only="video")

    def test_reusable_cue_asset_and_text_remain_local(self):
        retained = self.epdir / "production/audio/door.wav"
        atomic_write(retained, "fixture effect")
        title_card = self.epdir / "production/images/title_card.png"
        atomic_write(title_card, "fixture title")
        report_frame = self.epdir / "production/_reports/subtitle_frame.png"
        atomic_write(report_frame, "fixture report")
        atomic_write(self.epdir / "production/subtitles.srt", "subtitles")
        sheet = self.epdir / "production/sound_cues.yaml"
        data = load_yaml(sheet)
        data["cues"] = [{"cue_id": "C01", "asset_id": "production/audio/door.wav"}]
        write_yaml(sheet, data)
        selected = archive._selected(self.epdir)
        self.assertNotIn(retained, selected)
        self.assertNotIn(title_card, selected)
        self.assertNotIn(report_frame, selected)
        self.assertNotIn(self.epdir / "production/subtitles.srt", selected)

    def test_interrupted_removal_resumes_from_manifest(self):
        archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda path: False)
        path = self.project / "archive/ep01.yaml"
        data = load_yaml(path)
        first = data["files"][0]
        data["status"] = "evicted_awaiting_removal"
        first["removal_pending"] = True
        (self.project / first["source"]).unlink()
        write_yaml(path, data)
        self.assertEqual(confirmation_state(self.project, "release", 1)["state"], "passed")
        resumed = archive.run(self.project, 1, archive_root=self.cloud,
                              upload_check=lambda path: True, eviction_check=lambda path: False)
        self.assertEqual(resumed["status"], "awaiting_eviction")
        self.assertTrue(any((self.project / row["source"]).exists() for row in data["files"][1:]))
        resumed = archive.run(self.project, 1, archive_root=self.cloud,
                              upload_check=lambda path: True, eviction_check=lambda path: True)
        self.assertEqual(resumed["status"], "success")
        self.assertTrue(all(row["removed"] for row in load_yaml(path)["files"]))

    def test_completed_archive_never_rehydrates_cloud_copy_during_status_check(self):
        archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda path: True,
                    eviction_check=lambda path: True)
        manifest = load_yaml(self.project / "archive/ep01.yaml")
        row = next(item for item in manifest["files"] if item["source"].endswith("final.mp4"))
        real_sha256 = sha256_file

        def no_cloud_read(path):
            if Path(path).is_relative_to(self.cloud):
                raise AssertionError("online-only file was read")
            return real_sha256(path)

        with patch("bookflow.archive.sha256_file", side_effect=no_cloud_read):
            self.assertEqual(archive.archived_digest(self.project, row["source"]), row["sha256"])
            self.assertEqual(archive.run(self.project, 1, archive_root=self.cloud)["status"], "success")
        Path(row["target"]).unlink()
        self.assertIsNone(archive.archived_digest(self.project, row["source"]))
        self.assertEqual(confirmation_state(self.project, "release", 1)["state"], "invalidated")

    def test_archived_digest_rejects_target_outside_episode_archive(self):
        archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda path: True,
                    eviction_check=lambda path: True)
        path = self.project / "archive/ep01.yaml"
        data = load_yaml(path)
        row = next(item for item in data["files"] if item["source"].endswith("final.mp4"))
        outside = self.cloud / "other-video.mp4"
        outside.write_bytes(Path(row["target"]).read_bytes())
        row["target"] = str(outside)
        write_yaml(path, data)
        self.assertIsNone(archive.archived_digest(self.project, row["source"]))
        self.assertEqual(confirmation_state(self.project, "release", 1)["state"], "invalidated")
        with self.assertRaisesRegex(ValueError, "归档源文件"):
            archive.restore(self.project, 1, only="video")

    def test_next_rechecks_every_completed_archive_row_without_hydrating_cloud(self):
        archive.run(self.project, 1, archive_root=self.cloud, upload_check=lambda _: True,
                    eviction_check=lambda _: True)
        path = self.project / "archive/ep01.yaml"
        data = load_yaml(path)
        row = next(item for item in data["files"] if item["source"].endswith("production/voice.wav"))
        Path(row["target"]).unlink()
        real_sha256 = sha256_file

        def no_cloud_read(location):
            if Path(location).is_relative_to(self.cloud):
                raise AssertionError("online-only file was read")
            return real_sha256(location)

        with patch("bookflow.archive.sha256_file", side_effect=no_cloud_read):
            status = derive(self.project)
        self.assertEqual(status["stage"], "归档")
        self.assertNotIn("无待办", status["next_step"])
        self.assertIn("production/voice.wav", status["blockers"][0])

    def test_next_rejects_status_only_completed_archive(self):
        write_yaml(self.project / "archive/ep01.yaml", {
            "status": "complete", "project": str(self.project.resolve()),
            "episode": 1, "files": [],
        })
        state = derive(self.project)
        self.assertEqual(state["stage"], "归档")
        self.assertIn("文件列表不一致", state["blockers"][0])


if __name__ == "__main__":
    unittest.main()
