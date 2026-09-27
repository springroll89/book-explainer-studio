"""Isolated, model-free rehearsal through the media fixture and export stage."""
from __future__ import annotations

import tempfile
import time
import shutil
from pathlib import Path

from . import approvals, archive, planning, recap, source
from .common import ROOT, atomic_write, load_yaml, read_chapters, read_paragraphs, sha256_file, source_generation, write_json, write_yaml
from .exporting import export_episode
from .flow import derive
from . import produce
from .quality import lint, verify_quotes


def _exercise(root: Path, *, keep_artifacts: bool) -> dict:
    from .__main__ import new_project

    start = time.monotonic()
    checks: list[str] = []
    project = root / "projects" / "selftest-fixture"
    if (root / ".git").exists() or (root / "projects").exists():
        raise ValueError("selftest 只能使用全新的临时目录；拒绝写入现有仓库或书目项目")
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / ".bookflow-selftest", "test_fixture_only\n")

    created = new_project("selftest-fixture", "迟到的钟（测试夹具）", "原创演示", "suspense", root=root)
    if not created["doctor"]["passed"] or derive(project)["stage"] != "导入原文":
        raise ValueError("new 后的轻量体检或下一步状态不正确")
    checks.append("new+doctor+next")

    cfg = load_yaml(project / "project.yaml")
    cfg["format"]["episode_minutes"] = [0.1, 1]
    cfg["approvals"] = {"verify_transcript": False, "test_fixture_only": True}
    write_yaml(project / "project.yaml", cfg)
    original = ROOT / "demos/source/迟到的钟.txt"
    if not original.is_file():
        raise ValueError("缺少固定示范原文 demos/source/迟到的钟.txt；不要改用真实书目")
    imported = source.ingest(project, [original])
    if imported["status"] != "imported" or not source_generation(project):
        raise ValueError("示范原文导入失败或未生成有效批次")
    chapters = read_chapters(project)
    if not chapters:
        raise ValueError("示范原文未拆出章节")
    checks.append("ingest")

    foreign_source = ROOT / "demos/source/The_Lantern_at_the_Ferry.txt"
    if not foreign_source.is_file():
        raise ValueError("缺少固定公版英文原文 demos/source/The_Lantern_at_the_Ferry.txt")
    foreign_project = root / "projects" / "selftest-foreign-fixture"
    foreign_created = new_project("selftest-foreign-fixture", "The Lantern at the Ferry", "测试夹具", "suspense", root=root)
    if not foreign_created["doctor"]["passed"]:
        raise ValueError("外文测试书目创建后的轻量体检失败")
    foreign_cfg = load_yaml(foreign_project / "project.yaml")
    foreign_cfg["source"] = {"language": "en"}
    write_yaml(foreign_project / "project.yaml", foreign_cfg)
    foreign_hash = sha256_file(foreign_source)
    foreign_import = source.ingest(foreign_project, [foreign_source])
    foreign_chapters = read_chapters(foreign_project)
    foreign_paragraphs = read_paragraphs(foreign_project)
    if (foreign_import["status"] != "imported" or foreign_import["chapters"] != 2
            or [chapter["title"] for chapter in foreign_chapters]
            != ["Chapter One: The Last Crossing", "Chapter Two: At First Light"]
            or len(foreign_paragraphs) != 6 or not source_generation(foreign_project)
            or sha256_file(foreign_source) != foreign_hash):
        raise ValueError("公版英文原文导入、英文分章、段落索引或原文件不变性检查失败")
    checks.append("foreign_source_ingest")

    generation = source_generation(project)
    write_json(project / "analysis/source_validation.json", {
        "source_generation": generation, "checks": {"fixture_import": True}, "test_fixture_only": True})
    atomic_write(project / "analysis/intake_report.md", "测试夹具：逐章索引由本地导入器生成；不代表人工抽查。\n")
    atomic_write(project / "analysis/book_brief.md", f"测试夹具：原文首章依据 {chapters[0]['paragraphs'][0]}。\n")
    write_yaml(project / "analysis/characters.yaml", {"characters": []})
    write_yaml(project / "analysis/threads.yaml", {"threads": []})
    for chapter in chapters:
        atomic_write(project / "analysis/chapter_notes" / f"{chapter['id']}.md",
                     f"测试夹具：本章段落索引 {chapter['paragraphs'][0]}。\n")
    coverage = planning.coverage(project)
    if coverage["errors"]:
        raise ValueError("机器查漏失败：" + "；".join(coverage["errors"][:3]))
    write_yaml(project / "analysis/coverage_review.yaml", {
        "status": "completed_test_fixture", "source_generation": generation,
        "machine_report_sha256": sha256_file(project / "analysis/coverage_machine.json"),
        "unresolved": [], "test_fixture_only": True})
    checks.append("analysis+coverage")

    first = chapters[1]["paragraphs"][0] if len(chapters) > 1 else chapters[0]["paragraphs"][0]
    write_yaml(project / "plan/episodes.yaml", {"episodes": [{
        "ep": 1, "title_working": "钟为什么快了十二分钟", "core_question": "时间差从何而来？",
        "genre_mode": "suspense", "target_chars": 100, "covers": [chapter["id"] for chapter in chapters],
        "takeaways": [
            {"id": "TEST-2", "layer": 2, "text": "核对眼前事实", "evidence": [first]},
            {"id": "TEST-3", "layer": 3, "text": "保留尚未揭晓的问题", "evidence": [first]},
        ], "reveal": [], "withhold": [], "test_fixture_only": True}], "skipped": []})
    plan = planning.check_plan(project)
    if plan["errors"] or derive(project)["stage"] != "方案确认":
        raise ValueError("分集检查或方案待确认阶段失败：" + "；".join(plan["errors"][:3]))
    test_plan = approvals.record_confirmation(project, "plan", "拍板方案",
                                               session="selftest-fixture", verify_transcript=False)
    if not test_plan["passed"] or derive(project)["stage"] != "全季初稿":
        raise ValueError("临时测试记录未能推进到全季初稿；不得用于真实项目")
    checks.append("plan+test_confirmation")

    draft = project / "episodes/ep01/draft_v1.md"
    fixture = (ROOT / "demos/fixtures/ep01_draft.md").read_text(encoding="utf-8")
    atomic_write(draft, fixture)
    actual = load_yaml(ROOT / "demos/fixtures/ep01_recap.yaml")
    working = recap.update_working(project, 1, draft, actual)
    if not working["passed"] or not working["written"]:
        raise ValueError("固定稿件的统一前情记录生成失败")
    recap_path = project / "episodes/recap.yaml"
    recap_data = load_yaml(recap_path)
    recap_row = recap_data["episodes"][0]
    fingerprint = recap.inspect(project)["episodes"][0]["candidates"]["working"]
    recap_row["selected_basis"] = "working"
    recap_row["semantic_review"] = {
        "status": "completed", "reviewer": "selftest-fixture", "test_fixture_only": True,
        "note": "测试夹具：核对墙钟十二分钟差异、树影错觉与未解原因，不代表真实书目的语义审阅。",
        "ending_summary": "叙述者确认钟面晃动是树影造成的错觉。", "unresolved": [],
        "reviewed_file_sha256": fingerprint["file_sha256"],
        "reviewed_entry_sha256": fingerprint["entry_sha256"],
    }
    write_yaml(recap_path, recap_data)
    if not recap.check(project, required_episodes=[1])["passed"]:
        raise ValueError("固定稿件的统一前情表复核检查失败")
    checks.append("recap_working+fixture_semantic_review")
    if derive(project)["stage"] != "统一改稿":
        raise ValueError("固定稿件未推进到统一改稿阶段")
    write_yaml(project / "notes.yaml", {"workflow": {"season_review_status": "completed"},
                                        "test_fixture_only": True})
    final = draft.parent / "final.md"
    atomic_write(final, fixture.replace("episode: 1\n", "episode: 1\nstatus: final\n", 1))
    quality, quotes = lint(final), verify_quotes(final)
    if not quality["passed"] or not quotes["passed"]:
        raise ValueError("固定演示稿检查失败：" + "；".join((quality["errors"] + quotes["errors"])[:3]))
    if derive(project)["stage"] != "文案确认":
        raise ValueError("固定稿件未推进到文案确认阶段")
    test_script = approvals.record_confirmation(project, "script", "拍板文案", [1],
                                                 session="selftest-fixture", verify_transcript=False)
    before_snapshot = derive(project)
    if (not test_script["passed"] or before_snapshot["stage"] != "文案确认"
            or "定稿前情快照" not in before_snapshot["next_step"]):
        raise ValueError("临时文案确认后必须先保存统一前情定稿快照；不得直接进入声音")
    final_snapshot = recap.snapshot_final(project, 1)
    if (not final_snapshot["passed"] or not final_snapshot["review_carried"]
            or not recap.check(project, required_episodes=[1])["passed"]
            or derive(project)["stage"] != "声音"):
        raise ValueError("相同纯口播的测试定稿未能保留有效前情快照")
    checks.append("recap_final_snapshot+identical_spoken_review")
    write_yaml(project / "production/voice_cast.yaml", {"version": 1, "test_fixture_only": True,
                                                       "voices": {"narrator": "silent"}})
    checks.append("draft+lint+quotes+test_confirmation")

    preview = export_episode(final, preview=True)
    if preview["status"] != "preview_awaiting_human" or not (final.parent / "preview/index.html").is_file():
        raise ValueError("预览导出失败，或错误地宣称已获人工审阅")
    checks.append("export_preview")
    partial = produce.run(project, "ep01", test_mode=True, until="mix")
    if not partial["passed"]:
        raise ValueError(partial["summary"] + "：" + "；".join(partial.get("errors", [])))
    if partial["executed"] != list(produce.STAGES[:4]):
        raise ValueError("测试 produce --until mix 未按阶段生成静音声音资产")
    media = produce.run(project, "ep01", test_mode=True, until="render")
    if not media["passed"]:
        raise ValueError(media["summary"] + "：" + "；".join(media.get("errors", [])))
    if derive(project)["stage"] != "样片确认":
        raise ValueError("测试媒体未推进到样片确认阶段")
    repeated = produce.run(project, "ep01", test_mode=True, until="render")
    if repeated["executed"] or not produce.check(project, "ep01")["stages"][-1]["ready"]:
        raise ValueError("重复运行 produce 未跳过输入未变的已完成阶段")
    checks.append("produce_test_mode+resume+skip+ffprobe")
    test_sample = approvals.record_confirmation(project, "sample", "拍板样片", [1],
                                                 session="selftest-fixture", verify_transcript=False)
    if not test_sample["passed"] or derive(project)["stage"] != "成片确认":
        raise ValueError("临时样片测试记录未能推进到成片确认阶段")
    write_yaml(project / "release/compliance.yaml", {
        "test_fixture_only": True,
        "copyright": {"status": "fixture", "note": "仅用于隔离自检"},
        "ai_content_label": {"explicit_label": "fixture", "platform_setting": "fixture", "checked_rules": "fixture"},
        "likeness_and_assets": {"note": "仅用于隔离自检"},
        "platform": {"name": "fixture", "rules_checked_at": "fixture"}, "reviewer": "selftest-fixture"})
    test_release = approvals.record_confirmation(project, "release", "拍板成片", [1],
                                                  session="selftest-fixture", verify_transcript=False)
    if not test_release["passed"] or derive(project)["stage"] != "导出":
        raise ValueError("临时成片测试记录未能推进到导出阶段")
    checks.append("test_sample+test_release+export_stage")
    snapshot = {"draft_sha256": sha256_file(final), "source_generation": generation,
                "dependency_hashes": {}, "test_fixture_only": True}
    fact = final.parent / "review/fact_fixture.yaml"
    summary = final.parent / "review/summary_fixture.yaml"
    write_yaml(fact, {**snapshot, "findings": []})
    write_yaml(summary, {**snapshot, "findings": [], "reviewers": {
        "fact": {"status": "completed", "report": "review/fact_fixture.yaml"}}})
    if not recap.final_snapshot_state(project, [1])["passed"]:
        raise ValueError("隔离测试的定稿前情快照在正式导出前已失效")
    delivered = export_episode(final, preview=False, review_path=summary)
    if delivered["status"] != "delivered" or not (final.parent / "deliver/index.html").is_file():
        raise ValueError("测试正式格式交付包未生成")
    if derive(project)["stage"] != "归档":
        raise ValueError("正式格式交付后未推进到归档阶段")
    checks.append("fixture_fact_review+recap+formal_format_export")

    rehearsal = root / "archive-rehearsal" / "projects" / project.name
    shutil.copytree(project, rehearsal)
    cloud = root / "fake-onedrive"
    cloud.mkdir()
    pending = archive.run(rehearsal, 1, archive_root=cloud, upload_check=lambda _: False)
    video = rehearsal / "episodes/ep01/production/final.mp4"
    if pending["status"] != "awaiting_upload" or not video.is_file():
        raise ValueError("模拟未上传时错误删除了本地成片")
    complete = archive.run(rehearsal, 1, archive_root=cloud,
                           upload_check=lambda _: True, eviction_check=lambda _: True)
    if complete["status"] != "success" or video.exists():
        raise ValueError("模拟上传与仅在线完成后归档未结束")
    manifest = load_yaml(rehearsal / "archive/ep01.yaml")
    original_video_hash = next(row["sha256"] for row in manifest["files"]
                               if row["source"].endswith("production/final.mp4"))
    restored = archive.restore(rehearsal, 1, only="video")
    if str(video.resolve()) not in restored["restored"] or sha256_file(video) != original_video_hash:
        raise ValueError("模拟归档取回的成片哈希不一致")
    checks.append("fixture_archive_upload_guard+restore_hash")
    return {"status": "warning", "passed": True, "scope": "text_media_fixture", "test_fixture_only": True,
            "summary": f"隔离夹具从原文到正式格式交付、模拟归档与取回通过（{time.monotonic() - start:.2f} 秒）；真实付费适配器、人工审校、OneDrive 与发布仍未验收",
            "checks": checks, "probe": media["probe"],
            "next_actions": ["验证正式配音服务端接口与账单，接入正式音效适配器，并验证真实人工审校、OneDrive 上传/仅在线状态和发布"],
            "artifacts": [str(final.parent / "preview/index.html"),
                          str(final.parent / "deliver/index.html"),
                          str(final.parent / "production/final.mp4"),
                          str(final.parent / "production/subtitles.srt"),
                          str(final.parent / "production/manifest.json"),
                          str(rehearsal / "archive/ep01.yaml")] if keep_artifacts else []}


def run(workspace_root: Path | None = None) -> dict:
    """Use an ephemeral directory by default; explicit roots are for isolated tests only."""
    try:
        if workspace_root is None:
            with tempfile.TemporaryDirectory(prefix="bookflow-selftest-") as temporary:
                return _exercise(Path(temporary), keep_artifacts=False)
        return _exercise(Path(workspace_root).resolve(), keep_artifacts=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "error", "passed": False, "scope": "text_media_fixture", "test_fixture_only": True,
                "summary": f"全流程夹具自检失败：{exc}", "errors": [str(exc)],
                "checks": [], "next_actions": ["核对失败阶段和固定示范资料，换全新临时目录重跑；不要把测试确认记录用于真实项目"],
                "artifacts": []}
