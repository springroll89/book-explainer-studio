"""Derive one next action from project files and content approvals."""
from __future__ import annotations

import json
import re
from pathlib import Path
import yaml

from .approvals import confirmation_state
from .common import atomic_write, latest_draft, load_yaml, read_chapters, sha256_file, source_generation, write_json
from .planning import check_plan

STAGE_GUIDE = (
    {"name": "建项目", "actor": "助手", "completion": "项目配置有效，轻量 doctor 检查通过。"},
    {"name": "导入原文", "actor": "助手", "completion": "当前原文批次完整，抽查记录与批次指纹有效。"},
    {"name": "拆书", "actor": "助手", "completion": "逐章笔记、简报、人物与线索资料齐备，机器查漏和覆盖审阅通过。"},
    {"name": "分集", "actor": "助手", "completion": "分集计划存在且结构、覆盖和原文依据检查通过。"},
    {"name": "方案确认", "actor": "你", "completion": "当前方案交付物有有效的聊天确认记录。"},
    {"name": "全季初稿", "actor": "助手 / 任务队列", "completion": "计划集数均有通过稿件检查的初稿，实际工作前情已补齐并复核。"},
    {"name": "统一改稿", "actor": "你 + 助手", "completion": "项目统一改稿状态已标记完成；后续仍核对全季稿件和连续性。"},
    {"name": "文案确认", "actor": "你", "completion": "计划集数均有有效文案确认；启用统一前情表时，定稿快照也须有效；音色表含角色时，全季对白已标说话人且每个说话人都有确认音色。"},
    {"name": "声音", "actor": "助手 / 本地工具", "completion": "首集 cue、配音、音效绑定、混音与字幕阶段均通过清单检查。"},
    {"name": "画面", "actor": "助手 / 本地工具", "completion": "首集分镜、画面与渲染阶段均通过检查。"},
    {"name": "样片确认", "actor": "你", "completion": "首集带字幕样片有有效的聊天确认记录。"},
    {"name": "其余集制作", "actor": "助手 / 任务队列", "completion": "其余集数的声音和画面阶段均通过清单检查。"},
    {"name": "成片确认", "actor": "你", "completion": "计划集数均有当前成片的有效确认记录。"},
    {"name": "导出", "actor": "本地工具", "completion": "各集正式交付包已生成并通过索引检查。"},
    {"name": "归档", "actor": "本地工具 / 你", "completion": "归档副本、哈希清单与本机仅在线状态均核对完成。"},
)
STAGES = tuple(row["name"] for row in STAGE_GUIDE)
WORKFLOW_START = "<!-- BEGIN GENERATED FLOW OVERVIEW -->"
WORKFLOW_END = "<!-- END GENERATED FLOW OVERVIEW -->"


def render_workflow_overview() -> str:
    """Render the workflow stage overview from the same stage order used by next."""
    rows = [
        WORKFLOW_START,
        "## 阶段总览（由 `bookflow.flow` 生成）",
        "",
        "`next` 按项目文件给出当前唯一下一步；以下完成条件是阶段摘要，具体阻塞以命令输出为准。",
        "",
        "| # | 阶段 | 完成条件（摘要） | 执行者 |",
        "|---:|---|---|---|",
    ]
    rows.extend(
        f"| {index} | {stage['name']} | {stage['completion']} | {stage['actor']} |"
        for index, stage in enumerate(STAGE_GUIDE, 1)
    )
    rows.extend(["", WORKFLOW_END])
    return "\n".join(rows)
_MEDIA_IMPACT_LABELS = {
    "cues": "声音设计清单", "voice": "配音段落（未变部分按缓存复用）", "sfx": "音效",
    "mix": "混音", "subs": "字幕", "storyboard": "分镜", "images": "画面", "render": "成片",
}


def _planned(project: Path) -> list[int]:
    data = load_yaml(project / "plan/episodes.yaml", {}) or {}
    rows = data if isinstance(data, list) else data.get("episodes", [])
    return sorted({row["ep"] for row in rows if isinstance(row, dict) and type(row.get("ep")) is int})


