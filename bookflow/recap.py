"""Maintain one per-book recap while preserving legacy migration evidence."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import yaml

from .common import latest_draft, load_yaml, parse_draft, sha256_file, source_generation, write_yaml

CONTENT_FIELDS = {"revealed": list, "threads_setup": list, "threads_payoff": list,
                  "identities_known": dict, "open_questions": list, "recap_points": list}


def entry_sha256(entry: dict) -> str:
    """Fingerprint the exact narrative entry selected for semantic review."""
    payload = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _legacy_rows(path: Path) -> dict[int, dict]:
    if not path.is_file():
        return {}
    data = load_yaml(path, {}) or {}
    rows = data.get("episodes") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path.name} 的 episodes 必须是列表；原文件未修改")
    result: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("ep")) is not int or row["ep"] < 1:
            raise ValueError(f"{path.name} 存在无效集号；原文件未修改")
        if row["ep"] in result:
            raise ValueError(f"{path.name} 存在重复集号；原文件未修改")
        result[row["ep"]] = row
    return result


def _safe_working_path(project: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    target = (project / relative).resolve()
    return target if target.is_relative_to(project.resolve()) else None


def _candidate(path: Path | None, digest: object, generation: object, current_generation: str,
               entry: dict, *, latest: Path | None = None, require_latest: bool = False,
               dependencies_current: bool = False) -> dict:
    current_hash = sha256_file(path) if path is not None and path.is_file() else None
    current = bool(current_hash and current_hash == digest and generation == current_generation)
    if require_latest and (latest is None or path != latest.resolve()):
        current = False
    if entry.get("stale") or entry.get("requires_review"):
        current = False
    return {"file_current": current, "dependencies_current": dependencies_current,
            "current_sha256": current_hash}


def _migration_candidate(entry: dict, current: dict) -> dict:
    return {"legacy_entry": entry, "migration_snapshot": {
        "file_sha256": current["current_sha256"],
        "file_current": current["file_current"],
        "dependencies_current": current["dependencies_current"]}}


def _candidate_entry(candidate: object) -> dict | None:
    if not isinstance(candidate, dict):
        return None
    entry = candidate.get("entry", candidate.get("legacy_entry"))
    return entry if isinstance(entry, dict) else None


def _validated_actual(actual: object) -> dict:
    if not isinstance(actual, dict) or any(not isinstance(actual.get(name), kind)
                                           for name, kind in CONTENT_FIELDS.items()):
        raise ValueError("实际前情必须显式填写揭示、线索、身份、未决问题和回顾字段")
    if set(actual) != set(CONTENT_FIELDS):
        raise ValueError("实际前情只接受指定字段；结尾摘要留待语义复核填写")
    return {name: copy.deepcopy(actual[name]) for name in CONTENT_FIELDS}


def _dependencies_current(project: Path, ep: int, entry: dict, working: dict[int, dict],
                          *, final: bool) -> bool:
    dependencies = entry.get("dependency_hashes", {})
    if not isinstance(dependencies, dict):
        return False
    bases = entry.get("dependency_basis", {}) if not final else {}
    if not isinstance(bases, dict):
        return False
    for previous in range(1, ep):
        saved = dependencies.get(str(previous), dependencies.get(previous))
        if not isinstance(saved, str) or not saved:
            return False
        if final or bases.get(str(previous)) == "ledger":
            target = project / "episodes" / f"ep{previous:02d}" / "final.md"
        else:
            target = _safe_working_path(project, working.get(previous, {}).get("draft_path"))
        if target is None or not target.is_file() or sha256_file(target) != saved:
            return False
    return True


def build(project: Path) -> dict:
    """Build a migration candidate; never infer a semantic review or approval."""
    project = Path(project).resolve()
    if not (project / "project.yaml").is_file():
        raise ValueError("缺少 project.yaml；只接受已有书目项目")
    generation = source_generation(project)
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
        raise ValueError("缺少有效原文批次；先核对 source/current.json")
    batch = project / "source/imports" / generation
    if any(not (batch / name).is_file() for name in ("manifest.json", "chapters.json", "paragraphs.jsonl")):
        raise ValueError("原文批次文件不完整；先核对 source/imports，未修改旧前情")
    manifest = load_yaml(batch / "manifest.json", {}) or {}
    if not isinstance(manifest, dict) or manifest.get("generation") != generation:
        raise ValueError("原文批次清单与当前指针不一致；未修改旧前情")
    working_path = project / "episodes/working_continuity.yaml"
    ledger_path = project / "series_ledger.yaml"
    if not working_path.is_file() and not ledger_path.is_file():
        raise ValueError("未找到旧工作前情或正式账本；没有可迁移的资料")
    working = _legacy_rows(working_path)
    ledger = _legacy_rows(ledger_path)
    plan = load_yaml(project / "plan/episodes.yaml", {}) or {}
    plan_rows = plan.get("episodes", []) if isinstance(plan, dict) else plan
    if not isinstance(plan_rows, list):
        raise ValueError("分集计划的 episodes 必须是列表；原文件未修改")
    planned = {row["ep"] for row in plan_rows if isinstance(row, dict)
               and type(row.get("ep")) is int and row["ep"] > 0}
    numbers = sorted(planned | working.keys() | ledger.keys())
    episodes = []
    for ep in numbers:
        folder = project / "episodes" / f"ep{ep:02d}"
        draft = latest_draft(folder)
        candidates: dict[str, dict] = {}
        if ep in working:
            row = working[ep]
            recorded = _safe_working_path(project, row.get("draft_path"))
            current = _candidate(recorded, row.get("draft_sha256"),
                                 row.get("source_generation"), generation, row,
                                 latest=draft, require_latest=True,
                                 dependencies_current=_dependencies_current(
                                     project, ep, row, working, final=False))
            candidates["working"] = _migration_candidate(row, current)
        if ep in ledger:
            row = ledger[ep]
            current = _candidate(folder / "final.md", row.get("final_sha256"),
                                 row.get("source_generation"), generation, row,
                                 dependencies_current=_dependencies_current(
                                     project, ep, row, working, final=True))
            candidates["final"] = _migration_candidate(row, current)
        episodes.append({"ep": ep, "candidates": candidates,
                         "semantic_review": {"status": "pending", "note": ""},
                         "selected_basis": None})
    return {"schema_version": 1, "source_generation": generation,
            "migration_state_at_creation": "needs_semantic_review", "episodes": episodes}


def _write_new(path: Path, data: dict) -> None:
    """Install a complete YAML file only if no recap exists, including under a race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".recap-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValueError("episodes/recap.yaml 已存在；拒绝覆盖，先核对现有前情表") from exc
    finally:
        os.unlink(temporary)


