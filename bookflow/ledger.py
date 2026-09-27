"""Versioned series memory; later episodes retain invalid dependency state."""

from datetime import datetime, timezone
from pathlib import Path

from .common import load_config, load_yaml, parse_draft, sha256_file, source_generation, write_yaml
from .quality import lint, verify_quotes
from .review import evaluate, required_roles
from .approvals import confirmation_state

REQUIRED = {
    "revealed": list,
    "threads_setup": list,
    "threads_payoff": list,
    "identities_known": dict,
    "open_questions": list,
    "takeaways_delivered": list,
    "recap_points": list,
    "deviations_from_plan": list,
    "devices": dict,
}


def _final(project: Path, ep: int) -> Path:
    return project / "episodes" / f"ep{ep:02d}" / "final.md"


def _load(project: Path) -> dict:
    data = load_yaml(project / "series_ledger.yaml", default={}) or {}
    if not isinstance(data, dict) or not isinstance(data.get("episodes", []), list):
        raise ValueError("series_ledger.yaml 的 episodes 必须是列表。")
    data.setdefault("episodes", [])
    seen = set()
    for entry in data["episodes"]:
        if not isinstance(entry, dict) or type(entry.get("ep")) is not int or entry["ep"] < 1:
            raise ValueError("账本条目必须具有正整数 ep。")
        if entry["ep"] in seen:
            raise ValueError(f"账本集号重复：{entry['ep']}")
        seen.add(entry["ep"])
    return data


def _save(project: Path, data: dict) -> None:
    data["episodes"].sort(key=lambda e: e["ep"])
    write_yaml(project / "series_ledger.yaml", data)


def _entry(data: dict, ep: int):
    return next((e for e in data["episodes"] if e["ep"] == ep), None)


def _problems(project: Path, entry: dict, generation) -> list[str]:
    errors = []
    ep = entry["ep"]
    if confirmation_state(project, "script", ep)["state"] != "passed":
        errors.append("本集文案确认已撤回、失效或尚未记录。")
    final = _final(project, ep)
    if entry.get("stale"):
        errors.append(f"本集账本已过期：{entry.get('stale_reason', '')}")
    if entry.get("requires_review"):
        errors.append(f"须复核前集变更：{entry.get('requires_review_reason', '')}")
    if not final.is_file():
        errors.append("定稿不存在。")
    elif not entry.get("final_sha256") or entry["final_sha256"] != sha256_file(final):
        errors.append("定稿已修改或尚未盖章。")
    if not generation or entry.get("source_generation") != generation:
        errors.append("原文版本已变更或未记录。")
    if not str(entry.get("approved_by", "")).strip():
        errors.append("缺少人工确认记录。")
    review_name = entry.get("review_path")
    if not review_name:
        errors.append("缺少审校报告记录。")
    else:
        review_path = Path(review_name)
        if not review_path.is_absolute():
            review_path = project / review_path
        if not review_path.is_file() or entry.get("review_sha256") != sha256_file(review_path):
            errors.append("入账时的审校报告已缺失或修改。")
    artifacts = entry.get("review_artifacts", {})
    for role in dict.fromkeys((*required_roles(load_config(project)), *(artifacts.keys() if isinstance(artifacts, dict) else ()))):
        artifact = artifacts.get(role, {}) if isinstance(artifacts, dict) else {}
        role_name = artifact.get("path")
        role_path = Path(role_name) if role_name else None
        if role_path is not None and not role_path.is_absolute():
            role_path = project / role_path
        if role_path is None or not role_path.is_file() or artifact.get("sha256") != sha256_file(role_path):
            errors.append(f"入账时的 {role} 原始报告已缺失或修改。")
    dependencies = entry.get("dependency_hashes", {})
    if not isinstance(dependencies, dict):
        dependencies = {}
    for previous in range(1, ep):
        prior_final = _final(project, previous)
        saved = dependencies.get(str(previous), dependencies.get(previous))
        if not saved or not prior_final.is_file() or saved != sha256_file(prior_final):
            errors.append(f"依赖第 {previous} 集的定稿版本已变更或未记录，须重新审校。")
    return errors


def check(project: Path) -> dict:
    project = Path(project)
    try:
        data = _load(project)
    except ValueError as exc:
        return {"passed": False, "errors": [str(exc)], "warnings": [], "episodes": []}
    generation = source_generation(project)
    items, errors = [], []
    for entry in sorted(data["episodes"], key=lambda e: e["ep"]):
        problems = _problems(project, entry, generation)
        items.append({"ep": entry["ep"], "passed": not problems, "errors": problems})
        errors.extend(f"第 {entry['ep']} 集：{problem}" for problem in problems)
    for final in sorted((project / "episodes").glob("ep*/final.md")):
        suffix = final.parent.name[2:]
        if suffix.isdigit() and not _entry(data, int(suffix)):
            errors.append(f"第 {int(suffix)} 集有定稿但未入账。")
    return {"passed": not errors, "errors": errors, "warnings": [], "episodes": items}


def context(project: Path, ep: int) -> dict:
    project = Path(project)
    if ep < 1:
        return {"passed": False, "errors": ["集号必须为正整数。"], "episodes": []}
    try:
        data = _load(project)
    except ValueError as exc:
        return {"passed": False, "errors": [str(exc)], "episodes": []}
    errors, previous = [], []
    generation = source_generation(project)
    for number in range(1, ep):
        entry = _entry(data, number)
        if entry is None:
            errors.append(f"第 {number} 集未入账。")
            continue
        errors.extend(f"第 {number} 集：{problem}" for problem in _problems(project, entry, generation))
        previous.append(entry)
    if errors:
        return {"passed": False, "errors": errors, "episodes": []}
    known, setup, paid_off = {}, [], set()
    for entry in previous:
        known.update(entry.get("identities_known", {}))
        setup.extend(entry.get("threads_setup", []))
        paid_off.update(entry.get("threads_payoff", []))
    return {
        "passed": True,
        "errors": [],
        "episodes": previous,
        "identities_known": known,
        "threads_open": list(dict.fromkeys(t for t in setup if t not in paid_off)),
        "threads_payoff": sorted(paid_off),
        "recap_points": previous[-1].get("recap_points", []) if previous else [],
        "open_questions": previous[-1].get("open_questions", []) if previous else [],
    }