def _legacy_episodes(project: Path, episodes: list[int]) -> set[int]:
    """Episodes whose adopted pre-pipeline media are still current need no new voicing."""
    from .legacy_media import MODE, state as legacy_state
    result = set()
    for ep in episodes:
        epdir = project / "episodes" / f"ep{ep:02d}"
        manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
        if isinstance(manifest, dict) and manifest.get("mode") == MODE and legacy_state(epdir, manifest)[0]:
            result.add(ep)
    return result


def _lesson_count(project: Path) -> int:
    if not (project / "project.yaml").is_file():
        return 0
    from .lessons import pending_count
    return pending_count(project)


def _media_ready(project: Path, ep: int, stage: str) -> bool:
    production = project / "episodes" / f"ep{ep:02d}" / "production"
    manifest = load_yaml(production / "manifest.json", {}) or {}
    completed = manifest.get("stages", {}) if isinstance(manifest, dict) else {}
    if not isinstance(completed, dict):
        return False
    needed = ("cues", "voice", "sfx", "mix", "subs") if stage == "audio" else ("storyboard", "images", "render")
    from .produce import _test_fixture
    if (isinstance(manifest, dict) and manifest.get("mode") == "test"
            and manifest.get("test_fixture_only") is True and _test_fixture(project)):
        archive = load_yaml(project / "archive" / f"ep{ep:02d}.yaml", {}) or {}
        if isinstance(archive, dict) and archive.get("status") == "complete":
            return all(isinstance(completed.get(name), dict) and completed[name].get("status") == "done"
                       for name in needed)
        from .produce import check as produce_check
        try:
            readiness = {row["stage"]: row["ready"] for row in produce_check(project, ep)["stages"]}
        except (OSError, ValueError, KeyError):
            return False
        return all(readiness.get(name) is True for name in needed)
    from .produce import check as produce_check
    try:
        readiness = {row["stage"]: row["ready"] for row in produce_check(project, ep)["stages"]}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return all(readiness.get(name) is True for name in needed)


