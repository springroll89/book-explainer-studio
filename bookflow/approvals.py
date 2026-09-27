"""Approval records and content-bound validation."""
from __future__ import annotations
import difflib
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
import yaml
from .common import latest_draft, load_config, load_yaml, parse_draft, sha256_file, source_generation, write_yaml

GATES = ("G1","G2","G3","G4","AV1","RELEASE","SOURCE","OVERRIDE")

def _approval_dir(project: Path) -> Path:
    d=Path(project)/"approvals"; d.mkdir(parents=True, exist_ok=True); return d

def _digest_paths(project: Path, gate: str, ep: int|None=None) -> tuple[list[str],str]:
    p=Path(project); paths=[]
    if gate=="G1": paths=[p/"analysis/book_brief.md",p/"analysis/coverage_review.yaml",p/"analysis/characters.yaml",p/"analysis/threads.yaml"]
    elif gate=="G2": paths=[p/"plan/episodes.yaml"]
    elif gate in ("G3","G4") and ep:
        ed=p/"episodes"/f"ep{ep:02d}"
        draft=latest_draft(ed)
        paths=[(draft if draft else ed/"draft_v1.md"), ed/"review"] if gate=="G3" else [ed/"final.md", ed/"review"]
    elif gate=="AV1" and ep: paths=[p/"episodes"/f"ep{ep:02d}"/"production",p/"episodes"/f"ep{ep:02d}"/"preview"]
    elif gate=="RELEASE": paths=[p/"release/compliance.yaml"]
    existing=[]
    for x in paths:
        if x.is_file(): existing.append(x)
        elif x.is_dir():
            files = (y for y in x.rglob("*") if y.is_file() and "__pycache__" not in y.parts)
            if gate == "AV1":
                # Finder metadata is incidental and can change without any
                # production asset changing, so it must not stale AV1.
                files = (y for y in files if y.name != ".DS_Store")
            if gate == "G3" and x.name == "review":
                # G3 is bound to style/source review artifacts. Later G4 packets
                # are administrative sign-off materials and must not stale G3.
                files = (y for y in files if not y.relative_to(x).parts[0].startswith("g4_materials_"))
            existing.extend(sorted(files))
    records=[]
    for x in existing:
        records.append((str(x.resolve().relative_to(p.resolve())),sha256_file(x)))
    payload=json.dumps(records,ensure_ascii=False,separators=(",",":"))
    return [x for x,_ in records], hashlib.sha256(payload.encode()).hexdigest()

def list_valid(project: Path) -> list[dict]:
    out=[]
    for path in sorted(_approval_dir(Path(project)).glob("*.yaml")):
        try: d=load_yaml(path,{})
        except Exception: continue
        if not isinstance(d,dict) or d.get("revoked"): continue
        gate=d.get("gate")
        if gate not in GATES: continue
        if gate=="OVERRIDE":
            out.append({**d,"path":str(path),"valid":True}); continue
        files,digest=_digest_paths(Path(project),gate,d.get("ep"))
        out.append({**d,"path":str(path),"valid": bool(files) and digest==d.get("object_sha256") and files==d.get("object_files",[])})
    return out

def gate_state(project: Path, gate: str, ep: int|None=None) -> str:
    records=[]
    valid_by_path={a["path"]:a for a in list_valid(project)}
    for path in _approval_dir(Path(project)).glob("*.yaml"):
        d=load_yaml(path,{})
        if not isinstance(d,dict) or d.get("gate")!=gate or d.get("ep")!=ep: continue
        at=d.get("revoked_at") if d.get("revoked") else d.get("approved_at")
        records.append((str(at or path.name),path,d))
    if not records: return "pending"
    _,path,last=max(records,key=lambda row:(row[0],row[1].name))
    if last.get("revoked"): return "revoked"
    return "passed" if valid_by_path.get(str(path),{}).get("valid") else "invalidated"

def approve(project: Path, gate: str, ep: int|None=None) -> dict:
    if not sys.stdin.isatty(): return {"passed":False,"errors":["approve 只能由用户在交互式终端执行，代理或管道输入已拒绝"]}
    gate=gate.upper()
    if gate not in GATES: return {"passed":False,"errors":[f"未知闸门：{gate}"]}
    files,digest=_digest_paths(Path(project),gate,ep)
    print(f"待批准：{gate}{f' 第{ep}集' if ep else ''}\n对象哈希：{digest}\n文件：{len(files)} 个")
    expected=f"确认 {gate}"+(f" 第{ep}集" if ep else "")
    answer=input(f"请输入“{expected}”：").strip()
    if answer!=expected: return {"passed":False,"errors":["确认短语不匹配，未生成批准记录"]}
    user=load_yaml(Path.home()/".config/bookflow/user.yaml",{}) or {}
    approver=user.get("name") or user.get("user") or "本机用户"
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path=_approval_dir(Path(project))/(f"{gate}"+(f"-ep{ep:02d}" if ep else "")+f"-{stamp}.yaml")
    write_yaml(path,{"gate":gate,"ep":ep,"object_files":files,"object_sha256":digest,"approved_at":datetime.now(timezone.utc).isoformat(),"approver":approver,"note":"用户在交互终端确认"})
    return {"passed":True,"gate":gate,"ep":ep,"path":str(path),"object_sha256":digest}

def revoke(project: Path, gate: str, ep: int|None=None, reason: str="") -> dict:
    if not sys.stdin.isatty(): return {"passed":False,"errors":["revoke 只能由用户在交互式终端执行"]}
    answer=input(f"请输入“撤回 {gate.upper()}”以确认：").strip()
    if answer!=f"撤回 {gate.upper()}": return {"passed":False,"errors":["确认短语不匹配"]}
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path=_approval_dir(Path(project))/(f"revoked-{gate.upper()}"+(f"-ep{ep:02d}" if ep else "")+f"-{stamp}.yaml")
    write_yaml(path,{"gate":gate.upper(),"ep":ep,"revoked":True,"revoked_at":datetime.now(timezone.utc).isoformat(),"reason":reason})
    return {"passed":True,"path":str(path)}

def override(project: Path, rule: str, reason: str) -> dict:
    if not sys.stdin.isatty(): return {"passed":False,"errors":["override 只能由用户在交互式终端执行"]}
    expected=f"确认授权 {rule}"
    if input(f"请输入“{expected}”：").strip()!=expected:
        return {"passed":False,"errors":["确认短语不匹配"]}
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path=_approval_dir(Path(project))/(f"OVERRIDE-{stamp}.yaml")
    write_yaml(path,{"gate":"OVERRIDE","rule":rule,"reason":reason,"approved_at":datetime.now(timezone.utc).isoformat(),"approver":"本机用户"})
    return {"passed":True,"path":str(path),"status":"preview_override"}


