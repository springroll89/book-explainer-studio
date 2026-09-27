"""Check review evidence against the exact draft and planned core takeaways."""

from pathlib import Path

import yaml

from .common import full_season_review, load_config, load_yaml, parse_draft, sha256_file, source_generation


def required_roles(config: dict) -> tuple[str, ...]:
    """Facts are mandatory; separate listening/style review is explicit opt-in."""
    review = config.get("review", {})
    return ("fact",) + tuple(role for role in ("listener", "deai")
                             if review.get(f"{role}_enabled") is True)


def _episodes(project: Path) -> list:
    plan = load_yaml(project / "plan" / "episodes.yaml", default={})
    episodes = plan if isinstance(plan, list) else (plan or {}).get("episodes", [])
    if not isinstance(episodes, list):
        raise ValueError("分集计划的 episodes 必须是列表。")
    return episodes


def _snapshot_errors(project: Path, ep: int, draft_hash: str, generation, report: dict, label: str, working_dependencies: dict | None = None) -> list[str]:
    errors = []
    if report.get("draft_sha256") != draft_hash:
        errors.append(f"{label}缺少当前稿件指纹，或对应的是其他版本。")
    if not generation or report.get("source_generation") != generation:
        errors.append(f"{label}的原文版本缺失或已过期。")
    dependencies = report.get("dependency_hashes")
    if not isinstance(dependencies, dict):
        errors.append(f"{label}的 dependency_hashes 必须是对象，首集也需填写空对象。")
        dependencies = {}
    for previous in range(1, ep):
        final = project / "episodes" / f"ep{previous:02d}" / "final.md"
        saved = dependencies.get(str(previous), dependencies.get(previous))
        if working_dependencies is not None:
            if not saved or saved != working_dependencies.get(previous):
                errors.append(f"{label}未基于第 {previous} 集当前有效工作稿完成，须重新审校。")
        elif not final.is_file() or not saved or saved != sha256_file(final):
            errors.append(f"{label}未基于第 {previous} 集当前定稿完成，须重新审校。")
    return errors


def align_hooks(parsed: dict, listener: dict, cfg: dict) -> dict:
    """Match blind listener responses to sentence IDs, independent of timing."""
    sentences = {s["id"]: i for i, s in enumerate(parsed.get("sentences", []))}
    sentence_text = {s["id"]: s["text"] for s in parsed.get("sentences", [])}
    hook_ids = list(dict.fromkeys(h.get("sentence_id") for h in parsed.get("hooks", [])))
    invalid_hooks = [sid for sid in hook_ids if sid not in sentences]
    hooks = [sid for sid in hook_ids if sid in sentences]
    engaged, dropoff, invalid_responses = {}, {}, []
    for field, target in (("engaged", engaged), ("dropoff", dropoff)):
        for item in listener.get(field, []) or []:
            sid = item.get("sentence_id") if isinstance(item, dict) else None
            if sid not in sentences:
                invalid_responses.append({"field": field, "sentence_id": sid, "reason": "unknown_sentence"})
            elif item.get("trigger") != sentence_text[sid]:
                invalid_responses.append({"field": field, "sentence_id": sid, "reason": "trigger_mismatch"})
            else:
                target[sid] = item.get("why", "")
    matched = [{"sentence_id": sid, "why": engaged[sid]} for sid in hooks if sid in engaged]
    review_cfg = cfg.get("review", {})
    threshold = review_cfg.get("hook_fulfil_min", cfg.get("lint", {}).get("hook_fulfil_min", 0.6))
    drop_window = review_cfg.get("hook_dropoff_window_sentences", 1)
    failed = []
    for sid, why in dropoff.items():
        preceding = [h for h in hooks if 0 <= sentences[sid] - sentences[h] <= drop_window]
        if preceding:
            failed.append({"hook_sentence_id": preceding[-1], "sentence_id": sid, "why": why})
    return {
        "hooks": hooks,
        "matched": matched,
        "fake": [sid for sid in hooks if sid not in engaged],
        "organic": [{"sentence_id": sid, "why": why} for sid, why in engaged.items() if sid not in hooks],
        "failed": failed,
        "fulfil_rate": len(matched) / len(hooks) if hooks else 0.0,
        "threshold": threshold,
        "invalid_hooks": invalid_hooks,
        "invalid_responses": invalid_responses,
    }


