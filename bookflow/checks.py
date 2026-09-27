"""Run the deterministic checks for the stage reported by next."""
from __future__ import annotations

from pathlib import Path

import yaml

from .common import latest_draft, load_config, load_yaml, sha256_file
from .doctor import check as doctor_check
from .flow import _planned, derive


def run(project: Path) -> dict:
    project = Path(project).resolve()
    checks: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    artifacts: list[str] = []

    def add(name: str, problems: list[str], notices: list[str] | None = None) -> None:
        notices = notices or []
        checks.append({"id": name, "passed": not problems, "errors": problems, "warnings": notices})
        errors.extend(f"{name}：{message}" for message in problems)
        warnings.extend(f"{name}：{message}" for message in notices)

    health = doctor_check(project, full=False)
    add("doctor", health["errors"], health["warnings"])
    if not health["passed"]:
        return {"status": "error", "passed": False, "project": str(project), "stage": "建项目",
                "checks": checks, "errors": errors, "warnings": warnings, "needs_you": [],
                "next_step": health["next_actions"][0] if health["next_actions"] else "修复项目配置",
                "artifacts": artifacts}

    state = derive(project)
    stage = state["stage_index"]
    episodes = _planned(project) if stage >= 4 else []
    if stage == 3:
        from .planning import coverage
        try:
            report = coverage(project)
            add("coverage", report["errors"], report["warnings"])
            artifacts.append(str(project / "analysis/coverage_machine.json"))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            add("coverage", [str(exc)])
    elif stage >= 4:
        from .planning import check_plan
        try:
            plan = check_plan(project)
            add("plan", plan["errors"], plan["warnings"])
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            add("plan", [str(exc)])

    if 6 <= stage <= 8:
        from .names import check_project
        from .text_checks import check_drafts
        add("drafts", check_drafts(project, episodes)["errors"])
        try:
            add("names", check_project(project)["errors"])
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            add("names", [str(exc)])
        recap_path = project / "episodes/recap.yaml"
        if recap_path.exists() or recap_path.is_symlink():
            from .recap import check as recap_check, final_snapshot_state
            recap = (final_snapshot_state(project, episodes) if stage == 8
                     else recap_check(project, required_episodes=episodes))
            add("recap", recap["errors"])
        elif episodes:
            from .continuity import context
            try:
                continuity = context(project, max(episodes) + 1)
                problems = list(continuity.get("errors", []))
                if not continuity.get("complete", continuity.get("passed", False)) and not problems:
                    problems.append("前序工作稿依赖尚未补齐")
                add("continuity_legacy", problems)
            except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                add("continuity_legacy", [str(exc)])
        if load_config(project).get("profile") == "story" and episodes:
            from .quality import audit_story_season
            try:
                story = audit_story_season(project, prefer_final=stage == 8)
                add("story", story["errors"], story["warnings"])
            except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                add("story", [str(exc)])
        if stage in (6, 7):
            from .review import evaluate
            for ep in episodes:
                draft = latest_draft(project / "episodes" / f"ep{ep:02d}")
                if draft is None:
                    continue
                reports = sorted((draft.parent / "review").glob("summary*.yaml"))
                if not reports:
                    add(f"review_ep{ep:02d}", [], ["尚无当前初稿的独立审校汇总；后续定稿前仍须审校"])
                    continue
                try:
                    evaluations = [(path, evaluate(project, ep, draft, load_yaml(path, {}))) for path in reports]
                    passed = next(((path, result) for path, result in evaluations if result["passed"]), None)
                    if passed:
                        add(f"review_ep{ep:02d}", [], passed[1]["warnings"])
                    else:
                        path, result = evaluations[-1]
                        add(f"review_ep{ep:02d}", [f"{path.relative_to(project)}：{message}"
                                                      for message in result["errors"]])
                except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                    add(f"review_ep{ep:02d}", [str(exc)])
        if stage == 8:
            from .quality import lint, verify_quotes
            from .review import evaluate
            for ep in episodes:
                final = project / "episodes" / f"ep{ep:02d}" / "final.md"
                if not final.is_file():
                    add(f"final_ep{ep:02d}", [f"{final.relative_to(project)} 不存在"])
                    continue
                try:
                    script, quotes = lint(final), verify_quotes(final)
                    problems = [f"{final.relative_to(project)}：{item['message']}" for item in script["items"]
                                if item["level"] == "error"]
                    problems += [f"{final.relative_to(project)}：{message}" for message in quotes["errors"]]
                    add(f"final_ep{ep:02d}", problems)
                    reports = sorted((final.parent / "review").glob("summary*.yaml"))
                    if not reports:
                        add(f"review_ep{ep:02d}", ["缺少当前定稿的独立审校汇总"])
                        continue
                    evaluations = [(path, evaluate(project, ep, final, load_yaml(path, {}))) for path in reports]
                    passed = next(((path, result) for path, result in evaluations if result["passed"]), None)
                    if passed:
                        add(f"review_ep{ep:02d}", [], passed[1]["warnings"])
                    else:
                        path, result = evaluations[-1]
                        add(f"review_ep{ep:02d}", [f"{path.relative_to(project)}：{message}"
                                                      for message in result["errors"]])
                except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                    add(f"final_ep{ep:02d}", [str(exc)])

    if 9 <= stage <= 14:
        from .produce import check as media_check
        targets = episodes[:1] if stage in (9, 10, 11) else episodes
        until = "subs" if stage == 9 else "render"
        for ep in targets:
            try:
                archived = load_yaml(project / f"archive/ep{ep:02d}.yaml", {}) or {}
                if isinstance(archived, dict) and archived.get("status") == "complete":
                    continue  # Archived media is checked below without hydrating online-only files.
                media = media_check(project, ep, until=until)
                problems = [f"第 {ep} 集 {row['stage']}：{row['reason']}" for row in media["stages"]
                            if not row["ready"]]
                problems += [f"第 {ep} 集费用：{item}" for item in media["budget"]["blockers"]]
                add(f"media_ep{ep:02d}", problems)
            except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                add(f"media_ep{ep:02d}", [str(exc)])
    if stage >= 14:
        missing = []
        for ep in episodes:
            folder = project / f"episodes/ep{ep:02d}"
            package = folder / "deliver"
            required = ("index.html", "voiceover.txt", "listener_input.txt", "shotlist.md", "qa_summary.md")
            for name in required:
                path = package / name
                if not path.is_file() or not path.stat().st_size:
                    missing.append(f"{path.relative_to(project)} 缺失或为空")
            final = folder / "final.md"
            qa = package / "qa_summary.md"
            if not final.is_file():
                missing.append(f"{final.relative_to(project)} 不存在")
            elif qa.is_file():
                try:
                    if sha256_file(final) not in qa.read_text(encoding="utf-8"):
                        missing.append(f"{qa.relative_to(project)} 未绑定当前 final.md 指纹")
                except (OSError, UnicodeError) as exc:
                    missing.append(f"{qa.relative_to(project)} 无法核对：{exc}")
        add("deliver", missing)
    if stage >= 9:
        from .archive import check_complete
        pending = []
        inspected = False
        for ep in episodes:
            relative = f"archive/ep{ep:02d}.yaml"
            try:
                manifest = load_yaml(project / relative, {}) or {}
                if stage != 15 and (not isinstance(manifest, dict) or manifest.get("status") != "complete"):
                    continue
                inspected = True
                pending.extend(check_complete(project, ep)["errors"])
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
                pending.append(f"{relative} 无法读取：{exc}")
        if inspected or pending:
            add("archive", pending, [] if pending else ["只读核对目标路径和大小；不会下载仅在线文件重算哈希"])

    current = derive(project)
    if current["stage_index"] == stage and current["blockers"]:
        add("next", [message for message in current["blockers"] if not any(message in error for error in errors)])
    needs_you = current["needs_you"]
    return {"status": "error" if errors else "warning" if needs_you or warnings else "success",
            "passed": not errors, "project": str(project), "stage": state["stage"],
            "checks": checks, "errors": errors, "warnings": warnings,
            "needs_you": needs_you, "next_step": current["next_step"], "artifacts": artifacts}