def migrate(project: Path, *, write: bool = False) -> dict:
    """Preview by default; explicit writing creates recap.yaml without overwriting it."""
    project = Path(project).resolve()
    recap = project / "episodes/recap.yaml"
    if not recap.parent.resolve().is_relative_to(project):
        raise ValueError("前情表目录越出书目项目；拒绝迁移")
    if recap.exists() or recap.is_symlink():
        raise ValueError("episodes/recap.yaml 已存在；拒绝覆盖，先核对现有前情表")
    data = build(project)
    stale = [row["ep"] for row in data["episodes"] if any(
        not candidate["migration_snapshot"]["file_current"]
        or not candidate["migration_snapshot"]["dependencies_current"]
        for candidate in row["candidates"].values())]
    missing = [row["ep"] for row in data["episodes"] if not row["candidates"]]
    if write:
        _write_new(recap, data)
    return {"status": "warning", "passed": True, "written": write,
            "summary": "前情表迁移候选已写入，仍待逐集语义复核" if write else "前情表迁移预览；未写入书目文件",
            "episodes": [row["ep"] for row in data["episodes"]],
            "stale_episodes": stale, "missing_episodes": missing,
            "next_actions": ["逐集阅读当前稿件，核对揭示、身份、线索和结尾；不得把迁移候选当成已确认前情"],
            "artifacts": [str(recap)] if write else []}