def _downstream_impact(project: Path, episode: int, script_state: dict) -> str | None:
    relative = f"episodes/ep{episode:02d}/final.md"
    episodes_dir = project / "episodes"
    epdir = episodes_dir / f"ep{episode:02d}"
    production = epdir / "production"
    final = epdir / "final.md"
    manifest_path = production / "manifest.json"
    if any(path.is_symlink() for path in (episodes_dir, epdir, final, production, manifest_path)):
        return f"第{episode}集文案或媒体清单路径是符号链接，拒绝核验下游影响"
    if not manifest_path.is_file():
        return None
    changed = next((row for row in script_state.get("changed_files", [])
                    if row.get("file") == relative and row.get("reason") == "content_changed"), None)
    detail = changed.get("detail") if changed else None
    if not detail:
        from .approvals import _spoken_change, _spoken_snapshot, read_log
        approvals = [row for row in read_log(project) if row.get("gate") == "script"
                     and isinstance(row.get("episodes"), list) and episode in row["episodes"]]
        if approvals:
            latest = approvals[-1]
            prior_at = latest.get("prior_approval_at") if latest.get("action") == "carry" else None
            prior_rows = [row for row in approvals[:-1] if row.get("action") in {"approve", "carry"}]
            prior = (next((row for row in prior_rows if row.get("at") == prior_at), None)
                     if prior_at else prior_rows[-1] if prior_rows else None)
            old = prior.get("spoken_snapshots", {}).get(relative) if isinstance(prior, dict) else None
            if latest.get("action") == "revoke":
                current = _spoken_snapshot(project / relative)
            else:
                current = latest.get("spoken_snapshots", {}).get(relative)
            if isinstance(old, str) and isinstance(current, str) and old != current:
                detail = _spoken_change(old, current)
    if not detail:
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return f"第{episode}集媒体清单无法核验下游影响：episodes/ep{episode:02d}/production/manifest.json（{exc}）"
    records = manifest.get("stages") if isinstance(manifest, dict) else None
    if not isinstance(records, dict):
        return f"第{episode}集媒体清单缺少有效的 stages，无法核验下游影响"
    if not any(isinstance(records.get(stage), dict) and records[stage].get("status") == "done"
               for stage in _MEDIA_IMPACT_LABELS):
        return None
    from .produce import (DEPENDENCIES, LINKED_INPUTS, STAGES as MEDIA_STAGES,
                          _linked_inputs, _test_fixture, check as produce_check)
    completed = {stage for stage in MEDIA_STAGES
                 if isinstance(records.get(stage), dict) and records[stage].get("status") == "done"}
    if manifest.get("mode") == "test":
        if not _test_fixture(project):
            return f"第{episode}集媒体清单标为 test，但不是隔离夹具；拒绝读取媒体核验下游影响"
        try:
            readiness = {row["stage"]: row["ready"]
                         for row in produce_check(project, episode)["stages"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return f"第{episode}集测试媒体清单无法核验下游影响：{exc}"
        stale_stages = {stage for stage in completed if readiness.get(stage) is not True}
    elif manifest.get("mode") == "real":
        sidecar = final.with_suffix(".sentences.json")
        if final.is_symlink() or sidecar.is_symlink() or not final.is_file():
            return f"第{episode}集定稿或句子表路径无效，无法核验下游影响"
        small_inputs = {relative: sha256_file(final)}
        if sidecar.is_file():
            small_inputs[str(sidecar.relative_to(project))] = sha256_file(sidecar)
        stale_stages: set[str] = set()
        for stage in MEDIA_STAGES:
            if stage not in completed:
                continue
            record = records[stage]
            inputs = record.get("inputs")
            stale = not isinstance(inputs, list)
            if isinstance(inputs, list):
                paths = {row.get("path") for row in inputs if isinstance(row, dict)}
                if stage in {"cues", "voice"} and relative not in paths:
                    stale = True
                for row in inputs:
                    if not isinstance(row, dict):
                        stale = True
                        continue
                    path = row.get("path")
                    if path in small_inputs and row.get("sha256") != small_inputs[path]:
                        stale = True
            if any(dependency not in completed or dependency in stale_stages
                   for dependency in DEPENDENCIES.get(stage, ())):
                stale = True
            if not stale and stage in LINKED_INPUTS:
                try:
                    stale = not _linked_inputs(project, epdir, stage, record, records)
                except (OSError, ValueError, KeyError, TypeError):
                    stale = True
            if stale:
                stale_stages.add(stage)
    else:
        return f"第{episode}集媒体清单模式无效，无法核验下游影响"
    stale = [_MEDIA_IMPACT_LABELS[stage] for stage in MEDIA_STAGES if stage in stale_stages]
    if not stale:
        return None
    archive_dir = project / "archive"
    archive_path = archive_dir / f"ep{episode:02d}.yaml"
    try:
        if archive_dir.is_symlink() or archive_path.is_symlink():
            archive = {}
        else:
            archive = load_yaml(archive_path, {}) or {}
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        archive = {}
    restore = (f"；该集已归档，重做前先运行 archive restore <项目> ep{episode:02d}"
               if isinstance(archive, dict) and archive.get("status") == "complete" else "")
    return (f"第{episode}集文案变化（{detail}）；受影响的已完成下游阶段需核验/重做："
            + "、".join(stale) + restore)


def _compact(episodes: list[int]) -> str:
    """[2, 3, 4, 7] -> "2-4,7" for command arguments and prompts."""
    parts, start, prev = [], None, None
    for ep in sorted(set(episodes)):
        if start is None:
            start = prev = ep
        elif ep == prev + 1:
            prev = ep
        else:
            parts.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = ep
    if start is not None:
        parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(parts)


def _review_suffix(items: list[str]) -> str:
    return "；并一并过目：" + "；".join(items) if items else ""


def _pending_acknowledged(project: Path, item: dict) -> bool:
    """A later sample/release confirmation of the same episode covers a minor text edit."""
    ep, created = item.get("episode"), str(item.get("created_at") or "")
    if type(ep) is not int or not created:
        return False
    for gate in (("sample", "release") if ep == 1 else ("release",)):
        state = confirmation_state(project, gate, ep)
        if state["state"] == "passed" and str(state.get("at") or "") > created:
            return True
    return False


def derive(project: Path) -> dict:
    project = Path(project).resolve()
    from .doctor import check as doctor_check
    health = doctor_check(project, full=False)
    config = load_yaml(project / "project.yaml", {}) if health["passed"] else {}
    book = config.get("book", {}) if isinstance(config, dict) else {}
    name = book.get("title") if isinstance(book, dict) and book.get("title") else project.name
    try:
        lessons_pending = _lesson_count(project)
        lessons_error = ""
    except (ValueError, OSError) as exc:
        lessons_pending = 0
        lessons_error = str(exc)
    previous = load_yaml(project / "state.json", {}) or {}
    if not isinstance(previous, dict):
        previous = {}
    prior_index = previous.get("stage_index") if type(previous.get("stage_index")) is int else None
    result = {"project": str(project), "book": name, "stage": STAGES[0], "stage_index": 1,
              "next_step": "", "actor": "助手", "needs_you": [], "change_pending": [],
              "downstream_impacts": [],
              "blockers": [], "queue": {}, "lessons_pending": lessons_pending, "lesson_blocked": False,
              "status": "success", "summary": "", "next_actions": [],
              "artifacts": [str(project / "state.json"), str(project / "进度.md")]}
    job_cards = []
    if health["passed"]:
        from .jobs import summary as jobs_summary
        queue = jobs_summary(project)
        result["queue"] = queue["counts"]
        result["needs_you"].extend(queue["needs_you"])
        job_cards = queue["cards"]

    def queued_instruction(stage: str, ep: int, default: str) -> str:
        matching = next((card for card in job_cards if card.get("stage") == stage and card.get("episode") == ep), None)
        if matching is None:
            return default
        status = matching.get("status")
        if status == "todo":
            return f"领取 {matching['id']}：jobs claim <项目> --stage {stage} --session <会话ID>"
        if status == "doing":
            return f"继续 {matching['id']}，完成后运行 jobs done <项目> {matching['id']} --session <会话ID>"
        if status in ("failed", "needs_you"):
            return f"先处理 {matching['id']} 的 {status} 与任务卡说明"
        return default

    def step(index: int, instruction: str, actor: str = "助手", *, blocker: str | None = None,
             needs_you: str | None = None) -> dict:
        if (lessons_pending and index >= 3 and not result["blockers"]
                and (prior_index != index or previous.get("lesson_blocked") is True)):
            instruction = f"处理收件箱中 {lessons_pending} 条未分诊教训，运行 lessons triage"
            actor = "助手"
            blocker = f"有 {lessons_pending} 条 new 教训，阶段交界暂停"
            needs_you = None
            result["lesson_blocked"] = True
        result.update(stage=STAGES[index - 1], stage_index=index, next_step=instruction, actor=actor,
                      status="warning" if blocker or needs_you or result["needs_you"] else "success", summary=instruction,
                      next_actions=[instruction])
        if blocker:
            result["blockers"].append(blocker)
        if needs_you:
            result["needs_you"].append(needs_you)
        return result

    if lessons_error:
        return step(1, "修复工作室教训收件箱后重跑 next", blocker=lessons_error)
    if not health["passed"]:
        return step(1, health["next_actions"][0] if health["next_actions"] else "修复项目配置后重跑 doctor",
                    blocker=health["errors"][0])
    planned = _planned(project)
    script_states = {ep: confirmation_state(project, "script", ep) for ep in planned}
    pending_by_ep: dict[int, list[str]] = {}
    for ep in planned:
        script_state = script_states[ep]
        for item in script_state.get("change_pending_items", []):
            if not _pending_acknowledged(project, item):
                pending_by_ep.setdefault(ep, []).append(item["text"])
        result["change_pending"].extend(pending_by_ep.get(ep, []))
        impact = _downstream_impact(project, ep, script_state)
        if impact:
            result["downstream_impacts"].append(impact)
    try:
        generation = source_generation(project)
    except (ValueError, OSError):
        generation = ""
    source_dir = project / "source/imports" / generation if re.fullmatch(r"[a-f0-9]{16,64}", generation or "") else None
    if source_dir is None or any(not (source_dir / name).is_file()
                                 for name in ("paragraphs.jsonl", "chapters.json", "manifest.json")):
        return step(2, "修复或重新导入原文批次，再运行 next", blocker="source/current.json 未指向完整的导入批次")
    try:
        chapters = read_chapters(project)
        manifest = load_yaml(source_dir / "manifest.json", {}) or {}
        valid_source = isinstance(chapters, list) and bool(chapters) and manifest.get("generation") == generation
    except (ValueError, OSError, KeyError):
        valid_source = False
        chapters = []
    if not valid_source:
        return step(2, "核对 source/imports 下的原文段落与章节清单", blocker="原文 manifest.json 或 chapters.json 无效")
    validation_path = project / "analysis/source_validation.json"
    validation = load_yaml(validation_path, {}) or {}
    if not isinstance(validation, dict):
        validation = {}
    checks = validation.get("checks", {})
    if (validation.get("source_generation") != generation or not isinstance(checks, dict)
            or not checks or any(value is not True for value in checks.values())):
        return step(2, "完成原文逐章抽查并记录验证结果", blocker="analysis/source_validation.json 缺失、版本不符或仍有未通过检查")
    intake = project / "analysis/intake_report.md"
    if not intake.is_file() or not intake.read_text(encoding="utf-8").strip():
        return step(2, "记录原文抽查范围与结果", blocker="缺少非空的 analysis/intake_report.md")
    analysis = project / "analysis"
    essential = [analysis / name for name in ("book_brief.md", "characters.yaml", "threads.yaml", "coverage_machine.json", "coverage_review.yaml")]
    missing = [str(path.relative_to(project)) for path in essential if not path.is_file()]
    if missing:
        return step(3, "完成全书精读和覆盖审计", blocker="缺少：" + "、".join(missing))
    if not (analysis / "book_brief.md").read_text(encoding="utf-8").strip():
        return step(3, "补全全书简报", blocker="analysis/book_brief.md 为空")
    for chapter in chapters:
        note = analysis / "chapter_notes" / f"{chapter['id']}.md"
        if not note.is_file():
            return step(3, f"补写 {chapter['id']} 的逐章笔记", blocker=f"缺少 {note.relative_to(project)}")
        cited = set(re.findall(r"(?<![\w])p\d{5,}(?![\w])", note.read_text(encoding="utf-8")))
        if not cited.intersection(chapter.get("paragraphs", [])):
            return step(3, f"补充 {chapter['id']} 本章原文依据", blocker=f"{note.relative_to(project)} 未引用本章段落")
    machine_path = analysis / "coverage_machine.json"
    machine = load_yaml(machine_path, {}) or {}
    if not isinstance(machine, dict):
        machine = {}
    if machine.get("generation") != generation or machine.get("errors"):
        problems = machine.get("errors", [])
        detail = "；".join(str(item) for item in problems[:3]) if isinstance(problems, list) else "报告格式无效"
        return step(3, "修复查漏错误并重新生成覆盖报告", blocker=f"analysis/coverage_machine.json：{detail or '原文版本不符'}")
    review = load_yaml(analysis / "coverage_review.yaml", {}) or {}
    if (not isinstance(review, dict) or not str(review.get("status", "")).startswith("completed")
            or review.get("source_generation") != generation or review.get("unresolved")
            or (review.get("machine_report_sha256") and review["machine_report_sha256"] != sha256_file(machine_path))):
        return step(3, "完成独立覆盖审阅并解决未结项", blocker="analysis/coverage_review.yaml 未完成或与当前查漏报告不一致")
    if not planned:
        return step(4, "编写并检查分集计划", blocker="plan/episodes.yaml 没有有效集数")
    try:
        plan_errors = check_plan(project)["errors"]
    except (ValueError, OSError, KeyError, TypeError) as exc:
        plan_errors = [str(exc)]
    if plan_errors:
        return step(4, "修复分集计划错误后重新运行 next", blocker="plan/episodes.yaml：" + "；".join(plan_errors[:3]))
    plan_state = confirmation_state(project, "plan")
    if plan_state["state"] != "passed":
        if plan_state["state"] == "invalidated":
            result["blockers"] += ["方案交付物变动：" + "、".join(x["file"] for x in plan_state["changed_files"])]
        return step(5, "请你阅读方案并回复“拍板方案”", "你", needs_you="方案确认")
    for ep in planned:
        draft = latest_draft(project / "episodes" / f"ep{ep:02d}")
        if not draft:
            return step(6, queued_instruction("draft", ep, f"完成第 {ep} 集初稿，并更新实际前情"))
    from .text_checks import check_drafts
    draft_checks = check_drafts(project, planned)
    if not draft_checks["passed"]:
        return step(6, "修复当前初稿的稿件或原文依据错误后重跑 next", blocker=draft_checks["errors"][0])
    recap_path = project / "episodes/recap.yaml"
    if recap_path.exists() or recap_path.is_symlink():
        from .recap import check as recap_check
        recap = recap_check(project, required_episodes=planned)
        if not recap["passed"]:
            return step(6, "逐集核对统一前情表并完成语义复核", blocker=recap["errors"][0])
    else:
        from .continuity import context as continuity_context
        try:
            continuity = continuity_context(project, max(planned) + 1)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return step(6, "核对全季工作前情并重新运行 continuity context", blocker=str(exc))
        if not continuity["complete"]:
            detail = continuity["errors"][0] if continuity["errors"] else "前序工作稿依赖尚未补齐"
            return step(6, "按全季实际稿件补齐并复核工作前情", blocker=detail)
    notes = load_yaml(project / "notes.yaml", {}) or {}
    reviewed = (notes.get("workflow", {}) or {}).get("season_review_status") == "completed"
    if not reviewed:
        return step(7, "请你统一修改全季文案；改完后告诉我“改完了”", "你", needs_you="统一改稿")
    missing_final = [ep for ep in planned if not (project / "episodes" / f"ep{ep:02d}" / "final.md").is_file()]
    if missing_final:
        eps = _compact(missing_final)
        return step(8, f"按你认可的版本冻结第 {eps} 集定稿：final freeze <项目> --eps {eps}"
                       "（默认取各集最新草稿；冻结不等于拍板）")
    pending = [ep for ep in planned if script_states[ep]["state"] != "passed"]
    if pending:
        invalidated = [ep for ep in pending if script_states[ep]["state"] == "invalidated"]
        for ep in invalidated:
            state = script_states[ep]
            if not state.get("change_pending"):
                result["blockers"].append(f"第 {ep} 集文案变化：" + "；".join(
                    f"{item['file']}（{item.get('detail', item['reason'])}）" for item in state["changed_files"]))
        span = "第 " + "、".join(str(ep) for ep in pending) + " 集"
        review = "；并一并过目：" + "；".join(result["change_pending"]) if result["change_pending"] else ""
        return step(8, f"请你阅读{span}文案{review}并回复“拍板文案”", "你", needs_you=f"文案确认：{span}")
    if recap_path.exists() or recap_path.is_symlink():
        from .recap import final_snapshot_state
        snapshots = final_snapshot_state(project, planned)
        if not snapshots["passed"]:
            return step(8, "保存并核对已确认文案的定稿前情快照", blocker=snapshots["errors"][0])
    from . import voice_script
    if voice_script.required(project):
        voices = voice_script.season(project, planned, skip=_legacy_episodes(project, planned))
        unlabelled = voices["missing_scripts"] + voices["invalid_scripts"]
        if unlabelled:
            eps = _compact(unlabelled)
            return step(8, f"为第 {eps} 集标注对白说话人：voices scaffold <项目> --eps {eps}，"
                           "逐条填写人物 ID 或 narrator 后运行 voices check（可两三集一个会话）")
        if voices["needs_voice"]:
            names = "、".join(f"{row['name'] or row['id']}（{row['id']}，第 {_compact(row['episodes'])} 集共 {row['lines']} 句）"
                              for row in voices["needs_voice"])
            return step(8, f"请为这些说话人确定音色：{names}；你回复音色后由助手用 voices set 写入音色表",
                        "你", needs_you="角色音色")
    first = planned[0]
    if not _media_ready(project, first, "audio"):
        return step(9, queued_instruction("audio", first, f"制作第 {first} 集声音与字幕"))
    if not _media_ready(project, first, "visual"):
        return step(10, queued_instruction("visual", first, f"制作第 {first} 集分镜、画面和成片"))
    sample = confirmation_state(project, "sample", first)
    if sample["state"] != "passed":
        return step(11, f"请你观看第一集带字幕样片{_review_suffix(pending_by_ep.get(first, []))}并回复“拍板样片”",
                    "你", needs_you="样片确认")
    for ep in planned[1:]:
        if not (_media_ready(project, ep, "audio") and _media_ready(project, ep, "visual")):
            stage = "audio" if not _media_ready(project, ep, "audio") else "visual"
            return step(12, queued_instruction(stage, ep, f"制作第 {ep} 集音画与成片"))
    for ep in planned:
        if confirmation_state(project, "release", ep)["state"] != "passed":
            return step(13, f"请你确认第 {ep} 集成片{_review_suffix(pending_by_ep.get(ep, []))}并回复“拍板成片”",
                        "你", needs_you="成片确认")
        if not (project / "episodes" / f"ep{ep:02d}" / "deliver/index.html").is_file():
            return step(14, f"导出第 {ep} 集已确认的交付包", "脚本")
        from .archive import check_complete
        archive_state = check_complete(project, ep)
        if archive_state["pending"]:
            return step(15, f"归档第 {ep} 集：archive <项目> ep{ep:02d}", "脚本")
        if not archive_state["passed"]:
            return step(15, f"核对并修复第 {ep} 集归档清单", "脚本", blocker=archive_state["errors"][0])
    return step(15, "全部已确认集数的归档完成，无待办", "脚本")


def write(project: Path) -> dict:
    project = Path(project).resolve()
    from .versioning import touch
    from .approvals import reconcile_imported_edits
    touch(project)
    reconcile_imported_edits(project, _planned(project))
    state = derive(project)
    write_json(project / "state.json", state)
    lines = [f"# 《{state['book']}》进度", "", f"当前阶段：{state['stage']}（{state['stage_index']}/{len(STAGES)}）",
             f"下一步：{state['next_step']}（{state['actor']}）", "",
             "需要你：" + ("；".join(state["needs_you"]) if state["needs_you"] else "无"),
             "改动待过目：" + ("；".join(state["change_pending"]) if state["change_pending"] else "无"),
             "下游影响：" + ("；".join(state["downstream_impacts"]) if state["downstream_impacts"] else "无"),
             "阻塞：" + ("；".join(state["blockers"]) if state["blockers"] else "无"),
             "任务队列：" + (str(state["queue"]) if state["queue"] else "无"),
             f"教训收件箱：{state['lessons_pending']} 条待分诊", ""]
    atomic_write(project / "进度.md", "\n".join(lines))
    return state
