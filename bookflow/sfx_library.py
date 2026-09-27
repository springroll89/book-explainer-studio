"""Local reusable sound library; indexing never starts paid generation."""
from __future__ import annotations

import fcntl
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

from .common import ROOT, load_yaml, sha256_file, write_yaml

CLASSES = {"ambience", "event", "process", "design", "music"}
STATUSES = {"pending", "accepted", "rejected"}
SFX_ID = re.compile(r"SFX-[0-9]{4,}")
EPISODE = re.compile(r"(?:ep)?(0*[1-9][0-9]*)\Z")


def location(project: Path | None = None, *, library: Path | None = None) -> Path:
    if library is not None:
        return Path(library).expanduser().resolve()
    local = load_yaml(Path.home() / ".config/bookflow/config.yaml", {}) or {}
    configured = local.get("sfx_library") if isinstance(local, dict) else None
    if configured:
        selected = Path(str(configured)).expanduser()
        if not selected.is_absolute():
            raise ValueError("本机 sfx_library 配置必须是绝对路径")
        return selected.resolve()
    if project is not None:
        return Path(project).resolve().parent.parent / "音效库"
    return ROOT / "音效库"


def _rows(root: Path) -> list[dict]:
    index = root / "index.yaml"
    if index.is_symlink():
        raise ValueError("音效库 index.yaml 不能是符号链接")
    data = load_yaml(index, []) or []
    if not isinstance(data, list):
        raise ValueError("音效库 index.yaml 必须是列表；先备份修复，不覆盖")
    ids = set()
    for row in data:
        if not isinstance(row, dict) or not SFX_ID.fullmatch(str(row.get("id", ""))):
            raise ValueError("音效库存在无效条目 ID；先备份修复")
        relative = Path(str(row.get("file", "")))
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or row["id"] in ids or row.get("status") not in STATUSES
                or row.get("class") not in CLASSES or not isinstance(row.get("tags"), list)
                or not isinstance(row.get("used_in"), list)):
            raise ValueError("音效库条目路径、状态或 ID 重复；先备份修复")
        ids.add(row["id"])
    return data


def _valid_asset(root: Path, row: dict) -> bool:
    entry = root / row["file"]
    path = entry.resolve()
    has_link = any(part.is_symlink() for part in (entry, *entry.parents)
                   if part != root and part.is_relative_to(root))
    return (not has_link and path.is_relative_to(root.resolve()) and path.is_file()
            and isinstance(row.get("sha256"), str) and sha256_file(path) == row["sha256"])


def accepted_asset(identity: str, *, project: Path | None = None, library: Path | None = None) -> dict | None:
    root = location(project, library=library)
    row = next((row for row in _rows(root) if row["id"] == identity), None)
    return row if row and row["status"] == "accepted" and _valid_asset(root, row) else None


def search(terms: list[str], *, library: Path | None = None, project: Path | None = None,
           sound_class: str | None = None) -> dict:
    root = location(project, library=library)
    if sound_class is not None and sound_class not in CLASSES:
        raise ValueError("音效类别无效")
    query = [str(term).strip().casefold() for term in terms if str(term).strip()]
    results = []
    for row in _rows(root):
        if row.get("status") != "accepted" or sound_class and row.get("class") != sound_class:
            continue
        if not _valid_asset(root, row):
            continue
        haystack = [str(row.get("desc", "")).casefold(),
                    *[str(tag).casefold() for tag in row.get("tags", []) if isinstance(tag, str)]]
        score = sum(any(term in word or word in term for word in haystack if word) for term in query)
        if query and score == 0:
            continue
        results.append({"id": row["id"], "class": row["class"], "desc": row.get("desc", ""),
                        "tags": row.get("tags", []), "duration_sec": row.get("duration_sec"),
                        "score": score, "file": str(root / row["file"]), "cost_cny": row.get("cost_cny", 0)})
    results.sort(key=lambda item: (-item["score"], item["id"]))
    return {"status": "success", "passed": True, "summary": f"找到 {len(results)} 条已试听通过的可用音效",
            "items": results, "next_actions": ["试听候选并在 cue 的 asset_id 明确选用；未选用前仍按新生成估算"] if results else [],
            "artifacts": [str(root / "index.yaml")] if (root / "index.yaml").is_file() else []}


def _duration(path: Path) -> float:
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as stream:
                return stream.getnframes() / stream.getframerate()
        except (wave.Error, ZeroDivisionError, EOFError, OSError) as exc:
            raise ValueError("WAV 音效格式无效") from exc
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise ValueError("非 WAV 音效需要 ffprobe 核对时长")
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("ffprobe 未能读取音效时长")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ValueError("ffprobe 返回的音效时长无效") from exc
    if not math.isfinite(duration):
        raise ValueError("ffprobe 返回的音效时长无效")
    return duration