# Passphrase confirmations replace the terminal gates for new projects.
CONFIRMATIONS = {"方案": "plan", "文案": "script", "画风": "style", "定妆": "characters",
                 "声音": "sound", "样片": "sample", "成片": "release"}
PROJECT_GATES = ("plan", "style", "characters")
_COMMAND = re.compile(r"^(拍板|撤回)(方案|文案|画风|定妆|声音|样片|成片)"
                      r"(?:,?除(第?[1-9][0-9]*集?(?:,第?[1-9][0-9]*集?)*))?$")
STYLE_MANIFEST = "visual/style_choice.yaml"
CHARACTER_MANIFEST = "visual/character_sheet.yaml"
_DEFAULT_SOUND = {"mix": "production/final_mix.wav", "timing": "production/timing_actual.json"}
_DEFAULT_SAMPLE = {
    "mix": "production/final_mix.wav",
    "subtitles": "production/subtitles.srt",
    "storyboard": "production/storyboard.yaml",
    "video": "production/final.mp4",
}


def parse_confirmation(quote: str) -> dict:
    # NFKC folds full-width digits and punctuation from Chinese IMEs (５ → 5, ， → ,).
    normalized = unicodedata.normalize("NFKC", quote)
    compact = re.sub(r"[\s。；;：:!！?？]", "", normalized.replace("，", ",").replace("、", ",")).replace("拍版", "拍板")
    match = _COMMAND.fullmatch(compact)
    if not match:
        raise ValueError("只接受独立的“拍板方案/文案/画风/定妆/声音/样片/成片”或“撤回…”口令；"
                         "附带修改意见时先处理修改")
    action, label, excluded = match.groups()
    if excluded and action == "撤回":
        raise ValueError("撤回口令不使用“除”；请用 --eps 指定范围")
    return {"action": "approve" if action == "拍板" else "revoke",
            "gate": CONFIRMATIONS[label],
            "exclude": [int(value) for value in re.findall(r"[0-9]+", excluded)] if excluded else []}


def _planned_episodes(project: Path) -> list[int]:
    plan = load_yaml(Path(project) / "plan/episodes.yaml", {}) or {}
    rows = plan if isinstance(plan, list) else plan.get("episodes", [])
    return sorted({row["ep"] for row in rows if isinstance(row, dict) and type(row.get("ep")) is int})


def _sound_waiting(project: Path) -> list[int]:
    """Episodes with a finished mix whose sound confirmation is not current."""
    from .guard import sound_required
    configured = sound_paths(project)
    waiting = []
    for ep in _planned_episodes(project):
        if (Path(project) / f"episodes/ep{ep:02d}" / configured["mix"]).is_file() \
                and sound_required(project, ep) \
                and confirmation_state(project, "sound", ep)["state"] != "passed":
            waiting.append(ep)
    return waiting


def _relative_asset(project: Path, path: str) -> Path:
    base = Path(project).resolve()
    candidate = (base / path).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"交付物路径越出项目目录：{path}")
    return candidate


def _spoken_snapshot(path: Path) -> str:
    return re.sub(r"\s+", "", parse_draft(path.read_text(encoding="utf-8"))["spoken"])


def _spoken_change(before: str, after: str) -> str:
    changes = [part for part in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
               if part[0] != "equal"]
    if not changes:
        return "纯口播文字未变化"
    removed = sum(end - start for _, start, end, _, _ in changes)
    added = sum(end - start for _, _, _, start, end in changes)
    _, old_start, old_end, new_start, new_end = changes[0]
    old = before[old_start:old_end][:24] or "（空）"
    new = after[new_start:new_end][:24] or "（空）"
    return f"{len(changes)} 处；删 {removed} 字、增 {added} 字；首处「{old}」→「{new}」"


def _image_list(value: object) -> list[str] | None:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return [item.strip() for item in value]


def visual_manifest(project: Path, gate: str) -> dict:
    """Read the style or character-sheet manifest and list the images it binds."""
    name = STYLE_MANIFEST if gate == "style" else CHARACTER_MANIFEST
    path = Path(project) / name
    if not path.is_file():
        return {"passed": False, "path": name, "images": [], "errors": [f"缺少确认交付物：{name}"]}
    try:
        data = load_yaml(path, {}) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {"passed": False, "path": name, "images": [], "errors": [f"{name} 无法解析"]}
    errors: list[str] = []
    images: list[str] = []
    if not isinstance(data, dict):
        return {"passed": False, "path": name, "images": [], "errors": [f"{name} 必须是映射"]}
    if gate == "style":
        candidates = data.get("candidates")
        rows = {str(row.get("id")): row for row in candidates if isinstance(row, dict) and row.get("id")} \
            if isinstance(candidates, list) else {}
        chosen = data.get("chosen")
        if not rows:
            errors.append(f"{name} 需要 candidates 列表，每项有 id 和 images")
        elif not isinstance(chosen, str) or chosen not in rows:
            errors.append(f"{name} 的 chosen 须填写选中的候选 id（用户选定后再填）")
        else:
            listed = _image_list(rows[chosen].get("images"))
            if listed is None:
                errors.append(f"{name} 选中候选 {chosen} 没有 images")
            else:
                images = listed
    else:
        characters = data.get("characters")
        if isinstance(characters, list):
            characters = {str(row.get("id")): row for row in characters if isinstance(row, dict) and row.get("id")}
        if not isinstance(characters, dict) or not characters:
            errors.append(f"{name} 需要 characters，按稳定人物 ID 列出定妆图")
        else:
            for cid, row in characters.items():
                listed = _image_list(row.get("images") if isinstance(row, dict) else None)
                if listed is None:
                    errors.append(f"{name} 人物 {cid} 没有定妆图 images")
                else:
                    images.extend(listed)
    return {"passed": not errors, "path": name, "images": list(dict.fromkeys(images)), "errors": errors,
            "data": data}


def sound_paths(project: Path) -> dict:
    settings = load_config(project).get("mix", {})
    settings = settings if isinstance(settings, dict) else {}
    return {"mix": str(settings.get("output") or _DEFAULT_SOUND["mix"]),
            "timing": str(settings.get("timing_output") or _DEFAULT_SOUND["timing"])}


