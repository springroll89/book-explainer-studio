"""Offline silent-media rehearsal for selftest; never use as a production adapter."""
from __future__ import annotations

import json
import hashlib
import re
import shutil
import struct
import subprocess
import wave
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .common import atomic_write, parse_draft, sha256_file, write_json, write_yaml
from .sentences import generate as generate_sentences
from .timing import import_timing
from .voice_paths import cache_paths

WIDTH = 640
HEIGHT = 360
DURATION = 8.0


def _run(command: list[str], *, cwd: Path) -> bytes:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, check=False, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"测试媒体命令超过 60 秒：{Path(command[0]).name}；已停止，检查 FFmpeg 环境") from exc
    if result.returncode:
        raise ValueError(f"测试媒体命令失败：{Path(command[0]).name}，退出码 {result.returncode}；检查 FFmpeg 编码器和字幕滤镜")
    return result.stdout


def _silence(path: Path, frames: int = int(DURATION * 48000)) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\0" * (frames * 2))


def _voice_parts(manifest: Path, parts_dir: Path) -> list[Path]:
    if not manifest.is_file():
        return []
    data = json.loads(manifest.read_text(encoding="utf-8"))
    rows = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) or
                                         not re.fullmatch(r"para-[0-9]{4}\.wav", str(row.get("path", "")))
                                         for row in rows):
        raise ValueError("voice_segments.json 格式无效；先备份修复，不覆盖")
    return [parts_dir / row["path"] for row in rows]


def _paragraphs(parsed: dict) -> list[dict]:
    groups = []
    for line_number, text in parsed["lines"]:
        if groups and line_number == groups[-1]["lines"][-1] + 1:
            groups[-1]["lines"].append(line_number)
            groups[-1]["text"] += "\n" + text
        else:
            groups.append({"lines": [line_number], "text": text})
    return groups


def _segmented_voice(final: Path, parsed: dict, manifest_path: Path, parts_dir: Path) -> None:
    production = final.parent / "production"
    paragraphs = _paragraphs(parsed)
    if not paragraphs:
        raise ValueError("固定示范稿缺少口播段落")
    cast = final.parent.parent.parent / "production/voice_cast.yaml"
    cast_sha = sha256_file(cast)
    old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    previous = old.get("segments", []) if isinstance(old, dict) else []
    if not isinstance(previous, list):
        raise ValueError("voice_segments.json 格式无效；先备份修复，不覆盖")
    by_path = {row.get("path"): row for row in previous if isinstance(row, dict)}
    parts_dir.mkdir(parents=True, exist_ok=True)
    total_frames = int(DURATION * 48000)
    base_frames, remainder = divmod(total_frames, len(paragraphs))
    segments = []
    for index, paragraph in enumerate(paragraphs, 1):
        name = f"para-{index:04d}.wav"
        path = parts_dir / name
        frames = base_frames + (remainder if index == len(paragraphs) else 0)
        text_sha = hashlib.sha256(paragraph["text"].encode("utf-8")).hexdigest()
        prior = by_path.get(name, {})
        fresh = (prior.get("text_sha256") == text_sha and prior.get("voice_cast_sha256") == cast_sha
                 and prior.get("frames") == frames and path.is_file()
                 and prior.get("sha256") == sha256_file(path)) if isinstance(prior, dict) else False
        if fresh:
            segments.append(prior)
            continue
        _silence(path, frames)
        segments.append({"path": name, "text_sha256": text_sha, "voice_cast_sha256": cast_sha,
                         "frames": frames, "sha256": sha256_file(path),
                         "generated_at": datetime.now(timezone.utc).isoformat(), "cost_cny": 0})
    write_json(manifest_path, {"test_fixture_only": True, "segments": segments})
    with wave.open(str(production / "voice.wav"), "wb") as combined:
        combined.setnchannels(1)
        combined.setsampwidth(2)
        combined.setframerate(48000)
        for path in _voice_parts(manifest_path, parts_dir):
            with wave.open(str(path), "rb") as part:
                if (part.getnchannels(), part.getsampwidth(), part.getframerate()) != (1, 2, 48000):
                    raise ValueError(f"测试口播片段格式不符：{path.name}")
                combined.writeframes(part.readframes(part.getnframes()))


