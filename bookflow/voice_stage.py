"""Resumable real narration stage; paid calls require an explicit CLI opt-in."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from pathlib import Path
from uuid import uuid4

from .adapters.doubao_voice import DoubaoVoiceClient, ProviderUncertain, align_timestamps
from .adapters.ffmpeg_audio import _duration, _inside, checked_timing, script_rows
from .common import load_config, load_yaml, sha256_file, write_json
from .guard import check as guard_check
from .voice_jobs import reserve, settle, transition
from .voice_paths import cache_paths
from .voice_plan import plan as voice_plan

CENT = Decimal("0.01")


def _settings(epdir: Path) -> dict:
    project = epdir.parent.parent
    config = load_config(epdir)
    if config.get("producers", {}).get("tts") != "doubao_tts":
        raise ValueError("当前只接入 doubao_tts 配音适配器；请核对 producers.tts")
    voice_cfg = config.get("voice_production")
    narration = config.get("sound_design", {}).get("narration")
    if not isinstance(voice_cfg, dict) or not isinstance(narration, dict):
        raise ValueError("正式配音路径和口播设置必须是映射")
    if narration.get("generate_unit") != "paragraph" or narration.get("split_method") != "timestamps":
        raise ValueError("正式配音只支持按段生成并使用服务端时间戳")
    cast = load_yaml(project / "production/voice_cast.yaml", {}) or {}
    narrator = cast.get("narrator") if isinstance(cast, dict) else None
    engine = cast.get("engine", {}) if isinstance(cast, dict) else None
    if (not isinstance(narrator, dict) or narrator.get("status") != "confirmed"
            or not isinstance(narrator.get("voice_id"), str) or not narrator["voice_id"].strip()
            or not isinstance(engine, dict)):
        raise ValueError("项目旁白音色尚未确认或音色表格式无效")
    speaker = narrator["voice_id"]
    if narration.get("voice") and narration["voice"] != speaker:
        raise ValueError("project.yaml 的旁白音色与已确认音色表不一致")
    resource = engine.get("resource_id", "seed-tts-2.0")
    model = engine.get("model", "seed-tts-2.0-standard")
    if resource != "seed-tts-2.0" or model not in {"seed-tts-2.0-standard", "seed-tts-2.0-expressive"}:
        raise ValueError("音色表指定的豆包资源或模型尚未接入")
    if engine.get("sample_rate", 24000) != 24000:
        raise ValueError("当前豆包适配器仅支持 24000 Hz 原始 MP3")
    output = _inside(epdir, voice_cfg.get("output"), "配音输出")
    timing = _inside(epdir, voice_cfg.get("timing_output"), "配音时间表")
    segments, parts = cache_paths(epdir)
    production = epdir / "production"
    if (output.suffix.lower() != ".wav" or timing.suffix.lower() != ".json"
            or segments.suffix.lower() != ".json" or len({output, timing, segments, parts}) != 4
            or any(not path.resolve().is_relative_to(production.resolve())
                   for path in (output, timing, segments, parts))):
        raise ValueError("配音产物须为本集 production 内不同的 WAV、JSON 和片段目录")
    mix = config.get("mix", {})
    if not isinstance(mix, dict) or output != _inside(epdir, mix.get("voice"), "混音旁白") or timing != _inside(epdir, mix.get("timing"), "混音时间表"):
        raise ValueError("配音输出路径必须与 mix.voice、mix.timing 一致")
    return {"speaker": speaker, "resource_id": resource, "model": model,
            "output": output, "timing": timing, "segments": segments, "parts": parts}


def _manifest_lock(epdir: Path):
    """All media stages use this same lock for manifest mutations."""
    return (epdir / "production/.manifest.lock").open("a+")


def _preflight_storage(epdir: Path, settings: dict) -> None:
    manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
    if not isinstance(manifest, dict) or manifest.get("mode") != "real" or not isinstance(manifest.get("charges"), list):
        raise ValueError("正式配音要求 mode: real 与逐笔费用清单")
    segments = settings["segments"]
    if segments.is_file():
        data = load_yaml(segments, {})
        if not isinstance(data, dict) or data.get("mode") != "real" or not isinstance(data.get("segments"), list):
            raise ValueError("现有段落清单不是正式格式，付费前停止")
    if settings["parts"].is_symlink():
        raise ValueError("配音片段目录不能是符号链接")
    old = manifest.get("stages", {}).get("voice", {})
    outputs = old.get("outputs", []) if isinstance(old, dict) else []
    pending = manifest.get("voice_assembly_pending", {})
    for target in (settings["output"], settings["timing"]):
        if target.exists():
            digest = sha256_file(target)
            owned = any(isinstance(row, dict) and row.get("path") == str(target.relative_to(epdir))
                        and row.get("sha256") == digest for row in outputs)
            pending_owned = isinstance(pending, dict) and pending.get(str(target.relative_to(epdir))) == digest
            if not owned and not pending_owned:
                raise ValueError("现有口播或时间表不属于正式媒体清单，付费前停止")


def _charge(epdir: Path, job: dict, billable_chars: int) -> Decimal:
    if type(billable_chars) is not int or billable_chars <= 0:
        raise ValueError("豆包未报告有效计费字符数；先核对原任务账单")
    from .cost import _amount
    unit_price = _amount(job.get("unit_price_cny_per_10k"), "配音请求锁定单价")
    amount = (unit_price * Decimal(billable_chars) / Decimal(10000)).quantize(CENT, rounding=ROUND_UP)
    manifest_path = epdir / "production/manifest.json"
    with _manifest_lock(epdir) as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            manifest = load_yaml(manifest_path, {}) or {}
            charges = manifest.get("charges") if isinstance(manifest, dict) and manifest.get("mode") == "real" else None
            if not isinstance(charges, list):
                raise ValueError("正式媒体清单缺少逐笔费用列表，停止配音接续")
            prior = next((row for row in charges if isinstance(row, dict) and row.get("id") == job["request_id"]), None)
            if prior:
                if (prior.get("stage") != "voice" or Decimal(str(prior.get("cost_cny"))) != amount
                        or prior.get("provider_billable_chars") != billable_chars):
                    raise ValueError("原配音请求费用记录与本次查询不一致；停止接续")
            else:
                charges.append({"id": job["request_id"], "stage": "voice", "cost_cny": float(amount),
                                "provider_billable_chars": billable_chars,
                                "billing_basis": "provider_reported_chars_at_locked_price",
                                "invoice_reconciled": False})
                write_json(manifest_path, manifest)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return amount


def _save_part(epdir: Path, settings: dict, paragraph: dict, job: dict,
               response: dict, client: DoubaoVoiceClient, amount: Decimal) -> Path:
    audio = client.download_audio(response["audio_url"])
    parts = settings["parts"]
    if parts.is_symlink():
        raise ValueError("配音片段目录不能是符号链接")
    parts.mkdir(parents=True, exist_ok=True)
    name = f"para-{paragraph['index']:04d}-{job['request_id'].replace('-', '')}.mp3"
    target = parts / name
    fd, temporary = tempfile.mkstemp(prefix=".voice-", suffix=".mp3", dir=parts)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(audio)
            stream.flush()
            os.fsync(stream.fileno())
        staged = Path(temporary)
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise ValueError("正式配音需要本机 ffprobe")
        duration = _duration(staged, ffprobe)
        aligned = align_timestamps(response["sentences"], paragraph["sentences"])
        if aligned[-1]["endTime"] > duration + 0.05:
            raise ValueError("豆包时间戳超出音频时长，不能用作字幕")
        digest = sha256_file(staged)
        try:
            os.link(staged, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
                raise ValueError("同一请求的配音目标文件已存在且内容不同；拒绝覆盖")
        path = settings["segments"]
        data = load_yaml(path, {}) if path.is_file() else {"mode": "real", "segments": []}
        rows = data.get("segments") if isinstance(data, dict) and data.get("mode") == "real" else None
        if not isinstance(rows, list):
            raise ValueError("已有片段清单不是正式格式，拒绝覆盖")
        record = {"path": name, "status": "done", "request_id": job["request_id"],
                  "charge_id": job["request_id"], "cost_cny": float(amount),
                  "sha256": digest, "duration_sec": duration, "sentences": aligned,
                  "text_sha256": paragraph["text_sha256"],
                  "voice_cast_sha256": job["voice_cast_sha256"],
                  "config_sha256": job["config_sha256"]}
        prior = next((row for row in rows if isinstance(row, dict) and row.get("request_id") == job["request_id"]), None)
        if prior and prior != record:
            raise ValueError("同一请求已有不同片段记录，拒绝覆盖")
        if not prior:
            rows.append(record)
            write_json(path, data)
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _assemble(epdir: Path, settings: dict, planned: dict) -> dict:
    final, project = epdir / "final.md", epdir.parent.parent
    output, timing = settings["output"], settings["timing"]
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("正式配音拼接需要本机 ffmpeg 和 ffprobe")
    selected = [row["cached_record"] for row in planned["paragraphs"]]
    if not selected or any(row is None for row in selected):
        raise ValueError("口播段落尚未全部通过音频与时间戳校验")
    inputs = [settings["parts"] / row["path"] for row in selected]
    command = [ffmpeg, "-v", "error", "-y"]
    for path in inputs:
        command += ["-i", str(path)]
    filters = []
    for index, row in enumerate(selected):
        filters.append(f"[{index}:a]aresample=48000,apad,atrim=duration={float(row['duration_sec']):.6f},asetpts=PTS-STARTPTS[a{index}]")
    filters.append("".join(f"[a{index}]" for index in range(len(selected)))
                   + f"concat=n={len(selected)}:v=0:a=1[out]")
    command += ["-filter_complex", ";".join(filters), "-map", "[out]", "-ac", "1", "-ar", "48000",
                "-c:a", "pcm_s16le", "-f", "wav"]
    output.parent.mkdir(parents=True, exist_ok=True)
    timing.parent.mkdir(parents=True, exist_ok=True)
    audio_fd, audio_name = tempfile.mkstemp(prefix=".voice-combined-", suffix=".wav", dir=output.parent)
    os.close(audio_fd)
    timing_fd, timing_name = tempfile.mkstemp(prefix=".voice-timing-", suffix=".json", dir=timing.parent)
    os.close(timing_fd)
    try:
        result = subprocess.run([*command, audio_name], capture_output=True, check=False, timeout=7200)
        if result.returncode:
            raise ValueError(f"FFmpeg 配音拼接失败（退出码 {result.returncode}）")
        voice_duration = _duration(Path(audio_name), ffprobe)
        rows, offset = [], 0.0
        for paragraph, saved in zip(planned["paragraphs"], selected):
            for expected, original in zip(paragraph["sentences"], saved["sentences"]):
                if expected["text"] != original["text"]:
                    raise ValueError("缓存片段与当前定稿句子不一致")
                rows.append({"id": expected["id"], "text": expected["text"],
                             "startTime": round(offset + float(original["startTime"]), 3),
                             "endTime": round(offset + float(original["endTime"]), 3)})
            offset += float(saved["duration_sec"])
        rows = checked_timing(rows, script_rows(final), voice_duration)
        write_json(Path(timing_name), {"source": "tts", "draft": str(final),
                                      "draft_sha256": sha256_file(final), "audio": str(output),
                                      "audio_sha256": sha256_file(audio_name), "sentences": rows})
        audio_hash, timing_hash = sha256_file(audio_name), sha256_file(timing_name)
        manifest_path = epdir / "production/manifest.json"
        with _manifest_lock(epdir) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                manifest = load_yaml(manifest_path, {}) or {}
                if not isinstance(manifest, dict) or manifest.get("mode") != "real":
                    raise ValueError("正式配音需要 mode: real 的媒体清单")
                old = manifest.get("stages", {}).get("voice", {})
                old_outputs = old.get("outputs", []) if isinstance(old, dict) else []
                pending = manifest.get("voice_assembly_pending", {})
                for target in (output, timing):
                    if target.exists() and not any(isinstance(row, dict)
                            and row.get("path") == str(target.relative_to(epdir))
                            and row.get("sha256") == sha256_file(target) for row in old_outputs):
                        if not isinstance(pending, dict) or pending.get(str(target.relative_to(epdir))) != sha256_file(target):
                            raise ValueError("现有口播或时间表不属于本次清单，拒绝覆盖")
                charge_ids = list(dict.fromkeys(row["charge_id"] for row in selected))
                charges = manifest.get("charges", [])
                by_id = {row["id"]: Decimal(str(row["cost_cny"])) for row in charges if isinstance(row, dict)}
                if any(identity not in by_id for identity in charge_ids):
                    raise ValueError("当前口播片段缺少逐笔费用记录")
                manifest["voice_assembly_pending"] = {str(output.relative_to(epdir)): audio_hash,
                                                        str(timing.relative_to(epdir)): timing_hash}
                write_json(manifest_path, manifest)
                os.replace(audio_name, output)
                os.replace(timing_name, timing)
                sources = [final, final.with_suffix(".sentences.json"), project / "production/voice_cast.yaml"]
                outputs = [output, timing, settings["segments"], *inputs]
                manifest.setdefault("stages", {})["voice"] = {
                    "status": "done", "inputs": [{"path": str(path.relative_to(project)), "sha256": sha256_file(path)}
                                                  for path in sources],
                    "outputs": [{"path": str(path.relative_to(epdir)), "sha256": sha256_file(path)}
                                for path in outputs],
                    "config_sha256": planned["config_sha256"], "charge_ids": charge_ids,
                    "cost_cny": float(sum((by_id[identity] for identity in charge_ids), Decimal(0))),
                    "duration_sec": voice_duration,
                    "completed_at": datetime.now(timezone.utc).isoformat()}
                manifest.pop("voice_assembly_pending", None)
                write_json(manifest_path, manifest)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return {"status": "warning", "passed": True, "summary": "正式口播已按段拼接并核对逐句时间戳；仍需人工试听",
                "executed": ["voice"], "next_actions": ["人工试听整集口播；机器时间戳检查不代替听感验收"],
                "artifacts": [str(output), str(timing), str(manifest_path)]}
    finally:
        for path in (audio_name, timing_name):
            if os.path.exists(path):
                os.unlink(path)


def advance(project: Path, epdir: Path, *, client: DoubaoVoiceClient | None = None) -> dict:
    """One durable submit or query per call; never retry a paid submit implicitly."""
    project, epdir = Path(project).resolve(), Path(epdir).resolve()
    number = int(epdir.name[2:])
    guard = guard_check(project, "media-generate", number)
    if not guard["passed"]:
        return {"status": "warning", "passed": False, "progress": "guard_blocked",
                "summary": "正式配音守卫未通过",
                "errors": guard["errors"], "next_actions": ["先完成对应人工确认"], "artifacts": []}
    from .produce import check
    if not check(project, number, until="cues")["stages"][0]["ready"]:
        return {"status": "warning", "passed": False, "summary": "正式配音缺少有效音效规划阶段",
                "next_actions": ["先完成 cues 并登记正式媒体清单"], "artifacts": []}
    settings = _settings(epdir)
    _preflight_storage(epdir, settings)
    with (epdir / "production/.voice_stage.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            planned = voice_plan(epdir)
            missing = next((row for row in planned["paragraphs"] if not row["cached"]), None)
            if missing is None:
                return _assemble(epdir, settings, planned)
            client = client or DoubaoVoiceClient()
            state = reserve(epdir, missing, cast_sha=planned["cast_sha256"],
                            config_sha=planned["config_sha256"])
            if state["action"] == "budget_blocked":
                return {"status": "warning", "passed": False, "progress": "budget_blocked",
                        "summary": "正式配音预算预检未通过",
                        "budget": state["budget"], "errors": state["budget"]["blockers"],
                        "next_actions": state["budget"]["next_actions"], "artifacts": []}
            if state["action"] == "blocked":
                return {"status": "warning", "passed": False, "progress": "request_unsettled",
                        "summary": "已有未结清的豆包配音任务，禁止重发",
                        "next_actions": ["核对原请求或原 task_id 后再继续"],
                        "artifacts": [str(epdir / "production/voice_jobs.json")]}
            job = state["job"]
            if state["action"] == "submit":
                try:
                    task_id = client.submit(text=missing["text"], speaker=settings["speaker"],
                                            request_id=job["request_id"], resource_id=settings["resource_id"],
                                            model=settings["model"])
                except Exception:
                    transition(epdir, job["request_id"], status="unknown")
                    raise ProviderUncertain("配音提交结果未知；已锁定原请求，不能自动重发") from None
                transition(epdir, job["request_id"], status="running", task_id=task_id)
                return {"status": "warning", "passed": False, "progress": "submitted",
                        "summary": "豆包配音任务已提交；下次只查询原 task_id",
                        "next_actions": ["稍后重跑 produce 查询原任务"],
                        "artifacts": [str(epdir / "production/voice_jobs.json")]}
            response = client.query(task_id=job["task_id"], request_id=str(uuid4()),
                                    resource_id=settings["resource_id"])
            if response["state"] == "running":
                return {"status": "warning", "passed": False, "progress": "running",
                        "summary": "原豆包配音任务仍在运行",
                        "next_actions": ["稍后重跑 produce 查询同一 task_id"],
                        "artifacts": [str(epdir / "production/voice_jobs.json")]}
            if response["state"] == "failed":
                transition(epdir, job["request_id"], status="failed")
                return {"status": "warning", "passed": False, "progress": "provider_failed",
                        "summary": "原豆包配音任务失败；需核对是否计费",
                        "next_actions": ["核对原任务账单后决定是否重新生成"],
                        "artifacts": [str(epdir / "production/voice_jobs.json")]}
            if job["status"] != "done":
                transition(epdir, job["request_id"], status="provider_done")
            amount = _charge(epdir, job, response["billable_chars"])
            _save_part(epdir, settings, missing, job, response, client, amount)
            if job["status"] != "done":
                settle(epdir, job["request_id"])
            refreshed = voice_plan(epdir)
            if all(row["cached"] for row in refreshed["paragraphs"]):
                return _assemble(epdir, settings, refreshed)
            return {"status": "warning", "passed": False, "progress": "part_saved",
                    "summary": "一段正式配音已保存；其余段落待继续",
                    "next_actions": ["重跑 produce 处理下一段；已完成段落会复用"],
                    "artifacts": [str(settings["segments"])]}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