def deliverables(project: Path, gate: str, episodes: list[int] | None = None) -> dict:
    """Hash only approved outputs, never review reports or history folders."""
    project = Path(project)
    paths: list[tuple[str, bool]] = []
    if gate == "plan":
        paths = [(name, False) for name in (
            "analysis/book_brief.md", "analysis/characters.yaml", "plan/episodes.yaml")]
    elif gate in ("style", "characters"):
        manifest = visual_manifest(project, gate)
        if not manifest["passed"]:
            return {"passed": False, "files": {}, "errors": manifest["errors"]}
        paths = [(manifest["path"], False), *[(image, False) for image in manifest["images"]]]
    elif gate == "sound":
        if not episodes:
            return {"passed": False, "errors": ["请指定至少一集"]}
        configured = sound_paths(project)
        for ep in episodes:
            if ep < 1:
                return {"passed": False, "errors": ["集号必须为正整数"]}
            prefix = f"episodes/ep{ep:02d}/"
            paths += [(prefix + configured["mix"], False), (prefix + configured["timing"], False)]
    elif gate in ("script", "sample", "release"):
        if not episodes:
            return {"passed": False, "errors": ["请指定至少一集"]}
        for ep in episodes:
            if ep < 1:
                return {"passed": False, "errors": ["集号必须为正整数"]}
            prefix = f"episodes/ep{ep:02d}/"
            if gate == "script":
                paths.append((prefix + "final.md", True))
            else:
                manifest = load_yaml(project / prefix / "production/approval_assets.yaml", {}) or {}
                assets = manifest.get("assets", {}) if isinstance(manifest, dict) else {}
                defaults = _DEFAULT_SAMPLE if gate == "sample" else {
                    "subtitles": _DEFAULT_SAMPLE["subtitles"], "video": _DEFAULT_SAMPLE["video"]}
                if assets and not isinstance(assets, dict):
                    return {"passed": False, "errors": [f"第 {ep} 集 approval_assets.yaml 的 assets 必须为映射"]}
                for kind, default in defaults.items():
                    paths.append((prefix + str(assets.get(kind, default)), False))
        if gate == "release":
            paths.append(("release/compliance.yaml", False))
    else:
        return {"passed": False, "errors": [f"未知确认项：{gate}"]}

    files: dict[str, str] = {}
    errors: list[str] = []
    for relative, spoken in paths:
        try:
            path = _relative_asset(project, relative)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if {"_reports", "_history"} & set(path.relative_to(project.resolve()).parts):
            errors.append(f"自动报告或历史文件不能作为确认交付物：{relative}")
            continue
        if not path.is_file():
            from .archive import archived_digest
            archived = archived_digest(project, relative)
            if archived and not spoken:
                files[relative] = archived
                continue
            errors.append(f"缺少确认交付物：{relative}")
            continue
        if spoken:
            files[relative] = hashlib.sha256(_spoken_snapshot(path).encode("utf-8")).hexdigest()
        else:
            files[relative] = sha256_file(path)
    return {"passed": not errors and bool(paths), "files": files, "errors": errors}


def _session_user_message() -> tuple[str | None, str]:
    """Find the latest user message in this Codex thread, if locally readable."""
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if not thread_id:
        return None, "当前进程没有 CODEX_THREAD_ID，未能核对会话原话"
    folder = Path.home() / ".codex/sessions"
    matches = list(folder.rglob(f"*{thread_id}*.jsonl")) if folder.is_dir() else []
    if len(matches) != 1:
        return None, "找不到唯一的本机会话记录，未能核对原话"
    latest = None
    try:
        for line in matches[0].open(encoding="utf-8"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = row.get("payload", {})
            if row.get("type") == "response_item" and item.get("type") == "message" and item.get("role") == "user":
                content = item.get("content", [])
                latest = "".join(part.get("text", "") for part in content if part.get("type") == "input_text")
    except (OSError, ValueError, json.JSONDecodeError):
        return None, "会话记录无法读取，未能核对原话"
    return latest, "" if latest is not None else "会话记录里没有用户消息"


def _append_log(project: Path, record: dict) -> Path:
    path = Path(project) / "approvals/log.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(yaml.safe_dump([record], allow_unicode=True, sort_keys=False))
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream, fcntl.LOCK_UN)
    return path


def read_log(project: Path) -> list[dict]:
    rows = load_yaml(Path(project) / "approvals/log.yaml", []) or []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("approvals/log.yaml 格式错误")
    return rows


def _imported_round(project: Path, ep: int, directory: Path, before: str, after: str,
                    prior_at: str) -> dict | None:
    """Match a reviewed, intact edit round to the old approval and current script."""
    from . import feedback

    base = (project / "feedback/rounds").resolve()
    if directory.is_symlink() or directory.parent.resolve() != base or not directory.is_dir():
        return None
    evidence_files = [directory / name for name in ("baseline.json", "diff.json", "learning.yaml",
                      "original.md", "revised.txt", "edited.md", "edited.docx")]
    if any(path.is_symlink() for path in evidence_files):
        return None
    try:
        baseline = json.loads((directory / "baseline.json").read_text(encoding="utf-8"))
        report = json.loads((directory / "diff.json").read_text(encoding="utf-8"))
        learning = load_yaml(directory / "learning.yaml", {}) or {}
        if not isinstance(baseline, dict):
            return None
        identity = baseline.get("identity")
        edited_files = [path for path in (directory / "edited.md", directory / "edited.docx") if path.is_file()]
        if (not isinstance(identity, str) or len(edited_files) != 1
                or not isinstance(report, dict) or not isinstance(learning, dict)
                or learning.get("status") != "reviewed"
                or learning.get("round_id") != directory.name
                or baseline.get("episode") != ep or report.get("episode") != ep
                or Path(baseline.get("project", "")).resolve() != project
                or baseline.get("source_generation") != source_generation(project)
                or report.get("source_generation") != baseline["source_generation"]
                or report.get("baseline_identity") != identity
                or report.get("prior_approval_at") != prior_at
                or report.get("round_id") != directory.name
                or not isinstance(baseline.get("blocks"), list)
                or not all(isinstance(block, dict) and isinstance(block.get("text"), str)
                           for block in baseline["blocks"])):
            return None
        baseline_spoken = re.sub(r"\s+", "", "".join(block["text"] for block in baseline["blocks"]))
        if hashlib.sha256(baseline_spoken.encode()).hexdigest() != before:
            return None
        original = directory / "original.md"
        edited = edited_files[0]
        edited_digest = sha256_file(edited)
        if (sha256_file(original) != baseline.get("draft_sha256")
                or report.get("edited_file_sha256") != edited_digest
                or hashlib.sha256((identity + edited_digest).encode()).hexdigest()[:20] != directory.name
                or hashlib.sha256(_spoken_snapshot(original).encode()).hexdigest() != before):
            return None
        parsed = feedback.read_markdown(edited) if edited.suffix == ".md" else feedback.read_docx(edited)
        comparison = feedback.compare(baseline["blocks"], parsed["paragraphs"])
        revised = (directory / "revised.txt").read_text(encoding="utf-8")
        if (comparison["changes"] != report.get("changes")
                or comparison["revised_text"] != revised
                or hashlib.sha256(re.sub(r"\s+", "", revised).encode()).hexdigest() != after):
            return None
        return {"round": str(directory.relative_to(project)), "round_id": directory.name,
                "edited_file_sha256": edited_digest, "report_sha256": sha256_file(directory / "diff.json"),
                "prior_approval_at": prior_at,
                "original_spoken_sha256": before, "revised_spoken_sha256": after}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError):
        return None