def _invalidate_later(data: dict, ep: int, reason: str) -> list[int]:
    affected = []
    for entry in data["episodes"]:
        if entry["ep"] > ep:
            entry["requires_review"] = True
            reasons = entry.setdefault("requires_review_reasons", [])
            message = f"第 {ep} 集变更：{reason}"
            if message not in reasons:
                reasons.append(message)
            entry["requires_review_reason"] = "；".join(reasons)
            affected.append(entry["ep"])
    return sorted(affected)


def mark_stale(project: Path, ep: int, reason: str) -> dict:
    project = Path(project)
    try:
        data = _load(project)
    except ValueError as exc:
        return {"passed": False, "errors": [str(exc)]}
    if ep < 1:
        return {"passed": False, "errors": ["集号必须为正整数。"]}
    entry = _entry(data, ep)
    if entry:
        entry["stale"] = True
        entry["stale_reason"] = reason or "定稿已修改"
    affected = _invalidate_later(data, ep, reason or "定稿已修改")
    _save(project, data)
    return {"passed": True, "errors": [], "affected_episodes": affected}


def stamp(project: Path, ep: int, approved_by: str, review_path: Path) -> dict:
    project, review_path = Path(project), Path(review_path)
    errors = []
    if not isinstance(approved_by, str) or not approved_by.strip():
        return {"passed": False, "errors": ["必须提供此次人工确认者 approved_by，未确认不得入账。"]}
    if ep < 1:
        return {"passed": False, "errors": ["集号必须为正整数。"]}
    final = _final(project, ep)
    if not final.is_file():
        return {"passed": False, "errors": [f"定稿不存在：{final}"]}
    if parse_draft(final.read_text(encoding="utf-8"))["meta"].get("status") != "final":
        errors.append("只允许对 status: final 的 final.md 入账。")
    if not review_path.is_file():
        errors.append(f"审校报告不存在：{review_path}")
    if errors:
        return {"passed": False, "errors": errors}
    hard_warnings = []
    for label, validator in (("硬指标", lint), ("引文与来源", verify_quotes)):
        try:
            checked = validator(final)
        except (OSError, ValueError) as exc:
            errors.append(f"{label}检查未完成：{exc}")
            continue
        if not checked["passed"]:
            errors.extend(f"{label}检查未通过：{error}" for error in checked["errors"])
        hard_warnings.extend(checked.get("warnings", []))
    if errors:
        return {"passed": False, "errors": errors, "warnings": hard_warnings}
    if confirmation_state(project, "script", ep)["state"] != "passed":
        return {"passed": False, "errors": ["本集缺少当前口播稿有效的文案确认记录。"],
                "warnings": hard_warnings}
    report = load_yaml(review_path, default={})
    verdict = evaluate(project, ep, final, report)
    if not verdict["passed"]:
        return verdict
    previous = context(project, ep)
    if not previous["passed"]:
        return previous
    try:
        data = _load(project)
    except ValueError as exc:
        return {"passed": False, "errors": [str(exc)]}
    entry = _entry(data, ep)
    if entry is None:
        return {"passed": False, "errors": [f"第 {ep} 集缺少从定稿提取的账本内容，请先填写条目。"]}
    for key, kind in REQUIRED.items():
        if not isinstance(entry.get(key), kind):
            errors.append(f"账本字段 {key} 缺失或类型错误，应为 {kind.__name__}。")
    if errors:
        return {"passed": False, "errors": errors}
    digest = sha256_file(final)
    generation = source_generation(project)
    affected = []
    changed = bool(entry.get("stale") or (entry.get("final_sha256") and entry["final_sha256"] != digest))
    for later in data["episodes"]:
        dependencies = later.get("dependency_hashes", {})
        if later["ep"] > ep and isinstance(dependencies, dict):
            old = dependencies.get(str(ep), dependencies.get(ep))
            changed = changed or bool(old and old != digest)
    if changed:
        affected = _invalidate_later(data, ep, "前集已重新定稿，后续集数需重新审校与确认")
    try:
        report_name = str(review_path.resolve().relative_to(project.resolve()))
    except ValueError:
        report_name = str(review_path.resolve())
    role_artifacts = {}
    for role, artifact in verdict["role_reports"].items():
        role_path = Path(artifact["path"])
        try:
            role_name = str(role_path.resolve().relative_to(project.resolve()))
        except ValueError:
            role_name = str(role_path.resolve())
        role_artifacts[role] = {"path": role_name, "sha256": artifact["sha256"]}
    entry.update(
        final_sha256=digest,
        source_generation=generation,
        dependency_hashes={str(n): sha256_file(_final(project, n)) for n in range(1, ep)},
        approved_by=approved_by.strip(),
        approved_at=datetime.now(timezone.utc).isoformat(),
        review_path=report_name,
        review_sha256=sha256_file(review_path),
        review_artifacts=role_artifacts,
        stale=False,
        requires_review=False,
    )
    for field in ("stale_reason", "requires_review_reason", "requires_review_reasons"):
        entry.pop(field, None)
    _save(project, data)
    return {"passed": True, "errors": [], "warnings": hard_warnings + verdict["warnings"], "ep": ep, "affected_episodes": affected}
