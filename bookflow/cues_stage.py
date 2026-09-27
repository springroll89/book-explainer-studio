"""Validate and bind a human-authored cue sheet without generating media."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .common import load_config, load_yaml, parse_draft, sha256_file, write_json
from .guard import check as guard_check
from .media_manifest import real_stage_fresh
from .sound import FUNCTIONS, POLICIES, SOUND_CLASSES, _cue_path


def cues_digest(config: dict) -> str:
    relevant = {"cue_path": (config.get("mix") or {}).get("cues"),
                "speech_rate_cpm": (config.get("format") or {}).get("speech_rate_cpm"),
                "budget_per_13min": (config.get("sound_design") or {}).get("budget_per_13min")}
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _number(value: object, label: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}须为有限数字") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{label}须在 {low:g}–{high:g} 范围内")
    return number


def _validate(epdir: Path, config: dict, sheet: object, sidecar: object) -> dict:
    final = epdir / "final.md"
    digest = sha256_file(final)
    if not isinstance(sidecar, dict) or sidecar.get("draft_sha256") != digest:
        raise ValueError("final.sentences.json 句子表不是当前定稿版本")
    sentences = sidecar.get("sentences")
    parsed = parse_draft(final.read_text(encoding="utf-8"))["sentences"]
    if (not isinstance(sentences, list) or not sentences or len(sentences) != len(parsed)
            or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                   or row.get("text") != original.get("text")
                   for row, original in zip(sentences, parsed))):
        raise ValueError("final.sentences.json 句子表与当前定稿不一致")
    ids = [row["id"] for row in sentences]
    if len(ids) != len(set(ids)):
        raise ValueError("final.sentences.json 句子编号重复")
    if not isinstance(sheet, dict) or sheet.get("draft_sha256") != digest or not isinstance(sheet.get("cues"), list):
        raise ValueError("cue 表须含当前定稿哈希和 cues 列表")
    rows = sheet["cues"]
    if not rows and not str(sheet.get("no_sfx_reason", "")).strip():
        raise ValueError("空 cue 表须写明无音效理由")
    config_format = config.get("format")
    config_sound = config.get("sound_design")
    if not isinstance(config_format, dict) or not isinstance(config_sound, dict):
        raise ValueError("声音与语速配置无效")
    cpm = _number(config_format.get("speech_rate_cpm"), "口播语速", 1, 1000)
    by_id = {row["id"]: index for index, row in enumerate(sentences)}
    elapsed_chars = [0]
    for row in sentences:
        elapsed_chars.append(elapsed_chars[-1] + len(row["text"]))
    seen = set()
    event_process = 0
    active = 0
    for cue in rows:
        if not isinstance(cue, dict) or not isinstance(cue.get("cue_id"), str) or not cue["cue_id"].strip():
            raise ValueError("cue 表存在无效编号")
        identity = cue["cue_id"]
        if identity in seen:
            raise ValueError(f"{identity}: cue 编号重复")
        seen.add(identity)
        if cue.get("sound_class") not in SOUND_CLASSES or cue.get("function") not in FUNCTIONS:
            raise ValueError(f"{identity}: 音效类别或叙事功能无效")
        if cue.get("gap_policy") not in POLICIES or cue.get("status") not in {
                "planned", "reused", "asset_ready", "on_hold"}:
            raise ValueError(f"{identity}: 留白策略或 cue 状态无效")
        anchor = cue.get("anchor")
        sid = anchor.get("sentence_id") if isinstance(anchor, dict) else None
        if sid not in by_id:
            raise ValueError(f"{identity}: 锚点句子不存在于当前定稿")
        placement = cue.get("placement") or "before"
        if placement not in {"before", "after"}:
            raise ValueError(f"{identity}: placement 无效")
        _number(cue.get("duration_sec"), f"{identity} 时长", 0.02, 3600)
        _number(cue.get("level_db"), f"{identity} 音量", -60, 12)
        lead = _number(cue.get("lead_in_sec", 0), f"{identity} 提前量", 0, 30)
        if cue["status"] == "on_hold":
            continue
        active += 1
        if cue["sound_class"] in {"event", "process"}:
            event_process += 1
        index = by_id[sid]
        position = elapsed_chars[index + (placement == "after")]
        if position * 60 / cpm - lead < 5:
            raise ValueError(f"{identity}: 预计落点在开场前5秒，须移位或标为 on_hold")
    budget = config_sound.get("budget_per_13min")
    limits = budget.get("cues_event_process") if isinstance(budget, dict) else None
    if not isinstance(limits, list) or len(limits) != 2 or type(limits[1]) is not int or limits[1] < 0:
        raise ValueError("sound_design.budget_per_13min.cues_event_process 配置无效")
    if event_process > limits[1]:
        raise ValueError(f"event+process 共 {event_process} 条，超过每13分钟上限 {limits[1]}")
    return {"cue_count": len(rows), "active_cues": active, "event_process": event_process}


def bind_existing(epdir: Path) -> dict:
    """Bind an already edited cue sheet; never create or overwrite it."""
    epdir = Path(epdir).resolve()
    project = epdir.parent.parent
    final = epdir / "final.md"
    sidecar_path = epdir / "final.sentences.json"
    cue_path = _cue_path(epdir)
    if any(not path.is_file() or path.is_symlink() for path in (final, sidecar_path, cue_path)):
        raise ValueError("正式 cues 阶段需要当前定稿、稳定句子表和人工 cue 表；不会自动覆盖")
    config = load_config(epdir)
    metrics = _validate(epdir, config, load_yaml(cue_path, {}), load_yaml(sidecar_path, {}))
    inputs = [{"path": str(path.relative_to(project)), "sha256": sha256_file(path)}
              for path in (final, sidecar_path)]
    outputs = [{"path": str(cue_path.relative_to(epdir)), "sha256": sha256_file(cue_path)}]
    config_hash = cues_digest(config)
    manifest_path = epdir / "production/manifest.json"
    with (epdir / "production/.manifest.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            manifest = load_yaml(manifest_path, {}) or {}
            if not isinstance(manifest, dict) or manifest.get("mode") not in (None, "real"):
                raise ValueError("现有媒体清单不是正式模式，拒绝覆盖")
            if manifest and (not isinstance(manifest.get("stages"), dict)
                             or not isinstance(manifest.get("charges"), list)):
                raise ValueError("正式媒体清单结构无效，拒绝覆盖")
            stages = manifest.get("stages", {})
            current = stages.get("cues", {})
            if (real_stage_fresh(epdir, "cues", current) and current.get("inputs") == inputs
                    and current.get("outputs") == outputs and current.get("config_sha256") == config_hash):
                return {"status": "success", "passed": True, "summary": "正式 cue 表未变化，已复用",
                        "executed": [], "skipped": ["cues"], "metrics": metrics,
                        "next_actions": [], "artifacts": [str(cue_path), str(manifest_path)]}
            if (any(sha256_file(path) != row["sha256"] for path, row in
                    zip((final, sidecar_path), inputs)) or sha256_file(cue_path) != outputs[0]["sha256"]
                    or cues_digest(load_config(epdir)) != config_hash):
                raise ValueError("cue 输入或配置在绑定期间变化，未写入媒体清单")
            manifest.update(mode="real", charges=manifest.get("charges", []))
            manifest.setdefault("stages", {})["cues"] = {
                "status": "done", "inputs": inputs, "outputs": outputs,
                "config_sha256": config_hash, "cost_cny": 0, "charge_ids": [],
                "metrics": metrics, "completed_at": datetime.now(timezone.utc).isoformat()}
            write_json(manifest_path, manifest)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return {"status": "success", "passed": True, "summary": "现有 cue 表已校验并绑定，未修改人工内容",
            "executed": ["cues"], "metrics": metrics,
            "next_actions": ["继续正式配音与音效阶段"], "artifacts": [str(cue_path), str(manifest_path)]}


def advance(project: Path, epdir: Path) -> dict:
    """Apply the book media gate before touching a real episode manifest."""
    project, epdir = Path(project).resolve(), Path(epdir).resolve()
    guard = guard_check(project, "sound-plan", int(epdir.name[2:]))
    if not guard["passed"]:
        return {"status": "warning", "passed": False, "summary": "正式 cue 表守卫未通过",
                "errors": guard["errors"], "next_actions": ["先完成对应人工确认"], "artifacts": []}
    return bind_existing(epdir)