def _apply_replacements(text: str, replacements: list[dict]) -> str | None:
    for item in replacements:
        if (not isinstance(item, dict) or not isinstance(item.get("before"), str)
                or not isinstance(item.get("after"), str) or not item["before"]
                or not item["after"] or item["before"] == item["after"]
                or text.count(item["before"]) != 1):
            return None
        text = text.replace(item["before"], item["after"], 1)
    return text


def _whole_script_replacement(before: str, after: str) -> bool:
    return before != after and difflib.SequenceMatcher(None, before, after, autojunk=False).ratio() <= 0.2


def _spoken_edit_ratio(before: str, after: str) -> float:
    edits = sum((i2 - i1) + (j2 - j1)
                for kind, i1, i2, j1, j2 in difflib.SequenceMatcher(
                    None, before, after, autojunk=False).get_opcodes() if kind != "equal")
    return edits / max(1, len(before))


def _minor_change_limit(project: Path) -> float | None:
    value = load_config(project).get("approvals", {}).get("minor_change_ratio", 0.03)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1:
        return None
    return float(value)


def _instruction_id(record: dict) -> str:
    payload = {key: value for key, value in record.items()
               if key not in {"id", "created_at", "session", "transcript_check"}}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def _user_instruction_basis(project: Path, ep: int, path: Path, before: str, after: str,
                            prior_at: str, depth: int = 0, require_current: bool = True) -> dict | None:
    if depth > 32:
        return None
    instruction_dir = project / "feedback/instructions"
    if instruction_dir.is_symlink():
        return None
    base = (project / "feedback/instructions").resolve()
    if path.is_symlink() or path.parent.resolve() != base or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("id") != path.stem or _instruction_id(record) != path.stem:
            return None
        replacements = record.get("replacements")
        original = record.get("before_spoken")
        recorded_after = record.get("after_spoken")
        final = project / f"episodes/ep{ep:02d}/final.md"
        if (not isinstance(original, str) or not isinstance(recorded_after, str)
                or record.get("schema") != 1 or record.get("source") != "user_message"
                or record.get("episode") != ep or record.get("prior_approval_at") != prior_at
                or record.get("before_spoken_sha256") != before
                or hashlib.sha256(original.encode()).hexdigest() != before
                or not isinstance(record.get("quote"), str) or not record["quote"].strip()
                or record.get("transcript_check") not in {"matched", "unavailable"}
                or record.get("source_generation") != source_generation(project)
                or not isinstance(replacements, list) or not replacements
                or any(item.get("after") not in record["quote"] for item in replacements
                       if isinstance(item, dict) and isinstance(item.get("after"), str))
                or any(not isinstance(item, dict) for item in replacements)):
            return None
        transformed = _apply_replacements(original, replacements)
        current = _spoken_snapshot(final) if require_current else recorded_after
        if (transformed is None or transformed != recorded_after or _whole_script_replacement(original, transformed)
                or current != recorded_after
                or hashlib.sha256(recorded_after.encode()).hexdigest() != after
                or record.get("after_spoken_sha256") != after):
            return None
        rows = [row for row in read_log(project) if row.get("gate") == "script"
                and isinstance(row.get("episodes"), list) and ep in row["episodes"]
                and row.get("at") == prior_at]
        if len(rows) != 1:
            return None
        prior = rows[0]
        relative = f"episodes/ep{ep:02d}/final.md"
        prior_files = prior.get("deliverables")
        snapshots = prior.get("spoken_snapshots")
        if (prior.get("action") not in {"approve", "carry"} or not isinstance(prior_files, dict)
                or prior_files.get(relative) != before or not isinstance(snapshots, dict)
                or snapshots.get(relative) != original):
            return None
        if prior.get("action") == "carry":
            if not _prior_carry_valid(project, ep, prior, depth=depth + 1):
                return None
        return {"kind": "user_instruction", "record": str(path.relative_to(project)),
                "record_id": path.stem, "record_sha256": sha256_file(path),
                "prior_approval_at": prior_at, "before_spoken_sha256": before,
                "after_spoken_sha256": after}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError):
        return None


def _carry_basis_valid(project: Path, ep: int, basis: dict, before: str, after: str,
                       *, prior_at: str | None = None, depth: int = 0,
                       require_current: bool = True) -> bool:
    if basis.get("kind") == "user_instruction":
        if (basis.get("before_spoken_sha256") != before or basis.get("after_spoken_sha256") != after
                or not isinstance(basis.get("record"), str)
                or not isinstance(basis.get("prior_approval_at"), str)
                or (prior_at is not None and basis["prior_approval_at"] != prior_at)):
            return False
        path = project / basis["record"]
        checked = _user_instruction_basis(project, ep, path, before, after,
                                          basis["prior_approval_at"], depth=depth + 1,
                                          require_current=require_current)
        return checked == basis
    if not all(isinstance(basis.get(key), str) for key in (
            "round", "original_spoken_sha256", "revised_spoken_sha256")):
        return False
    if not isinstance(prior_at, str):
        return False
    return _imported_round(project, ep, project / basis["round"], before, after, prior_at) == basis