def _required_episodes(project: Path) -> list[int]:
    plan = load_yaml(project / "plan/episodes.yaml", {}) or {}
    rows = plan.get("episodes", []) if isinstance(plan, dict) else plan
    if not isinstance(rows, list) or not rows:
        raise ValueError("分集计划缺少有效 episodes 列表")
    numbers = [row.get("ep") if isinstance(row, dict) else None for row in rows]
    if any(type(number) is not int or number < 1 for number in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("分集计划含无效或重复集号")
    return sorted(numbers)


def _read(project: Path) -> dict:
    path = project / "episodes/recap.yaml"
    if path.is_symlink():
        raise ValueError("episodes/recap.yaml 不能是符号链接")
    if not path.is_file():
        raise ValueError("尚未建立 episodes/recap.yaml")
    try:
        data = load_yaml(path, {}) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("episodes/recap.yaml 无法读取或解析") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("episodes"), list):
        raise ValueError("episodes/recap.yaml 结构无效")
    return data


def update_working(project: Path, ep: int, draft: Path, actual: dict) -> dict:
    """Record an explicitly extracted draft recap; never assert semantic review."""
    project = Path(project).resolve()
    if not (project / "project.yaml").is_file():
        raise ValueError("缺少 project.yaml；只接受已有书目项目")
    if type(ep) is not int or ep not in _required_episodes(project):
        raise ValueError("集号不在当前分集计划中")
    actual = _validated_actual(actual)
    generation = source_generation(project)
    batch = project / "source/imports" / generation
    if (not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation)
            or any(not (batch / name).is_file() for name in
                   ("manifest.json", "chapters.json", "paragraphs.jsonl"))):
        raise ValueError("当前原文批次不完整；未修改前情表")
    manifest = load_yaml(batch / "manifest.json", {}) or {}
    if not isinstance(manifest, dict) or manifest.get("generation") != generation:
        raise ValueError("原文批次清单与当前指针不一致；未修改前情表")
    folder = project / "episodes" / f"ep{ep:02d}"
    draft = Path(draft)
    if (folder.is_symlink() or not folder.resolve().is_relative_to(project)
            or draft.is_symlink() or not draft.is_file() or draft.resolve().parent != folder.resolve()
            or draft.resolve() != latest_draft(folder)):
        raise ValueError("必须提供本集目录下当前最新的普通草稿文件")
    parsed = parse_draft(draft.read_text(encoding="utf-8"))
    if (parsed["meta"].get("episode") != ep
            or parsed["meta"].get("source_generation", generation) != generation):
        raise ValueError("草稿集号或原文批次与当前项目不一致")
    lock_path = project / "episodes/.recap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            return _write_working(project, ep, draft, actual, generation)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_working(project: Path, ep: int, draft: Path, actual: dict, generation: str) -> dict:
    path = project / "episodes/recap.yaml"
    if path.exists() or path.is_symlink():
        data = _read(project)
        if data.get("source_generation") != generation:
            raise ValueError("前情表绑定其他原文批次；先人工迁移，不覆盖")
    else:
        data = {"schema_version": 1, "source_generation": generation, "episodes": []}
    rows = {}
    for row in data["episodes"]:
        if (not isinstance(row, dict) or type(row.get("ep")) is not int or row["ep"] < 1
                or row["ep"] in rows or not isinstance(row.get("candidates", {}), dict)):
            raise ValueError("现有前情表条目结构无效或集号重复；拒绝覆盖")
        rows[row["ep"]] = row
    dependencies, bases, pending = {}, {}, []
    for previous in range(1, ep):
        row = rows.get(previous, {})
        candidates = row.get("candidates", {})
        basis = row.get("selected_basis")
        if basis not in ("working", "final"):
            basis = "working" if "working" in candidates else "final" if "final" in candidates else None
        entry = _candidate_entry(candidates.get(basis)) if basis else None
        digest = entry.get("draft_sha256" if basis == "working" else "final_sha256") if entry else None
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            pending.append(previous)
            continue
        dependencies[str(previous)] = digest
        bases[str(previous)] = "working" if basis == "working" else "ledger"
    entry = {"ep": ep, "draft_path": str(draft.resolve().relative_to(project)),
             "draft_sha256": sha256_file(draft), "source_generation": generation,
             "dependency_hashes": dependencies, "dependency_basis": bases,
             **actual}
    row = rows.get(ep)
    if row is None:
        row = {"ep": ep, "candidates": {}, "selected_basis": None,
               "semantic_review": {"status": "pending", "note": ""}}
        data["episodes"].append(row)
    current = _candidate_entry(row["candidates"].get("working"))
    if current is not None and entry_sha256(current) == entry_sha256(entry):
        return {"passed": True, "written": False, "unchanged": True, "ep": ep,
                "pending_dependencies": pending, "artifacts": [str(path)]}
    if row["candidates"]:
        history = row.setdefault("history", [])
        if not isinstance(history, list):
            raise ValueError("前情表历史结构无效；拒绝覆盖")
        history.append({"candidates": copy.deepcopy(row["candidates"]),
                        "selected_basis": row.get("selected_basis"),
                        "semantic_review": copy.deepcopy(row.get("semantic_review"))})
    row["candidates"]["working"] = {"entry": entry}
    row["selected_basis"] = "working"
    row["semantic_review"] = {"status": "pending", "note": ""}
    data["episodes"].sort(key=lambda item: item["ep"])
    if path.exists():
        write_yaml(path, data)
    else:
        _write_new(path, data)
    return {"passed": True, "written": True, "unchanged": False, "ep": ep,
            "pending_dependencies": pending, "artifacts": [str(path)],
            "summary": "已登记当前草稿实际前情；仍须核对叙事内容与前集依赖"}