def _approved_source(project: Path, ep: int, source: Path, digest: str) -> None:
    from .approvals import confirmation_state
    episode = project / "episodes" / f"ep{ep:02d}"
    if not source.is_relative_to(episode.resolve()) or not source.is_file():
        raise ValueError("accepted 音效必须来自当前集的 production，并有样片或成片确认")
    if not source.is_relative_to((episode / "production").resolve()):
        raise ValueError("accepted 音效必须来自本集 production")
    if (confirmation_state(project, "sample", ep)["state"] != "passed"
            and confirmation_state(project, "release", ep)["state"] != "passed"):
        raise ValueError("音效尚无有效样片或成片确认，先以 pending 入库")
    manifest = load_yaml(episode / "production/manifest.json", {}) or {}
    sfx = manifest.get("stages", {}).get("sfx", {}) if isinstance(manifest, dict) else {}
    outputs = sfx.get("outputs", []) if isinstance(sfx, dict) else []
    relative = str(source.relative_to(episode.resolve()))
    if not any(isinstance(item, dict) and item.get("path") == relative and item.get("sha256") == digest
               for item in outputs):
        raise ValueError("音效文件未绑定本集制作清单或哈希已变，不能标记 accepted")


def add(project: Path, episode: str | int, source: Path, *, desc: str, tags: list[str],
        sound_class: str, loop: bool = False, prompt: str = "", model: str = "",
        cost_cny: float = 0, status: str = "pending", library: Path | None = None) -> dict:
    project = Path(project).resolve()
    match = EPISODE.fullmatch(str(episode))
    if not (project / "project.yaml").is_file() or not match:
        raise ValueError("sfx add 需要有效书目项目和集号")
    ep = int(match.group(1))
    source = Path(source).resolve()
    if not source.is_file() or not desc.strip() or not tags or any(not str(tag).strip() for tag in tags):
        raise ValueError("音效文件、描述和非空标签均为必填")
    if (sound_class not in CLASSES or status not in STATUSES or not isinstance(cost_cny, (int, float))
            or not math.isfinite(cost_cny) or cost_cny < 0):
        raise ValueError("音效类别、状态或费用无效")
    if source.suffix.lower() not in {".wav", ".mp3", ".m4a", ".flac"}:
        raise ValueError("暂只支持 WAV、MP3、M4A、FLAC 音效")
    digest = sha256_file(source)
    duration = _duration(source)
    if duration <= 0:
        raise ValueError("音效时长必须大于零")
    if status == "accepted":
        _approved_source(project, ep, source, digest)
    root = location(project, library=library)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".index.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            rows = _rows(root)
            previous = next((row for row in rows if row.get("sha256") == digest), None)
            if previous:
                if not _valid_asset(root, previous):
                    raise ValueError("同哈希音效条目文件无效；先备份修复，不覆盖")
                return {"status": "warning", "passed": True, "summary": "同一音频哈希已入库，未重复复制或变更状态",
                        "id": previous["id"], "next_actions": ["核对现有条目；状态变更需单独审核"],
                        "artifacts": [str(root / "index.yaml")]}
            number = max((int(row["id"][4:]) for row in rows), default=0) + 1
            identity = f"SFX-{number:04d}"
            relative = Path(sound_class) / f"{digest[:16]}{source.suffix.lower()}"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".sfx-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as destination, source.open("rb") as original:
                    shutil.copyfileobj(original, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                if sha256_file(Path(temporary)) != digest:
                    raise ValueError("复制音效后哈希不一致，未入库")
                os.link(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
            row = {"id": identity, "file": str(relative), "sha256": digest, "desc": desc.strip(),
                   "tags": [str(tag).strip() for tag in tags], "class": sound_class, "loop": bool(loop),
                   "duration_sec": round(duration, 3), "prompt": prompt, "model": model,
                   "cost_cny": float(cost_cny), "from": f"{project.name}/ep{ep:02d}",
                   "used_in": [f"{project.name}/ep{ep:02d}"], "status": status,
                   "added_at": datetime.now(timezone.utc).isoformat()}
            rows.append(row)
            write_yaml(root / "index.yaml", rows)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return {"status": "success", "passed": True, "summary": f"音效 {identity} 已入库，状态 {status}",
            "id": identity, "next_actions": ["仅 accepted 音效会作为复用候选"],
            "artifacts": [str(root / "index.yaml"), str(target)]}


def stats(*, library: Path | None = None, project: Path | None = None) -> dict:
    root = location(project, library=library)
    rows = _rows(root)
    accepted = [row for row in rows if row["status"] == "accepted" and _valid_asset(root, row)]
    reuse = sum(max(0, len(row.get("used_in", [])) - 1) for row in accepted)
    saved = sum(max(0, len(row.get("used_in", [])) - 1) * float(row.get("cost_cny", 0)) for row in accepted)
    return {"status": "success", "passed": True, "summary": f"音效库 {len(rows)} 条，其中有效已试听通过 {len(accepted)} 条",
            "by_class": {kind: sum(row.get("class") == kind for row in accepted) for kind in sorted(CLASSES)},
            "by_status": {kind: sum(row.get("status") == kind for row in rows) for kind in sorted(STATUSES)},
            "reuse_count": reuse, "estimated_saved_cny": round(saved, 2),
            "next_actions": [], "artifacts": [str(root / "index.yaml")] if (root / "index.yaml").is_file() else []}