def _prior_carry_valid(project: Path, ep: int, record: dict, depth: int = 0) -> bool:
    if depth > 32:
        return False
    prior_at = record.get("prior_approval_at")
    basis = record.get("basis")
    relative = f"episodes/ep{ep:02d}/final.md"
    if not isinstance(prior_at, str) or not isinstance(basis, dict):
        return False
    previous = [row for row in read_log(project) if row.get("gate") == "script"
                and isinstance(row.get("episodes"), list) and ep in row["episodes"]
                and row.get("at") == prior_at]
    if len(previous) != 1:
        return False
    previous_files = previous[0].get("deliverables")
    current_files = record.get("deliverables")
    if not isinstance(previous_files, dict) or not isinstance(current_files, dict):
        return False
    return _carry_basis_valid(project, ep, basis, previous_files.get(relative),
                              current_files.get(relative), prior_at=prior_at, depth=depth + 1,
                              require_current=False)


def _assistant_change_basis(project: Path, ep: int, path: Path, before: str, after: str,
                            prior_at: str, *, require_current: bool = True) -> dict | None:
    folder = project / "feedback/assistant_changes"
    if folder.is_symlink() or path.is_symlink() or path.parent.resolve() != folder.resolve() or not path.is_file():
        return None
    limit = _minor_change_limit(project)
    if limit is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("id") != path.stem or _instruction_id(record) != path.stem:
            return None
        original, recorded_after = record.get("before_spoken"), record.get("after_spoken")
        replacements = record.get("replacements")
        flags = record.get("risk_review")
        if (record.get("schema") != 1 or record.get("source") != "assistant"
                or record.get("episode") != ep or record.get("prior_approval_at") != prior_at
                or record.get("source_generation") != source_generation(project)
                or not isinstance(original, str) or not isinstance(recorded_after, str)
                or hashlib.sha256(original.encode()).hexdigest() != before
                or record.get("before_spoken_sha256") != before
                or record.get("after_spoken_sha256") != after
                or not isinstance(replacements, list) or not replacements
                or not isinstance(flags, dict) or any(flags.get(name) is not False
                                                      for name in ("identity_change", "plot_fact_change", "ending_change"))
                or not isinstance(flags.get("note"), str) or not flags["note"].strip()):
            return None
        transformed = _apply_replacements(original, replacements)
        current = _spoken_snapshot(project / f"episodes/ep{ep:02d}/final.md") if require_current else recorded_after
        ratio = _spoken_edit_ratio(original, transformed) if transformed is not None else 1.0
        recorded_ratio = record.get("change_ratio")
        if (transformed is None or transformed != recorded_after or current != recorded_after
                or _whole_script_replacement(original, transformed) or ratio > limit
                or isinstance(recorded_ratio, bool) or not isinstance(recorded_ratio, (int, float))
                or not math.isfinite(recorded_ratio) or abs(recorded_ratio - ratio) > 1e-12):
            return None
        approvals = [row for row in read_log(project) if row.get("gate") == "script"
                     and isinstance(row.get("episodes"), list) and ep in row["episodes"]
                     and row.get("at") == prior_at]
        if len(approvals) != 1:
            return None
        approval = approvals[0]
        relative = f"episodes/ep{ep:02d}/final.md"
        files, snapshots = approval.get("deliverables"), approval.get("spoken_snapshots")
        if (approval.get("action") not in {"approve", "carry"} or not isinstance(files, dict)
                or files.get(relative) != before or not isinstance(snapshots, dict)
                or snapshots.get(relative) != original):
            return None
        if approval.get("action") == "carry" and not _prior_carry_valid(project, ep, approval):
            return None
        return {"kind": "assistant_change", "record": str(path.relative_to(project)),
                "record_id": path.stem, "record_sha256": sha256_file(path),
                "prior_approval_at": prior_at, "before_spoken_sha256": before,
                "after_spoken_sha256": after, "change_ratio": ratio}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError):
        return None


def record_assistant_edit(project: Path, ep: int, replacements: list[tuple[str, str]], risk_review: str,
                          *, no_identity_change: bool, no_plot_fact_change: bool,
                          no_ending_change: bool) -> dict:
    """Record an assistant-authored small edit after explicit semantic risk review."""
    project = Path(project).resolve()
    if (type(ep) is not int or ep < 1 or not isinstance(replacements, list) or not replacements
            or any(not isinstance(item, (tuple, list)) or len(item) != 2 for item in replacements)
            or not isinstance(risk_review, str) or not risk_review.strip()
            or no_identity_change is not True or no_plot_fact_change is not True or no_ending_change is not True):
        return {"passed": False, "errors": ["需要集号、精确替换和人物身份/情节事实/结尾三项明确的无风险核查"]}
    limit = _minor_change_limit(project)
    if limit is None:
        return {"passed": False, "errors": ["approvals.minor_change_ratio 必须是 0 到 1 之间的数值"]}
    approvals = [row for row in read_log(project) if row.get("gate") == "script"
                 and isinstance(row.get("episodes"), list) and ep in row["episodes"]]
    if not approvals or approvals[-1].get("action") not in {"approve", "carry"}:
        return {"passed": False, "errors": ["本集没有有效的既有文案确认可供登记小改"]}
    prior = approvals[-1]
    relative = f"episodes/ep{ep:02d}/final.md"
    files, snapshots = prior.get("deliverables"), prior.get("spoken_snapshots")
    before = snapshots.get(relative) if isinstance(snapshots, dict) else None
    before_hash = files.get(relative) if isinstance(files, dict) else None
    if (not isinstance(before, str) or hashlib.sha256(before.encode()).hexdigest() != before_hash
            or (prior["action"] == "carry" and not _prior_carry_valid(project, ep, prior))):
        return {"passed": False, "errors": ["既有确认或沿用依据无效，不能登记助手小改"]}
    changes = []
    for old, new in replacements:
        if not isinstance(old, str) or not old or not isinstance(new, str) or not new or old == new:
            return {"passed": False, "errors": ["每条替换都必须有不同且非空的改前、改后文字"]}
        changes.append({"before": old, "after": new})
    transformed = _apply_replacements(before, changes)
    current = _spoken_snapshot(project / relative)
    if transformed is None or transformed == before or transformed != current:
        return {"passed": False, "errors": ["当前定稿不等于登记的精确替换结果"]}
    ratio = _spoken_edit_ratio(before, transformed)
    if _whole_script_replacement(before, transformed) or ratio > limit:
        return {"passed": False, "errors": [f"改动比例 {ratio:.1%} 超过小改阈值 {limit:.1%}，须重新确认"]}
    record = {"schema": 1, "source": "assistant", "episode": ep,
              "created_at": datetime.now(timezone.utc).isoformat(), "prior_approval_at": prior.get("at"),
              "source_generation": source_generation(project), "file": relative,
              "before_spoken": before, "before_spoken_sha256": before_hash,
              "replacements": changes, "after_spoken": current,
              "after_spoken_sha256": hashlib.sha256(current.encode()).hexdigest(),
              "change_ratio": ratio,
              "risk_review": {"identity_change": False, "plot_fact_change": False,
                              "ending_change": False, "note": risk_review.strip()}}
    record["id"] = _instruction_id(record)
    folder = project / "feedback/assistant_changes"
    if folder.is_symlink():
        return {"passed": False, "errors": ["助手小改证据目录是符号链接，拒绝写入"]}
    path = folder / f"{record['id']}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return {"passed": False, "errors": ["同一小改证据文件无法读取，未覆盖"]}
        if not isinstance(existing, dict) or _instruction_id(existing) != record["id"]:
            return {"passed": False, "errors": ["助手小改证据编号冲突，未覆盖"]}
        record = existing
    else:
        from .common import write_json
        write_json(path, record)
    basis = _assistant_change_basis(project, ep, path, before_hash,
                                    hashlib.sha256(current.encode()).hexdigest(), str(prior.get("at", "")))
    if basis is None:
        return {"passed": False, "errors": ["小改证据写入后复核失败；记录保留但不会被 next 采纳"]}
    return {"passed": True, "record": record, "path": str(path), "change_ratio": ratio,
            "next_actions": ["运行 next；小改不阻塞制作，在下次人工确认时一并过目"]}