def _png(path: Path) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    row = b"\0" + bytes((24, 38, 54)) * WIDTH
    image = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(row * HEIGHT)) + chunk(b"IEND", b"")
    path.write_bytes(image)


def _srt_stamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    whole, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def _probe(video: Path, ffmpeg: str, ffprobe: str) -> dict:
    data = json.loads(_run([ffprobe, "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,codec_name,width,height,duration",
                            "-of", "json", video.name], cwd=video.parent))
    streams = {row.get("codec_type"): row for row in data.get("streams", [])}
    length = float(data.get("format", {}).get("duration", 0))
    video_length = float(streams.get("video", {}).get("duration", 0))
    audio_length = float(streams.get("audio", {}).get("duration", 0))
    if (streams.get("video", {}).get("codec_name") != "h264"
            or streams.get("audio", {}).get("codec_name") != "aac"
            or streams["video"].get("width") != WIDTH or streams["video"].get("height") != HEIGHT
            or abs(length - DURATION) > 0.35 or abs(video_length - audio_length) > 0.35):
        raise ValueError("测试 MP4 缺少预期音视频流、尺寸不符或时长偏差超过 0.35 秒")
    frame = _run([ffmpeg, "-v", "error", "-ss", "0.8", "-i", video.name, "-frames:v", "1",
                  "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], cwd=video.parent)
    if len(frame) != WIDTH * HEIGHT * 3 or max(frame[WIDTH * (HEIGHT * 3 // 4) * 3:]) < 170:
        raise ValueError("测试 MP4 抽帧未发现可见字幕；检查字体和字幕烧录")
    return {"duration_sec": length, "video_codec": "h264", "audio_codec": "aac", "subtitle_frame_visible": True}


def outputs(final: Path, stage: str) -> list[Path]:
    production = final.parent / "production"
    voice_cache_outputs = []
    if stage == "voice":
        segments, parts_dir = cache_paths(final.parent)
        voice_cache_outputs = [segments, *_voice_parts(segments, parts_dir)]
    table = {
        "cues": [production / "sound_cues.yaml"],
        "voice": [production / "voice.wav", production / "timestamps_fixture.json",
                  production / "timing.json", final.with_suffix(".sentences.json"),
                  *voice_cache_outputs],
        "sfx": [production / "sfx.wav"],
        "mix": [production / "final_mix.wav", production / "timing_actual.json"],
        "subs": [production / "subtitles.srt"],
        "storyboard": [production / "storyboard.yaml"],
        "images": [production / "placeholder.png"],
        "render": [production / "final.mp4"],
    }
    if stage not in table:
        raise ValueError(f"未知媒体阶段：{stage}")
    return table[stage]


def run_stage(final: Path, stage: str) -> dict:
    """Run one offline fixture stage and return only its own outputs."""
    final = Path(final).resolve()
    production = final.parent / "production"
    production.mkdir(parents=True, exist_ok=True)
    paths = outputs(final, stage)
    probe = None
    if stage == "cues":
        write_yaml(paths[0], {"cues": [], "test_fixture_only": True})
    elif stage == "voice":
        segments, parts_dir = cache_paths(final.parent)
        parsed = parse_draft(final.read_text(encoding="utf-8"))
        generated = generate_sentences(final)
        if not generated["passed"] or not parsed["sentences"]:
            raise ValueError("固定示范稿没有可生成字幕的句子")
        stable = json.loads(final.with_suffix(".sentences.json").read_text(encoding="utf-8"))["sentences"]
        if len(stable) != len(parsed["sentences"]):
            raise ValueError("稳定句子表与固定稿件不一致")
        _segmented_voice(final, parsed, segments, parts_dir)
        rows = []
        cursor = 0
        parts = json.loads(segments.read_text(encoding="utf-8"))["segments"]
        elapsed = 0.0
        for paragraph, part in zip(_paragraphs(parsed), parts):
            group = []
            while cursor < len(stable) and parsed["sentences"][cursor]["line"] in paragraph["lines"]:
                group.append(stable[cursor])
                cursor += 1
            if not group:
                raise ValueError("口播段落与稳定句子表无法对齐")
            duration = part["frames"] / 48000
            weights = [max(1, len(item["text"])) for item in group]
            total = sum(weights)
            paragraph_start = elapsed
            paragraph_end = paragraph_start + duration
            consumed = 0
            for index, (row, weight) in enumerate(zip(group, weights)):
                consumed += weight
                end = paragraph_end if index == len(group) - 1 else paragraph_start + duration * consumed / total
                rows.append({"id": row["id"], "text": row["text"],
                             "startTime": round(elapsed, 3), "endTime": round(end, 3)})
                elapsed = end
        if cursor != len(stable):
            raise ValueError("口播句子与静音分段数量不一致")
        write_json(paths[1], {"sentences": rows, "test_fixture_only": True})
        timing = import_timing(final.parent, paths[0], paths[1])
        if not timing["passed"] or timing["source"] != "timestamps":
            raise ValueError("静音口播的逐句时间表导入失败")
        paths = outputs(final, stage)
    elif stage == "sfx":
        _silence(paths[0])
    elif stage == "mix":
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("媒体自检需要 ffmpeg 和 ffprobe；请安装后重跑，不能把文字线当作成片验收")
        _run([ffmpeg, "-y", "-v", "error", "-i", "voice.wav", "-i", "sfx.wav",
              "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
              "-map", "[a]", "-c:a", "pcm_s16le", paths[0].name], cwd=production)
        timing = json.loads((production / "timing.json").read_text(encoding="utf-8"))
        timing.update(source="mix", audio="production/final_mix.wav",
                      audio_sha256=sha256_file(paths[0]), duration_sec=DURATION)
        write_json(paths[1], timing)
    elif stage == "subs":
        timing = json.loads((production / "timing_actual.json").read_text(encoding="utf-8"))
        if timing.get("draft_sha256") != sha256_file(final):
            raise ValueError("字幕时间表与最终口播稿不一致，先重跑 voice")
        rows = timing["sentences"]
        spoken = parse_draft(final.read_text(encoding="utf-8"))["sentences"]
        if [row["text"] for row in rows] != [row["text"] for row in spoken]:
            raise ValueError("字幕句子与最终口播稿不一致")
        entries = [f"{index}\n{_srt_stamp(row['startTime'])} --> {_srt_stamp(row['endTime'])}\n{row['text']}\n"
                   for index, row in enumerate(rows, 1)]
        atomic_write(paths[0], "\n".join(entries) + "\n")
    elif stage == "storyboard":
        write_yaml(paths[0], {"shots": [{"start": 0, "end": DURATION,
                                         "image": "placeholder.png"}], "test_fixture_only": True})
    elif stage == "images":
        _png(paths[0])
    elif stage == "render":
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ValueError("媒体自检需要 ffmpeg 和 ffprobe；请安装后重跑，不能把文字线当作成片验收")
        _run([ffmpeg, "-y", "-v", "error", "-loop", "1", "-framerate", "12", "-i", "placeholder.png",
              "-i", "final_mix.wav", "-vf", "subtitles=subtitles.srt", "-t", str(DURATION),
              "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "64k", "-shortest", paths[0].name], cwd=production)
        probe = _probe(paths[0], ffmpeg, ffprobe)
    if any(not path.is_file() or path.stat().st_size == 0 for path in paths):
        raise ValueError(f"测试媒体阶段 {stage} 缺少非空输出")
    return {"outputs": paths, "probe": probe}
