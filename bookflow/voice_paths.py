"""Resolve configured narration cache paths within an episode's production tree."""
from __future__ import annotations

from pathlib import Path

from .adapters.ffmpeg_audio import _inside
from .common import load_config


def cache_paths(epdir: Path) -> tuple[Path, Path]:
    epdir = Path(epdir).absolute()
    voice = load_config(epdir).get("voice_production")
    if not isinstance(voice, dict):
        raise ValueError("voice_production 必须是映射")
    segments = _inside(epdir, voice.get("segments"), "配音片段清单")
    parts = _inside(epdir, voice.get("parts_dir"), "配音片段目录")
    production = (epdir / "production").resolve()
    if (segments.suffix.lower() != ".json"
            or not segments.resolve().is_relative_to(production)
            or not parts.resolve().is_relative_to(production)
            or segments.resolve() == parts.resolve()
            or segments.resolve().is_relative_to(parts.resolve())
            or parts.resolve().is_relative_to(segments.resolve())):
        raise ValueError("配音片段清单与目录必须是 production 内不重叠的 JSON 文件和目录")
    if parts.exists() and not parts.is_dir():
        raise ValueError("配音片段目录目标已被非目录文件占用")
    if segments.exists() and not segments.is_file():
        raise ValueError("配音片段清单目标已被非文件占用")
    return segments, parts
