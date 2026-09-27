"""Render validated still-image shots with local FFmpeg; no model or network calls."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..common import load_config, load_yaml, sha256_file, write_json

VIDEO = {".png", ".jpg", ".jpeg", ".webp"}
TRANSITIONS = {"slideleft", "slideright", "fade"}
SRT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _number(value: object, label: str, *, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是 {low}–{high} 之间的数字") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{label}必须是 {low}–{high} 之间的数字")
    return number


def _inside(root: Path, name: object, label: str) -> Path:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label}缺少相对路径")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"{label}必须是当前目录内的相对路径")
    entry = root / relative
    if entry.is_symlink() or not entry.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{label}是符号链接或越出当前目录")
    return entry


def _run(command: list[str], *, cwd: Path, timeout: int) -> bytes:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"FFmpeg 执行失败或超时：{Path(command[0]).name}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-600:]
        raise ValueError(f"FFmpeg 退出码 {result.returncode}：{detail}")
    return result.stdout


def _duration(path: Path, ffprobe: str) -> float:
    raw = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path)], cwd=path.parent, timeout=30)
    return _number(raw.decode("utf-8").strip(), "媒体时长", low=0.01, high=86400)


def _caption_midpoint(path: Path, duration: float) -> float:
    match = SRT_TIME.search(path.read_text(encoding="utf-8-sig"))
    if not match:
        raise ValueError("字幕文件缺少有效的第一条时间码")
    values = [int(value) for value in match.groups()]
    start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
    end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
    if not 0 <= start < end <= duration + 0.2:
        raise ValueError("字幕时间码超出最终音频")
    return (start + end) / 2


def _settings(epdir: Path) -> dict:
    cfg = load_config(epdir)
    if cfg.get("producers", {}).get("render") != "ffmpeg":
        raise ValueError("当前只接入 ffmpeg 渲染适配器；请核对 producers.render")
    render = cfg.get("render", {})
    size = cfg.get("visual_pacing", {}).get("output", {})
    if not isinstance(render, dict) or not isinstance(size, dict):
        raise ValueError("render 和 visual_pacing.output 必须是映射")
    width = int(_number(size.get("width"), "画面宽度", low=160, high=7680))
    height = int(_number(size.get("height"), "画面高度", low=90, high=4320))
    fps = int(_number(render.get("fps"), "帧率", low=12, high=60))
    if width % 2 or height % 2 or float(size.get("width")) != width or float(size.get("height")) != height:
        raise ValueError("画面宽高必须是偶数整数")
    if float(render.get("fps")) != fps:
        raise ValueError("帧率必须是整数")
    transition = render.get("transition", {})
    if not isinstance(transition, dict) or transition.get("name") not in TRANSITIONS:
        raise ValueError("转场只能是 slideleft、slideright 或 fade")
    shift = _number(transition.get("duration_sec"), "转场时长", low=0.1, high=1)
    style = render.get("subtitle_style", {})
    if not isinstance(style, dict) or not re.fullmatch(r"[\w -]{1,60}", str(style.get("font_name", ""))):
        raise ValueError("字幕字体名称包含不支持的字符")
    font_size = int(_number(style.get("font_size"), "字幕字号", low=12, high=100))
    outline = int(_number(style.get("outline"), "字幕描边", low=0, high=10))
    margin = int(_number(style.get("margin_v"), "字幕底边距", low=0, high=500))
    title = render.get("title_card", {})
    if not isinstance(title, dict) or type(title.get("required")) is not bool:
        raise ValueError("render.title_card.required 必须是布尔值")
    timeout = int(_number(render.get("timeout_sec"), "渲染超时", low=30, high=86400))
    return {"render": render, "width": width, "height": height, "fps": fps,
            "transition": transition["name"], "shift": shift,
            "style": f"FontName={style['font_name']},FontSize={font_size},Outline={outline},MarginV={margin}",
            "title": title, "timeout": timeout}


def inputs(epdir: Path) -> dict:
    """Resolve all configurable inputs and reject malformed timelines before rendering."""
    epdir = Path(epdir).resolve()
    settings = _settings(epdir)
    render = settings["render"]
    storyboard = _inside(epdir, render.get("storyboard"), "分镜")
    audio = _inside(epdir, render.get("audio"), "最终混音")
    captions = _inside(epdir, render.get("subtitles"), "字幕")
    output = _inside(epdir, render.get("output"), "成片")
    if output.suffix.lower() != ".mp4" or not output.resolve().is_relative_to((epdir / "production").resolve()):
        raise ValueError("正式成片必须是本集 production 内的 MP4")
    if any(not path.is_file() for path in (storyboard, audio, captions)):
        raise ValueError("正式渲染缺少分镜、最终混音或字幕")
    board = load_yaml(storyboard, {}) or {}
    rows = board.get("shots") if isinstance(board, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("分镜表缺少非空 shots 列表")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("正式渲染需要 ffprobe")
    duration = _duration(audio, ffprobe)
    shots = []
    cursor = 0.0
    min_hold = _number(load_config(epdir).get("visual_pacing", {}).get("min_hold_sec"),
                       "镜头停留下限", low=0.1, high=60)
    max_hold = _number(load_config(epdir).get("visual_pacing", {}).get("max_static_sec"),
                       "镜头停留上限", low=min_hold, high=120)
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 镜不是映射")
        timing = row.get("time_actual") or row.get("time") or row
        if not isinstance(timing, dict):
            raise ValueError(f"第 {index} 镜缺少时间范围")
        start = _number(timing.get("start"), f"第 {index} 镜开始", low=0, high=86400)
        end = _number(timing.get("end"), f"第 {index} 镜结束", low=0, high=86400)
        if abs(start - cursor) > 0.04 or end <= start or not min_hold <= end - start <= max_hold:
            raise ValueError(f"第 {index} 镜有空档、重叠或不符合 {min_hold:g}–{max_hold:g} 秒节奏")
        picture = _inside(storyboard.parent, row.get("image"), f"第 {index} 镜图片")
        if picture.suffix.lower() not in VIDEO or not picture.is_file():
            raise ValueError(f"第 {index} 镜缺少可用静态图片")
        shots.append({"start": start, "end": end, "image": picture})
        cursor = end
    if abs(cursor - duration) > 0.2:
        raise ValueError(f"分镜结尾 {cursor:.2f}s 与最终音频 {duration:.2f}s 不一致")
    title_path = _inside(epdir, settings["title"].get("manifest"), "标题卡基线")
    title_image = None
    if title_path.is_file():
        title = load_yaml(title_path, {}) or {}
        if not isinstance(title, dict) or title.get("mode") != "overlay_on_original_plot_frame":
            raise ValueError("标题卡必须叠在原剧情画面上，不另插片段")
        title_image = _inside(title_path.parent, title.get("image"), "标题卡图片")
        if title_image.suffix.lower() != ".png" or not title_image.is_file():
            raise ValueError("标题卡缺少 PNG 透明图层")
        title_info = json.loads(_run([ffprobe, "-v", "error", "-show_entries", "stream=pix_fmt",
                                      "-of", "json", str(title_image)], cwd=title_image.parent, timeout=30))
        formats = {stream.get("pix_fmt") for stream in title_info.get("streams", [])}
        if not any(fmt in {"rgba", "bgra", "argb", "abgr", "ya8"} or
                   isinstance(fmt, str) and fmt.startswith("yuva") for fmt in formats):
            raise ValueError("标题卡图片没有透明通道，会遮住原剧情画面")
        title_start = _number(title.get("start_sec"), "标题卡开始", low=0, high=duration)
        title_end = _number(title.get("end_sec"), "标题卡结束", low=0, high=duration)
        if title_end <= title_start:
            raise ValueError("标题卡结束时间必须晚于开始")
    elif settings["title"]["required"]:
        raise ValueError("缺少标题卡基线文件；先明确标题卡在原剧情画面上的叠加区间")
    caption_at = _caption_midpoint(captions, duration)
    sources = [storyboard, audio, captions, *(shot["image"] for shot in shots)]
    if title_image:
        sources.extend((title_path, title_image))
    return {"settings": settings, "shots": shots, "audio": audio, "captions": captions,
            "output": output, "duration": duration, "caption_at": caption_at,
            "title": (title_image, title_start, title_end) if title_image else None,
            "sources": list(dict.fromkeys(sources))}


def _probe(path: Path, *, ffprobe: str, duration: float, width: int, height: int) -> dict:
    raw = _run([ffprobe, "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,duration",
                "-of", "json", str(path)], cwd=path.parent, timeout=30)
    data = json.loads(raw)
    streams = {row.get("codec_type"): row for row in data.get("streams", [])}
    video, audio = streams.get("video", {}), streams.get("audio", {})
    length = _number(data.get("format", {}).get("duration"), "成片时长", low=0.01, high=86400)
    video_length = _number(video.get("duration"), "视频流时长", low=0.01, high=86400)
    audio_length = _number(audio.get("duration"), "音频流时长", low=0.01, high=86400)
    if (video.get("codec_name") != "h264" or audio.get("codec_name") != "aac"
            or video.get("width") != width or video.get("height") != height
            or abs(length - duration) > 0.35 or abs(video_length - audio_length) > 0.35):
        raise ValueError("成片音视频流、尺寸或时长与输入不一致")
    return {"duration_sec": length, "video_codec": "h264", "audio_codec": "aac",
            "width": width, "height": height, "subtitle_frame_review": "awaiting_human"}


def render(epdir: Path, *, replace_owned: bool = False) -> dict:
    plan = inputs(epdir)
    project = Path(epdir).resolve().parent.parent
    source_rows = [{"path": str(path.relative_to(project)), "sha256": sha256_file(path)}
                   for path in plan["sources"]]
    settings = plan["settings"]
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("正式渲染需要本机 ffmpeg 和 ffprobe")
    target = plan["output"]
    if target.exists() and not target.is_file():
        raise ValueError("成片目标不是普通文件，拒绝覆盖")
    if target.exists() and not replace_owned:
        raise ValueError("成片文件已存在且不能证明由本流水线生成；拒绝覆盖")
    previous_digest = sha256_file(target) if target.is_file() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    reports = target.parent / "_reports"
    shots = plan["shots"]
    fps, shift = settings["fps"], settings["shift"] if len(shots) > 1 else 0
    if shift and any(shot["end"] - shot["start"] <= shift * 2 for shot in shots):
        raise ValueError("镜头时长不足以容纳平移转场")
    with tempfile.TemporaryDirectory(prefix=".render-", dir=target.parent) as temporary:
        staging = Path(temporary)
        shutil.copy2(plan["captions"], staging / "caption.srt")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for index, shot in enumerate(shots):
            extension = (shift / 2 if index in {0, len(shots) - 1} else shift) if shift else 0
            command += ["-loop", "1", "-framerate", str(fps), "-t",
                        f"{shot['end'] - shot['start'] + extension:.3f}", "-i", str(shot["image"])]
        audio_index = len(shots)
        command += ["-i", str(plan["audio"])]
        title_index = None
        if plan["title"]:
            title_index = audio_index + 1
            command += ["-loop", "1", "-framerate", str(fps), "-t",
                        f"{plan['duration']:.3f}", "-i", str(plan["title"][0])]
        filters = []
        w, h = settings["width"], settings["height"]
        for index, shot in enumerate(shots):
            extension = (shift / 2 if index in {0, len(shots) - 1} else shift) if shift else 0
            frames = max(2, round((shot["end"] - shot["start"] + extension) * fps))
            x = (f"(iw-iw/zoom)*on/{frames - 1}" if index % 2 == 0 else
                 f"(iw-iw/zoom)*(1-on/{frames - 1})")
            filters.append(f"[{index}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                           f"crop={w}:{h},zoompan=z=1.03:x='{x}':y='(ih-ih/zoom)/2':d=1:"
                           f"s={w}x{h}:fps={fps},trim=duration={frames / fps:.3f},"
                           f"setpts=PTS-STARTPTS,format=yuv420p,settb=AVTB[v{index}]")
        current = "v0"
        for index in range(1, len(shots)):
            output = f"x{index}"
            offset = shots[index - 1]["end"] - shift / 2
            filters.append(f"[{current}][v{index}]xfade=transition={settings['transition']}:"
                           f"duration={shift:.3f}:offset={offset:.3f}[{output}]")
            current = output
        if title_index is not None:
            _, start, end = plan["title"]
            filters.append(f"[{title_index}:v]scale={w}:{h},format=rgba[card]")
            filters.append(f"[{current}][card]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'[titled]")
            current = "titled"
        filters.append(f"[{current}]subtitles=caption.srt:force_style='{settings['style']}'[video]")
        video = staging / "render.mp4"
        command += ["-filter_complex", ";".join(filters), "-map", "[video]",
                    "-map", f"{audio_index}:a:0", "-t", f"{plan['duration']:.3f}",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(video)]
        _run(command, cwd=staging, timeout=settings["timeout"])
        probe = _probe(video, ffprobe=ffprobe, duration=plan["duration"], width=w, height=h)
        sample = staging / "subtitle_frame.png"
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss",
              f"{plan['caption_at']:.3f}", "-i", str(video), "-frames:v", "1", str(sample)],
             cwd=staging, timeout=60)
        if not sample.is_file() or sample.stat().st_size == 0:
            raise ValueError("字幕检查帧未生成，拒绝接收成片")
        if any(sha256_file(project / row["path"]) != row["sha256"] for row in source_rows):
            raise ValueError("渲染期间输入文件发生变化；未接收成片，请重新核对前序阶段")
        current_digest = sha256_file(target) if target.is_file() else None
        if current_digest != previous_digest or (target.exists() and not target.is_file()):
            raise ValueError("渲染期间成片目标发生变化；拒绝覆盖人工或外部文件")
        reports.mkdir(parents=True, exist_ok=True)
        os.replace(video, target)
        os.replace(sample, reports / "subtitle_frame.png")
    write_json(reports / "render_probe.json", probe)
    return {"output": target, "inputs": source_rows,
        "probe": probe, "report_frame": reports / "subtitle_frame.png"}
