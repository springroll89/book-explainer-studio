"""Resumable, hash-checked episode archive; never infer cloud upload from a copy."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .common import load_yaml, sha256_file, write_yaml

EPISODE = re.compile(r"(?:ep)?(0*[1-9][0-9]*)\Z")
MEDIA = {".mp4": "video", ".mov": "video", ".mkv": "video",
         ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".flac": "audio",
         ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image"}
REUSABLE_DIRS = {"sfx", "sound_effects", "sound-effects", "characters", "character_refs",
                 "title_cards", "styles", "reusable"}
REUSABLE_STEMS = ("sfx", "title_card", "titlecard", "series_intro", "character_ref", "logo")


def _episode(project: Path, episode: str | int) -> tuple[Path, int, Path]:
    project = Path(project).resolve()
    match = EPISODE.fullmatch(str(episode))
    if not (project / "project.yaml").is_file() or not match:
        raise ValueError("archive 需要有效书目项目和正整数集号")
    ep = int(match.group(1))
    return project, ep, project / "episodes" / f"ep{ep:02d}"


def _root(project: Path, archive_root: Path | None) -> Path:
    if archive_root is None:
        config = load_yaml(Path.home() / ".config/bookflow/config.yaml", {}) or {}
        configured = config.get("onedrive_archive_root") if isinstance(config, dict) else None
        if not configured:
            raise ValueError("先在本机 ~/.config/bookflow/config.yaml 配置 onedrive_archive_root")
        archive_root = Path(str(configured)).expanduser()
        if "OneDrive" not in str(archive_root):
            raise ValueError("onedrive_archive_root 必须指向 OneDrive 挂载目录")
    root = Path(archive_root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("OneDrive 归档根目录必须是已存在的绝对目录")
    root = root.resolve()
    if root.is_relative_to(project) or project.is_relative_to(root):
        raise ValueError("OneDrive 归档根目录不能与本地书目项目相互包含")
    return root


def _title(project: Path) -> str:
    data = load_yaml(project / "project.yaml", {}) or {}
    book = data.get("book", {}) if isinstance(data, dict) else {}
    title = book.get("title") if isinstance(book, dict) else None
    if not isinstance(title, str) or not title.strip() or title.strip() in {".", ".."} or "/" in title or "\\" in title:
        raise ValueError("project.yaml 的书名不能为空或包含路径分隔符")
    return title.strip()


def _selected(epdir: Path) -> list[Path]:
    result = []
    local_index = load_yaml(epdir.parent.parent / "sound_library/index.yaml", {}) or {}
    if not isinstance(local_index, dict) or not isinstance(local_index.get("assets", []), list):
        raise ValueError("本书 sound_library/index.yaml 格式无效，先核对音效清单再归档")
    assets = local_index.get("assets", [])
    retained = set()
    for row in assets:
        if isinstance(row, dict) and isinstance(row.get("file"), str):
            location = Path(row["file"])
            retained.add((location if location.is_absolute() else epdir.parent.parent / location).resolve())
    cues = load_yaml(epdir / "production/sound_cues.yaml", {}) or {}
    if not isinstance(cues, dict) or not isinstance(cues.get("cues", []), list):
        raise ValueError("本集 production/sound_cues.yaml 格式无效，先核对音效清单再归档")
    for cue in cues.get("cues", []):
        if isinstance(cue, dict):
            identity = cue.get("asset_id")
            if isinstance(identity, str) and identity and not identity.startswith("SFX-"):
                retained.add((epdir / identity).resolve())
    for path in epdir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"本集存在符号链接，归档前需人工检查：{path}")
        if not path.is_file() or path.suffix.lower() not in MEDIA or path.resolve() in retained:
            continue
        relative = path.relative_to(epdir)
        if ("_reports" in relative.parts[:-1]
                or any(part.lower() in REUSABLE_DIRS for part in relative.parts[:-1])
                or path.stem.lower().startswith(REUSABLE_STEMS)):
            continue
        result.append(path)
    return sorted(result)


def _copy_verified(source: Path, target: Path, digest: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"归档目标是符号链接，拒绝覆盖：{target}")
    if target.exists():
        if not target.is_file() or sha256_file(target) != digest:
            raise ValueError(f"归档目标已存在但内容不同，拒绝覆盖：{target}")
        return
    fd, temporary = tempfile.mkstemp(prefix=".bookflow-archive-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as original:
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        if sha256_file(Path(temporary)) != digest:
            raise ValueError(f"复制后哈希不一致，本地原件未动：{source}")
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _uploaded(path: Path) -> bool:
    """Conservative macOS File Provider check; unknown means not uploaded."""
    try:
        done = subprocess.run(["mdls", "-raw", "-name", "kMDItemIsUploaded", str(path)],
                              capture_output=True, text=True, timeout=15, check=False)
        busy = subprocess.run(["mdls", "-raw", "-name", "kMDItemIsUploading", str(path)],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and busy.returncode == 0 and done.stdout.strip() == "1" and busy.stdout.strip() == "0"


def _evicted(path: Path) -> bool:
    if not _uploaded(path):
        return False
    try:
        result = subprocess.run(["mdls", "-raw", "-name", "kMDItemIsDownloaded", str(path)],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "0"


def _manifest(project: Path, ep: int) -> Path:
    return project / "archive" / f"ep{ep:02d}.yaml"


def archived_digest(project: Path, relative: str, *, _data: dict | None = None) -> str | None:
    project = Path(project).resolve()
    match = re.fullmatch(r"episodes/ep([0-9]+)/(.+)", relative)
    if not match:
        return None
    ep = int(match.group(1))
    data = _data if _data is not None else load_yaml(_manifest(project, ep), {}) or {}
    if (not isinstance(data, dict) or data.get("project") != str(project)
            or data.get("episode") != int(match.group(1)) or data.get("status") not in {
            "copied_awaiting_upload", "uploaded_verified_awaiting_eviction", "evicted_awaiting_removal",
            "sources_removed_awaiting_eviction", "complete"}):
        return None
    row = next((item for item in data.get("files", []) if isinstance(item, dict)
                and item.get("source") == relative and
                (item.get("removed") is True or
                 data["status"] == "evicted_awaiting_removal" and item.get("removal_pending") is True
                 and not (Path(project) / relative).exists())), None)
    digest = row.get("sha256") if row else None
    target = Path(str(row.get("target", ""))) if row else None
    # Reading an online-only File Provider item here would hydrate it on every `next`.
    try:
        root = Path(str(data.get("archive_root", "")))
        expected = root / "书籍讲解" / _title(project) / f"ep{ep:02d}"
        return (digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                and root.is_absolute() and target is not None and target.is_absolute()
                and target.resolve().is_relative_to(expected.resolve())
                and not target.is_symlink() and target.is_file()
                and isinstance(row.get("size"), int) and target.stat().st_size == row["size"] else None)
    except (OSError, ValueError):
        return None


def check_complete(project: Path, episode: str | int) -> dict:
    """Check a completed archive using metadata only; never hydrate online-only files."""
    project, ep, _ = _episode(project, episode)
    relative = f"archive/ep{ep:02d}.yaml"
    try:
        data = load_yaml(_manifest(project, ep), {}) or {}
    except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError) as exc:
        return {"passed": False, "pending": False, "errors": [f"{relative} 无法读取：{exc}"]}
    if not isinstance(data, dict):
        return {"passed": False, "pending": False, "errors": [f"{relative} 格式无效"]}
    if data.get("status") != "complete":
        return {"passed": False, "pending": True, "errors": [f"{relative} 尚未完成上传、仅在线与本地清单核对"]}
    rows = data.get("files")
    if (data.get("project") != str(project) or data.get("episode") != ep
            or not isinstance(rows, list) or not rows):
        return {"passed": False, "pending": False,
                "errors": [f"{relative} 与当前书目、集号或文件列表不一致"]}
    errors: list[str] = []
    seen: set[str] = set()
    prefix = f"episodes/ep{ep:02d}/"
    for row in rows:
        source = row.get("source") if isinstance(row, dict) else None
        if (not isinstance(source, str) or not source.startswith(prefix)
                or ".." in Path(source).parts):
            errors.append(f"{relative} 存在非本集归档源文件")
            continue
        if source in seen:
            errors.append(f"{relative} 重复登记 {source}")
            continue
        seen.add(source)
        digest = row.get("sha256")
        if (row.get("removed") is not True or type(row.get("size")) is not int
                or row["size"] < 0 or not isinstance(row.get("target"), str)
                or not isinstance(digest, str)):
            errors.append(f"{relative} 的 {source} 清单条目无效")
            continue
        try:
            valid = archived_digest(project, source, _data=data) == digest
        except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError):
            valid = False
        if not valid:
            errors.append(f"{relative} 的 {source} 目标位置、大小或移除记录无效")
    if prefix + "production/final.mp4" not in seen:
        errors.append(f"{relative} 缺少成片 production/final.mp4 的归档记录")
    return {"passed": not errors, "pending": False, "errors": errors}


def run(project: Path, episode: str | int, *, archive_root: Path | None = None,
        upload_check: Callable[[Path], bool] | None = None,
        eviction_check: Callable[[Path], bool] | None = None) -> dict:
    from .approvals import confirmation_state

    project, ep, epdir = _episode(project, episode)
    if confirmation_state(project, "release", ep)["state"] != "passed":
        raise ValueError("成片确认未通过或已失效，不能归档")
    if not (epdir / "deliver/index.html").is_file():
        raise ValueError("本集尚无正式导出交付包，不能归档")
    root = _root(project, archive_root)
    target_base = root / "书籍讲解" / _title(project) / f"ep{ep:02d}"
    if not target_base.resolve().is_relative_to(root) or target_base.resolve().is_relative_to(project):
        raise ValueError("归档目标越出 OneDrive 根目录或位于本地书目项目内")
    manifest_path = _manifest(project, ep)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with (manifest_path.parent / f".ep{ep:02d}.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = load_yaml(manifest_path, {}) or {}
            if data and (not isinstance(data, dict) or data.get("archive_root") != str(root)
                         or data.get("project") != str(project) or data.get("episode") != ep):
                raise ValueError("归档清单与当前目标不一致；先备份核对，不覆盖")
            if not data:
                files = _selected(epdir)
                if not files or not (epdir / "production/final.mp4") in files:
                    raise ValueError("没有可归档媒体或缺少成片视频")
                rows = []
                for source in files:
                    relative = source.relative_to(epdir)
                    target = target_base / relative
                    if not target.resolve().is_relative_to(root):
                        raise ValueError("归档目标路径越出 OneDrive 根目录")
                    digest = sha256_file(source)
                    _copy_verified(source, target, digest)
                    rows.append({"source": str(source.relative_to(project)), "target": str(target),
                                 "kind": MEDIA[source.suffix.lower()], "size": source.stat().st_size,
                                 "sha256": digest, "removed": False})
                data = {"status": "copied_awaiting_upload", "project": str(project), "episode": ep,
                        "archive_root": str(root), "created_at": datetime.now(timezone.utc).isoformat(),
                        "bytes_freed": 0, "files": rows}
                write_yaml(manifest_path, data)
            rows = data.get("files")
            if (data.get("status") not in {"copied_awaiting_upload", "uploaded_verified_awaiting_eviction",
                                        "evicted_awaiting_removal",
                                        "sources_removed_awaiting_eviction", "complete"}
                    or not isinstance(rows, list) or not rows
                    or any(not isinstance(row, dict) or not {"source", "target", "sha256", "size", "removed"} <= row.keys()
                           or not isinstance(row["size"], int) or row["size"] < 0
                           or not isinstance(row["removed"], bool)
                           or ("removal_pending" in row and not isinstance(row["removal_pending"], bool))
                           for row in rows)):
                raise ValueError("归档清单状态或文件条目无效；先备份修复")
            selected = {str(path.relative_to(project)) for path in _selected(epdir)}
            expected = {row["source"] for row in rows
                        if not row.get("removed") and not row.get("removal_pending")}
            known = {row["source"] for row in rows}
            if not expected.issubset(selected) or not selected.issubset(known):
                raise ValueError("本集媒体文件清单已变化，拒绝删除；请先人工核对")
            for row in rows:
                source = (project / row["source"]).resolve()
                target = Path(row["target"])
                if (not source.is_relative_to(epdir) or not target.resolve().is_relative_to(target_base.resolve())
                        or target.is_symlink() or not target.is_file() or target.stat().st_size != row["size"]
                        or (not row.get("removed") and not source.is_file()
                            and not (data["status"] == "evicted_awaiting_removal"
                                     and row.get("removal_pending") is True))
                        or (source.is_file() and sha256_file(source) != row["sha256"])):
                    raise ValueError("归档文件或哈希已变化，拒绝删除本地原件")
            if data["status"] == "complete":
                if not all(row["removed"] for row in rows):
                    raise ValueError("已完成的归档清单仍有未移除文件")
                return {"status": "success", "passed": True, "summary": "本集已归档；云端内容在释放空间前已核验",
                        "bytes_freed": data["bytes_freed"], "artifacts": [str(manifest_path)]}
            checker = upload_check or _uploaded
            if not all(checker(Path(row["target"])) for row in rows):
                originals_present = data["status"] in {"copied_awaiting_upload", "uploaded_verified_awaiting_eviction"}
                return {"status": "awaiting_upload" if originals_present else "awaiting_sync_evidence", "passed": True,
                        "summary": ("OneDrive 上传尚未逐文件确认；本地原件未删除" if originals_present
                                    else "原件可能已部分移除，但目前无法重新读取 OneDrive 上传证据；等待核对"),
                        "bytes_freed": 0, "artifacts": [str(manifest_path)]}
            if data["status"] == "copied_awaiting_upload":
                if any(sha256_file(Path(row["target"])) != row["sha256"] for row in rows):
                    raise ValueError("上传后云端副本哈希不一致，本地原件未删除")
                data["status"] = "uploaded_verified_awaiting_eviction"
                data["uploaded_verified_at"] = datetime.now(timezone.utc).isoformat()
                write_yaml(manifest_path, data)
            if not all((eviction_check or _evicted)(Path(row["target"])) for row in rows):
                return {"status": "awaiting_eviction", "passed": True,
                        "summary": "上传已核实，但云端副本尚未逐文件证实为仅在线；未开始移除的本地原件保留",
                        "source_bytes_removed": sum(row["size"] for row in rows
                                                    if not (project / row["source"]).exists()), "bytes_freed": 0,
                        "artifacts": [str(manifest_path)],
                        "next_actions": ["在访达对该集 OneDrive 归档目录使用“释放空间”，然后重跑 archive 核验"]}
            if data["status"] != "evicted_awaiting_removal":
                data["status"] = "evicted_awaiting_removal"
                write_yaml(manifest_path, data)
            for row in rows:
                if row.get("removed"):
                    continue
                source = project / row["source"]
                if source.is_file():
                    row["removal_pending"] = True
                    write_yaml(manifest_path, data)
                    source.unlink()
                elif row.get("removal_pending") is not True:
                    raise ValueError("本地原件缺失且没有中断记录，停止归档")
                row["removed"] = True
                row.pop("removal_pending", None)
                write_yaml(manifest_path, data)
            data["status"] = "complete"
            data["bytes_freed"] = sum(row["size"] for row in rows if not (project / row["source"]).exists())
            data["completed_at"] = datetime.now(timezone.utc).isoformat()
            write_yaml(manifest_path, data)
            return {"status": "success", "passed": True,
                    "summary": "本集归档完成；bytes_freed 是源文件逻辑大小，实际磁盘空间请用 du/df 复核",
                    "bytes_freed": data["bytes_freed"], "artifacts": [str(manifest_path)]}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def restore(project: Path, episode: str | int, *, only: str | None = None) -> dict:
    project, ep, epdir = _episode(project, episode)
    if only is not None and only not in set(MEDIA.values()):
        raise ValueError("--only 只能是 audio、video 或 image")
    data = load_yaml(_manifest(project, ep), {}) or {}
    if (not isinstance(data, dict) or data.get("project") != str(project) or data.get("episode") != ep
            or data.get("status") not in {"sources_removed_awaiting_eviction", "complete"}
            or not isinstance(data.get("files"), list)):
        raise ValueError("本集没有已完成的归档清单")
    root = Path(str(data.get("archive_root", "")))
    if not root.is_absolute():
        raise ValueError("归档清单缺少有效的云端根目录")
    target_base = root / "书籍讲解" / _title(project) / f"ep{ep:02d}"
    restored = []
    for row in data["files"]:
        if only and row.get("kind") != only:
            continue
        destination = (project / row["source"]).resolve()
        source = Path(row["target"])
        if (not destination.is_relative_to(epdir) or not source.is_absolute()
                or not source.resolve().is_relative_to(target_base.resolve()) or source.is_symlink()
                or not source.is_file() or sha256_file(source) != row["sha256"]):
            raise ValueError("归档源文件缺失或哈希不一致，停止取回")
        if destination.exists():
            if not destination.is_file() or sha256_file(destination) != row["sha256"]:
                raise ValueError(f"本地文件已存在且内容不同，拒绝覆盖：{destination}")
            continue
        _copy_verified(source, destination, row["sha256"])
        restored.append(str(destination))
    return {"status": "success", "passed": True, "summary": f"取回 {len(restored)} 个文件并核对哈希",
            "restored": restored, "artifacts": [str(_manifest(project, ep))]}
