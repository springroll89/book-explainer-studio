import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.__main__ import dispatch, parser
from bookflow.approvals import confirmation_state, parse_confirmation, record_confirmation
from bookflow.common import ROOT, atomic_write, load_yaml, sha256_file, write_json, write_yaml
from bookflow.continuity import update as update_continuity
from bookflow.flow import STAGES, STAGE_GUIDE, WORKFLOW_END, WORKFLOW_START, derive, render_workflow_overview, write
from bookflow.lessons import accept, inbox, observe, propose
from bookflow.recap import entry_sha256, migrate as migrate_recap
from bookflow.selftest import run as selftest_run
from bookflow.text_checks import check_drafts


class FlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "projects" / "sample"
        self.project.mkdir(parents=True)
        write_yaml(self.project / "project.yaml", {"book": {"title": "测试书"},
                   "approvals": {"verify_transcript": False}})

    def prepare_source_plan(self):
        generation = "a" * 64
        write_json(self.project / "source/current.json", {"generation": generation})
        atomic_write(self.project / "source/imports" / generation / "paragraphs.jsonl",
                     '{"id":"p00001","chapter":"ch01","text":"第一章原文。"}\n'
                     '{"id":"p00002","chapter":"ch02","text":"第二章原文。"}\n')
        write_json(self.project / "source/imports" / generation / "chapters.json", [
            {"id": "ch01", "title": "第一章", "paragraphs": ["p00001"], "chars": 6},
            {"id": "ch02", "title": "第二章", "paragraphs": ["p00002"], "chars": 6},
        ])
        write_json(self.project / "source/imports" / generation / "manifest.json", {"generation": generation})
        write_json(self.project / "analysis/source_validation.json", {
            "source_generation": generation, "checks": {"sampled_chapters_match": True},
        })
        atomic_write(self.project / "analysis/intake_report.md", "已逐章抽查原文。")
        atomic_write(self.project / "analysis/book_brief.md", "〔据 p00001-p00002〕全书简报。")
        write_yaml(self.project / "analysis/characters.yaml", {"characters": []})
        write_yaml(self.project / "analysis/threads.yaml", {"threads": []})
        for number in (1, 2):
            atomic_write(self.project / f"analysis/chapter_notes/ch{number:02d}.md", f"本章记录 p{number:05d}。")
        report = self.project / "analysis/coverage_machine.json"
        write_json(report, {"generation": generation, "errors": []})
        write_yaml(self.project / "analysis/coverage_review.yaml", {
            "status": "completed", "source_generation": generation,
            "machine_report_sha256": sha256_file(report), "unresolved": [],
        })
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [
            {"ep": ep, "title_working": f"第 {ep} 集", "core_question": "为什么？",
             "genre_mode": "suspense", "target_chars": 3000, "covers": [f"ch{ep:02d}"],
             "takeaways": [{"id": f"K{ep}", "layer": 2,
                            "text": "核心收获", "evidence": [f"p{ep:05d}"]}]
                          + ([{"id": "K3", "layer": 3, "text": "全季收获", "evidence": ["p00001"]}]
                             if ep == 1 else []),
             "reveal": [], "withhold": []} for ep in (1, 2)
        ], "skipped": []})

    def test_next_is_deterministic_and_writes_progress(self):
        first = write(self.project)
        self.assertEqual(first["stage"], "导入原文")
        self.prepare_source_plan()
        pending = write(self.project)
        self.assertEqual(pending["stage"], "方案确认")
        self.assertEqual(pending["actor"], "你")
        self.assertEqual(write(self.project), pending)
        self.assertIn("拍板方案", (self.project / "进度.md").read_text(encoding="utf-8"))
        self.assertTrue((self.project / "state.json").is_file())

    def test_read_only_next_reports_state_without_writing_progress_or_version(self):
        config_path = self.project / "project.yaml"
        original = config_path.read_bytes()
        args = parser().parse_args(["next", str(self.project), "--json", "--read-only"])
        state = dispatch(args)
        self.assertEqual(state["stage"], "导入原文")
        self.assertEqual(state["artifacts"], [])
        self.assertEqual(config_path.read_bytes(), original)
        self.assertFalse((self.project / "state.json").exists())
        self.assertFalse((self.project / "进度.md").exists())

    def test_new_lesson_blocks_only_when_entering_next_stage(self):
        self.prepare_source_plan()
        self.assertEqual(write(self.project)["stage"], "方案确认")
        observe(self.project, "以后别把重复报错静默略过。", "chat:lesson-one")
        self.assertEqual(derive(self.project)["next_step"], "请你阅读方案并回复“拍板方案”")
        record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        pending = derive(self.project)
        self.assertEqual(pending["stage"], "全季初稿")
        self.assertIn("处理收件箱", pending["next_step"])
        self.assertEqual(pending["lessons_pending"], 1)
        self.assertNotIn("方案确认", pending["needs_you"])
        lesson_id = inbox(self.project)["items"][0]["id"]
        propose(self.project, lesson_id, kind="process_bug", destination="bookflow/flow.py",
                before="旧流程", after="新流程", check="tests/test_flow.py", rationale="跨项目流程缺陷")
        accept(self.project, "可以", verify_transcript=False)
        self.assertIn("完成第 1 集初稿", derive(self.project)["next_step"])

    @patch("bookflow.text_checks.check_drafts", return_value={"passed": True, "errors": []})
    def test_partial_script_confirmation_and_revoke(self, _draft_checks):
        self.prepare_source_plan()
        plan = record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        self.assertTrue(plan["passed"], plan)
        for ep in (1, 2):
            folder = self.project / "episodes" / f"ep{ep:02d}"
            atomic_write(folder / "draft_v1.md", f"---\nepisode: {ep}\n---\n第 {ep} 集。")
            atomic_write(folder / "final.md", f"---\nepisode: {ep}\nstatus: final\n---\n第 {ep} 集。")
        for ep in (1, 2):
            result = update_continuity(self.project, ep, self.project / f"episodes/ep{ep:02d}/draft_v1.md")
            self.assertTrue(result["passed"], result)
        write_yaml(self.project / "notes.yaml", {"workflow": {"season_review_status": "completed"}})
        self.assertEqual(derive(self.project)["stage"], "文案确认")
        one = record_confirmation(self.project, "script", "拍版文案 除2", verify_transcript=False)
        self.assertTrue(one["passed"], one)
        self.assertEqual(one["record"]["episodes"], [1])
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        self.assertEqual(confirmation_state(self.project, "script", 2)["state"], "pending")
        self.assertIn("第 2 集", derive(self.project)["next_step"])
        first_final = self.project / "episodes/ep01/final.md"
        atomic_write(first_final, first_final.read_text(encoding="utf-8").replace("第 1 集。", "第 1 集转身。"))
        changed = derive(self.project)
        self.assertEqual(changed["stage"], "文案确认")
        self.assertIn("第 1 集文案变化", " ".join(changed["blockers"]))
        self.assertIn("转身", " ".join(changed["blockers"]))
        revoked = record_confirmation(self.project, "script", "撤回文案", [1], verify_transcript=False)
        self.assertTrue(revoked["passed"], revoked)
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "revoked")

    @patch("bookflow.text_checks.check_drafts", return_value={"passed": True, "errors": []})
    def test_next_requires_current_drafts_and_real_dependency_hashes_before_season_review(self, _draft_checks):
        self.prepare_source_plan()
        record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        for ep in (1, 2):
            atomic_write(self.project / f"episodes/ep{ep:02d}/draft_v1.md",
                         f"---\nepisode: {ep}\n---\n第 {ep} 集。")
        write_yaml(self.project / "episodes/working_continuity.yaml", {"episodes": [{"ep": 1}, {"ep": 2}]})
        placeholder = derive(self.project)
        self.assertEqual(placeholder["stage"], "全季初稿")
        self.assertIn("工作前情", placeholder["next_step"])
        for ep in (1, 2):
            result = update_continuity(self.project, ep, self.project / f"episodes/ep{ep:02d}/draft_v1.md")
            self.assertTrue(result["passed"], result)
        self.assertEqual(derive(self.project)["stage"], "统一改稿")
        first = self.project / "episodes/ep01/draft_v1.md"
        atomic_write(first, first.read_text(encoding="utf-8") + "\n多了一句。")
        stale = derive(self.project)
        self.assertEqual(stale["stage"], "全季初稿")
        self.assertTrue(stale["blockers"])
        self.assertIn("draft_sha256", stale["blockers"][0])
        self.assertTrue(update_continuity(self.project, 1, first)["passed"])
        dependency = derive(self.project)
        self.assertEqual(dependency["stage"], "全季初稿")
        self.assertIn("依赖第 1 集", dependency["blockers"][0])

    @patch("bookflow.text_checks.check_drafts", return_value={"passed": True, "errors": []})
    def test_next_never_bypasses_existing_pending_recap(self, _draft_checks):
        self.prepare_source_plan()
        record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        for ep in (1, 2):
            draft = self.project / f"episodes/ep{ep:02d}/draft_v1.md"
            atomic_write(draft, f"---\nepisode: {ep}\n---\n第 {ep} 集。")
            self.assertTrue(update_continuity(self.project, ep, draft)["passed"])
        self.assertEqual(derive(self.project)["stage"], "统一改稿")
        migrate_recap(self.project, write=True)
        pending = derive(self.project)
        self.assertEqual(pending["stage"], "全季初稿")
        self.assertIn("语义复核", pending["next_step"])
        recap = self.project / "episodes/recap.yaml"
        data = load_yaml(recap)
        for row in data["episodes"]:
            candidate = row["candidates"]["working"]
            entry = candidate["legacy_entry"]
            row["selected_basis"] = "working"
            row["semantic_review"] = {"status": "completed", "reviewer": "fixture-agent",
                "note": "已逐集核对测试稿前情。", "ending_summary": "本集结束。", "unresolved": [],
                "reviewed_file_sha256": candidate["migration_snapshot"]["file_sha256"],
                "reviewed_entry_sha256": entry_sha256(entry)}
        write_yaml(recap, data)
        self.assertEqual(derive(self.project)["stage"], "统一改稿")
        first = self.project / "episodes/ep01/draft_v1.md"
        atomic_write(first, first.read_text(encoding="utf-8") + "\n新句。")
        self.assertEqual(derive(self.project)["stage"], "全季初稿")

    def test_script_markers_and_metadata_do_not_stale_confirmation(self):
        self.prepare_source_plan()
        final = self.project / "episodes/ep01/final.md"
        atomic_write(final, "---\nepisode: 1\n---\n〔据 p00001〕他开门。")
        approved = record_confirmation(self.project, "script", "拍板文案", [1], verify_transcript=False)
        self.assertTrue(approved["passed"], approved)
        atomic_write(final, "---\nepisode: 1\nstatus: final\n---\n[画面：门口]\n他开门。\n")
        self.assertEqual(confirmation_state(self.project, "script", 1)["state"], "passed")
        atomic_write(final, "---\nepisode: 1\nstatus: final\n---\n他关门。\n")
        status = confirmation_state(self.project, "script", 1)
        self.assertEqual(status["state"], "invalidated")
        self.assertEqual(status["changed_files"][0]["file"], "episodes/ep01/final.md")
        change = status["changed_files"][0]
        self.assertEqual(change["episode"], 1)
        self.assertEqual(change["reason"], "content_changed")
        self.assertNotEqual(change["before_sha256"], change["after_sha256"])
        self.assertIn("开", change["detail"])
        self.assertIn("关", change["detail"])

    def test_legacy_script_confirmation_without_snapshot_reports_limit(self):
        self.prepare_source_plan()
        final = self.project / "episodes/ep01/final.md"
        atomic_write(final, "---\nepisode: 1\n---\n〔据 p00001〕他开门。")
        self.assertTrue(record_confirmation(self.project, "script", "拍板文案", [1],
                                            verify_transcript=False)["passed"])
        log = self.project / "approvals/log.yaml"
        records = load_yaml(log)
        records[-1].pop("spoken_snapshots")
        write_yaml(log, records)
        atomic_write(final, "---\nepisode: 1\n---\n〔据 p00001〕他关门。")
        state = confirmation_state(self.project, "script", 1)
        self.assertEqual(state["state"], "invalidated")
        self.assertIn("旧确认无有效口播快照", state["changed_files"][0]["detail"])

    def test_next_blocks_real_draft_lint_error_before_unified_editing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(selftest_run(root)["passed"])
            project = root / "projects/selftest-fixture"
            draft = project / "episodes/ep01/draft_v1.md"
            atomic_write(draft, draft.read_text(encoding="utf-8") + "\nTODO：这一句待核实。\n")
            state = derive(project)
        self.assertEqual(state["stage"], "全季初稿")
        self.assertIn("episodes/ep01/draft_v1.md", state["blockers"][0])
        self.assertIn("待核实", state["blockers"][0])

    def test_draft_check_reports_source_file_and_sentence_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(selftest_run(root)["passed"])
            project = root / "projects/selftest-fixture"
            draft = project / "episodes/ep01/draft_v1.md"
            original = draft.read_text(encoding="utf-8")
            prefix = original.rstrip("\n") + "\n"
            line = len(prefix.splitlines()) + 1
            atomic_write(draft, prefix + "他" * 60 + "。\n")
            result = check_drafts(project, [1])
        self.assertFalse(result["passed"])
        self.assertTrue(any(f"episodes/ep01/draft_v1.md:{line}" in error for error in result["errors"]))

    def test_plain_ok_is_not_confirmation(self):
        with self.assertRaises(ValueError):
            parse_confirmation("OK")
        with self.assertRaises(ValueError):
            parse_confirmation("拍板文案，但第 2 集再改")

    def test_confirmation_accepts_ime_punctuation_and_episode_labels(self):
        for quote, expected in (("拍板文案，除5", [5]), ("拍板文案除第5集", [5]),
                                ("拍板样片，除第3集、4集", [3, 4]),
                                ("拍版 文案 除 1,第12集。", [1, 12])):
            with self.subTest(quote=quote):
                self.assertEqual(parse_confirmation(quote)["exclude"], expected)
        self.assertEqual(parse_confirmation("拍板 方案")["gate"], "plan")
        for quote in ("拍板文案除0", "拍板文案除,5", "拍板文案除5,", "拍板文案除第5集再改",
                      "撤回文案，除5", "拍板文案除1-12"):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                parse_confirmation(quote)

    @patch("bookflow.text_checks.check_drafts", return_value={"passed": True, "errors": []})
    @patch("bookflow.flow.check_plan", return_value={"errors": []})
    @patch("bookflow.continuity.context", return_value={"complete": True, "errors": []})
    def test_pending_confirmation_lists_nonconsecutive_episodes(self, *_checks):
        self.prepare_source_plan()
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": ep} for ep in (1, 2, 12)]})
        record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        for ep in (1, 2, 12):
            for name in ("draft_v1.md", "final.md"):
                atomic_write(self.project / f"episodes/ep{ep:02d}" / name, "测试文本。")
        write_yaml(self.project / "notes.yaml", {"workflow": {"season_review_status": "completed"}})
        record_confirmation(self.project, "script", "拍板文案", [2], verify_transcript=False)
        state = derive(self.project)
        self.assertEqual(state["needs_you"], ["文案确认：第 1、12 集"])
        self.assertIn("第 1、12 集", state["next_step"])

    @patch("bookflow.text_checks.check_drafts", return_value={"passed": True, "errors": []})
    @patch("bookflow.flow.check_plan", return_value={"errors": []})
    @patch("bookflow.continuity.context", return_value={"complete": True, "errors": []})
    def test_missing_finals_point_to_freeze_command(self, *_checks):
        self.prepare_source_plan()
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": ep} for ep in range(1, 5)]})
        record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        for ep in range(1, 5):
            atomic_write(self.project / f"episodes/ep{ep:02d}/draft_v1.md", "---\nepisode: %d\n---\n测试文本。" % ep)
        for ep in (1, 2):
            atomic_write(self.project / f"episodes/ep{ep:02d}/final.md", "测试文本。")
        write_yaml(self.project / "notes.yaml", {"workflow": {"season_review_status": "completed"}})
        state = derive(self.project)
        self.assertEqual(state["stage_index"], 8)
        self.assertIn("final freeze <项目> --eps 3-4", state["next_step"])

    @patch("bookflow.text_checks.check_drafts", return_value={"passed": True, "errors": []})
    @patch("bookflow.flow.check_plan", return_value={"errors": []})
    @patch("bookflow.continuity.context", return_value={"complete": True, "errors": []})
    def test_season_voice_cast_comes_after_script_confirmation(self, *_checks):
        from bookflow import voice_script
        self.prepare_source_plan()
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": 1}, {"ep": 2}]})
        record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        for ep in (1, 2):
            text = "---\nepisode: %d\n---\n他抬起头：“走吧。”\n" % ep
            atomic_write(self.project / f"episodes/ep{ep:02d}/draft_v1.md", text)
            atomic_write(self.project / f"episodes/ep{ep:02d}/final.md", text)
        write_yaml(self.project / "notes.yaml", {"workflow": {"season_review_status": "completed"}})
        record_confirmation(self.project, "script", "拍板文案", verify_transcript=False)
        write_yaml(self.project / "production/voice_cast.yaml", {
            "revision": 1, "narrator": {"voice_id": "v-n", "status": "confirmed"},
            "characters": {"P01": {"name": "劳拉", "voice_id": "v-l", "status": "confirmed"}}})
        state = derive(self.project)
        self.assertEqual(state["stage_index"], 8)
        self.assertIn("voices scaffold <项目> --eps 1-2", state["next_step"])
        for ep, speaker in ((1, "P01"), (2, "P09")):
            voice_script.scaffold(self.project, ep)
            path = self.project / f"episodes/ep{ep:02d}/production/voice_script.yaml"
            data = load_yaml(path)
            data["quotes"][0]["speaker"] = speaker
            write_yaml(path, data)
        state = derive(self.project)
        self.assertEqual((state["stage"], state["stage_index"]), ("设计确认", 9))
        self.assertIn("style_choice.yaml", state["next_step"])
        self.confirm_design()
        state = derive(self.project)
        self.assertEqual(state["needs_you"], ["角色音色"])
        self.assertIn("P09，第 2 集共 1 句", state["next_step"])
        self.assertIn("voices sheet", state["next_step"])
        voice_script.set_voice(self.project, "P09", voice_id="v-new", name="新人物")
        self.assertEqual(derive(self.project)["stage"], "声音")

    def confirm_design(self):
        from bookflow.media_fixture import _png
        (self.project / "visual").mkdir(exist_ok=True)
        for name in ("a.png", "b.png", "P01.png"):
            _png(self.project / "visual" / name)
        candidates = [{"id": "A", "images": ["visual/a.png"]}, {"id": "B", "images": ["visual/b.png"]}]
        write_yaml(self.project / "visual/style_choice.yaml", {"candidates": candidates})
        state = derive(self.project)
        self.assertEqual(state["needs_you"][-1], "画风选择")
        self.assertFalse(record_confirmation(self.project, "style", "拍板画风", verify_transcript=False)["passed"])
        write_yaml(self.project / "visual/style_choice.yaml", {"chosen": "B", "candidates": candidates})
        self.assertEqual(derive(self.project)["needs_you"][-1], "画风确认")
        result = record_confirmation(self.project, "style", "拍板画风", verify_transcript=False)
        self.assertEqual(set(result["record"]["deliverables"]), {"visual/style_choice.yaml", "visual/b.png"})
        self.assertIn("character_sheet.yaml", derive(self.project)["next_step"])
        write_yaml(self.project / "visual/character_sheet.yaml",
                   {"characters": {"P01": {"name": "劳拉", "images": ["visual/P01.png"]}}})
        self.assertEqual(derive(self.project)["needs_you"][-1], "定妆确认")
        self.assertTrue(record_confirmation(self.project, "characters", "拍板定妆", verify_transcript=False)["passed"])

    @patch("bookflow.text_checks.check_drafts", return_value={"passed": True, "errors": []})
    @patch("bookflow.flow.check_plan", return_value={"errors": []})
    @patch("bookflow.continuity.context", return_value={"complete": True, "errors": []})
    def test_sound_is_confirmed_per_batch_before_visuals(self, *_checks):
        self.prepare_source_plan()
        write_yaml(self.project / "plan/episodes.yaml", {"episodes": [{"ep": ep} for ep in range(1, 5)]})
        record_confirmation(self.project, "plan", "拍板方案", verify_transcript=False)
        for ep in range(1, 5):
            for name in ("draft_v1.md", "final.md"):
                atomic_write(self.project / f"episodes/ep{ep:02d}" / name, "---\nepisode: %d\n---\n测试文本。\n" % ep)
        write_yaml(self.project / "notes.yaml", {"workflow": {"season_review_status": "completed"}})
        record_confirmation(self.project, "script", "拍板文案", verify_transcript=False)
        self.confirm_design()
        ready = {(ep, kind): False for ep in range(1, 5) for kind in ("audio", "visual")}
        sounds, sheets, sample = set(), set(), {"passed": False}
        real_state = confirmation_state

        def fake_state(project, gate, ep=None):
            if gate == "sound":
                return {"state": "passed" if ep in sounds else "pending", "changed_files": []}
            if gate == "sample":
                return {"state": "passed" if sample["passed"] else "pending", "changed_files": []}
            return real_state(project, gate, ep)

        with patch("bookflow.flow._media_ready", side_effect=lambda _p, ep, kind: ready[(ep, kind)]), \
                patch("bookflow.flow.confirmation_state", side_effect=fake_state), \
                patch("bookflow.guard.confirmation_state", side_effect=fake_state), \
                patch("bookflow.audition.sheet_state",
                      side_effect=lambda _p, ep: {"state": "current" if ep in sheets else "missing"}):
            self.assertEqual(derive(self.project)["stage"], "声音")
            ready[(1, "audio")] = True
            state = derive(self.project)
            self.assertEqual(state["stage"], "声音")
            self.assertIn("audition <项目> 1", state["next_step"])
            sheets.add(1)
            self.assertEqual(derive(self.project)["needs_you"][-1], "声音确认：第 1 集")
            sounds.add(1)
            self.assertEqual(derive(self.project)["stage"], "画面")
            ready[(1, "visual")] = True
            self.assertEqual(derive(self.project)["stage"], "样片确认")
            sample["passed"] = True
            self.assertIn("第 2 集声音", derive(self.project)["next_step"])
            ready[(2, "audio")] = True
            state = derive(self.project)
            self.assertIn("第 3 集声音", state["next_step"])  # finish the batch's audio first
            ready[(3, "audio")] = True
            self.assertIn("audition <项目> 2；audition <项目> 3", derive(self.project)["next_step"])
            sheets.update({2, 3})
            state = derive(self.project)
            self.assertEqual((state["stage"], state["needs_you"][-1]), ("其余集制作", "声音确认：第 2、3 集"))
            sounds.update({2, 3})
            self.assertIn("第 2 集分镜", derive(self.project)["next_step"])
            ready[(2, "visual")] = ready[(3, "visual")] = True
            self.assertIn("第 4 集声音", derive(self.project)["next_step"])

    def test_import_requires_source_validation_record(self):
        self.prepare_source_plan()
        (self.project / "analysis/source_validation.json").unlink()
        state = derive(self.project)
        self.assertEqual(state["stage"], "导入原文")
        self.assertIn("source_validation.json", " ".join(state["blockers"]))

    def test_malformed_stage_reports_become_actionable_blockers(self):
        self.prepare_source_plan()
        write_yaml(self.project / "analysis/source_validation.json", ["bad shape"])
        self.assertEqual(derive(self.project)["stage"], "导入原文")
        self.prepare_source_plan()
        write_yaml(self.project / "analysis/coverage_machine.json", ["bad shape"])
        self.assertEqual(derive(self.project)["stage"], "拆书")

    def test_analysis_requires_each_chapter_reference_and_clean_coverage(self):
        self.prepare_source_plan()
        note = self.project / "analysis/chapter_notes/ch02.md"
        atomic_write(note, "本章没写段落编号。")
        state = derive(self.project)
        self.assertEqual(state["stage"], "拆书")
        self.assertIn("ch02.md", " ".join(state["blockers"]))
        atomic_write(note, "本章记录 p00002。")
        write_json(self.project / "analysis/coverage_machine.json", {"generation": "a" * 64,
                                                              "errors": ["线索缺少依据"]})
        state = derive(self.project)
        self.assertEqual(state["stage"], "拆书")
        self.assertIn("线索缺少依据", " ".join(state["blockers"]))

    def test_plan_errors_hold_before_plan_confirmation(self):
        self.prepare_source_plan()
        plan = self.project / "plan/episodes.yaml"
        data = __import__("yaml").safe_load(plan.read_text())
        data["episodes"][1]["covers"] = ["ch99"]
        write_yaml(plan, data)
        state = derive(self.project)
        self.assertEqual(state["stage"], "分集")
        self.assertIn("ch99", " ".join(state["blockers"]))

    def test_agent_rules_use_chat_confirmation_and_next(self):
        rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("./run.sh next <project>", rules)
        self.assertIn("最新一条独立消息", rules)
        self.assertIn("不得代填", rules)
        self.assertNotIn("闸门批准只能由用户在交互终端执行", rules)

    def test_workflow_overview_matches_runtime_stage_order(self):
        self.assertEqual(tuple(row["name"] for row in STAGE_GUIDE), STAGES)
        document = (ROOT / "docs/WORKFLOW.md").read_text(encoding="utf-8")
        start = document.index(WORKFLOW_START)
        end = document.index(WORKFLOW_END, start) + len(WORKFLOW_END)
        self.assertEqual(document[start:end], render_workflow_overview())


if __name__ == "__main__":
    unittest.main()
