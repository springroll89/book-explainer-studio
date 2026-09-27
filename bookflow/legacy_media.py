"""Adopt an episode finished with the pre-pipeline tools as its current media.

Adoption records the four sample deliverables (mix, subtitles, storyboard,
video) and the spoken text they were made from. It does not pretend that the
stage pipeline ran: ``produce check`` reports every stage as
``legacy_adopted`` only while those files and the script stay unchanged. The
user still confirms the result with 「拍板样片」/「拍板成片」.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .common import load_yaml, sha256_file, write_json, write_yaml

MODE = "legacy_adopted"
KINDS = ("mix", "subtitles", "storyboard", "video")
SUFFIXES = {"mix": {".wav", ".mp3", ".m4a", ".flac"}, "subtitles": {".srt"},
            "storyboard": {".yaml", ".yml", ".json", ".md"}, "video": {".mp4", ".mov", ".mkv"}}


def _spoken_sha(final: Path) -> str:
    from .approvals import _spoken_snapshot
    return hashlib.sha256(_spoken_snapshot(final).encode("utf-8")).hexdigest()


def _relative(epdir: Path, kind: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少 {kind} 文件路径")
    candidate = Path(value)
    path = candidate if candidate.is_absolute() else epdir / candidate
    if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(epdir) and part != epdir):
        raise ValueError(f"{kind} 路径含符号链接：{value}")
    resolved = path.resolve()
    if not resolved.is_relative_to(epdir.resolve()):
        raise ValueError(f"{kind} 文件必须在本集目录内：{value}")
    relative = resolved.relative_to(epdir.resolve())
    if {"_reports", "_history"} & set(relative.parts):
        raise ValueError(f"自动报告或历史文件不能作为成品：{value}")
    if not resolved.is_file():
        raise ValueError(f"{kind} 文件不存在：{value}")
    if resolved.suffix.lower() not in SUFFIXES[kind]:
        raise ValueError(f"{kind} 文件类型应为 {'、'.join(sorted(SUFFIXES[kind]))}：{value}")
    return str(relative)


def adopt(project: Path, ep: int, *, files: dict[str, str], note: str, replace: bool = False) -> dict:
    project = Path(project).resolve()
    if type(ep) is not int or ep < 1:
        raise ValueError("集号必须为正整数")
    epdir = project / "episodes" / f"ep{ep:02d}"
    final = epdir / "final.md"
    if epdir.is_symlink() or not final.is_file() or final.is_symlink():
        raise ValueError(f"第 {ep} 集缺少 final.md；先用 final freeze 冻结定稿")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("收编需要一句说明，例如“旧流程 V3，用户已审看”")
    if set(files) != set(KINDS):
        raise ValueError("收编需要 mix、subtitles、storyboard、video 四个文件")
    relative = {kind: _relative(epdir, kind, files[kind]) for kind in KINDS}
    production = epdir / "production"
    production.mkdir(exist_ok=True)
    manifest_path = production / "manifest.json"
    assets_path = production / "approval_assets.yaml"
    manifest = load_yaml(manifest_path, {}) or {}
    if manifest and not isinstance(manifest, dict):
        raise ValueError("production/manifest.json 格式错误；先备份再处理")
    if manifest and manifest.get("mode") != MODE and (manifest.get("stages") or manifest.get("charges")):
        raise ValueError("本集已有新流水线媒体清单，不能再收编旧成品")
    deliverables = {kind: {"path": relative[kind], "sha256": sha256_file(epdir / relative[kind])}
                    for kind in KINDS}
    if manifest.get("mode") == MODE and manifest.get("deliverables") != deliverables and not replace:
        raise ValueError("本集已收编过另一组成品；确认替换时加 --replace")
    assets = load_yaml(assets_path, {}) or {}
    wanted = {"assets": relative}
    if assets and assets != wanted and not replace:
        raise ValueError("production/approval_assets.yaml 已指向其他文件；确认替换时加 --replace")
    record = {"schema": 1, "mode": MODE, "note": note.strip(),
              "adopted_at": datetime.now(timezone.utc).isoformat(),
              "final_spoken_sha256": _spoken_sha(final),
              "deliverables": deliverables, "charges": [], "stages": {}}
    write_yaml(assets_path, wanted)
    write_json(manifest_path, record)
    confirm = "拍板样片" if ep == 1 else "拍板成片"
    return {"passed": True, "status": "success", "episode": ep,
            "summary": f"第 {ep} 集旧流程成品已收编；媒体阶段按收编成品计为有效",
            "deliverables": relative,
            "next_actions": [f"请用户观看 {relative['video']} 后回复「{confirm}」；收编不等于确认"],
            "artifacts": [str(manifest_path), str(assets_path)]}


def state(epdir: Path, manifest: dict) -> tuple[bool, str]:
    """Adopted media stay valid only while the script and every file are unchanged."""
    epdir = Path(epdir).resolve()
    final = epdir / "final.md"
    if not final.is_file() or manifest.get("final_spoken_sha256") != _spoken_sha(final):
        return False, "legacy_script_changed"
    deliverables = manifest.get("deliverables")
    if not isinstance(deliverables, dict) or set(deliverables) != set(KINDS):
        return False, "legacy_manifest_invalid"
    from .archive import archived_digest
    project = epdir.parent.parent
    for kind in KINDS:
        row = deliverables[kind]
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            return False, "legacy_manifest_invalid"
        path = (epdir / row["path"]).resolve()
        if not path.is_relative_to(epdir) or path.is_symlink():
            return False, "legacy_manifest_invalid"
        digest = sha256_file(path) if path.is_file() else archived_digest(project, str(path.relative_to(project)))
        if digest != row.get("sha256"):
            return False, f"legacy_{kind}_changed"
    return True, "legacy_adopted"
