"""Read-only paragraph plan for paid narration and conservative cache reuse."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .common import load_config, load_yaml, parse_draft, sha256_file
from .voice_paths import cache_paths

PART_NAME = re.compile(r"para-([0-9]{4,})(?:-[0-9a-f]{12,32})?\.mp3\Z")


def narration_digest(config: dict) -> str:
    producers, sound = config.get("producers"), config.get("sound_design")
    if not isinstance(producers, dict) or not isinstance(sound, dict) or not isinstance(sound.get("narration"), dict):
        raise ValueError("配音服务商与口播配置必须是映射")
    relevant = {"producer": producers.get("tts"), "narration": sound["narration"],
                "voice_production": config.get("voice_production")}
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _cached_rows(epdir: Path, path: Path) -> dict[int, list[dict]]:
    if not path.is_file():
        return {}
    data = load_yaml(path, {})
    if not isinstance(data, dict) or data.get("mode") != "real":
        return {}
    rows = data.get("segments")
    if not isinstance(rows, list):
        raise ValueError("正式 voice_segments.json 缺少段落列表；请先备份并修复")
    manifest = load_yaml(epdir / "production/manifest.json", {}) or {}
    charges = manifest.get("charges", []) if isinstance(manifest, dict) and manifest.get("mode") == "real" else []
    paid = {row.get("id") for row in charges if isinstance(row, dict) and row.get("stage") == "voice"}
    result: dict[int, list[dict]] = {}
    names: set[str] = set()
    for row in rows:
        name = row.get("path") if isinstance(row, dict) else None
        match = PART_NAME.fullmatch(name) if isinstance(name, str) else None
        if not match or name in names:
            raise ValueError("正式 voice_segments.json 存在无效或重复的片段路径")
        names.add(name)
        charge_id = row.get("charge_id")
        result.setdefault(int(match.group(1)), []).append(
            row if isinstance(charge_id, str) and charge_id in paid else {})
    return result


def _usable(epdir: Path, parts_dir: Path, row: dict, *, text_sha: str, cast_sha: str,
            config_sha: str, sentences: list[dict]) -> bool:
    if (row.get("status") != "done" or row.get("text_sha256") != text_sha
            or row.get("voice_cast_sha256") != cast_sha
            or row.get("config_sha256") != config_sha):
        return False
    path = parts_dir / row["path"]
    if (path.parent.is_symlink() or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to((epdir / "production").resolve())
            or row.get("sha256") != sha256_file(path)):
        return False
    try:
        duration = float(row.get("duration_sec"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(duration) or duration <= 0:
        return False
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", str(path)],
                               capture_output=True, check=False, timeout=15)
        measured = float(probe.stdout.decode().strip())
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return False
    if probe.returncode or not math.isfinite(measured) or abs(measured - duration) > 0.10:
        return False
    timing = row.get("sentences")
    if not isinstance(timing, list) or len(timing) != len(sentences):
        return False
    cursor = 0.0
    for saved, expected in zip(timing, sentences):
        if not isinstance(saved, dict) or saved.get("text") != expected["text"]:
            return False
        try:
            start, end = float(saved["startTime"]), float(saved["endTime"])
        except (KeyError, TypeError, ValueError):
            return False
        if not all(map(math.isfinite, (start, end))) or start + 0.001 < cursor or end - start < 0.02 or end > duration + 0.05:
            return False
        cursor = end
    return True


def plan(epdir: Path) -> dict:
    """Return synthesis units, hashes and billable chars; never generate or mutate files.

    Narration-only books keep one unit per paragraph. Books whose cast lists
    characters split each paragraph into narrator/character runs from
    ``voice_script.yaml``; a unit's ``sentences`` are then sentence fragments.
    """
    epdir = Path(epdir).resolve()
    final = epdir / "final.md"
    project = epdir.parent.parent
    cast = project / "production/voice_cast.yaml"
    if not final.is_file() or not cast.is_file():
        raise ValueError("配音计划缺少定稿或项目音色表")
    from .adapters.ffmpeg_audio import script_rows
    from . import voice_script
    stable = script_rows(final)
    parsed = parse_draft(final.read_text(encoding="utf-8"))
    config_sha = narration_digest(load_config(epdir))
    segments_path, parts_dir = cache_paths(epdir)
    cached = _cached_rows(epdir, segments_path)
    cast_data = voice_script.load_cast(project)
    narrator_voice, _ = voice_script.resolve(cast_data, voice_script.NARRATOR)
    multi = voice_script.required(project)
    if multi:
        units = voice_script.episode_units(project, int(epdir.name[2:]), final, stable)
        cast_sha = voice_script.voices_digest(units, cast_data)
    else:
        units = []
        groups: list[dict] = []
        for line, text in parsed["lines"]:
            if groups and line == groups[-1]["last_line"] + 1:
                groups[-1]["last_line"] = line
                groups[-1]["text"] += "\n" + text
            else:
                groups.append({"first_line": line, "last_line": line, "text": text})
        for group in groups:
            units.append({"text": group["text"], "voice": narrator_voice, "speaker_key": voice_script.NARRATOR,
                          "fragments": [{"id": row["id"], "text": row["text"]}
                                        for row, parsed_row in zip(stable, parsed["sentences"])
                                        if group["first_line"] <= parsed_row["line"] <= group["last_line"]]})
        cast_sha = sha256_file(cast)
    if not units:
        raise ValueError("定稿没有可配音的段落")
    every_cached = [row for rows in cached.values() for row in rows if row]
    planned = []
    for index, unit in enumerate(units, 1):
        sentences = unit["fragments"]
        name = f"para-{index:04d}.mp3"
        text_sha = (voice_script.unit_sha(unit["text"], unit["voice"], narrator_voice) if multi
                    else hashlib.sha256(unit["text"].encode("utf-8")).hexdigest())

        def usable(row: dict) -> bool:
            return _usable(epdir, parts_dir, row, text_sha=text_sha, cast_sha=cast_sha,
                           config_sha=config_sha, sentences=sentences)
        prior = next((row for row in reversed(cached.get(index, [])) if row and usable(row)), None)
        if prior is None and multi:
            prior = next((row for row in reversed(every_cached) if usable(row)), None)
        reused = prior is not None
        planned.append({"index": index, "path": name, "text": unit["text"], "speaker": unit["voice"],
                        "speaker_key": unit["speaker_key"], "text_sha256": text_sha, "sentences": sentences,
                        "cached": reused, "cached_record": prior if reused else None})
    return {"paragraphs": planned, "cast_sha256": cast_sha, "config_sha256": config_sha,
            "multi_voice": multi,
            "new_chars": sum(len(row["text"]) for row in planned if not row["cached"]),
            "reused_paragraphs": sum(row["cached"] for row in planned)}