def record_user_edit_instruction(project: Path, ep: int, quote: str,
                                 replacements: list[tuple[str, str]], session: str = "Codex",
                                 verify_transcript: bool = True) -> dict:
    """Bind an explicit user request to exact, deterministic spoken-text replacements."""
    project = Path(project).resolve()
    quote = quote.strip()
    if (type(ep) is not int or ep < 1 or not quote or not isinstance(replacements, list)
            or not replacements or any(not isinstance(item, (tuple, list)) or len(item) != 2
                                       for item in replacements)):
        return {"passed": False, "errors": ["需要有效集号、用户原话和至少一条改前/改后替换"]}
    rows = [row for row in read_log(project) if row.get("gate") == "script"
            and isinstance(row.get("episodes"), list) and ep in row["episodes"]]
    if not rows or rows[-1].get("action") not in {"approve", "carry"}:
        return {"passed": False, "errors": ["本集没有可沿用的文案确认；撤回后须重新确认"]}
    prior = rows[-1]
    relative = f"episodes/ep{ep:02d}/final.md"
    old_files = prior.get("deliverables")
    snapshots = prior.get("spoken_snapshots")
    before = snapshots.get(relative) if isinstance(snapshots, dict) else None
    before_digest = old_files.get(relative) if isinstance(old_files, dict) else None
    if (not isinstance(before, str) or hashlib.sha256(before.encode()).hexdigest() != before_digest
            or (prior["action"] == "carry" and not _prior_carry_valid(project, ep, prior))):
        return {"passed": False, "errors": ["既有确认或沿用依据无效，不能登记指令改动"]}
    change_rows = []
    for old, new in replacements:
        if (not isinstance(old, str) or not old or not isinstance(new, str) or not new or old == new
                or new not in quote):
            return {"passed": False, "errors": ["每条替换都必须有明确改前、改后文字，且改后文字出现在用户原话中"]}
        change_rows.append({"before": old, "after": new})
    transformed = _apply_replacements(before, change_rows)
    current = _spoken_snapshot(project / relative)
    if (transformed is None or transformed == before or _whole_script_replacement(before, transformed)
            or transformed != current):
        return {"passed": False, "errors": ["当前定稿不等于用户指令列出的精确替换结果；请按改动范围重新核对"]}
    warnings = []
    transcript_check = "unavailable"
    configured = load_yaml(project / "project.yaml", {}) or {}
    if verify_transcript and configured.get("approvals", {}).get("verify_transcript", True):
        latest, warning = _session_user_message()
        if latest is not None and latest.strip() != quote:
            return {"passed": False, "errors": ["最近一条用户消息与登记的指令原话不一致"]}
        if warning:
            warnings.append(warning)
        else:
            transcript_check = "matched"
    record = {"schema": 1, "source": "user_message", "episode": ep, "quote": quote,
              "session": session, "transcript_check": transcript_check,
              "created_at": datetime.now(timezone.utc).isoformat(), "prior_approval_at": prior.get("at"),
              "source_generation": source_generation(project), "file": relative,
              "before_spoken": before, "before_spoken_sha256": before_digest,
              "replacements": change_rows, "after_spoken": current,
              "after_spoken_sha256": hashlib.sha256(current.encode()).hexdigest()}
    record["id"] = _instruction_id(record)
    folder = project / "feedback/instructions"
    if folder.is_symlink():
        return {"passed": False, "errors": ["指令证据目录是符号链接，拒绝写入"]}
    path = folder / f"{record['id']}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return {"passed": False, "errors": ["同一指令证据文件已存在但无法读取，未覆盖"]}
        if not isinstance(existing, dict) or _instruction_id(existing) != record["id"]:
            return {"passed": False, "errors": ["指令证据编号冲突，未覆盖"]}
        record = existing
    else:
        from .common import write_json
        write_json(path, record)
    return {"passed": True, "record": record, "path": str(path), "warnings": warnings}


