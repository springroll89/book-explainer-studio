"""O13 sound design workflow.

The module is deliberately model-free: it creates/checks cue metadata and can
assemble a local preview from already generated assets. Paid generation is
never started by these commands.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal, ROUND_UP
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import atomic_write, latest_draft, load_config, load_yaml, sha256_file, write_json, write_yaml

SOUND_CLASSES = {"ambience", "event", "process", "design", "music"}
FUNCTIONS = {"bed", "story_event", "hook", "reveal", "transition", "mood", "motif"}
POLICIES = {"none", "duck", "gap", "partial_gap"}


def _cue_path(epdir: Path) -> Path:
    from .adapters.ffmpeg_audio import _inside
    epdir = Path(epdir).resolve()
    mix = load_config(epdir).get("mix")
    if not isinstance(mix, dict):
        raise ValueError("混音 cue 路径配置必须是映射")
    path = _inside(epdir, mix.get("cues"), "音效 cue 表")
    if not path.resolve().is_relative_to((epdir / "production").resolve()):
        raise ValueError("音效 cue 表必须在本集 production 内")
    return path


def default_gap_policy(sound_class: str, function: str) -> str:
    if sound_class in {"ambience", "music"}:
        return "duck"
    if sound_class == "event":
        return "gap" if function in {"story_event", "hook", "reveal"} else "duck"
    if sound_class == "process":
        return "partial_gap" if function in {"story_event", "hook", "reveal"} else "duck"
    if sound_class == "design":
        return "gap" if function in {"story_event", "hook", "reveal"} else "duck"
    return "duck"


def _episode_no(epdir: Path) -> int:
    m = re.search(r"ep(\d+)$", epdir.name)
    return int(m.group(1)) if m else 1


def _draft(epdir: Path) -> Path:
    draft = latest_draft(epdir, prefer_final=True)
    if not draft:
        raise ValueError(f"找不到稿件：{epdir}")
    return draft


def _narration_source(epdir: Path) -> Path:
    configured = load_config(epdir).get("sound_design", {}).get("narration", {}).get("audio")
    if configured:
        source = Path(configured)
        source = source if source.is_absolute() else epdir / source
        if not source.is_file():
            raise ValueError(f"配置的旁白音频不存在：{source}")
        return source
    manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
    voice = manifest.get("stages", {}).get("voice", {}) if isinstance(manifest, dict) else {}
    output = voice.get("audio") if isinstance(voice, dict) else None
    if output:
        source = Path(output)
        source = source if source.is_absolute() else epdir / source
        if not source.is_file():
            raise ValueError(f"制作清单中的旁白音频不存在：{source}")
        return source
    candidates = sorted(path for pattern in ("voiceover*.mp3", "voiceover*.wav", "voiceover*.m4a")
                        for path in (epdir / "production").glob(pattern) if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ValueError("找到多份旁白音频；请在 project.yaml 的 sound_design.narration.audio 指定正式文件")
    raise ValueError("找不到旁白音频；请在 project.yaml 的 sound_design.narration.audio 指定文件")


def _timing(epdir: Path) -> list[dict[str, Any]]:
    # Once gaps are inserted, the shifted timing is the authoritative source
    # for sound checks and downstream alignment. Keep the original timing.json
    # intact as the ASR/reference timing.
    actual = epdir / "production/timing_actual.json"
    data = load_yaml(actual, {})
    if not data:
        data = load_yaml(epdir / "production/timing.json", {})
    if not data:
        p = epdir / "production/timing.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("sentences", []) if isinstance(data, dict) else []


def _sentence_order(epdir: Path) -> dict[str, int]:
    sidecar = epdir / "final.sentences.json"
    if not sidecar.is_file():
        draft = latest_draft(epdir)
        sidecar = draft.with_suffix(".sentences.json") if draft else sidecar
    data = load_yaml(sidecar, {}) or {}
    rows = data.get("sentences", []) if isinstance(data, dict) else []
    return {str(row["id"]).lower(): index for index, row in enumerate(rows)
            if isinstance(row, dict) and row.get("id")}


def _timing_row(epdir: Path, timing: list[dict[str, Any]], sentence_id: str | None) -> dict | None:
    if not sentence_id:
        return None
    sid = str(sentence_id).lower()
    index = _sentence_order(epdir).get(sid)
    if index is not None:
        return timing[index] if index < len(timing) else None
    return next((row for row in timing if str(row.get("id") or row.get("sentence_id") or "").lower() == sid), None)


def _time_for(epdir: Path, timing: list[dict[str, Any]], sentence_id: str | None) -> float | None:
    row = _timing_row(epdir, timing, sentence_id)
    return float(row.get("startTime", row.get("start", 0))) if row is not None else None


def _source_cues(epdir: Path) -> list[dict[str, Any]]:
    old = load_yaml(epdir / "production/sound_plan.yaml", {}) or {}
    rows = list(old.get("cues", [])) + list(old.get("cues_extra", []))
    return [r for r in rows if isinstance(r, dict)]


def _classify(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("sound_content", ""))
    purpose = str(row.get("narrative_purpose", "")).lower()
    if row.get("sound_class") in SOUND_CLASSES:
        cls = row["sound_class"]
    elif row.get("sync_mode") == "bed":
        cls = "ambience"
    elif "脉冲" in text or "余响" in text or "设计" in text:
        cls = "design"
    elif "环境" in text or "风" in text or "广播" in text or "回声" in text:
        cls = "ambience" if row.get("sync_mode") == "bed" else "process"
    else:
        cls = "event"
    fn = "reveal" if any(k in purpose for k in ("揭", "转折", "线索")) else "story_event"
    if "片头" in purpose:
        fn = "hook"
    if row.get("function") in FUNCTIONS:
        fn = row["function"]
    return cls, fn


def cues(epdir: Path, *, migrate: bool = False) -> dict[str, Any]:
    """Create a cue sheet; migrate a legacy plan only when explicitly requested."""
    epdir = Path(epdir)
    path = _cue_path(epdir)
    if path.exists() and not migrate:
        raise ValueError(f"声音清单已存在，拒绝覆盖：{path}；旧清单迁移请显式使用 --migrate")
    draft = _draft(epdir)
    old_rows = _source_cues(epdir) if migrate else []
    if migrate and not old_rows:
        raise ValueError("找不到可迁移的 production/sound_plan.yaml cue 条目")
    backup = None
    if path.exists():
        backup = path.parent / "_history" / f"sound_cues-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.yaml"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    result: list[dict[str, Any]] = []
    timing = _timing(epdir)
    for i, row in enumerate(old_rows, 1):
        cls, fn = _classify(row)
        # Hand-tuned migration rules preserve the intent of the existing plan
        # while removing the old blanket blocking behavior.
        rid = str(row.get("id", f"C{_episode_no(epdir):02d}-{i:02d}"))
        candidate = row.get("output", "")
        candidate_path = epdir / str(candidate) if candidate else None
        status = "asset_ready" if candidate_path and candidate_path.is_file() else "planned"
        if cls == "event":
            policy = "gap" if fn in {"story_event", "hook", "reveal"} else "duck"
            placement = "after"
        elif cls == "process":
            policy, placement = "partial_gap", "after"
        else:
            policy, placement = "duck", None
        sid = row.get("sentence_id") or ""
        start = _time_for(epdir, timing, sid)
        # O13 hard rule: no sound effect in the first five seconds.
        if start is not None and start < 5:
            status = "on_hold"
        asset = str(row.get("output", ""))
        if asset.startswith("production/"):
            asset_path = epdir / asset
        else:
            asset_path = Path(asset)
        duration = float(row.get("target_duration_sec", row.get("target_duration", 0)) or 0)
        peak = None
        if policy == "partial_gap":
            peak = {"start": max(0.0, duration / 2 - 0.75), "end": min(duration, duration / 2 + 0.75)}
        cue = {
            "cue_id": rid,
            "sound_class": cls,
            "function": fn,
            "description": row.get("sound_content", ""),
            "scene_id": re.sub(r"\W+", "_", str(row.get("section", "scene"))).strip("_") or "SC01",
            "anchor": {"sentence_id": sid, "text": row.get("anchor_text", "")},
            "placement": placement,
            "gap_policy": policy,
            "gap_reason": "首5秒黄金开场不放音效" if start is not None and start < 5 else "",
            "duration_sec": duration,
            "peak_window": peak,
            "pre_pad_sec": 0.3 if policy in {"gap", "partial_gap"} else 0.0,
            "post_pad_sec": 0.5 if policy in {"gap", "partial_gap"} else 0.0,
            "level_db": float(row.get("gain_db", -12) or -12),
            "lead_in_sec": 0.0,
            "fact_level": "source" if "据正文" in str(row.get("source_basis", "")) else "design",
            "evidence": [],
            "asset_id": asset,
            "status": status,
            "legacy_id": rid,
            "source": "migrated_from_sound_plan.yaml",
        }
        if asset and asset_path.is_file():
            cue["asset_sha256"] = sha256_file(asset_path)
        result.append(cue)
    out = {"episode": _episode_no(epdir), "draft": str(draft),
           "draft_sha256": sha256_file(draft), "schema": "O13.v1", "migration": migrate,
           "notes": ["由历史 sound_plan.yaml 显式迁移；请复核每条 cue。"] if migrate else ["空白清单；请人工转录采用的声音构想。"],
           "cues": result}
    write_yaml(path, out)
    return {"passed": True, "output": str(path), "cue_count": len(result), "on_hold": sum(x.get("status") == "on_hold" for x in result), "migrated": migrate, "backup": str(backup) if backup else None}


def estimate_data(epdir: Path) -> dict[str, Any]:
    """Read-only estimate; suggestions do not count as selected reusable assets."""
    from . import sfx_library

    epdir = Path(epdir)
    config = load_config(epdir)
    cue_path = _cue_path(epdir)
    sheet = load_yaml(cue_path, {})
    if not isinstance(sheet, dict) or not isinstance(sheet.get("cues"), list):
        raise ValueError(f"缺少有效 {cue_path.relative_to(epdir)}；先完成 cues 阶段")
    rows = sheet.get("cues", [])
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("sound_cues.yaml 的 cues 只能包含映射条目")
    cfg = config.get("sound_design", {})
    raw_price = cfg.get("pricing", {}).get("sfx_model_per_minute")
    try:
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("音效单价必须是非负数字或 null") from exc
    if price is not None and (not math.isfinite(price) or price < 0):
        raise ValueError("音效单价必须是有限非负数字")
    project = epdir.parent.parent
    suggestions: dict[str, list[str]] = {}
    reused: list[str] = []
    reused_rows: list[dict] = []
    new = []
    for row in rows:
        if row.get("status") in {"asset_ready", "on_hold"}:
            continue
        identity = str(row.get("asset_id", ""))
        if sfx_library.SFX_ID.fullmatch(identity):
            if not sfx_library.accepted_asset(identity, project=project):
                raise ValueError(f"{row.get('cue_id', '未知 cue')}: 选用音效 {identity} 未通过验收或文件哈希失效")
            reused.append(identity)
            reused_rows.append(row)
            continue
        description = str(row.get("description", "")).strip()
        terms = [description, *[str(tag) for tag in row.get("tags", []) if str(tag).strip()]]
        if terms:
            found = sfx_library.search(terms, project=project,
                                       sound_class=row.get("sound_class") if row.get("sound_class") in SOUND_CLASSES else None)
            if found["items"]:
                suggestions[str(row.get("cue_id", ""))] = [item["id"] for item in found["items"][:5]]
        new.append(row)
    def minutes(items: list[dict]) -> float:
        total = 0.0
        for row in items:
            try:
                duration = float(row.get("duration_sec", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{row.get('cue_id', '未知 cue')}: 时长必须是有限非负数") from exc
            if not math.isfinite(duration) or duration < 0:
                raise ValueError(f"{row.get('cue_id', '未知 cue')}: 时长必须是有限非负数")
            total += duration * (2 if row.get("function") in {"story_event", "hook", "reveal", "motif"} else 1)
        return total / 60

    generated_minutes = minutes(new)
    saved_minutes = minutes(reused_rows)
    gap_total = sum((float(x.get("duration_sec", 0) or 0) if x.get("gap_policy") == "gap" else ((x.get("peak_window", {}).get("end", 0) - x.get("peak_window", {}).get("start", 0)) if x.get("gap_policy") == "partial_gap" and x.get("peak_window") else 0)) + float(x.get("pre_pad_sec", 0) or 0) + float(x.get("post_pad_sec", 0) or 0) for x in rows)
    budget = config.get("cost", {}).get("per_episode_cny", cfg.get("budget_per_episode"))
    def money(amount: float) -> float:
        return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_UP))

    cost = money(generated_minutes * price) if price is not None else (0.0 if not new else None)
    savings = money(saved_minutes * price) if price is not None else (0.0 if not reused else None)
    out = {"passed": True, "episode": _episode_no(epdir), "cue_count": len(rows), "by_class": {c: sum(x.get("sound_class") == c for x in rows) for c in sorted(SOUND_CLASSES)},
           "reusable_assets": sum(x.get("status") == "asset_ready" for x in rows) + len(reused),
           "library_reused_ids": reused, "library_candidates": suggestions,
           "new_assets": len(new), "generated_minutes": round(generated_minutes, 2),
           "estimated_cost_cny": cost, "sfx_estimated_cost_cny": cost,
           "estimated_saved_cny": savings, "sfx_price_known": price is not None,
           "budget_per_episode_cny": budget, "gap_total_sec": round(gap_total, 2), "paid_generation": False,
           "note": "仅按用户核对后的配置单价估算音效，不含口播；候选需试听并明确选用，未调用付费模型。"}
    return out


def estimate(epdir: Path) -> dict[str, Any]:
    epdir = Path(epdir)
    if not _cue_path(epdir).is_file():
        cues(epdir)
    out = estimate_data(epdir)
    reports = epdir / "production/_reports"
    if reports.is_symlink() or not reports.resolve().is_relative_to(epdir.resolve()):
        raise ValueError("自动报告目录必须位于本集 production/_reports，且不能是符号链接")
    write_json(reports / "sound_estimate.json", out)
    atomic_write(reports / "sound_estimate.md", f"# 第{_episode_no(epdir)}集声音估算\n\n" + json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return out


def check(epdir: Path) -> dict[str, Any]:
    epdir = Path(epdir)
    cue_path = _cue_path(epdir)
    sheet = load_yaml(cue_path, {})
    if not sheet:
        cues(epdir); sheet = load_yaml(cue_path, {})
    errors: list[str] = []; warnings: list[str] = []; rows = sheet.get("cues", [])
    timing = _timing(epdir)
    for row in rows:
        cls, fn, pol = row.get("sound_class"), row.get("function"), row.get("gap_policy")
        if cls not in SOUND_CLASSES: errors.append(f"{row.get('cue_id')}: sound_class 无效")
        if fn not in FUNCTIONS: errors.append(f"{row.get('cue_id')}: function 无效")
        if pol not in POLICIES: errors.append(f"{row.get('cue_id')}: gap_policy 无效")
        if row.get("fact_level") == "source" and not row.get("evidence"):
            warnings.append(f"{row.get('cue_id')}: 故事世界音效缺少 evidence，需按原文补齐")
        start = _time_for(epdir, timing, (row.get("anchor") or {}).get("sentence_id"))
        opening_low_bed = (
            cls == "ambience"
            and pol == "duck"
            and float(row.get("level_db", 0) or 0) <= -18
        )
        if start is not None and start < 5 and row.get("status") != "on_hold" and not opening_low_bed:
            errors.append(f"{row.get('cue_id')}: 前5秒不应放音效")
        total = float(row.get("duration_sec", 0) or 0) + float(row.get("pre_pad_sec", 0) or 0) + float(row.get("post_pad_sec", 0) or 0)
        if pol == "partial_gap" and row.get("peak_window"):
            pw = row["peak_window"]; total = float(pw.get("end", 0)) - float(pw.get("start", 0)) + float(row.get("pre_pad_sec", 0) or 0) + float(row.get("post_pad_sec", 0) or 0)
        if total > 3.0 and pol == "gap": warnings.append(f"{row.get('cue_id')}: 完整留白 {total:.2f}s 超过3秒")
    counts = {c: sum(x.get("sound_class") == c for x in rows) for c in SOUND_CLASSES}
    event_limit = load_config(epdir).get("sound_design", {}).get("budget_per_13min", {}).get("cues_event_process", [10, 25])[-1]
    if counts["event"] + counts["process"] > event_limit:
        warnings.append(f"event+process 超过13分钟预算上限{event_limit}，需人工删减")
    def gap_seconds(x: dict[str, Any]) -> float:
        if x.get("gap_policy") == "gap":
            audible = float(x.get("duration_sec", 0) or 0)
        elif x.get("gap_policy") == "partial_gap" and x.get("peak_window"):
            pw = x["peak_window"]; audible = float(pw.get("end", 0)) - float(pw.get("start", 0))
        else:
            audible = 0.0
        return audible + float(x.get("pre_pad_sec", 0) or 0) + float(x.get("post_pad_sec", 0) or 0)
    time_source = "timing_actual.json" if (epdir / "production/timing_actual.json").exists() else ("timing.json" if timing else "estimate")
    out = {"passed": not errors, "errors": errors, "warnings": warnings, "metrics": {"cue_count": len(rows), "by_class": counts, "gap_total_sec": round(sum(gap_seconds(x) for x in rows), 2), "time_source": time_source}}
    reports = epdir / "production/_reports"
    if reports.is_symlink() or not reports.resolve().is_relative_to(epdir.resolve()):
        raise ValueError("自动报告目录必须位于本集 production/_reports，且不能是符号链接")
    write_json(reports / "sound_report.json", out)
    atomic_write(reports / "sound_report.md", "# 声音检查\n\n" + json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return out


def assemble(epdir: Path) -> dict[str, Any]:
    """Assemble the existing narration and insert O13 cue gaps locally.

    This never calls a model. Gaps are inserted at sentence boundaries for
    ready, audible cues; the original timing.json remains unchanged and a
    shifted timing_actual.json is written for downstream alignment.
    """
    epdir = Path(epdir); audio_dir = epdir / "production/audio"; audio_dir.mkdir(parents=True, exist_ok=True)
    source = _narration_source(epdir)
    target = audio_dir / "voice_track.wav"
    timing_data = load_yaml(epdir / "production/timing.json", {})
    rows = list(timing_data.get("sentences", [])) if isinstance(timing_data, dict) else []
    sheet = load_yaml(epdir / "production/sound_cues.yaml", {}) or {}
    cue_rows = list(sheet.get("cues", []))

    # Convert sentence-anchored gap cues to source-time insertions.
    insertions: list[dict[str, Any]] = []
    for cue in cue_rows:
        if cue.get("status") in {"on_hold", "planned"}:
            continue
        policy = cue.get("gap_policy")
        if policy not in {"gap", "partial_gap"}:
            continue
        sid = str((cue.get("anchor") or {}).get("sentence_id", ""))
        row = _timing_row(epdir, rows, sid)
        if row is None:
            continue
        if cue.get("placement") == "before":
            at = float(row.get("startTime", row.get("start", 0.0)))
        else:
            at = float(row.get("endTime", row.get("end", row.get("startTime", row.get("start", 0.0)))))
        if policy == "partial_gap" and cue.get("peak_window"):
            pw = cue["peak_window"]
            audible = max(0.0, float(pw.get("end", 0.0)) - float(pw.get("start", 0.0)))
        else:
            audible = max(0.0, float(cue.get("duration_sec", 0.0) or 0.0))
        pre = max(0.0, float(cue.get("pre_pad_sec", 0.0) or 0.0))
        post = max(0.0, float(cue.get("post_pad_sec", 0.0) or 0.0))
        total = pre + audible + post
        if total <= 0:
            continue
        insertions.append({"cue_id": cue.get("cue_id"), "at": at, "pre": pre, "audible": audible, "post": post, "total": total})
    insertions.sort(key=lambda x: (x["at"], str(x["cue_id"])))

    # Build a concat filter from source chunks and generated silence chunks.
    if insertions:
        parts: list[str] = []
        source_last = 0.0
        labels: list[str] = []
        for i, ins in enumerate(insertions):
            at = max(source_last, float(ins["at"]))
            if at > source_last:
                label = f"v{i}"
                parts.append(f"[0:a]atrim=start={source_last:.6f}:end={at:.6f},asetpts=PTS-STARTPTS[{label}]")
                labels.append(f"[{label}]")
            slabel = f"s{i}"
            parts.append(f"anullsrc=r=48000:cl=stereo:d={ins['total']:.6f}[{slabel}]")
            labels.append(f"[{slabel}]")
            source_last = at
        tail = f"v{len(insertions)}"
        parts.append(f"[0:a]atrim=start={source_last:.6f},asetpts=PTS-STARTPTS[{tail}]")
        labels.append(f"[{tail}]")
        filt = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
        cmd = ["ffmpeg", "-y", "-i", str(source), "-filter_complex", filt, "-map", "[out]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(target)]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(source), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(target)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Shift sentence and word timings by all preceding insertions.
    def shift_at(t: float) -> float:
        return sum(float(x["total"]) for x in insertions if float(x["at"]) < t - 1e-6)
    actual_rows: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        start = float(row.get("startTime", 0.0)); end = float(row.get("endTime", start))
        out["startTime"] = start + shift_at(start)
        out["endTime"] = end + shift_at(end)
        if isinstance(row.get("words"), list):
            words = []
            for word in row["words"]:
                w = dict(word); ws = float(word.get("startTime", 0.0)); we = float(word.get("endTime", ws))
                w["startTime"] = ws + shift_at(ws); w["endTime"] = we + shift_at(we); words.append(w)
            out["words"] = words
        actual_rows.append(out)
    actual_data = dict(timing_data) if isinstance(timing_data, dict) else {"sentences": rows}
    actual_data["sentences"] = actual_rows
    actual_data["source"] = str(source)
    actual_data["inserted_gaps"] = insertions
    write_json(epdir / "production/timing_actual.json", actual_data)

    cue_timeline: list[dict[str, Any]] = []
    for ins in insertions:
        shifted = float(ins["at"]) + shift_at(float(ins["at"]))
        cue_timeline.append({"cue_id": ins["cue_id"], "start_sec": round(shifted + float(ins["pre"]), 6), "end_sec": round(shifted + float(ins["pre"]) + float(ins["audible"]), 6), "gap_total_sec": round(float(ins["total"]), 6)})
    write_json(audio_dir / "cue_timeline.json", {"source": str(source), "voice_track": str(target), "inserted_gaps": cue_timeline, "timing": actual_rows})
    duration = sum(float(x.get("total", 0.0)) for x in insertions)
    return {"passed": True, "voice_track": str(target), "inserted_gaps": len(insertions), "inserted_gap_sec": round(duration, 3), "timing_actual": str(epdir / "production/timing_actual.json"), "note": "已在句子边界插入 O13 gap/partial_gap；未调用付费模型。"}


def mix(epdir: Path) -> dict[str, Any]:
    epdir = Path(epdir); audio_dir = epdir / "production/audio"; audio_dir.mkdir(parents=True, exist_ok=True)
    voice = audio_dir / "voice_track.wav"
    if not voice.exists(): assemble(epdir)
    out = audio_dir / "mix_rough.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(voice), "-af", "loudnorm=I=-15:TP=-1.0:LRA=7", "-ar", "48000", "-c:a", "pcm_s24le", str(out)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"passed": True, "mix_rough": str(out), "sfx_tracks": 0, "note": "当前仅输出旁白标准化粗混；音效轨待 AV1 参数确认后加入。"}


def baseline(epdir: Path) -> dict[str, Any]:
    epdir = Path(epdir); project = epdir
    while project != project.parent and not (project / "project.yaml").exists(): project = project.parent
    from .approvals import confirmation_state
    if confirmation_state(project, "sample", _episode_no(epdir))["state"] != "passed":
        return {"passed": False, "errors": ["sound baseline 需要有效的样片确认记录"]}
    report_path = epdir / "production/_reports/sound_report.json"
    if not report_path.is_file():
        report_path = epdir / "production/sound_report.json"
    report = load_yaml(report_path, {})
    if not report:
        return {"passed": False, "errors": ["请先运行 sound check"]}
    output = epdir / "production/sound_baseline.json"
    metrics = report.get("metrics", {})
    write_json(output, {"episode": _episode_no(epdir), "report_sha256": sha256_file(report_path), "metrics": metrics})
    return {"passed": True, "baseline": metrics, "output": str(output)}


def generate(epdir: Path) -> dict[str, Any]:
    """Return a paid-generation manifest without calling a remote service."""
    estimate_result = estimate(epdir)
    return {"passed": True, "executed": False, "requires_user_confirmation": True,
            "estimate": estimate_result, "message": "O13 实施阶段禁止自动调用付费模型；确认费用后再由专用脚本执行。"}


def import_assets(epdir: Path, files: list[Path]) -> dict[str, Any]:
    """Register hand-generated assets in the project library without uploading."""
    epdir = Path(epdir); library = epdir.parent.parent / "sound_library" / "index.yaml"
    data = load_yaml(library, {"assets": []}) or {"assets": []}; assets = data.setdefault("assets", [])
    added = []
    for file in files:
        file = Path(file)
        if not file.exists(): raise ValueError(f"音频文件不存在：{file}")
        item = {"asset_id": f"LOCAL-{sha256_file(file)[:12]}", "file": str(file), "sha256": sha256_file(file), "status": "asset_ready"}
        assets.append(item); added.append(item["asset_id"])
    write_yaml(library, data)
    return {"passed": True, "added": added, "library": str(library)}


def run(action: str, epdir: Path, files: list[Path] | None = None, *, migrate: bool = False) -> dict[str, Any]:
    if action == "import":
        return import_assets(Path(epdir), files or [])
    if action == "cues":
        return cues(Path(epdir), migrate=migrate)
    return {"cues": cues, "estimate": estimate, "check": check, "assemble": assemble, "mix": mix, "baseline": baseline, "generate": generate}[action](Path(epdir))
