"""Read-only integrity checks for real media stage manifests."""
from __future__ import annotations

import re
from pathlib import Path

from .common import sha256_file

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED_INPUTS = {
    "cues": {"episodes/{ep}/final.md", "episodes/{ep}/final.sentences.json"},
    "voice": {"episodes/{ep}/final.md", "production/voice_cast.yaml"},
}
OUTPUT_SUFFIXES = {
    "cues": ({".yaml", ".yml", ".json"},),
    "voice": ({".wav", ".mp3", ".m4a", ".flac"}, {".json"}),
    "sfx": ({".wav", ".mp3", ".m4a", ".flac", ".json"},),
    "mix": ({".wav", ".mp3", ".m4a", ".flac"}, {".json"}),
    "subs": ({".srt"},),
    "storyboard": ({".yaml", ".yml", ".json"},),
    "images": ({".png", ".jpg", ".jpeg", ".webp"},),
    "render": ({".mp4", ".mov", ".mkv"},),
}


def _file(root: Path, row: object, *, project: Path | None = None, archived: bool = False) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("path"), str):
        return False
    relative = Path(row["path"])
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or not isinstance(row.get("sha256"), str) or not SHA256.fullmatch(row["sha256"])):
        return False
    entry = root / relative
    if any(part.is_symlink() for part in (entry, *entry.parents) if part != root and part.is_relative_to(root)):
        return False
    resolved = entry.resolve()
    if not resolved.is_relative_to(root.resolve()):
        return False
    if resolved.is_file():
        return sha256_file(resolved) == row["sha256"]
    if archived and project is not None:
        from .archive import archived_digest
        return archived_digest(project, str(entry.relative_to(project))) == row["sha256"]
    return False


def real_stage_fresh(epdir: Path, stage: str, record: object) -> bool:
    """A status flag alone never proves a real stage is current."""
    epdir = Path(epdir).resolve()
    if stage not in OUTPUT_SUFFIXES or not isinstance(record, dict) or record.get("status") != "done":
        return False
    inputs, outputs = record.get("inputs"), record.get("outputs")
    if (not isinstance(inputs, list) or not inputs or not isinstance(outputs, list) or not outputs
            or len(inputs) != len({(row.get("scope", "project"), row.get("path"))
                                   for row in inputs if isinstance(row, dict)})
            or len(outputs) != len({row.get("path") for row in outputs if isinstance(row, dict)})):
        return False
    suffixes = {Path(row["path"]).suffix.lower() for row in outputs
                if isinstance(row, dict) and isinstance(row.get("path"), str)}
    if any(not suffixes.intersection(group) for group in OUTPUT_SUFFIXES[stage]):
        return False
    project = epdir.parent.parent
    used_project_inputs: set[str] = set()
    for row in inputs:
        if not isinstance(row, dict):
            return False
        scope = row.get("scope", "project")
        if scope == "project":
            root = project
            used_project_inputs.add(str(row.get("path")))
        elif scope == "sfx_library" and stage in {"sfx", "mix"}:
            from .sfx_library import location
            root = location(project)
        else:
            return False
        if not _file(root, row, project=project, archived=scope == "project"):
            return False
    required = {name.format(ep=epdir.name) for name in REQUIRED_INPUTS.get(stage, set())}
    if stage == "sfx":
        from .sound import _cue_path
        try:
            required.add(str(_cue_path(epdir).relative_to(project)))
        except ValueError:
            return False
    if not required.issubset(used_project_inputs):
        return False
    return all(_file(epdir, row, project=project, archived=True) for row in outputs)