def snapshot_final(project: Path, ep: int, actual: dict | None = None) -> dict:
    """Snapshot a confirmed final script, carrying review only for identical spoken text."""
    project = Path(project).resolve()
    if not (project / "project.yaml").is_file() or type(ep) is not int or ep not in _required_episodes(project):
        raise ValueError("定稿快照需要有效书目和计划内集号")
    if actual is not None:
        actual = _validated_actual(actual)
    folder = project / "episodes" / f"ep{ep:02d}"
    final = folder / "final.md"
    if (folder.is_symlink() or not folder.resolve().is_relative_to(project)
            or final.is_symlink() or not final.is_file()):
        raise ValueError("本集 final.md 不是项目内普通文件")
    generation = source_generation(project)
    batch = project / "source/imports" / generation
    if (not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation)
            or any(not (batch / name).is_file() for name in
                   ("manifest.json", "chapters.json", "paragraphs.jsonl"))):
        raise ValueError("当前原文批次不完整；未修改前情表")
    manifest = load_yaml(batch / "manifest.json", {}) or {}
    if not isinstance(manifest, dict) or manifest.get("generation") != generation:
        raise ValueError("原文批次清单与当前指针不一致；未修改前情表")
    parsed = parse_draft(final.read_text(encoding="utf-8"))
    if (parsed["meta"].get("status") != "final" or parsed["meta"].get("episode", ep) != ep
            or parsed["meta"].get("source_generation", generation) != generation):
        raise ValueError("final.md 未标为当前集、当前原文批次的定稿")
    from .approvals import confirmation_state
    if confirmation_state(project, "script", ep)["state"] != "passed":
        raise ValueError("本集缺少当前有效文案确认；拒绝创建定稿快照")
    lock_path = project / "episodes/.recap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            return _snapshot_final_locked(project, ep, final, parsed["spoken"], actual, generation)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _snapshot_final_locked(project: Path, ep: int, final: Path, spoken: str,
                           actual: dict | None, generation: str) -> dict:
    from .approvals import confirmation_state
    confirmation = confirmation_state(project, "script", ep)
    if confirmation["state"] != "passed":
        raise ValueError("文案确认在等待写入期间失效；拒绝创建定稿快照")
    data = _read(project)
    if data.get("source_generation") != generation:
        raise ValueError("前情表原文批次与当前项目不一致；拒绝覆盖")
    rows = {}
    for row in data["episodes"]:
        if (not isinstance(row, dict) or type(row.get("ep")) is not int or row["ep"] < 1
                or row["ep"] in rows or not isinstance(row.get("candidates", {}), dict)):
            raise ValueError("现有前情表条目结构无效或集号重复；拒绝覆盖")
        rows[row["ep"]] = row
    row = rows.get(ep)
    dependencies, missing = {}, []
    for previous in range(1, ep):
        prior_final = project / "episodes" / f"ep{previous:02d}" / "final.md"
        if (prior_final.is_symlink() or not prior_final.resolve().is_relative_to(project)
                or not prior_final.is_file()):
            missing.append(previous)
        else:
            dependencies[str(previous)] = sha256_file(prior_final)
    final_hash = sha256_file(final)
    spoken_hash = hashlib.sha256(re.sub(r"\s+", "", spoken).encode("utf-8")).hexdigest()
    old_candidate = row.get("candidates", {}).get("final") if row else None
    old_final = _candidate_entry(old_candidate)
    if (actual is None and row and row.get("selected_basis") == "final" and old_final
            and old_final.get("final_sha256") == final_hash
            and old_final.get("source_generation") == generation
            and old_final.get("dependency_hashes") == dependencies):
        return {"passed": True, "written": False, "unchanged": True, "ep": ep,
                "pending_dependencies": missing, "artifacts": [str(project / "episodes/recap.yaml")]}
    carried_review = None
    carry_source = None
    if actual is None:
        if (old_final and row.get("selected_basis") == "final" and isinstance(old_candidate, dict)
                and old_candidate.get("spoken_sha256") == spoken_hash):
            actual = {name: copy.deepcopy(old_final[name]) for name in CONTENT_FIELDS}
            review = row.get("semantic_review")
            if (isinstance(review, dict) and review.get("status") == "completed"
                    and review.get("reviewed_file_sha256") == old_final.get("final_sha256")
                    and review.get("reviewed_entry_sha256") == entry_sha256(old_final)
                    and review.get("unresolved") == []):
                carried_review = copy.deepcopy(review)
                carried_review["carried_from_identical_final_spoken"] = {
                    "final_file_sha256": old_final["final_sha256"],
                    "final_entry_sha256": entry_sha256(old_final)}
            carry_source = "identical_existing_final"
        else:
            if not row or row.get("selected_basis") != "working":
                raise ValueError("没有已选中的工作稿前情；请显式提供定稿实际内容")
            reviewed = context(project, ep + 1)
            if not reviewed["passed"]:
                raise ValueError("工作稿前情尚未完成有效语义复核；请先复核或提供定稿实际内容")
            working = _candidate_entry(row["candidates"].get("working"))
            draft = _safe_working_path(project, working.get("draft_path")) if working else None
            if draft is None or not draft.is_file():
                raise ValueError("已复核工作稿不存在；不能沿用其前情")
            if re.sub(r"\s+", "", parse_draft(draft.read_text(encoding="utf-8"))["spoken"]) != re.sub(r"\s+", "", spoken):
                raise ValueError("定稿纯口播与已复核工作稿不同；请显式提供定稿实际内容")
            actual = {name: copy.deepcopy(working[name]) for name in CONTENT_FIELDS}
            carried_review = copy.deepcopy(row["semantic_review"])
            carried_review["carried_from_identical_spoken"] = {
                "working_file_sha256": working["draft_sha256"],
                "working_entry_sha256": entry_sha256(working)}
            carry_source = "identical_reviewed_working"
    entry = {"ep": ep, "final_sha256": final_hash, "source_generation": generation,
             "dependency_hashes": dependencies, **actual}
    if row is None:
        row = {"ep": ep, "candidates": {}, "selected_basis": None,
               "semantic_review": {"status": "pending", "note": ""}}
        data["episodes"].append(row)
    if (old_final and row.get("selected_basis") == "final"
            and entry_sha256(old_final) == entry_sha256(entry)):
        return {"passed": True, "written": False, "unchanged": True, "ep": ep,
                "pending_dependencies": missing, "artifacts": [str(project / "episodes/recap.yaml")]}
    if row["candidates"]:
        history = row.setdefault("history", [])
        if not isinstance(history, list):
            raise ValueError("前情表历史结构无效；拒绝覆盖")
        history.append({"candidates": copy.deepcopy(row["candidates"]),
                        "selected_basis": row.get("selected_basis"),
                        "semantic_review": copy.deepcopy(row.get("semantic_review"))})
    row["candidates"]["final"] = {"entry": entry,
                                  "snapshot_source": carry_source or "explicit_actual",
                                  "spoken_sha256": spoken_hash,
                                  "confirmation_at": confirmation.get("at")}
    row["selected_basis"] = "final"
    row["semantic_review"] = carried_review or {"status": "pending", "note": ""}
    if carried_review:
        row["semantic_review"]["reviewed_file_sha256"] = final_hash
        row["semantic_review"]["reviewed_entry_sha256"] = entry_sha256(entry)
    data["episodes"].sort(key=lambda item: item["ep"])
    write_yaml(project / "episodes/recap.yaml", data)
    return {"passed": True, "written": True, "unchanged": False, "ep": ep,
            "pending_dependencies": missing, "review_carried": bool(carried_review),
            "artifacts": [str(project / "episodes/recap.yaml")],
            "summary": "定稿快照已写入；纯口播相同的旧复核已沿用" if carried_review else "定稿快照已写入，仍待语义复核"}