def _append_carry(project: Path, ep: int, prior: dict, record: dict) -> bool:
    """Append only if the prior approval and final text are unchanged under the log lock."""
    path = project / "approvals/log.yaml"
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.seek(0)
            rows = yaml.safe_load(stream.read()) or []
            if not isinstance(rows, list):
                return False
            matching = [row for row in rows if isinstance(row, dict) and row.get("gate") == "script"
                        and isinstance(row.get("episodes"), list) and ep in row["episodes"]]
            if (not matching or matching[-1] != prior
                    or deliverables(project, "script", [ep])["files"] != record["deliverables"]):
                return False
            basis = record["basis"]
            before = prior.get("deliverables", {}).get(f"episodes/ep{ep:02d}/final.md")
            after = record["deliverables"].get(f"episodes/ep{ep:02d}/final.md")
            if not _carry_basis_valid(project, ep, basis, before, after, prior_at=prior.get("at")):
                return False
            stream.seek(0, os.SEEK_END)
            stream.write(yaml.safe_dump([record], allow_unicode=True, sort_keys=False))
            stream.flush()
            os.fsync(stream.fileno())
            return True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def reconcile_imported_edits(project: Path, episodes: list[int]) -> list[int]:
    """Carry script approval only across a reviewed user edit with matching file evidence."""
    project = Path(project).resolve()
    if not (project / "approvals/log.yaml").is_file():
        return []
    rounds = project / "feedback/rounds"
    instruction_dir = project / "feedback/instructions"
    if not rounds.is_dir() and not instruction_dir.is_dir():
        return []
    carried = []
    for ep in sorted(set(episodes)):
        rows = [row for row in read_log(project) if row.get("gate") == "script"
                and isinstance(row.get("episodes"), list) and ep in row["episodes"]]
        if not rows or rows[-1].get("action") not in {"approve", "carry"}:
            continue
        prior = rows[-1]
        if prior["action"] == "carry":
            if not _prior_carry_valid(project, ep, prior):
                continue
        relative = f"episodes/ep{ep:02d}/final.md"
        old_files = prior.get("deliverables")
        before = old_files.get(relative) if isinstance(old_files, dict) else None
        current = deliverables(project, "script", [ep])
        after = current["files"].get(relative)
        if not current["passed"] or not isinstance(before, str) or not after or before == after:
            continue
        snapshot = _spoken_snapshot(project / relative)
        if hashlib.sha256(snapshot.encode()).hexdigest() != after:
            continue
        bases = []
        if rounds.is_dir():
            bases = [basis for directory in sorted(rounds.iterdir())
                     if (basis := _imported_round(project, ep, directory, before, after,
                                                  str(prior.get("at", "")))) is not None]
        if len(bases) == 1:
            basis = bases[0]
            reason = "人工改稿导入后沿用先前文案确认"
        elif not bases and instruction_dir.is_dir():
            matches = []
            for path in sorted(instruction_dir.glob("*.json")):
                basis = _user_instruction_basis(project, ep, path, before, after, str(prior.get("at", "")))
                if basis is not None:
                    matches.append(basis)
            if len(matches) == 1:
                basis = matches[0]
                reason = "按用户聊天原话完成精确修改后沿用文案确认"
            else:
                continue
        else:
            continue
        record = {"gate": "script", "episodes": [ep], "action": "carry", "quote": None,
                  "at": datetime.now(timezone.utc).isoformat(), "session": "bookflow", "reason": reason,
                  "prior_approval_at": prior.get("at"), "deliverables": {relative: after},
                  "spoken_snapshots": {relative: snapshot}, "basis": basis}
        if _append_carry(project, ep, prior, record):
            carried.append(ep)
    return carried


def record_confirmation(project: Path, gate: str, quote: str,
                        episodes: list[int] | None = None, session: str = "Codex",
                        verify_transcript: bool = True) -> dict:
    project = Path(project)
    command = parse_confirmation(quote)
    if command["gate"] != gate:
        return {"passed": False, "errors": ["口令确认项与命令确认项不一致"]}
    configured = load_yaml(project / "project.yaml", {}) or {}
    if gate in ("script", "release"):
        approved_eps = sorted(set(episodes or _planned_episodes(project)))
    elif gate == "sound":
        approved_eps = sorted(set(episodes or _sound_waiting(project)))
    else:
        approved_eps = [1] if gate == "sample" else []
    excluded = set(command["exclude"])
    if excluded - set(approved_eps):
        return {"passed": False, "errors": ["口令排除的集数不在本次范围内"]}
    approved_eps = [ep for ep in approved_eps if ep not in excluded]
    if gate not in PROJECT_GATES and not approved_eps:
        return {"passed": False, "errors": ["没有可确认的集数"]}
    if gate == "sound" and command["action"] == "approve":
        from .audition import sheet_state
        stale = [ep for ep in approved_eps if sheet_state(project, ep)["state"] != "current"]
        if stale:
            return {"passed": False, "errors": [f"第 {ep} 集试听单缺失或落后于当前混音；先运行 audition 并试听"
                                                for ep in stale]}
    warnings: list[str] = []
    if verify_transcript and configured.get("approvals", {}).get("verify_transcript", True):
        latest, warning = _session_user_message()
        if latest is not None and latest.strip() != quote.strip():
            return {"passed": False, "errors": ["最近一条用户消息与传入的确认原话不一致"]}
        if warning:
            warnings.append(warning)
    assets = deliverables(project, gate, approved_eps) if command["action"] == "approve" else {"passed": True, "files": {}, "errors": []}
    if not assets["passed"]:
        return {"passed": False, "errors": assets["errors"]}
    snapshots = {}
    if gate == "script" and command["action"] == "approve":
        for relative, digest in assets["files"].items():
            snapshot = _spoken_snapshot(_relative_asset(project, relative))
            if hashlib.sha256(snapshot.encode("utf-8")).hexdigest() != digest:
                return {"passed": False, "errors": [f"{relative} 在确认期间发生变化，请重新核对"]}
            snapshots[relative] = snapshot
    record = {"gate": gate, "episodes": approved_eps, "action": command["action"],
              "quote": quote, "at": datetime.now(timezone.utc).isoformat(), "session": session,
              "deliverables": assets["files"]}
    if snapshots:
        record["spoken_snapshots"] = snapshots
    path = _append_log(project, record)
    return {"passed": True, "record": record, "path": str(path), "warnings": warnings}


