"""Regressions called out by the workflow redesign PRD."""
import tempfile
import unittest
import difflib
from unittest.mock import patch
from pathlib import Path

from bookflow import approvals
from bookflow import anchors, feedback, migration, pacing, sentences, sound, timing
from bookflow.quality import listener_input
from bookflow.common import atomic_write, latest_draft, write_json, write_yaml


class ApprovalRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        write_yaml(self.project / "project.yaml", {"book": {"title": "测试书"}})

    def test_latest_revoke_wins_over_earlier_valid_approval(self):
        brief = self.project / "analysis/book_brief.md"
        atomic_write(brief, "简报")
        files, digest = approvals._digest_paths(self.project, "G1")
        write_yaml(self.project / "approvals/G1-20260101T000000Z.yaml", {
            "gate": "G1", "object_files": files, "object_sha256": digest,
            "approved_at": "2026-01-01T00:00:00+00:00",
        })
        write_yaml(self.project / "approvals/revoked-G1-20260102T000000Z.yaml", {
            "gate": "G1", "revoked": True, "revoked_at": "2026-01-02T00:00:00+00:00",
        })
        self.assertEqual(approvals.gate_state(self.project, "G1"), "revoked")

    def test_empty_binding_never_passes(self):
        import hashlib
        import json

        empty_digest = hashlib.sha256(json.dumps([], separators=(",", ":")).encode()).hexdigest()
        write_yaml(self.project / "approvals/G3-20260101T000000Z.yaml", {
            "gate": "G3", "object_files": [], "object_sha256": empty_digest,
            "approved_at": "2026-01-01T00:00:00+00:00",
        })
        self.assertNotEqual(approvals.gate_state(self.project, "G3"), "passed")

    def test_latest_draft_uses_numeric_version(self):
        episode = self.project / "episodes/ep01"
        for version in (1, 9, 10):
            atomic_write(episode / f"draft_v{version}.md", str(version))
        self.assertEqual(latest_draft(episode).name, "draft_v10.md")

    def test_sample_binding_ignores_reports_and_history(self):
        episode = self.project / "episodes/ep01"
        atomic_write(episode / "production/final_mix.wav", "mix")
        atomic_write(episode / "production/subtitles.srt", "sub")
        atomic_write(episode / "production/storyboard.yaml", "shots")
        atomic_write(episode / "production/final.mp4", "video")
        atomic_write(episode / "production/_reports/check.json", "report")
        atomic_write(episode / "production/_history/old_mix.wav", "old")
        first = approvals.deliverables(self.project, "sample", [1])
        self.assertTrue(first["passed"], first)
        atomic_write(episode / "production/_reports/check.json", "changed")
        atomic_write(episode / "production/_history/old_mix.wav", "changed")
        second = approvals.deliverables(self.project, "sample", [1])
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(len(first["files"]), 4)

    def test_sample_rejects_report_or_history_as_custom_deliverable(self):
        episode = self.project / "episodes/ep01"
        for name in ("final_mix.wav", "subtitles.srt", "storyboard.yaml", "final.mp4"):
            atomic_write(episode / "production" / name, "asset")
        for folder in ("_reports", "_history"):
            relative = f"production/{folder}/fake.mp4"
            atomic_write(episode / relative, "not an approved video")
            write_yaml(episode / "production/approval_assets.yaml", {"assets": {"video": relative}})
            result = approvals.deliverables(self.project, "sample", [1])
            self.assertFalse(result["passed"])
            self.assertTrue(any("不能作为确认交付物" in error for error in result["errors"]))

    def test_migration_keeps_legacy_records_and_only_valid_plan_approval(self):
        for name in ("analysis/book_brief.md", "analysis/coverage_review.yaml",
                     "analysis/characters.yaml", "analysis/threads.yaml", "plan/episodes.yaml"):
            atomic_write(self.project / name, "内容")
        for gate in ("G1", "G2"):
            files, digest = approvals._digest_paths(self.project, gate)
            write_yaml(self.project / f"approvals/{gate}-20260101T000000Z.yaml", {
                "gate": gate, "object_files": files, "object_sha256": digest,
                "approved_at": "2026-01-01T00:00:00+00:00",
            })
        result = approvals.migrate_legacy(self.project)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["migrated"], ["plan"])
        self.assertEqual(approvals.confirmation_state(self.project, "plan")["state"], "passed")
        self.assertEqual(approvals.confirmation_state(self.project, "script", 1)["state"], "pending")
        self.assertEqual(len(list((self.project / "approvals/legacy").glob("*.yaml"))), 2)


class ProductionRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        write_yaml(self.project / "project.yaml", {"format": {"speech_rate_cpm": 300},
                   "visual_pacing": {"min_hold_sec": 1, "max_static_sec": 15,
                                     "opening": {"min_hard_changes": 0}}})
        self.episode = self.project / "episodes/ep01"
        atomic_write(self.episode / "draft_v1.md", "---\nepisode: 1\n---\n第一句。第二句。")

    def test_sound_cues_preserve_manual_sheet_and_backup_explicit_migration(self):
        sheet = self.episode / "production/sound_cues.yaml"
        write_yaml(sheet, {"episode": 1, "cues": [{"cue_id": "HAND", "description": "人工记录"}]})
        write_yaml(self.episode / "production/sound_plan.yaml", {"cues": [{"id": "OLD", "sound_content": "敲门"}]})
        before = sheet.read_bytes()
        with self.assertRaises(ValueError):
            sound.cues(self.episode)
        self.assertEqual(sheet.read_bytes(), before)
        result = sound.cues(self.episode, migrate=True)
        self.assertTrue(result["passed"])
        self.assertEqual(Path(result["backup"]).read_bytes(), before)

    def test_stable_sentence_id_maps_to_timing_for_opening_and_assembly(self):
        write_json(self.episode / "final.sentences.json", {"sentences": [
            {"id": "s112", "text": "第一句。", "text_sha": "a" * 64, "order": 1},
            {"id": "s137", "text": "第二句。", "text_sha": "b" * 64, "order": 2},
        ]})
        write_json(self.episode / "production/timing.json", {"sentences": [
            {"startTime": 0, "endTime": 1}, {"startTime": 2, "endTime": 3},
        ]})
        write_yaml(self.episode / "production/sound_cues.yaml", {"cues": [{
            "cue_id": "C01", "sound_class": "event", "function": "story_event",
            "gap_policy": "gap", "status": "asset_ready", "duration_sec": 1,
            "anchor": {"sentence_id": "s137"}, "placement": "after",
        }]})
        checked = sound.check(self.episode)
        self.assertTrue(any("前5秒" in e for e in checked["errors"]))
        self.assertTrue((self.episode / "production/_reports/sound_report.json").is_file())
        self.assertFalse((self.episode / "production/sound_report.json").exists())
        atomic_write(self.episode / "production/voiceover_test.mp3", "audio")
        with patch("bookflow.sound.subprocess.run"):
            assembled = sound.assemble(self.episode)
        self.assertEqual(assembled["inserted_gaps"], 1)
        actual = __import__("json").loads((self.episode / "production/timing_actual.json").read_text())
        self.assertEqual(actual["inserted_gaps"][0]["at"], 3.0)

    def test_old_ordinal_timing_id_cannot_override_stable_sentence_order(self):
        write_json(self.episode / "final.sentences.json", {"sentences": [
            {"id": "s112", "order": 1}, {"id": "s137", "order": 2},
        ]})
        rows = [{"id": f"s{i:03d}", "startTime": float(i), "endTime": float(i + 1)}
                for i in range(1, 138)]
        self.assertEqual(sound._time_for(self.episode, rows, "s137"), 2.0)

    def test_listener_and_edit_copy_use_stable_ids_after_sentence_insertion(self):
        first = self.episode / "draft_v1.md"
        atomic_write(first, "---\nepisode: 1\n---\n第一句。第二句。")
        sentences.generate(first)
        second = self.episode / "draft_v2.md"
        atomic_write(second, "---\nepisode: 1\n---\n第一句。新增内容。第二句。")
        sentences.generate(second, first)
        ids = [row["id"] for row in __import__("json").loads(second.with_suffix(".sentences.json").read_text())["sentences"]]
        self.assertEqual(ids, ["s001", "s003", "s002"])
        spoken = listener_input(second)
        self.assertEqual([line.split()[0][1:] for line in spoken.splitlines()], ids)
        edit_dir = self.episode / "human_edit/v2"
        result = feedback.create_copy(second, edit_dir, format="md")
        baseline = __import__("json").loads(Path(result["baseline"]).read_text())
        self.assertEqual(baseline["blocks"][0]["source_sentence_ids"], ids)

    def test_json_storyboard_anchors_use_final_sentence_table(self):
        write_json(self.episode / "final.sentences.json", {"sentences": [
            {"id": "s137", "text": "第二句。", "text_sha": "b" * 64, "order": 1},
        ]})
        write_json(self.episode / "production/storyboard.json", {"shots": [
            {"sentence_id": "s137", "text_sha": "b" * 64},
            {"sentence_id": "s999"},
        ]})
        result = anchors.check(self.episode)
        self.assertEqual([a["sentence_id"] for a in result["anchors"]], ["s137", "s999"])
        self.assertEqual([a["status"] for a in result["anchors"]], ["ok", "orphaned"])
        self.assertTrue((self.episode / "production/_reports/anchors_report.json").is_file())
        self.assertFalse((self.episode / "production/anchors_report.json").exists())
        write_json(self.episode / "production/_reports/fake.json", {"sentence_id": "s998"})
        self.assertEqual(len(anchors.check(self.episode)["anchors"]), 2)

    def test_three_short_shots_report_warning_instead_of_crashing(self):
        write_yaml(self.episode / "production/storyboard.yaml", {"shots": [
            {"time_actual": {"start": i * 2, "end": i * 2 + 2},
             "asset_id": f"A{i}", "segment_role": "setup"} for i in range(3)
        ]})
        result = pacing.check(self.episode)
        self.assertTrue(any("连续短镜头" in w for w in result["warnings"]))

    def test_pacing_budget_requires_a_new_image_for_every_shot(self):
        atomic_write(self.episode / "draft_v1.md", "---\nepisode: 1\n---\n" + "他走进房间，发现门开着。" * 200)
        result = pacing.budget(self.episode)
        self.assertGreater(result["recommended_shots"], 1)
        self.assertEqual(result["recommended_assets"], result["recommended_shots"])
        self.assertTrue(all(row["recommended_new_assets"] == row["recommended_shots"]
                            for row in result["sections"]))
        self.assertEqual(Path(result["output"]).parent.name, "_reports")
        self.assertFalse((self.episode / "production/shot_budget.json").exists())

    def test_pacing_rejects_missing_and_nonadjacent_reused_image_ids(self):
        write_yaml(self.episode / "production/storyboard.yaml", {"shots": [
            {"time_actual": {"start": i * 8, "end": (i + 1) * 8},
             "asset_id": asset} for i, asset in enumerate(("A", "B", "A", None))
        ]})
        result = pacing.check(self.episode)
        self.assertFalse(result["passed"])
        self.assertTrue(any("第 1 镜重复" in error for error in result["errors"]))
        self.assertTrue(any("缺少独立剧情图" in error for error in result["errors"]))
        self.assertTrue((self.episode / "production/_reports/pacing_report.json").is_file())
        self.assertFalse((self.episode / "production/pacing_report.json").exists())

    def test_pacing_accepts_render_image_paths_but_rejects_same_file_under_new_id(self):
        board = self.episode / "production/storyboard.yaml"
        write_yaml(board, {"shots": [
            {"start": i * 8, "end": (i + 1) * 8, "image": f"still-{i}.png"} for i in range(3)
        ]})
        self.assertTrue(pacing.check(self.episode)["passed"])
        rows = [{"start": i * 8, "end": (i + 1) * 8,
                 "asset_id": f"ID-{i}", "image": "still-0.png" if i == 2 else f"still-{i}.png"}
                for i in range(3)]
        write_yaml(board, {"shots": rows})
        result = pacing.check(self.episode)
        self.assertFalse(result["passed"])
        self.assertTrue(any("剧情图文件与第 1 镜重复" in error for error in result["errors"]))
        rows[2]["asset_id"] = {"invalid": "id"}
        rows[2].pop("image")
        write_yaml(board, {"shots": rows})
        self.assertFalse(pacing.check(self.episode)["passed"])

    def test_timing_rejects_missing_timestamps_and_uses_project_speech_rate(self):
        audio = self.episode / "production/voice.wav"
        atomic_write(audio, "audio")
        with self.assertRaises(ValueError):
            timing.import_timing(self.episode, audio, self.episode / "missing.json")
        result = timing.import_timing(self.episode, audio)
        self.assertEqual(result["source"], "estimate")
        rows = __import__("json").loads((self.episode / "production/timing.json").read_text())["sentences"]
        self.assertEqual(rows[0]["endTime"], 0.6)

    def test_multiline_markdown_comment_is_not_narration_and_survives_as_feedback(self):
        edited = self.project / "edited.md"
        atomic_write(edited, "# 编辑稿\n\n## 第一节\n他开门。<!-- 这里改成更紧张的动作\n因为后面要留悬念 -->门后没人。\n")
        parsed = feedback.read_markdown(edited)
        self.assertEqual(parsed["paragraphs"], ["他开门。门后没人。"])
        self.assertEqual(len(parsed["comments"]), 1)
        comment = next(iter(parsed["comments"].values()))
        self.assertIn("因为后面要留悬念", comment["text"])
        self.assertEqual(comment["anchor"], "他开门。门后没人。")

    def test_baselines_are_independent_files_and_preserve_project_yaml(self):
        project_yaml = self.project / "project.yaml"
        original = project_yaml.read_bytes()
        write_json(self.episode / "production/pacing_report.json", {"metrics": {"shots": 10}})
        write_json(self.episode / "production/sound_report.json", {"metrics": {"cue_count": 3}})
        with patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}):
            pacing_result = pacing.baseline(self.episode)
            sound_result = sound.baseline(self.episode)
        self.assertTrue(pacing_result["passed"], pacing_result)
        self.assertTrue(sound_result["passed"], sound_result)
        self.assertEqual(project_yaml.read_bytes(), original)
        self.assertTrue((self.episode / "production/pacing_baseline.json").is_file())
        self.assertTrue((self.episode / "production/sound_baseline.json").is_file())

    def test_sound_baseline_prefers_new_report_over_legacy_report(self):
        write_json(self.episode / "production/sound_report.json", {"metrics": {"cue_count": 3}})
        write_json(self.episode / "production/_reports/sound_report.json", {"metrics": {"cue_count": 5}})
        with patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}):
            result = sound.baseline(self.episode)
        self.assertTrue(result["passed"])
        self.assertEqual(result["baseline"]["cue_count"], 5)

    def test_pacing_baseline_prefers_new_report_over_legacy_report(self):
        write_json(self.episode / "production/pacing_report.json", {"metrics": {"shots": 3}})
        write_json(self.episode / "production/_reports/pacing_report.json", {"metrics": {"shots": 5}})
        with patch("bookflow.approvals.confirmation_state", return_value={"state": "passed"}):
            result = pacing.baseline(self.episode)
        self.assertTrue(result["passed"])
        self.assertEqual(result["baseline"]["shots"], 5)

    def test_narration_source_comes_from_project_config_not_first_book_filename(self):
        cfg = __import__("yaml").safe_load((self.project / "project.yaml").read_text())
        cfg["sound_design"] = {"narration": {"audio": "production/custom_voice.wav"}}
        write_yaml(self.project / "project.yaml", cfg)
        custom = self.episode / "production/custom_voice.wav"
        atomic_write(custom, "custom")
        atomic_write(self.episode / "production/voiceover_ep01_v6_r2.mp3", "old book")
        write_json(self.episode / "production/timing.json", {"sentences": []})
        write_yaml(self.episode / "production/sound_cues.yaml", {"cues": []})
        with patch("bookflow.sound.subprocess.run"):
            result = sound.assemble(self.episode)
        self.assertTrue(result["passed"])
        actual = __import__("json").loads((self.episode / "production/timing_actual.json").read_text())
        self.assertEqual(actual["source"], str(custom))

    def test_sound_event_budget_uses_configured_limit(self):
        cfg = __import__("yaml").safe_load((self.project / "project.yaml").read_text())
        cfg["sound_design"] = {"budget_per_13min": {"cues_event_process": [1, 2]}}
        write_yaml(self.project / "project.yaml", cfg)
        write_yaml(self.episode / "production/sound_cues.yaml", {"cues": [
            {"cue_id": f"C{i}", "sound_class": "event", "function": "story_event",
             "gap_policy": "gap", "status": "on_hold", "duration_sec": 0.1} for i in range(3)
        ]})
        report = sound.check(self.episode)
        self.assertTrue(any("上限2" in warning for warning in report["warnings"]))

    def test_source_migration_hash_matches_before_local_fuzzy_search(self):
        old_generation, new_generation = "a" * 64, "b" * 64
        write_json(self.project / "source/current.json", {"generation": old_generation})
        old = self.project / "source/imports" / old_generation / "paragraphs.jsonl"
        new = self.project / "source/imports" / new_generation / "paragraphs.jsonl"
        import json
        old_rows = [{"id": f"p{i:05d}", "text": f"第{i}段：这是不同的原文内容{i}。"} for i in range(1, 81)]
        new_rows = [dict(row) for row in old_rows]
        new_rows[39]["text"] += "小修改"
        atomic_write(old, "\n".join(json.dumps(row, ensure_ascii=False) for row in old_rows) + "\n")
        atomic_write(new, "\n".join(json.dumps(row, ensure_ascii=False) for row in new_rows) + "\n")
        real_matcher = difflib.SequenceMatcher
        calls = []
        def counted(*args, **kwargs):
            calls.append(1)
            return real_matcher(*args, **kwargs)
        with patch("bookflow.migration.difflib.SequenceMatcher", side_effect=counted):
            result = migration.migrate(self.project, new_generation)
        self.assertTrue(result["passed"], result)
        self.assertLess(len(calls), 200)
        self.assertEqual(result["mapping"][39]["relation"], "edited")

    def test_source_switch_writes_current_pointer_atomically(self):
        old_generation, new_generation = "a" * 64, "b" * 64
        write_json(self.project / "source/current.json", {"generation": old_generation})
        write_json(self.project / "source/imports" / new_generation / "pid_map.json", {"mapping": []})
        with patch("bookflow.migration.sys.stdin") as stdin, \
             patch("builtins.input", return_value="切换原文 bbbbbbbb"), \
             patch("bookflow.migration.atomic_write", wraps=atomic_write) as writer:
            stdin.isatty.return_value = True
            result = migration.switch(self.project, new_generation)
        self.assertTrue(result["passed"], result)
        self.assertEqual(writer.call_args.args[0], self.project / "source/current.json")


if __name__ == "__main__":
    unittest.main()