def evaluate(project: Path, ep: int, draft: Path, report: dict) -> dict:
    """Return validation errors; never treat a review of an older draft as current."""
    project, draft = Path(project), Path(draft)
    errors, warnings = [], []
    result = {"passed": False, "errors": errors, "warnings": warnings}
    if not isinstance(report, dict):
        errors.append("审校报告必须是对象。")
        return result
    if not draft.is_file():
        errors.append(f"稿件不存在：{draft}")
        return result
    draft_hash = sha256_file(draft)
    generation = source_generation(project)
    cfg = load_config(project)
    story = cfg.get("profile") == "story"
    working_dependencies = None
    if full_season_review(project) and draft.name != 'final.md' and parse_draft(draft.read_text(encoding='utf-8'))['meta'].get('status') != 'final':
        recap_path = project / "episodes/recap.yaml"
        if recap_path.exists() or recap_path.is_symlink():
            from .recap import context as recap_context
            continuity = recap_context(project, ep)
            working_dependencies = {e["ep"]: e["file_sha256"] for e in continuity["episodes"]}
            warnings.append('本次为全季文字草稿审校，使用已复核的统一前情表；不代表文案已获确认。')
        else:
            from .continuity import context
            continuity = context(project, ep)
            working_dependencies = {e['ep']: e.get('final_sha256') if e['basis']=='ledger' else e.get('draft_sha256') for e in continuity['episodes']}
            warnings.append('本次为全季文字草稿审校，使用有效工作连续性；不代表文案已确认。')
        errors.extend(continuity['errors'])
        result['review_mode'] = 'full_season_draft'
    errors.extend(_snapshot_errors(project, ep, draft_hash, generation, report, "审校汇总", working_dependencies))
    try:
        from .sentences import parse_draft_file
        parsed = parse_draft_file(draft)
        plan_ep = next((e for e in _episodes(project) if isinstance(e, dict) and e.get("ep") == ep), None)
    except (ValueError, yaml.YAMLError) as exc:
        errors.append(f"稿件或计划格式无效：{exc}")
        return result
    if plan_ep is None:
        errors.append(f"分集计划缺少第 {ep} 集，无法核对核心认知。")
        core_ids = []
    else:
        takeaways = plan_ep.get("takeaways", []) or []
        core = [t for t in takeaways if isinstance(t, dict) and t.get("core", True)]
        core_ids = [t.get("id") for t in core]
        if not story and (not core_ids or any(not isinstance(tid, str) or not tid.strip() for tid in core_ids)):
            errors.append("本集计划必须列出带 id 的核心 takeaways。")
        core_ids = [tid for tid in core_ids if isinstance(tid, str) and tid.strip()]
        if len(core_ids) != len(set(core_ids)):
            errors.append("分集计划存在重复的核心 takeaway id。")
    evidence = {}
    evidence_items = report.get("core_takeaways", [])
    if not isinstance(evidence_items, list):
        errors.append("core_takeaways 必须是列表。")
        evidence_items = []
    for item in evidence_items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append("核心认知审校条目缺少有效 id。")
            continue
        if item["id"] in evidence:
            errors.append(f"核心认知审校条目重复：{item['id']}")
        evidence[item["id"]] = item
    if not story:
        for tid in core_ids:
            item = evidence.get(tid, {})
            if item.get("delivered") is not True or item.get("supported") is not True:
                errors.append(f"核心认知 {tid} 未证明已讲出且有原文依据。")
    reviewers = report.get("reviewers", {})
    role_reports, role_artifacts = {}, {}
    supplied_roles = tuple(role for role in ("listener", "deai") if isinstance(reviewers, dict) and role in reviewers)
    roles_to_check = tuple(dict.fromkeys((*required_roles(cfg), *supplied_roles)))
    for role in roles_to_check:
        record = reviewers.get(role) if isinstance(reviewers, dict) else None
        if not isinstance(record, dict) or record.get("status") != "completed":
            errors.append(f"缺少独立审稿角色记录：{role}")
            continue
        if role == "listener" and record.get("independent") is not True:
            errors.append("listener 必须在独立上下文中审稿。")
        report_name = record.get("report")
        if not isinstance(report_name, str) or not report_name.strip():
            errors.append(f"缺少 {role} 的原始审校报告路径。")
            continue
        role_path = Path(report_name)
        if not role_path.is_absolute():
            role_path = draft.parent / role_path
        if not role_path.is_file():
            errors.append(f"{role} 的原始审校报告不存在：{role_path}")
            continue
        role_hash = sha256_file(role_path)
        if record.get("sha256") and record["sha256"] != role_hash:
            errors.append(f"{role} 的原始审校报告指纹不一致。")
        try:
            original = load_yaml(role_path, default={})
        except yaml.YAMLError as exc:
            errors.append(f"{role} 原始报告格式无效：{exc}")
            continue
        if not isinstance(original, dict):
            errors.append(f"{role} 原始报告必须是对象。")
            continue
        role_reports[role] = original
        role_artifacts[role] = {"path": str(role_path.resolve()), "sha256": role_hash}
        errors.extend(_snapshot_errors(project, ep, draft_hash, generation, original, f"{role} 原始报告", working_dependencies))
    result["role_reports"] = role_artifacts
    if not story:
        fact_evidence = role_reports.get("fact", {}).get("core_takeaways")
        if not isinstance(fact_evidence, list):
            errors.append("fact 原始报告缺少 core_takeaways 核验。")
            fact_evidence = []
        for tid in core_ids:
            items = [item for item in fact_evidence if isinstance(item, dict) and item.get("id") == tid]
            if len(items) != 1 or items[0].get("delivered") is not True or items[0].get("supported") is not True:
                errors.append(f"fact 原始报告未证实核心认知 {tid} 已讲出且有原文依据。")
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        errors.append("findings 必须是列表。")
        findings = []
    findings = list(findings)
    for role, original in role_reports.items():
        original_findings = original.get("findings", [])
        if role in {"fact", "deai"} and "findings" not in original:
            errors.append(f"{role} 原始报告缺少 findings 列表。")
        if not isinstance(original_findings, list):
            errors.append(f"{role} 原始报告的 findings 必须是列表。")
        else:
            findings.extend(original_findings)
    unresolved_p1 = 0
    seen_findings = set()
    for item in findings:
        if not isinstance(item, dict) or item.get("severity") not in {"P0", "P1", "P2"}:
            errors.append("发现无效审校问题条目或严重程度。")
            continue
        if item.get("resolved") is True:
            continue
        severity = item["severity"]
        identity = tuple(str(item.get(key, "")) for key in ("id", "severity", "category", "message"))
        if identity in seen_findings:
            continue
        seen_findings.add(identity)
        message = f"{item.get('id', '?')}：{item.get('message', '未说明问题')}"
        if severity == "P0" or (not story and item.get("category") == "depth" and severity == "P1"):
            errors.append(f"尚未解决的 {severity} 问题：{message}")
        else:
            warnings.append(f"尚未解决的 {severity} 问题：{message}")
        if severity == "P1":
            unresolved_p1 += 1
    limit = cfg.get("review", {}).get("max_p1", cfg.get("review", {}).get("max_unresolved_p1", 3))
    if unresolved_p1 > limit:
        errors.append(f"未解决 P1 共 {unresolved_p1} 条，超过上限 {limit}。")
    if "listener" in roles_to_check:
        listener_role = role_reports.get("listener", {})
        listener = listener_role.get("listener", listener_role if "engaged" in listener_role else None)
        if not isinstance(listener, dict):
            errors.append("listener 原始报告缺少独立听感内容。")
        else:
            summary_listener = report.get("listener")
            if not isinstance(summary_listener, dict) or any(summary_listener.get(key) != listener.get(key) for key in ("engaged", "dropoff", "takeaway")):
                errors.append("审校汇总的 listener 内容与原始独立报告不一致。")
            alignment = align_hooks(parsed, listener, cfg)
            result["hook_alignment"] = alignment
            if alignment["invalid_hooks"] or alignment["invalid_responses"]:
                errors.append("钩子或观众反应的句子 id 无效，或触发原句与当前稿件不一致。")
            if not story and (not alignment["hooks"] or alignment["fulfil_rate"] < alignment["threshold"]):
                errors.append("钩子与独立观众反应的匹配率未达标。")
            if alignment["failed"]:
                warnings.append("存在钩子后立即失去兴趣的观众反馈，需在终审中说明。")
            if not str(listener.get("takeaway", "")).strip():
                errors.append("listener 未提供听完后实际获得的认知。")
    result["passed"] = not errors
    return result
