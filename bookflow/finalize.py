"""Freeze an accepted draft version into final.md.

Freezing only creates the confirmation candidate. It never records a user
confirmation; ``next`` still asks for the 「拍板文案」 passphrase afterwards.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .approvals import _spoken_snapshot
from .common import atomic_write, latest_draft, parse_draft, sha256_file

FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
DRAFT = re.compile(r"draft_v([1-9][0-9]*)\.md")


def _episode_dir(project: Path, ep: int) -> Path:
    if type(ep) is not int or ep < 1:
        raise ValueError("集号必须为正整数")
    project = Path(project).resolve()
    if not (project / "project.yaml").is_file():
        raise ValueError("不是有效的书目项目：缺少 project.yaml")
    epdir = project / "episodes" / f"ep{ep:02d}"
    if any(path.is_symlink() for path in (project / "episodes", epdir)):
        raise ValueError("本集目录是符号链接，拒绝写入定稿")
    if not epdir.is_dir():
        raise ValueError(f"第 {ep} 集目录不存在")
    return epdir


def _source(epdir: Path, source: str | None) -> Path:
    if source is None:
        path = latest_draft(epdir)
        if path is None:
            raise ValueError(f"{epdir.name} 没有 draft_vN.md，无法冻结定稿")
        return path
    name = f"draft_v{source}.md" if str(source).isdigit() else Path(str(source)).name
    if not DRAFT.fullmatch(name):
        raise ValueError("--from 只接受 draft_vN.md 或版本号 N")
    path = epdir / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"找不到 {epdir.name}/{name}")
    return path


def _final_text(text: str, ep: int, source: Path) -> str:
    match = FRONT.match(text)
    if not match:
        raise ValueError(f"{source.name} 缺少 YAML 前言，无法标记为定稿")
    lines = [line for line in match.group(1).split("\n")
             if not re.match(r"(status|frozen_from|frozen_from_sha256|frozen_at):", line)]
    episode = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("episode:")), None)
    if episode is not None and episode != str(ep):
        raise ValueError(f"{source.name} 的 episode 为 {episode}，与第 {ep} 集不一致")
    lines += ["status: final", f"frozen_from: {source.name}",
              f"frozen_from_sha256: {sha256_file(source)}",
              f"frozen_at: '{datetime.now(timezone.utc).isoformat()}'"]
    return "---\n" + "\n".join(lines) + "\n---\n" + text[match.end():]


def freeze(project: Path, ep: int, *, source: str | None = None, replace: bool = False) -> dict:
    epdir = _episode_dir(project, ep)
    draft = _source(epdir, source)
    final = epdir / "final.md"
    if final.is_symlink():
        raise ValueError("final.md 是符号链接，拒绝覆盖")
    text = _final_text(draft.read_text(encoding="utf-8"), ep, draft)
    if not parse_draft(text)["spoken"].strip():
        raise ValueError(f"{draft.name} 没有口播正文")
    from .sentences import generate
    if final.is_file():
        if _spoken_snapshot(final) == _spoken_snapshot(draft):
            if not final.with_suffix(".sentences.json").is_file():
                generate(final, draft)
            return {"passed": True, "episode": ep, "status": "unchanged", "source": draft.name,
                    "summary": f"第 {ep} 集 final.md 的口播正文已与 {draft.name} 一致，未改动"}
        if not replace:
            return {"passed": False, "episode": ep, "status": "conflict", "source": draft.name,
                    "errors": [f"第 {ep} 集已有 final.md，口播正文与 {draft.name} 不同；"
                               "确认要用该版本替换时加 --replace（旧定稿会另存，不删除）"]}
        history = epdir / "final_history"
        history.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        kept = history / f"final_{stamp}.md"
        kept.write_bytes(final.read_bytes())
    else:
        kept = None
    atomic_write(final, text)
    generate(final, draft)
    result = {"passed": True, "episode": ep, "status": "frozen", "source": draft.name,
              "spoken_chars": len(_spoken_snapshot(final)),
              "summary": f"第 {ep} 集已从 {draft.name} 冻结为 final.md（尚未确认）",
              "next_actions": ["全部待确认集冻结后运行 next，请用户阅读并回复「拍板文案」"]}
    if kept is not None:
        result["previous_final"] = str(kept.relative_to(epdir))
    return result


def freeze_many(project: Path, episodes: list[int], *, source: str | None = None,
                replace: bool = False) -> dict:
    if source is not None and len(episodes) != 1:
        raise ValueError("--from 只能配合单集使用；多集时各取最新草稿")
    rows, errors = [], []
    for ep in episodes:
        try:
            row = freeze(project, ep, source=source, replace=replace)
        except ValueError as exc:
            row = {"passed": False, "episode": ep, "status": "error", "errors": [str(exc)]}
        rows.append(row)
        errors.extend(row.get("errors", []))
    frozen = [row["episode"] for row in rows if row["status"] == "frozen"]
    unchanged = [row["episode"] for row in rows if row["status"] == "unchanged"]
    return {"passed": not errors, "status": "success" if not errors else "warning",
            "summary": f"冻结 {len(frozen)} 集，已一致 {len(unchanged)} 集，未处理 {len(rows) - len(frozen) - len(unchanged)} 集",
            "episodes": rows, "errors": errors,
            "next_actions": ["运行 next；定稿冻结不等于文案确认"]}
