"""Generate one missing sound effect per call with Doubao audio 1.0, within budget.

Order per request: reserve in sfx_jobs.json (budget checked) → provider call →
save the audio in production/sfx_generated → record the charge → mark the cue
asset_ready → re-bind the cue sheet → settle. An uncertain provider answer
locks the request; nothing is ever re-sent automatically.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from decimal import ROUND_UP, Decimal
from pathlib import Path

from .common import load_config, load_yaml, sha256_file, write_json, write_yaml
from .sfx_library import _duration
from .sound import _cue_path

CENT = Decimal("0.01")
GENERATED_DIR = "production/sfx_generated"


def _active_missing(sheet: dict) -> list[dict]:
    return [cue for cue in sheet.get("cues", []) if isinstance(cue, dict) and cue.get("status") != "on_hold"
            and not cue.get("asset_id")]


def _loop_to(source: Path, target: Path, duration: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("循环延长音效需要本机 ffmpeg")
    result = subprocess.run([ffmpeg, "-v", "error", "-y", "-stream_loop", "-1", "-i", str(source),
                             "-t", f"{duration:.3f}", "-ar", "48000", "-c:a", "pcm_s16le", str(target)],
                            capture_output=True, check=False, timeout=600)
    if result.returncode or not target.is_file():
        raise ValueError("FFmpeg 延长音效失败")


def _record_charge(epdir: Path, request_id: str, seconds: float, price: Decimal) -> Decimal:
    amount = (price * Decimal(str(seconds)) / Decimal(60)).quantize(CENT, rounding=ROUND_UP)
    manifest_path = epdir / "production/manifest.json"
    with (epdir / "production/.manifest.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            manifest = load_yaml(manifest_path, {}) or {}
            charges = manifest.get("charges") if manifest.get("mode") == "real" else None
            if not isinstance(charges, list):
                raise ValueError("正式媒体清单缺少逐笔费用列表")
            if not any(isinstance(row, dict) and row.get("id") == request_id for row in charges):
                charges.append({"id": request_id, "stage": "sfx", "cost_cny": float(amount),
                                "billed_seconds": round(seconds, 3), "unit_price_cny_per_minute": str(price),
                                "billing_basis": "audio_seconds_at_locked_price", "invoice_reconciled": False})
                write_json(manifest_path, manifest)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return amount


def _mark_cue(epdir: Path, cue_id: str, asset: Path, request_id: str) -> None:
    path = _cue_path(epdir)
    sheet = load_yaml(path, {}) or {}
    for cue in sheet.get("cues", []):
        if isinstance(cue, dict) and cue.get("cue_id") == cue_id:
            cue.update(status="asset_ready", asset_id=str(asset.relative_to(epdir)),
                       asset_sha256=sha256_file(asset), asset_charge_id=request_id, asset_origin="generated")
            break
    else:
        raise ValueError(f"cue 表中找不到 {cue_id}")
    write_yaml(path, sheet)


def generate_next(project: Path, epdir: Path, *, client=None) -> dict:
    project, epdir = Path(project).resolve(), Path(epdir).resolve()
    from .sfx_jobs import reserve, settle, transition
    from .sfx_library import search
    sheet = load_yaml(_cue_path(epdir), {}) or {}
    missing = _active_missing(sheet)
    if not missing:
        return {"status": "success", "passed": True, "summary": "没有待生成的音效", "executed": []}
    cue = missing[0]
    cue_id = cue.get("cue_id")
    if not cue.get("generate"):
        terms = [str(cue.get("description", "")).strip(),
                 *[str(tag) for tag in cue.get("tags", []) if isinstance(tag, str)]]
        found = search([term for term in terms if term], project=project, sound_class=cue.get("sound_class"))
        if found["items"]:
            return {"status": "warning", "passed": False, "progress": "library_candidates",
                    "summary": f"{cue_id} 在音效库有可复用候选；先试听选用，确需新做时在 cue 写 generate: true",
                    "candidates": [item["id"] for item in found["items"][:5]], "artifacts": [str(_cue_path(epdir))]}
    config = load_config(epdir)
    from .cost import _amount
    price = _amount(config.get("sound_design", {}).get("pricing", {}).get("sfx_model_per_minute"), "音效每分钟单价")
    state = reserve(epdir, cue_id)
    action = state["action"]
    if action in {"guard_blocked", "budget_blocked"}:
        return {"status": "warning", "passed": False, "progress": action,
                "summary": "音效生成守卫未通过" if action == "guard_blocked" else "音效生成超出本集费用上限",
                "errors": state.get("errors") or state.get("budget", {}).get("blockers", []),
                "budget": state.get("budget"), "artifacts": []}
    if action in {"blocked", "query", "settle", "recover"}:
        return {"status": "warning", "passed": False, "progress": "request_unsettled",
                "summary": f"音效请求 {state['job']['request_id'][:8]} 状态为 {state['job']['status']}，禁止自动重发",
                "next_actions": ["对照豆包账单核对该请求是否已计费，再人工处理 sfx_jobs.json"],
                "artifacts": [str(epdir / "production/sfx_jobs.json")]}
    if action == "already_selected":
        return {"status": "success", "passed": True, "summary": f"{cue_id} 已有素材", "executed": []}
    job = state["job"]
    from .adapters.doubao_sfx import DoubaoSfxClient, SfxProviderError, prompt_for
    suffix = str(config.get("sound_design", {}).get("sfx", {}).get("prompt_suffix", ""))
    try:
        client = client or DoubaoSfxClient()
        result = client.generate(prompt=prompt_for(cue, suffix), request_id=job["request_id"])
    except SfxProviderError as exc:
        transition(epdir, job["request_id"], status="failed")
        return {"status": "warning", "passed": False, "progress": "provider_failed",
                "summary": f"{cue_id} 音效生成失败：{exc}", "next_actions": ["调整 cue 描述后重跑；核对账单是否计费"],
                "artifacts": [str(epdir / "production/sfx_jobs.json")]}
    except Exception:
        transition(epdir, job["request_id"], status="unknown")
        return {"status": "error", "passed": False, "progress": "provider_uncertain",
                "summary": f"{cue_id} 音效请求结果未知；已锁定原请求，不会自动重发",
                "next_actions": ["对照豆包账单核对后再处理"], "artifacts": [str(epdir / "production/sfx_jobs.json")]}
    transition(epdir, job["request_id"], status="provider_done")
    folder = epdir / GENERATED_DIR
    if folder.is_symlink():
        raise ValueError("音效生成目录不能是符号链接")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{cue_id}-{job['request_id'][:8]}"
    raw = folder / f"{stem}.mp3"
    fd, temporary = tempfile.mkstemp(prefix=".sfx-", suffix=".mp3", dir=folder)
    with os.fdopen(fd, "wb") as stream:
        stream.write(result["audio"])
    os.replace(temporary, raw)
    measured = _duration(raw)
    billed = result.get("duration_sec") or measured
    _record_charge(epdir, job["request_id"], float(billed), price)
    planned = float(job["duration_sec"])
    asset = raw
    if measured + 0.05 < planned:
        asset = folder / f"{stem}-loop.wav"
        _loop_to(raw, asset, planned)
    _mark_cue(epdir, cue_id, asset, job["request_id"])
    from .cues_stage import bind_existing as bind_cues
    bind_cues(epdir)
    settle(epdir, job["request_id"])
    remaining = len(missing) - 1
    return {"status": "warning", "passed": True, "progress": "generated", "executed": ["sfx_generate"],
            "summary": f"{cue_id} 已生成（{measured:.1f}s，约 {float(billed) / 60 * float(price):.2f} 元）；还剩 {remaining} 条",
            "next_actions": ["重跑 produce 继续生成或绑定音效；成片确认后用 sfx harvest 收入音效库"],
            "artifacts": [str(asset)]}


def harvest(project: Path, ep: int) -> dict:
    """After the episode's sample or release confirmation, file its generated effects as accepted."""
    project = Path(project).resolve()
    epdir = project / "episodes" / f"ep{ep:02d}"
    from .adapters.doubao_sfx import MODEL, prompt_for
    from .sfx_library import add
    sheet = load_yaml(_cue_path(epdir), {}) or {}
    manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
    costs = {row.get("id"): row.get("cost_cny") for row in manifest.get("charges", []) if isinstance(row, dict)}
    suffix = str(load_config(epdir).get("sound_design", {}).get("sfx", {}).get("prompt_suffix", ""))
    rows, errors = [], []
    for cue in sheet.get("cues", []):
        if not isinstance(cue, dict) or cue.get("asset_origin") != "generated" or cue.get("status") != "asset_ready":
            continue
        tags = [str(tag) for tag in cue.get("tags", []) if isinstance(tag, str) and tag.strip()] or [str(cue.get("description", ""))[:12]]
        try:
            result = add(project, ep, epdir / cue["asset_id"], desc=str(cue.get("description", "")).strip(),
                         tags=tags, sound_class=cue.get("sound_class"), loop=cue.get("sound_class") == "ambience",
                         prompt=prompt_for(cue, suffix), model=MODEL,
                         cost_cny=float(costs.get(cue.get("asset_charge_id")) or 0), status="accepted")
            rows.append({"cue_id": cue["cue_id"], "summary": result["summary"]})
        except ValueError as exc:
            errors.append(f"{cue.get('cue_id')}: {exc}")
    return {"passed": not errors, "status": "success" if not errors else "warning",
            "summary": f"收入音效库 {len(rows)} 条" + (f"，{len(errors)} 条未收入" if errors else ""),
            "items": rows, "errors": errors}
