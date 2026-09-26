"""Entity-based name changes: inspect impact before editing authoritative material."""
from __future__ import annotations

import re
from pathlib import Path

from .common import load_yaml, sha256_file


def characters(project: Path) -> list[dict]:
    items = load_yaml(project / "analysis/characters.yaml").get("characters", [])
    ids = [item.get("id") for item in items]
    if any(not key for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("人物表缺少稳定编号，或编号重复")
    return items


def replacements(project: Path) -> dict:
    result = {}
    items = characters(project)
    for item in items:
        for alias in item.get("aliases_zh", []):
            if alias.get("status") != "deprecated":
                continue
            old, new = alias.get("text", ""), alias.get("replacement", "")
            if not old or not new or old == new:
                raise ValueError(f"{item['id']} 的旧称映射无效")
            if new not in (item.get("name_zh"), item.get("spoken_name")):
                raise ValueError(f"{item['id']} 的旧称必须直接指向现用名，不能形成中间跳转")
            for other in items:
                if other["id"] != item["id"] and any(old in other.get(field, "") for field in ("name_zh", "spoken_name")):
                    raise ValueError(f"旧称 {old} 同时是 {other['id']} 的现用名，必须先消歧")
            if old in result and (result[old]["replacement"] != new or result[old]["entity_id"] != item["id"]):
                raise ValueError(f"旧称 {old} 存在冲突映射")
            result[old] = {"entity_id": item["id"], "replacement": new}
    return result


def check_text(project: Path, text: str) -> list[dict]:
    mapping = replacements(project)
    if not mapping:
        return []
    # Prefer the full form over a contained short form; do not report it twice.
    accepted = {item.get(field, "") for item in characters(project) for field in ("name_zh", "spoken_name")} - {""}
    pattern = re.compile("|".join(re.escape(x) for x in sorted(set(mapping) | accepted, key=len, reverse=True)))
    return [{"old": m.group(), **mapping[m.group()], "line": text[:m.start()].count("\n") + 1}
            for m in pattern.finditer(text) if m.group() in mapping]


def current_files(project: Path) -> list[Path]:
    files = [project / "project.yaml"]
    for directory in ("analysis", "plan"):
        files.extend(p for p in (project / directory).rglob("*")
                     if p.suffix in (".md", ".yaml", ".json") and p.name != "characters.yaml")
    for episode in (project / "episodes").glob("ep*"):
        drafts = [p for p in episode.glob("draft_v*.md") if re.fullmatch(r"draft_v\d+", p.stem)]
        if drafts:
            files.append(max(drafts, key=lambda p: int(re.search(r"_v(\d+)", p.stem).group(1))))
        files.extend(p for p in (episode / "final.md", episode / "package.md") if p.is_file())
        for directory in ("preview", "deliver"):
            files.extend(p for p in (episode / directory).glob("*") if p.suffix in (".txt", ".md", ".html", ".srt", ".vtt"))
    return sorted(set(p for p in files if p.is_file()))


def check_project(project: Path) -> dict:
    project = project.resolve()
    if not (project / "project.yaml").is_file():
        raise ValueError("书目项目不存在")
    findings = []
    for path in current_files(project):
        for hit in check_text(project, path.read_text(encoding="utf-8")):
            findings.append({"path": str(path.relative_to(project)), **hit})
    for item in characters(project):
        for field in ("name_zh", "spoken_name", "role"):
            for hit in check_text(project, item.get(field, "")):
                findings.append({"path": "analysis/characters.yaml", "field": field, **hit})
    return {"passed": not findings, "findings": findings,
            "errors": [f"{h['path']}：旧称 {h['old']} → {h['replacement']}（{h['entity_id']}）" for h in findings],
            "scope": "当前分析、计划、最新稿、定稿和文字导出；原文、旧稿、反馈证据及审校历史不改写"}


def change_plan(project: Path, entity_id: str, new_name: str) -> dict:
    project = project.resolve()
    items = characters(project)
    entity = next((x for x in items if x["id"] == entity_id), None)
    if entity is None:
        raise ValueError("人物编号不存在")
    new_name = new_name.strip()
    old = entity.get("spoken_name", "")
    if not old or not new_name or old == new_name or any(c in new_name for c in "\n\r\t"):
        raise ValueError("请指定有效且不同的新口播名")
    for other in items:
        if other["id"] != entity_id and (new_name in [other.get("name_zh"), other.get("spoken_name")]
                                       or old in str(other.get("name_zh", ""))):
            raise ValueError(f"名称涉及另一人物 {other['id']}，不能自动全局替换")
    mappings = {old: new_name}
    full = entity.get("name_zh", "")
    if full and old in full:
        mappings[full] = full.replace(old, new_name)
    active = set(current_files(project))
    editable, regenerate, preserved = [], [], []
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.suffix not in (".md", ".yaml", ".txt", ".json", ".jsonl", ".html", ".srt", ".vtt"):
            continue
        text = path.read_text(encoding="utf-8")
        if old not in text:
            continue
        rel = path.relative_to(project)
        hit = {"path": str(rel), "sha256": sha256_file(path), "count": text.count(old)}
        if "source" == rel.parts[0] or "feedback" == rel.parts[0] or "human_edit" in rel.parts or "review" in rel.parts:
            preserved.append(hit)
        elif "preview" in rel.parts or "deliver" in rel.parts:
            regenerate.append(hit)
        elif path in active or path == project / "analysis/characters.yaml":
            editable.append(hit)
        else:
            preserved.append(hit)
    return {"status": "impact_plan_only", "entity_id": entity_id, "name_de": entity.get("name_de"),
            "old_spoken_name": old, "new_spoken_name": new_name, "mappings": mappings,
            "update_with_backup": editable, "regenerate": regenerate, "preserve_history": preserved,
            "registry": str(project / "analysis/characters.yaml"),
            "instruction": "核对人物后按范围同步；中文直接引文逐处核对，不机械替换。保留变更前快照；稿件另存版本。登记旧称后运行 names-check。"}


def feedback_candidates(project: Path, blocks: list[dict], changes: list[dict]) -> list[dict]:
    before = "".join(b["text"] for b in blocks)
    result = []
    for entity in characters(project):
        known = {entity.get("spoken_name", ""), entity.get("name_zh", "")}
        known.update(x.get("text", "") for x in entity.get("aliases_zh", []))
        affected = set()
        for name in known - {""}:
            for match in re.finditer(re.escape(name), before):
                for c in changes:
                    start, end = c["before_range"]
                    if (start < match.end() and end > match.start()) or (start == end and match.start() < start < match.end()):
                        affected.add(c["id"])
        if affected:
            result.append({"entity_id": entity["id"], "name_de": entity.get("name_de"),
                           "current_name": entity.get("spoken_name"), "changes": sorted(affected),
                           "status": "needs_identity_review", "note": "名称或所在句子发生变化，可能是删句/重排；不能仅凭差异自动认定改名。"})
    return result