def confirmation_state(project: Path, gate: str, ep: int | None = None) -> dict:
    """Evaluate the latest matching log record and explain stale assets."""
    rows = [row for row in read_log(project) if row.get("gate") == gate and
            (ep is None or ep in row.get("episodes", []))]
    if not rows:
        return {"state": "pending", "changed_files": []}
    last = rows[-1]
    if last.get("action") == "revoke":
        return {"state": "revoked", "changed_files": []}
    current = deliverables(project, gate, [ep] if ep is not None else last.get("episodes", []))
    old = last.get("deliverables", {})
    changed = []
    snapshots = last.get("spoken_snapshots") if isinstance(last.get("spoken_snapshots"), dict) else {}
    for name, digest in old.items():
        if ep is not None and gate not in PROJECT_GATES and not name.startswith(f"episodes/ep{ep:02d}/") and name != "release/compliance.yaml":
            continue
        if current["files"].get(name) != digest:
            row = {"file": name, "reason": "missing" if name not in current["files"] else "content_changed",
                   "before_sha256": digest, "after_sha256": current["files"].get(name)}
            match = re.match(r"episodes/ep([0-9]+)/", name)
            if match:
                row["episode"] = int(match.group(1))
            if gate == "script" and row["reason"] == "content_changed":
                snapshot = snapshots.get(name)
                if isinstance(snapshot, str) and hashlib.sha256(snapshot.encode("utf-8")).hexdigest() == digest:
                    row["detail"] = _spoken_change(snapshot, _spoken_snapshot(_relative_asset(project, name)))
                else:
                    row["detail"] = "旧确认无有效口播快照，无法复原改动文字"
            elif row["reason"] == "missing":
                row["detail"] = "文件缺失"
            else:
                row["detail"] = "交付物内容变化"
            changed.append(row)
    if current.get("errors"):
        changed.extend({"file": error.removeprefix("缺少确认交付物："), "reason": "missing"}
                       for error in current["errors"] if error.startswith("缺少确认交付物：") and
                       not any(item["file"] == error.removeprefix("缺少确认交付物：") for item in changed))
    if gate == "script" and last.get("action") == "carry":
        basis = last.get("basis")
        affected = last.get("episodes", [])
        episode = ep if type(ep) is int else affected[0] if affected and type(affected[0]) is int else None
        valid = episode is not None and isinstance(basis, dict) and _prior_carry_valid(
            Path(project).resolve(), episode, last)
        if not valid:
            changed.append({"file": f"episodes/ep{episode:02d}/final.md" if episode is not None
                            else "approvals/log.yaml",
                            "reason": "carry_evidence_invalid", "detail": "人工改稿沿用依据已失效"})
    change_pending = []
    change_pending_items = []
    minor_files = set()
    if gate == "script":
        assistant_dir = Path(project).resolve() / "feedback/assistant_changes"
        if assistant_dir.is_dir() and not assistant_dir.is_symlink():
            for item in changed:
                episode = item.get("episode")
                if type(episode) is not int or item.get("reason") != "content_changed":
                    continue
                candidates = []
                for path in sorted(assistant_dir.glob("*.json")):
                    basis = _assistant_change_basis(Path(project).resolve(), episode, path,
                                                    item.get("before_sha256", ""),
                                                    item.get("after_sha256", ""), str(last.get("at", "")))
                    if basis is not None:
                        candidates.append((path, basis))
                if len(candidates) == 1:
                    path, basis = candidates[0]
                    report = json.loads(path.read_text(encoding="utf-8"))
                    minor_files.add(item["file"])
                    text = (f"第{episode}集改动待过目：{_spoken_change(report['before_spoken'], report['after_spoken'])}；"
                            f"风险核查：{report['risk_review']['note']}（{basis['change_ratio']:.1%}）")
                    change_pending.append(text)
                    change_pending_items.append({"episode": episode, "created_at": str(report.get("created_at", "")),
                                                 "text": text})
    # Keep all changed hashes visible to downstream media invalidation. Only
    # evidenced minor edits are exempt from a new human script confirmation.
    blocking_changes = [item for item in changed if not (
        item["reason"] == "content_changed" and item["file"] in minor_files)]
    return {"state": "invalidated" if blocking_changes or not current["passed"] else "passed",
            "changed_files": changed, "change_pending": change_pending,
            "change_pending_items": change_pending_items,
            "at": last.get("at"), "quote": last.get("quote")}


def migrate_legacy(project: Path) -> dict:
    """Carry valid old G1+G2 into the plan approval and retain all old records.

    A scope that already has any passphrase record (approve, revoke or carry) is
    never migrated: the newer record wins, including a revoke or an approval that
    has since become invalid.
    """
    project = Path(project)
    # log.yaml holds the new passphrase confirmations and must stay in place.
    records = [path for path in (project / "approvals").glob("*.yaml") if path.name != "log.yaml"]
    if not records:
        return {"passed": True, "migrated": [], "warnings": ["没有旧版批准记录"]}
    legacy = project / "approvals/legacy"
    collisions = [str(legacy / path.name) for path in records if (legacy / path.name).exists()]
    if collisions:
        return {"passed": False, "errors": [f"旧批准目标文件已存在：{collisions[0]}"]}
    rows = read_log(project)

    def has_new_record(gate: str, ep: int | None = None) -> bool:
        return any(row.get("gate") == gate and (ep is None or ep in (row.get("episodes") or []))
                   for row in rows)

    migrated: list[str] = []
    warnings: list[str] = []
    if has_new_record("plan"):
        warnings.append("已有新口令方案记录（含撤回或失效），不用旧版 G1/G2 覆盖")
    elif gate_state(project, "G1") == "passed" and gate_state(project, "G2") == "passed":
        assets = deliverables(project, "plan")
        if not assets["passed"]:
            return {"passed": False, "errors": assets["errors"]}
        _append_log(project, {"gate": "plan", "episodes": [], "action": "approve",
                              "quote": "由旧版终端确认迁移", "at": datetime.now(timezone.utc).isoformat(),
                              "session": "legacy_migration", "deliverables": assets["files"],
                              "reason": "G1 和 G2 的最新旧版批准均有效"})
        migrated.append("plan")
    else:
        warnings.append("旧版 G1/G2 未同时有效，方案确认保持待确认")
    if has_new_record("sample", 1):
        if gate_state(project, "AV1", 1) == "passed":
            warnings.append("已有新口令样片记录（含撤回或失效），不用旧版 AV1 覆盖")
    elif gate_state(project, "AV1", 1) == "passed":
        assets = deliverables(project, "sample", [1])
        if assets["passed"]:
            _append_log(project, {"gate": "sample", "episodes": [1], "action": "approve",
                                  "quote": "由旧版终端确认迁移", "at": datetime.now(timezone.utc).isoformat(),
                                  "session": "legacy_migration", "deliverables": assets["files"],
                                  "reason": "第 1 集旧版 AV1 与当前样片交付物均有效"})
            migrated.append("sample")
        else:
            warnings.append("旧版 AV1 有效，但新样片交付物不齐：" + "；".join(assets["errors"]))
    legacy.mkdir(parents=True, exist_ok=True)
    for path in records:
        path.rename(legacy / path.name)
    return {"passed": True, "migrated": migrated, "legacy_records": len(records), "warnings": warnings}
