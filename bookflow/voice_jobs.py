"""Durable, fail-closed request journal for paid paragraph synthesis."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .common import load_yaml, write_json
from .voice_paths import cache_paths

SHA = re.compile(r"[0-9a-f]{64}\Z")
UNRESOLVED = {"submitting", "running", "unknown", "provider_done", "failed"}
STATUSES = UNRESOLVED | {"done"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read(epdir: Path) -> dict:
    path = Path(epdir) / "production/voice_jobs.json"
    if not path.is_file():
        return {"schema": 1, "mode": "real", "jobs": []}
    data = load_yaml(path, {})
    jobs = data.get("jobs") if isinstance(data, dict) and data.get("mode") == "real" else None
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError("voice_jobs.json 格式无效；不能推断已付费任务状态")
    ids = [job.get("request_id") for job in jobs]
    if any(not isinstance(identity, str) or not identity for identity in ids) or len(ids) != len(set(ids)):
        raise ValueError("voice_jobs.json 的请求 ID 无效或重复")
    if any(job.get("status") not in STATUSES or not isinstance(job.get("input_key"), str)
           or not SHA.fullmatch(job["input_key"]) for job in jobs):
        raise ValueError("voice_jobs.json 含未知状态或无效输入哈希；禁止新付费请求")
    return data


@contextmanager
def _locked(epdir: Path):
    production = Path(epdir) / "production"
    production.mkdir(parents=True, exist_ok=True)
    with (production / ".voice_jobs.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield production / "voice_jobs.json"
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _key(paragraph: dict, cast_sha: str, config_sha: str) -> str:
    index, text_sha = paragraph.get("index"), paragraph.get("text_sha256")
    if (type(index) is not int or index < 1 or not isinstance(text_sha, str)
            or not all(isinstance(value, str) and SHA.fullmatch(value)
                       for value in (text_sha, cast_sha, config_sha))):
        raise ValueError("配音任务缺少有效段号或输入哈希")
    data = [index, text_sha, cast_sha, config_sha]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def reserve(epdir: Path, paragraph: dict, *, cast_sha: str, config_sha: str) -> dict:
    """Journal before submit; an unresolved prior task always wins over new work."""
    epdir = Path(epdir).resolve()
    key = _key(paragraph, cast_sha, config_sha)
    with _locked(epdir) as path:
        data = read(epdir)
        matching = next((job for job in reversed(data["jobs"]) if job.get("input_key") == key), None)
        if matching and matching.get("status") in UNRESOLVED:
            return {"action": "query" if matching.get("status") in {"running", "provider_done"}
                    and matching.get("task_id") else "blocked", "job": matching}
        if matching and matching.get("status") == "done":
            return {"action": "recover", "job": matching}
        pending = next((job for job in data["jobs"] if job.get("status") in UNRESOLVED), None)
        if pending:
            return {"action": "blocked", "job": pending}
        manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
        if not isinstance(manifest, dict) or manifest.get("mode") != "real":
            raise ValueError("只有正式媒体清单可预留付费配音任务")
        from .cost import estimate_episode
        budget = estimate_episode(epdir.parent.parent, int(epdir.name[2:]))
        if not budget["passed"]:
            return {"action": "budget_blocked", "budget": budget}
        from .common import load_config
        price = load_config(epdir).get("sound_design", {}).get("pricing", {}).get("narration_model_per_10k_chars")
        from .cost import _amount
        unit_price = _amount(price, "口播每万字符单价")
        request_id = str(uuid4())
        job = {"request_id": request_id, "input_key": key, "paragraph_index": paragraph["index"],
               "text_sha256": paragraph["text_sha256"], "voice_cast_sha256": cast_sha,
               "speaker": paragraph.get("speaker"), "speaker_key": paragraph.get("speaker_key"),
               "config_sha256": config_sha, "unit_price_cny_per_10k": str(unit_price),
               "status": "submitting", "created_at": _now()}
        data["jobs"].append(job)
        write_json(path, data)
        return {"action": "submit", "job": job}


def transition(epdir: Path, request_id: str, *, status: str, task_id: str | None = None) -> dict:
    """Record only allowed state changes; never delete an attempt or create another ID."""
    allowed = {"submitting": {"running", "unknown"},
               "running": {"running", "provider_done", "failed", "unknown"},
               "provider_done": {"provider_done", "unknown"},
               "unknown": {"unknown"}, "failed": {"failed"}}
    with _locked(epdir) as path:
        data = read(epdir)
        job = next((row for row in data["jobs"] if row["request_id"] == request_id), None)
        if not job or status not in allowed.get(job.get("status"), set()):
            raise ValueError("配音任务状态转移无效；拒绝覆盖旧尝试")
        if status in {"running", "provider_done"}:
            identity = task_id or job.get("task_id")
            if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
                raise ValueError("已提交任务缺少可查询的 task_id")
            if job.get("task_id") and job["task_id"] != identity:
                raise ValueError("同一请求不得更换豆包 task_id")
            job["task_id"] = identity
        job["status"], job["updated_at"] = status, _now()
        write_json(path, data)
        return dict(job)


def settle(epdir: Path, request_id: str) -> dict:
    """Mark settled only after the immutable charge and audio evidence both exist."""
    epdir = Path(epdir).resolve()
    with _locked(epdir) as path:
        data = read(epdir)
        job = next((row for row in data["jobs"] if row["request_id"] == request_id), None)
        if not job or job.get("status") not in {"provider_done", "done"}:
            raise ValueError("豆包任务尚未报告完成，不能结清")
        manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
        charges = manifest.get("charges") if isinstance(manifest, dict) else None
        if not isinstance(charges, list) or not any(
            isinstance(row, dict) and row.get("id") == request_id and row.get("stage") == "voice"
            for row in charges):
            raise ValueError("配音任务缺少逐笔费用记录，不能结清")
        segments_path, parts_dir = cache_paths(epdir)
        segments = load_yaml(segments_path, {}) or {}
        rows = segments.get("segments") if isinstance(segments, dict) and segments.get("mode") == "real" else None
        segment = next((row for row in rows if isinstance(row, dict) and row.get("request_id") == request_id), None) if isinstance(rows, list) else None
        if (not segment or segment.get("charge_id") != request_id
                or segment.get("text_sha256") != job.get("text_sha256")
                or segment.get("voice_cast_sha256") != job.get("voice_cast_sha256")
                or segment.get("config_sha256") != job.get("config_sha256")):
            raise ValueError("配音任务缺少同请求 ID 的片段证据，不能结清")
        from .voice_plan import PART_NAME
        from .common import sha256_file
        name = segment.get("path")
        if not isinstance(name, str) or not PART_NAME.fullmatch(name):
            raise ValueError("配音任务片段路径无效")
        audio = parts_dir / name
        if (audio.is_symlink() or audio.parent.is_symlink() or not audio.is_file()
                or segment.get("sha256") != sha256_file(audio)):
            raise ValueError("配音任务音频缺失或哈希变化，不能结清")
        job["status"], job["updated_at"] = "done", _now()
        write_json(path, data)
        return dict(job)