def inspect(project: Path) -> dict:
    """Expose current fingerprints without printing any narrative or legacy metadata."""
    project = Path(project).resolve()
    data = _read(project)
    generation = source_generation(project)
    rows = data["episodes"]
    working = {}
    for row in rows:
        if isinstance(row, dict) and type(row.get("ep")) is int:
            candidates = row.get("candidates", {})
            candidate = candidates.get("working") if isinstance(candidates, dict) else None
            entry = _candidate_entry(candidate)
            if entry is not None:
                working[row["ep"]] = entry
    summaries = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("ep")) is not int:
            raise ValueError("前情表含无效集号；先修复文件")
        ep = row["ep"]
        candidates = row.get("candidates", {})
        if not isinstance(candidates, dict):
            raise ValueError("前情表候选结构无效；先修复文件")
        fingerprint = {}
        for basis in ("working", "final"):
            candidate = candidates.get(basis)
            if not isinstance(candidate, dict):
                continue
            entry = _candidate_entry(candidate)
            if entry is None:
                continue
            folder = project / "episodes" / f"ep{ep:02d}"
            if basis == "working":
                path = _safe_working_path(project, entry.get("draft_path"))
                current = _candidate(path, entry.get("draft_sha256"), entry.get("source_generation"),
                                     generation, entry, latest=latest_draft(folder), require_latest=True,
                                     dependencies_current=_dependencies_current(project, ep, entry, working, final=False))
            else:
                current = _candidate(folder / "final.md", entry.get("final_sha256"),
                                     entry.get("source_generation"), generation, entry,
                                     dependencies_current=_dependencies_current(project, ep, entry, working, final=True))
            fingerprint[basis] = {"file_sha256": current["current_sha256"],
                                  "entry_sha256": entry_sha256(entry),
                                  "file_current": current["file_current"],
                                  "dependencies_current": current["dependencies_current"]}
        summaries.append({"ep": ep, "candidates": fingerprint})
    return {"status": "warning", "passed": True,
            "summary": "仅显示版本指纹；不表示已完成语义复核或人工批准",
            "episodes": summaries, "artifacts": []}


