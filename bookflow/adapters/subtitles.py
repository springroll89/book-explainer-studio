"""Exact Chinese captions from the final script and post-mix sentence times."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..common import load_config, load_yaml, sha256_file
from .ffmpeg_audio import _duration, _inside, checked_timing, script_rows


def _stamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    whole, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{fraction:03d}"


def inputs(epdir: Path) -> dict:
    epdir = Path(epdir).resolve()
    project = epdir.parent.parent
    config = load_config(epdir)
    if config.get("producers", {}).get("subs") != "local":
        raise ValueError("当前只接入 local 字幕适配器；请核对 producers.subs")
    settings = config.get("subs", {})
    if not isinstance(settings, dict):
        raise ValueError("subs 配置必须是映射")
    timing = _inside(epdir, settings.get("timing"), "最终时间表")
    audio = _inside(epdir, settings.get("audio"), "最终混音")
    output = _inside(epdir, settings.get("output"), "字幕")
    if (output.suffix.lower() != ".srt" or not output.resolve().is_relative_to((epdir / "production").resolve())
            or not timing.is_file() or not audio.is_file()):
        raise ValueError("字幕缺少本集最终时间表、混音，或输出不是 production 内 SRT")
    final = epdir / "final.md"
    script = script_rows(final)
    data = load_yaml(timing, {}) or {}
    if (not isinstance(data, dict) or data.get("source") != "mix"
            or data.get("draft_sha256") != sha256_file(final)
            or data.get("audio_sha256") != sha256_file(audio)):
        raise ValueError("最终时间表未绑定当前定稿与混音")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("字幕验收需要本机 ffprobe")
    duration = _duration(audio, ffprobe)
    rows = checked_timing(data.get("sentences"), script, duration)
    if rows[-1]["endTime"] > duration + 0.001:
        raise ValueError("末条字幕超出最终音频")
    sources = [project / "project.yaml", final, final.with_suffix(".sentences.json"), timing, audio]
    return {"project": project, "epdir": epdir, "output": output, "rows": rows,
            "audio_duration": duration, "sources": sources}


def build(epdir: Path, *, replace_owned: bool = False) -> dict:
    plan = inputs(epdir)
    target = plan["output"]
    if target.exists() and (not target.is_file() or not replace_owned):
        raise ValueError("字幕已存在且不能证明属于流水线，拒绝覆盖")
    old = sha256_file(target) if target.is_file() else None
    sources = [(path, sha256_file(path)) for path in plan["sources"]]
    cues = []
    for index, row in enumerate(plan["rows"], 1):
        text = row["text"]
        if any(char in text for char in "\r\n\0"):
            raise ValueError(f"第 {index} 句含字幕不支持的控制字符")
        start, end = _stamp(row["startTime"]), _stamp(row["endTime"])
        if start >= end:
            raise ValueError(f"第 {index} 句四舍五入后时长不足 1 毫秒")
        cues.append(f"{index}\n{start} --> {end}\n{text}\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".subs-", dir=target.parent) as temporary:
        staged = Path(temporary) / "subtitles.srt"
        staged.write_text("\n".join(cues), encoding="utf-8")
        if any(sha256_file(path) != digest for path, digest in sources):
            raise ValueError("字幕生成期间输入文件变化，拒绝提交")
        if (sha256_file(target) if target.is_file() else None) != old:
            raise ValueError("字幕目标在生成期间发生变化，拒绝覆盖")
        os.replace(staged, target)
    return {"inputs": [{"path": str(path.relative_to(plan["project"])), "sha256": digest}
                       for path, digest in sources],
            "outputs": [target], "probe": {"sentences": len(plan["rows"]),
                                             "last_end_sec": plan["rows"][-1]["endTime"],
                                             "audio_duration_sec": plan["audio_duration"],
                                             "text_match": True}}
