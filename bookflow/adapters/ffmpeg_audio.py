"""Local, hash-bound narration/SFX mix. No generation service is called here."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..common import load_config, load_yaml, parse_draft, sha256_file, write_json
from ..sfx_library import SFX_ID, accepted_asset, location


def _number(value: object, label: str, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是有限数字") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{label}必须在 {low:g}–{high:g} 范围内")
    return result


def _inside(root: Path, name: object, label: str) -> Path:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label}缺少相对路径")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"{label}必须是本集内的相对路径")
    path = root / relative
    if (any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and parent.is_relative_to(root))
            or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError(f"{label}是符号链接或越出本集目录")
    return path


def _run(command: list[str], *, timeout: int, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"本地媒体命令失败或超时：{Path(command[0]).name}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-700:]
        raise ValueError(f"本地媒体命令退出码 {result.returncode}：{detail}")
    return result


def _duration(path: Path, ffprobe: str) -> float:
    result = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(path)], timeout=30, cwd=path.parent)
    return _number(result.stdout.decode().strip(), "音频时长", 0.01, 86400)


def script_rows(final: Path) -> list[dict]:
    """A saved final sentence table must still describe the exact spoken script."""
    sidecar = final.with_suffix(".sentences.json")
    if not sidecar.is_file():
        raise ValueError("缺少 final.sentences.json；先从定稿重建稳定句子表")
    data = load_yaml(sidecar, {}) or {}
    parsed = parse_draft(final.read_text(encoding="utf-8"))["sentences"]
    rows = data.get("sentences") if isinstance(data, dict) else None
    if (not isinstance(rows, list) or not rows or data.get("draft_sha256") != sha256_file(final)
            or len(rows) != len(parsed) or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or row.get("text") != expected["text"] for row, expected in zip(rows, parsed))
            or len({row["id"] for row in rows}) != len(rows)):
        raise ValueError("final.sentences.json 与定稿不一致；拒绝沿用旧句子映射")
    return rows


def checked_timing(rows: object, script: list[dict], duration: float) -> list[dict]:
    if not isinstance(rows, list) or len(rows) != len(script):
        raise ValueError("句子时间表与定稿句数不一致")
    checked = []
    cursor = 0.0
    for index, (row, sentence) in enumerate(zip(rows, script), 1):
        if not isinstance(row, dict) or row.get("id") != sentence["id"] or row.get("text") != sentence["text"]:
            raise ValueError(f"第 {index} 句 ID 或文字与定稿不一致")
        start = _number(row.get("startTime"), f"第 {index} 句开始", 0, 86400)
        end = _number(row.get("endTime"), f"第 {index} 句结束", 0, 86400)
        if start + 0.001 < cursor or end - start < 0.02 or end > duration + 0.05:
            raise ValueError(f"第 {index} 句时间重叠、过短或超出音频")
        checked.append({**row, "startTime": start, "endTime": end})
        cursor = end
    return checked


def _stage_outputs(epdir: Path, stage: str) -> list[Path]:
    manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
    record = manifest.get("stages", {}).get(stage, {}) if isinstance(manifest, dict) else {}
    if not isinstance(record, dict) or record.get("status") != "done":
        raise ValueError(f"缺少有效 {stage} 阶段清单")
    outputs = record.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ValueError(f"{stage} 阶段没有文件产物")
    paths = []
    for row in outputs:
        path = _inside(epdir, row.get("path") if isinstance(row, dict) else None, f"{stage} 产物")
        if not path.is_file() or row.get("sha256") != sha256_file(path):
            raise ValueError(f"{stage} 产物缺失或哈希变化")
        paths.append(path)
    return paths


def _cue_asset(epdir: Path, cue: dict, available: set[Path]) -> tuple[Path, str]:
    identity = cue.get("asset_id")
    if not isinstance(identity, str) or not identity:
        raise ValueError(f"{cue['cue_id']} 缺少选定音效")
    if SFX_ID.fullmatch(identity):
        accepted = accepted_asset(identity, project=epdir.parent.parent)
        if not accepted:
            raise ValueError(f"{cue['cue_id']} 选用的共享音效未验收或哈希失效")
        asset = location(epdir.parent.parent) / accepted["file"]
        scope = "sfx_library"
    else:
        asset = _inside(epdir, identity, f"{cue['cue_id']} 音效")
        if not asset.resolve().is_relative_to((epdir / "production").resolve()) or asset not in available:
            raise ValueError(f"{cue['cue_id']} 音效未绑定 sfx 阶段产物")
        scope = "project"
    if not asset.is_file() or cue.get("asset_sha256") not in (None, sha256_file(asset)):
        raise ValueError(f"{cue['cue_id']} 音效缺失或与 cue 哈希不符")
    return asset, scope


def inputs(epdir: Path) -> dict:
    epdir = Path(epdir).resolve()
    project, production = epdir.parent.parent, epdir / "production"
    config = load_config(epdir)
    if config.get("producers", {}).get("mix") != "ffmpeg":
        raise ValueError("当前只接入 ffmpeg 混音适配器；请核对 producers.mix")
    settings = config.get("mix", {})
    if not isinstance(settings, dict):
        raise ValueError("mix 配置必须是映射")
    voice = _inside(epdir, settings.get("voice"), "旁白")
    timing = _inside(epdir, settings.get("timing"), "原始时间表")
    cue_sheet = _inside(epdir, settings.get("cues"), "cue 表")
    output = _inside(epdir, settings.get("output"), "最终混音")
    timing_output = _inside(epdir, settings.get("timing_output"), "最终时间表")
    if (output.suffix.lower() != ".wav" or timing_output.suffix.lower() != ".json"
            or output == timing_output or any(
                not path.resolve().is_relative_to(production.resolve())
                for path in (voice, timing, cue_sheet, output, timing_output))):
        raise ValueError("混音路径必须在本集 production 内，输出须为 WAV 与 JSON")
    final = epdir / "final.md"
    if any(not path.is_file() for path in (voice, timing, cue_sheet, final, project / "project.yaml")):
        raise ValueError("正式混音缺少定稿、口播、时间表或 cue 表")
    voice_outputs = _stage_outputs(epdir, "voice")
    sfx_outputs = _stage_outputs(epdir, "sfx")
    if voice not in voice_outputs or timing not in voice_outputs:
        raise ValueError("旁白或时间表未绑定 voice 阶段产物")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("正式混音需要本机 ffprobe")
    voice_duration = _duration(voice, ffprobe)
    script = script_rows(final)
    timing_data = load_yaml(timing, {}) or {}
    if (not isinstance(timing_data, dict)
            or timing_data.get("source") not in {"timestamps", "tts", "voice_timestamps"}
            or timing_data.get("audio_sha256") != sha256_file(voice)
            or timing_data.get("draft_sha256") != sha256_file(final)):
        raise ValueError("口播时间表不是当前定稿和旁白的实测句子时间")
    spoken = checked_timing(timing_data.get("sentences"), script, voice_duration)
    sheet = load_yaml(cue_sheet, {}) or {}
    if (not isinstance(sheet, dict) or not isinstance(sheet.get("cues"), list)
            or sheet.get("draft_sha256") not in (None, sha256_file(final))):
        raise ValueError("cue 表格式错误或基于旧版定稿")
    gap_max = _number(config.get("sound_design", {}).get("gap", {}).get("max_single_sec"),
                      "单次留白上限", 0.1, 30)
    sources: list[tuple[str, Path]] = [("project", path) for path in
                                      (project / "project.yaml", final, final.with_suffix(".sentences.json"),
                                       voice, timing, cue_sheet, *sfx_outputs)]
    insertions = []
    effects = []
    by_id = {row["id"]: row for row in spoken}
    seen = set()
    for row in sheet["cues"]:
        if not isinstance(row, dict) or not isinstance(row.get("cue_id"), str) or row["cue_id"] in seen:
            raise ValueError("cue 表存在无效或重复 ID")
        seen.add(row["cue_id"])
        if row.get("status") == "on_hold":
            continue
        selected_library_asset = isinstance(row.get("asset_id"), str) and bool(SFX_ID.fullmatch(row["asset_id"]))
        if row.get("status") not in {"asset_ready", "reused"} and not selected_library_asset:
            raise ValueError(f"{row['cue_id']} 音效尚未准备好")
        policy = row.get("gap_policy")
        if policy not in {"gap", "partial_gap", "duck", "none"}:
            raise ValueError(f"{row['cue_id']} 留白策略无效")
        sid = (row.get("anchor") or {}).get("sentence_id")
        if sid not in by_id:
            raise ValueError(f"{row['cue_id']} 锚点句子不存在")
        anchor = by_id[sid]
        placement = row.get("placement") or "before"
        if placement not in {"before", "after"}:
            raise ValueError(f"{row['cue_id']} placement 无效")
        at = anchor["startTime"] if placement == "before" else anchor["endTime"]
        asset, scope = _cue_asset(epdir, row, set(sfx_outputs))
        sources.append((scope, asset))
        if scope == "sfx_library":
            sources.append((scope, location(project) / "index.yaml"))
        duration = _number(row.get("duration_sec"), f"{row['cue_id']} 时长", 0.02, 3600)
        if _duration(asset, ffprobe) + 0.05 < duration:
            raise ValueError(f"{row['cue_id']} 音效文件短于计划时长")
        level = _number(row.get("level_db"), f"{row['cue_id']} 音量", -60, 12)
        lead = _number(row.get("lead_in_sec", 0), f"{row['cue_id']} 提前量", 0, 30)
        effect = {"cue_id": row["cue_id"], "asset": asset, "scope": scope,
                  "duration": duration, "level_db": level, "policy": policy,
                  "at": at, "placement": placement, "lead": lead, "sentence_id": sid}
        if policy in {"gap", "partial_gap"}:
            pre = _number(row.get("pre_pad_sec", 0), f"{row['cue_id']} 前留白", 0, 10)
            post = _number(row.get("post_pad_sec", 0), f"{row['cue_id']} 后留白", 0, 10)
            peak = row.get("peak_window") if policy == "partial_gap" else None
            if policy == "partial_gap":
                if not isinstance(peak, dict):
                    raise ValueError(f"{row['cue_id']} 缺少 peak_window")
                peak_start = _number(peak.get("start"), f"{row['cue_id']} 峰值开始", 0, duration)
                peak_end = _number(peak.get("end"), f"{row['cue_id']} 峰值结束", 0, duration)
                if peak_end <= peak_start:
                    raise ValueError(f"{row['cue_id']} peak_window 无效")
                audible = peak_end - peak_start
            else:
                peak_start, audible = 0.0, duration
            total = pre + audible + post
            if total > gap_max:
                raise ValueError(f"{row['cue_id']} 留白超过配置上限 {gap_max:g}s")
            insertion = {"at": at, "total": total, "pre": pre, "peak_start": peak_start,
                         "cue_id": row["cue_id"]}
            effect["insertion"] = insertion
            insertions.append(insertion)
        effects.append(effect)
    insertions.sort(key=lambda item: (item["at"], item["cue_id"]))
    shift = 0.0
    for insertion in insertions:
        insertion["output_at"] = insertion["at"] + shift
        shift += insertion["total"]
    final_duration = voice_duration + shift

    def shifted(value: float, *, include_equal: bool) -> float:
        return value + sum(item["total"] for item in insertions
                           if item["at"] < value - 0.000001
                           or include_equal and abs(item["at"] - value) <= 0.000001)

    actual = []
    for row in spoken:
        updated = {**row, "startTime": shifted(row["startTime"], include_equal=True),
                   "endTime": shifted(row["endTime"], include_equal=False)}
        if isinstance(row.get("words"), list):
            updated["words"] = [{**word,
                                 "startTime": shifted(float(word["startTime"]), include_equal=True),
                                 "endTime": shifted(float(word["endTime"]), include_equal=False)}
                                for word in row["words"]]
        actual.append(updated)
    checked_timing(actual, script, final_duration)
    actual_by_id = {row["id"]: row for row in actual}
    for effect in effects:
        if "insertion" in effect:
            insertion = effect["insertion"]
            start = insertion["output_at"] + insertion["pre"] - insertion["peak_start"] - effect["lead"]
        else:
            row = actual_by_id[effect["sentence_id"]]
            anchor_time = row["startTime"] if effect["placement"] == "before" else row["endTime"]
            start = anchor_time - effect["lead"]
        if start < 5 - 0.001 or start + effect["duration"] > final_duration + 0.05:
            raise ValueError(f"{effect['cue_id']} 音效进入开场前5秒或超出最终音频")
        effect["start"] = start
    loudness = config.get("sound_design", {}).get("loudness", {})
    low, high = loudness.get("integrated_lufs", [-16, -14])
    low = _number(low, "响度下限", -16, -14)
    high = _number(high, "响度上限", low, -14)
    peak = _number(loudness.get("true_peak_dbtp", -1), "真峰值上限", -9, -1)
    max_silence = _number(settings.get("max_silence_sec"), "最长空白", 0.5, 30)
    timeout = int(_number(settings.get("timeout_sec"), "混音超时", 30, 86400))
    if any(path in {output, timing_output} for _, path in sources):
        raise ValueError("混音输出与输入路径相同，拒绝覆盖上游素材")
    return {"project": project, "epdir": epdir, "voice": voice, "timing_data": timing_data,
            "script": script, "effects": effects, "insertions": insertions,
            "actual": actual, "duration": final_duration, "output": output,
            "timing_output": timing_output, "sources": list(dict.fromkeys(sources)),
            "loudness": (low, high, peak), "max_silence": max_silence, "timeout": timeout}


def _assemble_voice(plan: dict, target: Path, ffmpeg: str) -> None:
    insertions = plan["insertions"]
    command = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(plan["voice"])]
    if insertions:
        filters, labels = [], []
        cursor = 0.0
        for index, item in enumerate(insertions):
            if item["at"] > cursor + 0.000001:
                name = f"v{index}"
                filters.append(f"[0:a]atrim=start={cursor:.6f}:end={item['at']:.6f},asetpts=PTS-STARTPTS[{name}]")
                labels.append(f"[{name}]")
            name = f"g{index}"
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={item['total']:.6f}[{name}]")
            labels.append(f"[{name}]")
            cursor = item["at"]
        if plan["duration"] > cursor + sum(item["total"] for item in insertions) + 0.000001:
            filters.append(f"[0:a]atrim=start={cursor:.6f},asetpts=PTS-STARTPTS[tail]")
            labels.append("[tail]")
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]")
        command += ["-filter_complex", ";".join(filters), "-map", "[out]"]
    command += ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(target)]
    _run(command, timeout=plan["timeout"], cwd=target.parent)


def _mix_tracks(plan: dict, voice: Path, target: Path, ffmpeg: str) -> None:
    effects = plan["effects"]
    command = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(voice)]
    command.extend(part for effect in effects for part in ("-i", str(effect["asset"])))
    if effects:
        duck_count = sum(effect["policy"] in {"duck", "partial_gap"} for effect in effects)
        keys = [f"[key{index}]" for index in range(duck_count)]
        filters = ([f"[0:a]asplit={duck_count + 1}[main]{''.join(keys)}"] if duck_count
                   else ["[0:a]anull[main]"])
        labels = ["[main]"]
        key_index = 0
        for index, effect in enumerate(effects, 1):
            delay = round(effect["start"] * 1000)
            filters.append(f"[{index}:a]atrim=duration={effect['duration']:.6f},asetpts=PTS-STARTPTS,"
                           f"aresample=48000,aformat=channel_layouts=stereo,volume={effect['level_db']:.3f}dB,"
                           f"adelay={delay}|{delay}[fx{index}]")
            label = f"[fx{index}]"
            if effect["policy"] in {"duck", "partial_gap"}:
                filters.append(f"{label}[key{key_index}]sidechaincompress=threshold=0.035:ratio=6:"
                               f"attack=80:release=400[duck{index}]")
                label = f"[duck{index}]"
                key_index += 1
            labels.append(label)
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0,"
                       f"atrim=duration={plan['duration']:.6f}[mixed]")
        command += ["-filter_complex", ";".join(filters), "-map", "[mixed]"]
    command += ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(target)]
    _run(command, timeout=plan["timeout"], cwd=target.parent)


def _quality(path: Path, plan: dict, ffmpeg: str, ffprobe: str) -> dict:
    duration = _duration(path, ffprobe)
    if abs(duration - plan["duration"]) > 0.15:
        raise ValueError("混音时长与句子时间表、留白预算不一致")
    low, high, peak_limit = plan["loudness"]
    measured = _run([ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-af",
                     f"loudnorm=I={(low + high) / 2:g}:TP={max(-9, min(peak_limit - 0.3, -1.2)):g}:LRA=7:print_format=json",
                     "-f", "null", "-"], timeout=plan["timeout"], cwd=path.parent)
    stderr = measured.stderr.decode("utf-8", errors="replace")
    matches = re.findall(r'\{\s*"input_i".*?\}', stderr, flags=re.S)
    if not matches:
        raise ValueError("FFmpeg 未返回响度检测数据")
    data = json.loads(matches[-1])
    integrated = _number(data.get("input_i"), "实测响度", -70, 0)
    peak = _number(data.get("input_tp"), "实测真峰值", -90, 10)
    if not low - 0.05 <= integrated <= high + 0.05 or peak > peak_limit + 0.05:
        raise ValueError(f"响度/真峰值未达标：{integrated:.2f} LUFS、{peak:.2f} dBTP")
    silence = _run([ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-af",
                    f"silencedetect=noise=-45dB:d={plan['max_silence']:g}", "-f", "null", "-"],
                   timeout=plan["timeout"], cwd=path.parent)
    if "silence_start:" in silence.stderr.decode("utf-8", errors="replace"):
        raise ValueError(f"混音存在超过 {plan['max_silence']:g}s 的空白")
    return {"duration_sec": duration, "integrated_lufs": integrated,
            "true_peak_dbtp": peak, "long_silence": False,
            "sfx_tracks": len(plan["effects"]), "human_listening": "awaiting_human"}


def mix(epdir: Path, *, replace_owned: bool = False) -> dict:
    plan = inputs(epdir)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("正式混音需要本机 ffmpeg 和 ffprobe")
    targets = [plan["output"], plan["timing_output"]]
    for target in targets:
        if target.exists() and (not target.is_file() or not replace_owned):
            raise ValueError("混音产物已存在且不能证明属于流水线，拒绝覆盖")
    previous = {target: sha256_file(target) if target.is_file() else None for target in targets}
    sources = [(scope, path, sha256_file(path)) for scope, path in plan["sources"]]
    plan["output"].parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mix-", dir=plan["output"].parent) as temporary:
        workspace = Path(temporary)
        voice, raw, rendered = (workspace / name for name in ("voice.wav", "raw.wav", "final.wav"))
        _assemble_voice(plan, voice, ffmpeg)
        _mix_tracks(plan, voice, raw, ffmpeg)
        low, high, peak_limit = plan["loudness"]
        _run([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(raw), "-af",
              f"loudnorm=I={(low + high) / 2:g}:TP={max(-9, min(peak_limit - 0.3, -1.2)):g}:LRA=7",
              "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(rendered)],
             timeout=plan["timeout"], cwd=workspace)
        probe = _quality(rendered, plan, ffmpeg, ffprobe)
        timing_data = {**plan["timing_data"], "source": "mix", "sentences": plan["actual"],
                       "source_timing_sha256": sha256_file(_inside(plan["epdir"],
                           load_config(plan["epdir"])["mix"]["timing"], "原始时间表")),
                       "audio": str(plan["output"].relative_to(plan["epdir"])),
                       "audio_sha256": sha256_file(rendered), "duration_sec": probe["duration_sec"],
                       "inserted_gaps": [{"cue_id": item["cue_id"], "at": item["at"],
                                          "output_at": item["output_at"], "duration_sec": item["total"]}
                                         for item in plan["insertions"]]}
        staged_timing = workspace / "timing_actual.json"
        write_json(staged_timing, timing_data)
        if any(sha256_file(path) != digest for _, path, digest in sources):
            raise ValueError("混音期间输入文件发生变化，拒绝提交")
        if any((sha256_file(target) if target.is_file() else None) != previous[target]
               for target in targets):
            raise ValueError("混音目标在处理期间发生变化，拒绝覆盖")
        os.replace(rendered, plan["output"])
        os.replace(staged_timing, plan["timing_output"])
    report = plan["output"].parent / "_reports/mix_qc.json"
    write_json(report, {"probe": probe, "cue_timeline": [
        {"cue_id": effect["cue_id"], "start_sec": effect["start"],
         "end_sec": effect["start"] + effect["duration"], "policy": effect["policy"]}
        for effect in plan["effects"]]})
    inputs_rows = []
    for scope, path, digest in sources:
        root = plan["project"] if scope == "project" else location(plan["project"])
        inputs_rows.append({"scope": scope, "path": str(path.relative_to(root)), "sha256": digest})
    return {"inputs": inputs_rows, "outputs": targets, "probe": probe, "report": report}