def check(project: Path, *, required_episodes: list[int] | None = None) -> dict:
    """Validate review records and current files; do not judge narrative truth."""
    project = Path(project).resolve()
    errors: list[str] = []
    try:
        data = _read(project)
        required = _required_episodes(project) if required_episodes is None else required_episodes
        generation = source_generation(project)
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        return {"passed": False, "errors": [str(exc)], "episodes": []}
    batch = project / "source/imports" / generation
    if (not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation)
            or any(not (batch / name).is_file() for name in
                   ("manifest.json", "chapters.json", "paragraphs.jsonl"))):
        errors.append("当前原文批次不完整")
    else:
        try:
            manifest = load_yaml(batch / "manifest.json", {}) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            manifest = None
        if not isinstance(manifest, dict) or manifest.get("generation") != generation:
            errors.append("当前原文批次清单与指针不一致")
    if data.get("source_generation") != generation:
        errors.append("前情表原文批次与当前项目不一致")
    rows: dict[int, dict] = {}
    for row in data["episodes"]:
        if not isinstance(row, dict) or type(row.get("ep")) is not int or row["ep"] < 1:
            errors.append("前情表含无效集号")
            continue
        if row["ep"] in rows:
            errors.append(f"第 {row['ep']} 集前情条目重复")
        rows[row["ep"]] = row
    working = {}
    for ep, row in rows.items():
        candidates = row.get("candidates", {})
        if isinstance(candidates, dict) and isinstance(candidates.get("working"), dict):
            entry = _candidate_entry(candidates["working"])
            if entry is not None:
                working[ep] = entry
    checked = []
    for ep in required:
        row = rows.get(ep)
        if row is None:
            errors.append(f"第 {ep} 集缺少前情条目")
            continue
        basis = row.get("selected_basis")
        candidates = row.get("candidates", {})
        candidate = candidates.get(basis) if isinstance(candidates, dict) and basis in ("working", "final") else None
        entry = _candidate_entry(candidate)
        if entry is None:
            errors.append(f"第 {ep} 集尚未选择有效的工作稿或定稿候选")
            continue
        if entry.get("ep") != ep:
            errors.append(f"第 {ep} 集候选集号不一致")
            continue
        for field, kind in CONTENT_FIELDS.items():
            if not isinstance(entry.get(field), kind):
                errors.append(f"第 {ep} 集候选缺少 {field} 前情字段")
        folder = project / "episodes" / f"ep{ep:02d}"
        try:
            if basis == "working":
                path = _safe_working_path(project, entry.get("draft_path"))
                current = _candidate(path, entry.get("draft_sha256"), entry.get("source_generation"),
                                     generation, entry, latest=latest_draft(folder), require_latest=True,
                                     dependencies_current=_dependencies_current(project, ep, entry, working, final=False))
            else:
                current = _candidate(folder / "final.md", entry.get("final_sha256"),
                                     entry.get("source_generation"), generation, entry,
                                     dependencies_current=_dependencies_current(project, ep, entry, working, final=True))
                from .approvals import confirmation_state
                if not (project / "approvals").is_dir() or confirmation_state(project, "script", ep)["state"] != "passed":
                    errors.append(f"第 {ep} 集定稿候选缺少有效文案确认")
        except (OSError, ValueError, TypeError, KeyError):
            errors.append(f"第 {ep} 集候选文件、依赖或确认记录无法核对")
            continue
        if not current["file_current"] or not current["dependencies_current"]:
            errors.append(f"第 {ep} 集所选候选的稿件或前集依赖已失效")
        review = row.get("semantic_review", {})
        if (not isinstance(review, dict) or review.get("status") != "completed"
                or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
                or not isinstance(review.get("note"), str) or not review["note"].strip()
                or not isinstance(review.get("ending_summary"), str) or not review["ending_summary"].strip()
                or review.get("unresolved") != []
                or review.get("reviewed_file_sha256") != current["current_sha256"]
                or review.get("reviewed_entry_sha256") != entry_sha256(entry)):
            errors.append(f"第 {ep} 集尚未对当前候选完成前情语义复核，或复核记录已失效")
        checked.append(ep)
    return {"passed": not errors, "errors": errors, "episodes": checked,
            "summary": "前情表版本与复核记录通过；叙事语义仍需人工判断" if not errors else "前情表待补或复核失效"}


