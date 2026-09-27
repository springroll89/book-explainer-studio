"""Read-only reservation for billable voice and SFX stages; no provider calls."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path

from .common import load_config, load_yaml, parse_draft

CENT = Decimal("0.01")


def _amount(value: object, label: str, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label}必须是有限非负数字") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label}必须是有限非负数字")
    return amount


def _cny(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_UP))


def _stage_fresh(epdir: Path, stage: str, record: dict) -> bool:
    from .media_manifest import real_stage_fresh
    return real_stage_fresh(epdir, stage, record)


def _actual_spend(manifest: dict, stages: dict) -> tuple[Decimal, list[str]]:
    """An append-only charge ledger survives replacement of the latest stage result."""
    blockers: list[str] = []
    charges = manifest.get("charges")
    if charges is None:
        spent = Decimal(0)
        for stage in ("voice", "sfx"):
            record = stages.get(stage, {})
            if record:
                amount = _amount(record.get("cost_cny"), f"{stage} 已发生费用", nullable=True)
                if amount is not None:
                    spent += amount
                blockers.append(f"{stage} 只有可覆盖的阶段费用，缺少逐笔 charges；先核对历史账单")
        return spent, blockers
    if not isinstance(charges, list):
        raise ValueError("媒体 manifest.json 的 charges 必须是逐笔费用列表")
    by_id: dict[str, tuple[str, Decimal]] = {}
    for row in charges:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("逐笔费用缺少有效 id")
        identity = row["id"]
        if identity in by_id:
            raise ValueError(f"重复的费用 id：{identity}")
        stage = row.get("stage")
        if stage not in {"voice", "sfx"}:
            raise ValueError(f"{identity} 的收费阶段只能是 voice 或 sfx")
        by_id[identity] = (stage, _amount(row.get("cost_cny"), f"{identity} 实际费用"))
    for stage in ("voice", "sfx"):
        record = stages.get(stage, {})
        if not record:
            continue
        ids = record.get("charge_ids")
        if not isinstance(ids, list) or len(ids) != len(set(map(str, ids))):
            blockers.append(f"{stage} 缺少有效 charge_ids，不能核对本次费用")
            continue
        if any(not isinstance(identity, str) or identity not in by_id
               or by_id[identity][0] != stage for identity in ids):
            blockers.append(f"{stage} 的 charge_ids 与逐笔费用不一致")
            continue
        current = _amount(record.get("cost_cny"), f"{stage} 阶段费用", nullable=True)
        if current is None or current != sum((by_id[identity][1] for identity in ids), Decimal(0)):
            blockers.append(f"{stage} 阶段费用与逐笔费用之和不一致")
    return sum((amount for _, amount in by_id.values()), Decimal(0)), blockers


def estimate_episode(project: Path, episode: int) -> dict:
    """Include already incurred real charges, then reserve only stale/missing stages."""
    project = Path(project).resolve()
    if type(episode) is not int or episode < 1 or not (project / "project.yaml").is_file():
        raise ValueError("费用预检需要有效书目项目和正整数集号")
    epdir = project / "episodes" / f"ep{episode:02d}"
    final = epdir / "final.md"
    if not final.is_file():
        raise ValueError("费用预检缺少当前集 final.md")
    config = load_config(project)
    sound_cfg = config.get("sound_design", {})
    prices = sound_cfg.get("pricing", {})
    cost_cfg = config.get("cost", {})
    if not all(isinstance(item, dict) for item in (sound_cfg, prices, cost_cfg)):
        raise ValueError("费用配置必须是映射，不能使用列表或纯文本")
    budget = _amount(cost_cfg.get("per_episode_cny", sound_cfg.get("budget_per_episode", 20)), "每集费用上限")
    if budget == 0:
        raise ValueError("每集费用上限必须大于零")
    narration_price = _amount(prices.get("narration_model_per_10k_chars"), "口播每万字符单价", nullable=True)
    sfx_price = _amount(prices.get("sfx_model_per_minute"), "音效每分钟单价", nullable=True)
    spoken = parse_draft(final.read_text(encoding="utf-8"))["spoken"].strip()
    chars = len(spoken)
    if not chars:
        raise ValueError("当前集没有可配音的纯口播")
    manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
    if not isinstance(manifest, dict):
        raise ValueError("媒体 manifest.json 格式无效")
    real_stages = manifest.get("stages", {}) if manifest.get("mode") == "real" else {}
    if not isinstance(real_stages, dict):
        raise ValueError("媒体 manifest.json 的 stages 格式无效")
    spent, blockers = _actual_spend(manifest, real_stages) if manifest.get("mode") == "real" else (Decimal(0), [])
    if manifest.get("mode") == "real":
        from .voice_jobs import UNRESOLVED, read as read_voice_jobs
        jobs = read_voice_jobs(epdir)["jobs"]
        if any(row.get("status") in UNRESOLVED for row in jobs):
            blockers.append("存在未结清的豆包配音任务；只查询原 task_id，不能重复提交")
        from .sfx_jobs import UNRESOLVED as SFX_UNRESOLVED, read as read_sfx_jobs
        sfx_jobs = read_sfx_jobs(epdir)["jobs"]
        if any(row.get("status") in SFX_UNRESOLVED for row in sfx_jobs):
            blockers.append("存在未结清的豆包音效任务；只核对原请求或原 task_id，不能重复提交")
    fresh = {}
    for stage in ("voice", "sfx"):
        record = real_stages.get(stage, {})
        if not isinstance(record, dict):
            raise ValueError(f"{stage} 费用记录格式无效")
        if record.get("status") in {"submitted", "running", "unknown"}:
            blockers.append(f"{stage} 已提交但费用或结果未定；先查询原任务，不能重发")
        if record and record.get("cost_cny") is None:
            blockers.append(f"{stage} 已有调用记录但没有实际费用，先核对账单")
        fresh[stage] = _stage_fresh(epdir, stage, record)
    if not (project / "production/voice_cast.yaml").is_file():
        blockers.append("缺少 production/voice_cast.yaml；未锁定音色不能生成口播")
    voice_new_chars = chars
    voice_reused_paragraphs = 0
    if not fresh["voice"] and (project / "production/voice_cast.yaml").is_file() and manifest.get("mode") == "real":
        from .voice_plan import plan
        try:
            voice_plan = plan(epdir)
            voice_new_chars = voice_plan["new_chars"]
            voice_reused_paragraphs = voice_plan["reused_paragraphs"]
        except ValueError as exc:
            blockers.append(str(exc))
    if fresh["voice"]:
        voice_new_chars = 0
    voice_reservation = (Decimal(0) if voice_new_chars == 0 else
                         narration_price * Decimal(voice_new_chars) / Decimal(10000)
                         if narration_price is not None else None)
    if voice_reservation is None:
        blockers.append("缺少已核对的口播每万字符单价 narration_model_per_10k_chars")
    from .sound import estimate_data
    try:
        sound = estimate_data(epdir)
    except ValueError as exc:
        sound = None
        blockers.append(str(exc))
    if sound is not None and sound["new_assets"] and sfx_price is None and not fresh["sfx"]:
        blockers.append("缺少已核对的音效每分钟单价 sfx_model_per_minute")
    sfx_reservation = (Decimal(0) if fresh["sfx"] else
                       Decimal(str(sound["sfx_estimated_cost_cny"]))
                       if sound is not None and sound["sfx_estimated_cost_cny"] is not None else None)
    total = (Decimal(str(_cny(spent))) + Decimal(str(_cny(voice_reservation)))
             + Decimal(str(_cny(sfx_reservation)))) if voice_reservation is not None and sfx_reservation is not None else None
    over_budget = total is not None and total > budget
    if over_budget:
        blockers.append(f"预计总费用 {_cny(total)} 元超过本集上限 {_cny(budget)} 元；先请用户决定缩减或调整上限")
    return {"status": "warning" if blockers else "success", "passed": not blockers,
            "episode": episode, "budget_cny": _cny(budget), "voice_billable_chars": chars,
            "voice_new_billable_chars": voice_new_chars,
            "voice_reused_paragraphs": voice_reused_paragraphs,
            "actual_spent_cny": _cny(spent),
            "voice_reserved_cny": _cny(voice_reservation) if voice_reservation is not None else None,
            "sfx_reserved_cny": _cny(sfx_reservation) if sfx_reservation is not None else None,
            "total_estimated_cny": _cny(total) if total is not None else None,
            "over_budget": over_budget, "cached_stages": [stage for stage in ("voice", "sfx") if fresh[stage]],
            "reused_sfx": sound["reusable_assets"] if sound else None,
            "new_sfx": sound["new_assets"] if sound else None,
            "estimated_sfx_saved_cny": sound["estimated_saved_cny"] if sound else None,
            "blockers": blockers, "paid_generation": False,
            "next_actions": blockers[:1], "artifacts": [str(epdir / "production/manifest.json")]
            if (epdir / "production/manifest.json").is_file() else []}
