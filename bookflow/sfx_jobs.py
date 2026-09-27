"""Provider-neutral, durable journal for paid sound-effect requests."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .adapters.ffmpeg_audio import _inside
from .common import load_config, load_yaml, sha256_file, write_json
from .cost import _amount, estimate_episode
from .guard import check as guard_check
from .media_manifest import real_stage_fresh
from .sfx_library import _duration
from .sfx_stage import sfx_digest
from .sound import _cue_path

SHA = re.compile(r"[0-9a-f]{64}\Z")
UNRESOLVED = {"submitting", "running", "unknown", "provider_done", "failed"}
STATUSES = UNRESOLVED | {"done"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read(epdir: Path) -> dict:
    path = Path(epdir) / "production/sfx_jobs.json"
    if not path.is_file():
        return {"schema": 1, "mode": "real", "jobs": []}
    data = load_yaml(path, {})
    jobs = data.get("jobs") if isinstance(data, dict) and data.get("schema") == 1 and data.get("mode") == "real" else None
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError("sfx_jobs.json 格式无效；不能推断已付费任务状态")
    ids = [job.get("request_id") for job in jobs]
    if any(not isinstance(identity, str) or not identity for identity in ids) or len(ids) != len(set(ids)):
        raise ValueError("sfx_jobs.json 的请求 ID 无效或重复")
    if any(job.get("status") not in STATUSES or not isinstance(job.get("cue_id"), str)
           or not job["cue_id"].strip() or any(not isinstance(job.get(key), str)
           or not SHA.fullmatch(job[key]) for key in ("input_key", "cue_sha256", "config_sha256", "final_sha256"))
           for job in jobs):
        raise ValueError("sfx_jobs.json 含未知状态或无效输入哈希；禁止新付费请求")
    if any("task_id" in job and (not isinstance(job["task_id"], str)
           or not job["task_id"].strip() or len(job["task_id"]) > 256) for job in jobs):
        raise ValueError("sfx_jobs.json 含无效 task_id；禁止新付费请求")
    return data


@contextmanager
def _locked(epdir: Path):
    production = Path(epdir) / "production"
    production.mkdir(parents=True, exist_ok=True)
    with (production / ".sfx_jobs.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield production / "sfx_jobs.json"
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _cue(epdir: Path, cue_id: str) -> dict:
    if not isinstance(cue_id, str) or not cue_id.strip():
        raise ValueError("音效任务缺少 cue_id")
    sheet = load_yaml(_cue_path(epdir), {}) or {}
    rows = sheet.get("cues") if isinstance(sheet, dict) else None
    if not isinstance(rows, list):
        raise ValueError("正式音效任务缺少有效 sound_cues.yaml")
    matches = [row for row in rows if isinstance(row, dict) and row.get("cue_id") == cue_id]
    if len(matches) != 1:
        raise ValueError("音效编号不存在或重复；拒绝付费提交")
    return matches[0]


def _cue_sha(cue: dict) -> str:
    output_fields = {"status", "asset_id", "asset_sha256", "asset_charge_id", "asset_origin"}
    stable = {key: value for key, value in cue.items() if key not in output_fields}
    try:
        body = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("音效 cue 含不可序列化字段") from exc
    return hashlib.sha256(body).hexdigest()


def _inputs(epdir: Path, cue_id: str) -> tuple[dict, str, str, str, str]:
    cue = _cue(epdir, cue_id)
    final = epdir / "final.md"
    if not final.is_file():
        raise ValueError("音效任务缺少本集 final.md")
    final_sha = sha256_file(final)
    sheet = load_yaml(_cue_path(epdir), {}) or {}
    if sheet.get("draft_sha256") not in (None, final_sha):
        raise ValueError("音效 cue 表基于旧版定稿")
    cue_sha = _cue_sha(cue)
    config_sha = sfx_digest(load_config(epdir))
    key = hashlib.sha256(json.dumps([cue_id, cue_sha, config_sha, final_sha]).encode()).hexdigest()
    return cue, cue_sha, config_sha, final_sha, key


def reserve(epdir: Path, cue_id: str) -> dict:
    """Reserve exactly once before a paid submit; unresolved attempts win over new work."""
    epdir = Path(epdir).resolve()
    with _locked(epdir) as path:
        cue, cue_sha, config_sha, final_sha, key = _inputs(epdir, cue_id)
        data = read(epdir)
        matching = next((job for job in reversed(data["jobs"]) if job["input_key"] == key), None)
        if matching and matching["status"] in UNRESOLVED:
            action = ("settle" if matching["status"] == "provider_done" else
                      "query" if matching["status"] == "running" and matching.get("task_id") else "blocked")
            return {"action": action, "job": matching}
        if matching and matching["status"] == "done":
            return {"action": "recover", "job": matching}
        pending = next((job for job in data["jobs"] if job["status"] in UNRESOLVED), None)
        if pending:
            return {"action": "blocked", "job": pending}
        if cue.get("status") == "on_hold" or cue.get("asset_id"):
            return {"action": "already_selected"}
        if not isinstance(cue.get("description"), str) or not cue["description"].strip():
            raise ValueError("音效任务缺少可生成的描述")
        try:
            duration = float(cue.get("duration_sec"))
        except (TypeError, ValueError) as exc:
            raise ValueError("音效任务时长无效") from exc
        if not math.isfinite(duration) or not 0.02 <= duration <= 3600:
            raise ValueError("音效任务时长超出允许范围")
        project = epdir.parent.parent
        guard = guard_check(project, "media-generate", int(epdir.name[2:]))
        if not guard["passed"]:
            return {"action": "guard_blocked", "errors": guard["errors"]}
        manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
        stages = manifest.get("stages", {}) if isinstance(manifest, dict) else {}
        if (not isinstance(manifest, dict) or manifest.get("mode") != "real"
                or not isinstance(manifest.get("charges"), list) or not isinstance(stages, dict)
                or not real_stage_fresh(epdir, "cues", stages.get("cues"))
                or not real_stage_fresh(epdir, "voice", stages.get("voice"))):
            raise ValueError("付费音效须先有已核验 cues、voice 和逐笔费用清单")
        budget = estimate_episode(project, int(epdir.name[2:]))
        if not budget["passed"]:
            return {"action": "budget_blocked", "budget": budget}
        request_id = str(uuid4())
        job = {"request_id": request_id, "input_key": key, "cue_id": cue_id,
               "cue_sha256": cue_sha, "config_sha256": config_sha,
               "final_sha256": final_sha, "duration_sec": duration,
               "status": "submitting", "created_at": _now()}
        if _inputs(epdir, cue_id)[-1] != key:
            raise ValueError("音效 cue、定稿或配置在预留期间变化，拒绝付费提交")
        data["jobs"].append(job)
        write_json(path, data)
        return {"action": "submit", "job": job}


def transition(epdir: Path, request_id: str, *, status: str, task_id: str | None = None) -> dict:
    allowed = {"submitting": {"running", "provider_done", "unknown"},
               "running": {"running", "provider_done", "failed", "unknown"},
               "provider_done": {"provider_done", "unknown"},
               "unknown": {"unknown"}, "failed": {"failed"}}
    with _locked(epdir) as path:
        data = read(epdir)
        job = next((row for row in data["jobs"] if row["request_id"] == request_id), None)
        if not job or status not in allowed.get(job["status"], set()):
            raise ValueError("音效任务状态转移无效；拒绝覆盖旧尝试")
        if status == "running" or (status == "provider_done" and
                                   (job["status"] == "running" or job.get("task_id"))):
            identity = task_id or job.get("task_id")
            if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
                raise ValueError("已提交音效任务缺少可查询的 task_id")
            if job.get("task_id") and job["task_id"] != identity:
                raise ValueError("同一音效请求不得更换 task_id")
            job["task_id"] = identity
        elif status == "provider_done" and task_id is not None:
            if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
                raise ValueError("音效任务 task_id 无效")
            job["task_id"] = task_id
        elif task_id is not None:
            raise ValueError("非提交成功的状态不能新增 task_id")
        job["status"], job["updated_at"] = status, _now()
        write_json(path, data)
        return dict(job)


def settle(epdir: Path, request_id: str) -> dict:
    """Settle only after a matching charge and the exact local audio are verified."""
    epdir = Path(epdir).resolve()
    with _locked(epdir) as path:
        data = read(epdir)
        job = next((row for row in data["jobs"] if row["request_id"] == request_id), None)
        if not job or job["status"] not in {"provider_done", "done"}:
            raise ValueError("音效任务尚未报告完成，不能结清")
        manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
        charges = manifest.get("charges") if isinstance(manifest, dict) and manifest.get("mode") == "real" else None
        matched = [row for row in charges if isinstance(row, dict) and row.get("id") == request_id] if isinstance(charges, list) else []
        if len(matched) != 1 or matched[0].get("stage") != "sfx":
            raise ValueError("音效任务缺少唯一的逐笔收费记录，不能结清")
        _amount(matched[0].get("cost_cny"), "音效已发生费用")
        cue = _cue(epdir, job["cue_id"])
        if (_cue_sha(cue) != job["cue_sha256"] or sha256_file(epdir / "final.md") != job["final_sha256"]
                or sfx_digest(load_config(epdir)) != job["config_sha256"]):
            raise ValueError("音效任务的 cue、定稿或配置已变化，不能结清")
        if (cue.get("status") != "asset_ready" or cue.get("asset_charge_id") != request_id
                or cue.get("asset_origin") != "generated"):
            raise ValueError("音效任务缺少同请求 ID 的已接受素材记录")
        audio = _inside(epdir, cue.get("asset_id"), "已生成音效")
        if (not audio.resolve().is_relative_to((epdir / "production").resolve())
                or audio.suffix.lower() not in {".wav", ".mp3", ".m4a", ".flac"}
                or not audio.is_file() or cue.get("asset_sha256") != sha256_file(audio)):
            raise ValueError("音效文件缺失、越界或哈希变化，不能结清")
        if _duration(audio) + 0.05 < job["duration_sec"]:
            raise ValueError("音效文件短于计划时长，不能结清")
        job["status"], job["updated_at"] = "done", _now()
        write_json(path, data)
        return dict(job)