def drafting_state(project: Path, ep: int | None = None) -> dict:
    """Allow-missing view for parallel first drafts, mirroring the old continuity gate.

    A selected (or working) candidate whose draft no longer matches blocks drafting;
    missing prior entries and changed dependencies are only warnings, because the
    season review (``check``) must settle them before the unified edit.
    """
    project = Path(project).resolve()
    if ep is not None and (type(ep) is not int or ep < 1):
        return {"passed": False, "errors": ["集号必须为正整数"], "warnings": [], "complete": False,
                "missing_episodes": [], "pending_dependencies": [], "source": "recap"}
    data = _read(project)
    selected = {row["ep"]: row.get("selected_basis") for row in data["episodes"]
                if isinstance(row, dict) and type(row.get("ep")) is int}
    fingerprints = {row["ep"]: row["candidates"] for row in inspect(project)["episodes"]}
    wanted = list(range(1, ep)) if ep is not None else sorted(fingerprints)
    errors: list[str] = []
    missing: list[int] = []
    pending: list[int] = []
    for number in wanted:
        candidates = fingerprints.get(number, {})
        basis = selected.get(number)
        if basis not in candidates:
            basis = "working" if "working" in candidates else None
        if basis is None:
            missing.append(number)
            continue
        if not candidates[basis]["file_current"]:
            errors.append(f"第 {number} 集前情候选（{basis}）对应的稿件已变化；先更新 recap 前情表")
        elif not candidates[basis]["dependencies_current"]:
            pending.append(number)
    warnings = []
    if missing:
        warnings.append(f"第 {'、'.join(map(str, missing))} 集前情待补：并行初稿暂用计划限制信息揭示，"
                        "整季汇总前必须按实际稿件补录并复核。")
    if pending:
        warnings.append(f"第 {'、'.join(map(str, pending))} 集前情的前序依赖已变化，汇总前需重新核对。")
    return {"passed": not errors, "errors": errors, "warnings": warnings,
            "complete": not errors and not missing and not pending,
            "missing_episodes": missing, "pending_dependencies": pending, "source": "recap"}


