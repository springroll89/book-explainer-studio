"""Bind already selected sound assets to the real SFX stage without paid calls."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .adapters.ffmpeg_audio import _inside
from .common import load_config, load_yaml, sha256_file, write_json
from .cost import _amount
from .guard import check as guard_check
from .media_manifest import real_stage_fresh
from .sfx_library import CLASSES, SFX_ID, _duration, accepted_asset, location, search
from .sound import _cue_path


def sfx_digest(config: dict) -> str:
    producers = config.get("producers")
    if not isinstance(producers, dict):
        raise ValueError("音效服务商配置必须是映射")
    relevant = {"producer": producers.get("sfx"),
                "sfx_production": config.get("sfx_production")}
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _binding_path(epdir: Path, config: dict) -> Path:
    settings = config.get("sfx_production")
    if not isinstance(settings, dict):
        raise ValueError("音效阶段配置必须是映射")
    path = _inside(epdir, settings.get("binding_output"), "音效绑定清单")
    if path.suffix.lower() != ".json" or not path.resolve().is_relative_to((epdir / "production").resolve()):
        raise ValueError("音效绑定清单必须是本集 production 内的 JSON")
    return path


def _selected(epdir: Path, sheet: dict) -> tuple[list[dict], list[tuple[str, Path]], list[Path], dict, list[str]]:
    project = epdir.parent.parent
    rows = sheet.get("cues")
    if not isinstance(rows, list):
        raise ValueError("正式音效需要有效 sound_cues.yaml 的 cues 列表")
    selected, inputs, local_outputs, candidates, missing = [], [], [], {}, []
    seen = set()
    for cue in rows:
        if not isinstance(cue, dict) or not isinstance(cue.get("cue_id"), str) or not cue["cue_id"].strip():
            raise ValueError("cue 表存在无效的音效编号")
        cue_id = cue["cue_id"]
        if cue_id in seen:
            raise ValueError("cue 表存在重复的音效编号")
        seen.add(cue_id)
        if cue.get("status") == "on_hold":
            continue
        if cue.get("sound_class") not in CLASSES:
            raise ValueError(f"{cue_id}: 音效类别无效")
        try:
            duration = float(cue.get("duration_sec"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{cue_id}: 音效时长无效") from exc
        if not math.isfinite(duration) or not 0.02 <= duration <= 3600:
            raise ValueError(f"{cue_id}: 音效时长超出允许范围")
        identity = cue.get("asset_id")
        if isinstance(identity, str) and SFX_ID.fullmatch(identity):
            if cue.get("asset_charge_id"):
                raise ValueError(f"{cue_id}: 共享库复用不得重复绑定本集收费记录")
            accepted = accepted_asset(identity, project=project)
            if not accepted:
                raise ValueError(f"{cue_id}: 共享音效未试听通过或文件哈希失效")
            root = location(project)
            asset = root / accepted["file"]
            scope = "sfx_library"
            inputs.extend([(scope, asset), (scope, root / "index.yaml")])
        elif isinstance(identity, str) and identity.startswith("production/"):
            if cue.get("status") != "asset_ready":
                raise ValueError(f"{cue_id}: 本集音效须标记 asset_ready")
            charge_id, origin = cue.get("asset_charge_id"), cue.get("asset_origin")
            asset = _inside(epdir, identity, f"{cue_id} 音效")
            if not asset.resolve().is_relative_to((epdir / "production").resolve()):
                raise ValueError(f"{cue_id}: 音效不在本集 production 内")
            scope = "project"
            local_outputs.append(asset)
            inputs.append((scope, asset))
        else:
            description = str(cue.get("description", "")).strip()
            tags = cue.get("tags", [])
            if not isinstance(tags, list):
                raise ValueError(f"{cue_id}: 音效标签必须是列表")
            terms = [description, *[str(tag).strip() for tag in tags
                                     if isinstance(tag, str) and tag.strip()]]
            found = search(terms, project=project, sound_class=cue["sound_class"]) if terms else {"items": []}
            candidates[cue_id] = [item["id"] for item in found["items"][:5]]
            missing.append(cue_id)
            continue
        if asset.suffix.lower() not in {".wav", ".mp3", ".m4a", ".flac"} or not asset.is_file():
            raise ValueError(f"{cue_id}: 所选音效文件不存在或格式不受支持")
        digest = sha256_file(asset)
        if scope == "project":
            if cue.get("asset_sha256") != digest:
                raise ValueError(f"{cue_id}: 本集音效须在 cue 表写入当前文件哈希")
            if charge_id and not isinstance(charge_id, str):
                raise ValueError(f"{cue_id}: 音效收费 ID 无效")
            if bool(charge_id) == (origin == "user_supplied"):
                raise ValueError(f"{cue_id}: 本集音效须关联已有收费记录，或明确标注 user_supplied")
        if scope == "sfx_library" and cue.get("asset_sha256") not in (None, digest):
            raise ValueError(f"{cue_id}: cue 表音效哈希与共享库不一致")
        if _duration(asset) + 0.05 < duration:
            raise ValueError(f"{cue_id}: 所选音效文件短于计划时长")
        record = {"cue_id": cue_id, "asset_id": identity, "scope": scope,
                  "path": str(asset.relative_to(project if scope == "project" else location(project))),
                  "sha256": digest, "duration_sec": duration}
        if scope == "project":
            record["charge_id"] = charge_id if charge_id else None
            record["origin"] = "charged" if charge_id else "user_supplied"
        selected.append(record)
    return selected, inputs, local_outputs, candidates, missing


def _existing_charges(manifest: dict, selected: list[dict]) -> tuple[list[str], Decimal]:
    ids = list(dict.fromkeys(row["charge_id"] for row in selected if row.get("charge_id")))
    charges = manifest.get("charges")
    if not isinstance(charges, list):
        raise ValueError("正式音效缺少逐笔费用列表")
    matched = {}
    for row in charges:
        if isinstance(row, dict) and row.get("id") in ids:
            identity = row["id"]
            if identity in matched or row.get("stage") != "sfx":
                raise ValueError("音效收费记录重复或阶段错误")
            matched[identity] = _amount(row.get("cost_cny"), "音效已发生费用")
    if any(identity not in matched for identity in ids):
        raise ValueError("本集音效缺少对应的已发生收费记录")
    return ids, sum((matched[identity] for identity in ids), Decimal(0))


def _check_request_settlement(epdir: Path, selected: list[dict]) -> None:
    from .sfx_jobs import UNRESOLVED, _inputs, read

    jobs = read(epdir)["jobs"]
    if any(job["status"] in UNRESOLVED for job in jobs):
        raise ValueError("存在未结清的音效付费请求，不能绑定正式音效阶段")
    by_id = {job["request_id"]: job for job in jobs}
    for row in selected:
        request_id = row.get("charge_id")
        if request_id in by_id and (by_id[request_id]["status"] != "done"
                                    or by_id[request_id]["cue_id"] != row["cue_id"]):
            raise ValueError(f"{row['cue_id']}: 音效请求尚未结清或与 cue 不匹配")
        if request_id in by_id and by_id[request_id]["input_key"] != _inputs(epdir, row["cue_id"])[-1]:
            raise ValueError(f"{row['cue_id']}: 音效请求的原始输入已变化，不能沿用旧素材")


def bind_existing(epdir: Path) -> dict:
    """Create a zero-charge SFX stage only when every active cue has a verified asset."""
    epdir = Path(epdir).resolve()
    project = epdir.parent.parent
    config = load_config(epdir)
    final = epdir / "final.md"
    cue_path = _cue_path(epdir)
    manifest_path = epdir / "production/manifest.json"
    manifest = load_yaml(manifest_path, {}) or {}
    stages = manifest.get("stages", {}) if isinstance(manifest, dict) else {}
    if (not isinstance(manifest, dict) or manifest.get("mode") != "real"
            or not isinstance(manifest.get("charges"), list) or not isinstance(stages, dict)
            or not real_stage_fresh(epdir, "cues", stages.get("cues"))
            or not real_stage_fresh(epdir, "voice", stages.get("voice"))):
        raise ValueError("正式音效要求已核验的 cues、voice 阶段和逐笔费用清单")
    sheet = load_yaml(cue_path, {}) or {}
    if not isinstance(sheet, dict) or sheet.get("draft_sha256") not in (None, sha256_file(final)):
        raise ValueError("cue 表格式无效或基于旧版定稿")
    binding = _binding_path(epdir, config)
    if binding == cue_path:
        raise ValueError("音效绑定清单不能覆盖人工 cue 表")
    selected, assets, local_outputs, candidates, missing = _selected(epdir, sheet)
    if missing:
        return {"status": "warning", "passed": False, "summary": "尚有未选定音效；未调用付费服务或改写媒体清单",
                "missing_cues": missing, "candidates": candidates,
                "next_actions": ["试听候选并把接受的 SFX ID 写入 cue；无合适候选时仍需正式音效生成"],
                "artifacts": [str(cue_path)]}
    _check_request_settlement(epdir, selected)
    charge_ids, prior_cost = _existing_charges(manifest, selected)
    input_paths = [("project", cue_path), ("project", final), *assets]
    unique_inputs = dict.fromkeys((scope, path) for scope, path in input_paths)
    input_rows = [{"scope": scope, "path": str(path.relative_to(project if scope == "project" else location(project))),
                   "sha256": sha256_file(path)} for scope, path in unique_inputs]
    output_paths = list(dict.fromkeys([binding, *local_outputs]))
    binding_data = {"mode": "real", "cues": selected}
    content = json.dumps(binding_data, ensure_ascii=False, indent=2) + "\n"
    binding_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    output_rows = [{"path": str(path.relative_to(epdir)),
                    "sha256": binding_hash if path == binding else sha256_file(path)} for path in output_paths]
    current = stages.get("sfx", {})
    if (real_stage_fresh(epdir, "sfx", current) and current.get("inputs") == input_rows
            and current.get("outputs") == output_rows and current.get("config_sha256") == sfx_digest(config)
            and current.get("charge_ids") == charge_ids
            and Decimal(str(current.get("cost_cny"))) == prior_cost):
        return {"status": "success", "passed": True, "summary": "正式音效绑定未变化，已复用",
                "executed": [], "skipped": ["sfx"], "next_actions": [], "artifacts": [str(binding)]}
    with (epdir / "production/.manifest.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            manifest = load_yaml(manifest_path, {}) or {}
            if not isinstance(manifest, dict) or manifest.get("mode") != "real" or not isinstance(manifest.get("charges"), list):
                raise ValueError("正式媒体清单在音效绑定期间发生变化")
            stages = manifest.get("stages", {})
            if (not isinstance(stages, dict) or not real_stage_fresh(epdir, "cues", stages.get("cues"))
                    or not real_stage_fresh(epdir, "voice", stages.get("voice"))):
                raise ValueError("前置媒体阶段在音效绑定期间发生变化")
            if _existing_charges(manifest, selected) != (charge_ids, prior_cost):
                raise ValueError("音效收费记录在绑定期间发生变化")
            _check_request_settlement(epdir, selected)
            old = stages.get("sfx", {})
            old_outputs = old.get("outputs", []) if isinstance(old, dict) else []
            pending = manifest.get("sfx_binding_pending", {})
            relative = str(binding.relative_to(epdir))
            if binding.exists() and not (
                any(isinstance(row, dict) and row.get("path") == relative
                    and row.get("sha256") == sha256_file(binding) for row in old_outputs)
                or isinstance(pending, dict) and pending.get(relative) == sha256_file(binding)
            ):
                raise ValueError("现有音效绑定文件不属于正式清单，拒绝覆盖")
            if any(sha256_file(project / row["path"] if row["scope"] == "project"
                               else location(project) / row["path"]) != row["sha256"] for row in input_rows):
                raise ValueError("音效输入在绑定期间变化，拒绝写入")
            manifest["sfx_binding_pending"] = {relative: binding_hash}
            write_json(manifest_path, manifest)
            write_json(binding, binding_data)
            manifest.setdefault("stages", {})["sfx"] = {
                "status": "done", "inputs": input_rows, "outputs": output_rows,
                "config_sha256": sfx_digest(config), "charge_ids": charge_ids,
                "cost_cny": float(prior_cost),
                "reused_assets": sum(row["scope"] == "sfx_library" for row in selected),
                "local_assets": sum(row["scope"] == "project" for row in selected),
                "completed_at": datetime.now(timezone.utc).isoformat()}
            manifest.pop("sfx_binding_pending", None)
            write_json(manifest_path, manifest)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return {"status": "warning", "passed": True, "summary": "已绑定已验收音效；仍需人工试听最终混音",
            "executed": ["sfx"], "next_actions": ["运行 produce 接续正式混音，并人工试听"],
            "artifacts": [str(binding), str(manifest_path)]}


def advance(project: Path, epdir: Path) -> dict:
    """The public entry point enforces the real-book media gate before local binding."""
    project, epdir = Path(project).resolve(), Path(epdir).resolve()
    guard = guard_check(project, "media-generate", int(epdir.name[2:]))
    if not guard["passed"]:
        return {"status": "warning", "passed": False, "summary": "正式音效守卫未通过",
                "errors": guard["errors"], "next_actions": ["先完成对应人工确认"], "artifacts": []}
    return bind_existing(epdir)