def final_snapshot_state(project: Path, required_episodes: list[int]) -> dict:
    """Require selected, current final snapshots before any confirmed-script production."""
    project = Path(project).resolve()
    try:
        data = _read(project)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return {"passed": False, "errors": [str(exc)]}
    rows = {row["ep"]: row for row in data["episodes"] if isinstance(row, dict)
            and type(row.get("ep")) is int}
    pending = [ep for ep in required_episodes if rows.get(ep, {}).get("selected_basis") != "final"]
    if pending:
        return {"passed": False, "errors": [f"第 {pending[0]} 集尚未保存已确认文案的定稿前情快照"],
                "pending_episodes": pending}
    result = check(project, required_episodes=required_episodes)
    return {"passed": result["passed"], "errors": result["errors"], "pending_episodes": []}


def context(project: Path, ep: int) -> dict:
    """Return only reviewed prior-episode narrative facts."""
    if type(ep) is not int or ep < 1:
        return {"passed": False, "errors": ["集号必须为正整数"], "episodes": []}
    project = Path(project).resolve()
    result = check(project, required_episodes=list(range(1, ep)))
    if not result["passed"]:
        return {"passed": False, "errors": result["errors"], "episodes": []}
    data = _read(project)
    rows = {row["ep"]: row for row in data["episodes"] if isinstance(row, dict)
            and type(row.get("ep")) is int}
    episodes = []
    for number in range(1, ep):
        row = rows[number]
        entry = _candidate_entry(row["candidates"][row["selected_basis"]])
        episodes.append({"ep": number, "basis": row["selected_basis"],
                         "file_sha256": entry["draft_sha256" if row["selected_basis"] == "working" else "final_sha256"],
                         "actual": {name: entry[name] for name in CONTENT_FIELDS},
                         "ending_summary": row["semantic_review"]["ending_summary"]})
    return {"passed": True, "errors": [], "episodes": episodes}
