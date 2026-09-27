"""Bind an agent-made storyboard and its images to the real manifest (no paid calls).

Storyboards and pictures are made in the Codex session (image generation is
covered by the Codex plan). This stage only validates them against the
current mix, subtitles and pacing rules and records the storyboard/images
stages, so the local FFmpeg render can follow.
"""
from __future__ import annotations

import fcntl
from datetime import datetime, timezone
from pathlib import Path

from .common import load_yaml, sha256_file, write_json
from .media_manifest import real_stage_fresh
from .guard import check as guard_check


def _row(root: Path, path: Path) -> dict:
    return {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}


def bind_existing(project: Path, epdir: Path) -> dict:
    project, epdir = Path(project).resolve(), Path(epdir).resolve()
    manifest_path = epdir / "production/manifest.json"
    manifest = load_yaml(manifest_path, {}) or {}
    stages = manifest.get("stages", {}) if isinstance(manifest, dict) else {}
    if (not isinstance(manifest, dict) or manifest.get("mode") != "real" or not isinstance(stages, dict)
            or not all(real_stage_fresh(epdir, name, stages.get(name)) for name in ("mix", "subs"))):
        raise ValueError("分镜与画面须接在已核验的正式混音和字幕之后")
    from .adapters.ffmpeg_render import inputs
    try:
        plan = inputs(epdir)
    except ValueError as exc:
        return {"status": "warning", "passed": False, "summary": "分镜或画面尚未齐备",
                "errors": [str(exc)],
                "next_actions": ["按最终时间表写 production/storyboard.yaml，逐镜生成图片后重跑 produce"],
                "artifacts": []}
    from .pacing import check as pacing_check
    pacing = pacing_check(epdir)
    if not pacing["passed"]:
        return {"status": "warning", "passed": False, "summary": "分镜未通过画面节奏检查",
                "errors": pacing["errors"][:5], "next_actions": ["按节奏规则调整分镜后重跑 produce"],
                "artifacts": []}
    storyboard = plan["sources"][0]
    images = list(dict.fromkeys(shot["image"] for shot in plan["shots"]))
    if any(not path.resolve().is_relative_to(epdir) for path in (storyboard, *images)):
        raise ValueError("分镜和图片必须放在本集目录内")
    mix_outputs = stages["mix"].get("outputs", [])
    audio_row = next((row for row in mix_outputs if isinstance(row, dict)
                      and (epdir / row.get("path", "")).resolve() == plan["audio"].resolve()), None)
    if audio_row is None or audio_row.get("sha256") != sha256_file(plan["audio"]):
        raise ValueError("分镜对应的混音不是当前正式混音阶段的产物")
    final = epdir / "final.md"
    storyboard_record = {
        "status": "done", "inputs": [_row(project, final), _row(project, plan["audio"])],
        "outputs": [_row(epdir, storyboard)], "cost_cny": 0, "charge_ids": [],
        "pacing": pacing["metrics"], "completed_at": datetime.now(timezone.utc).isoformat()}
    images_record = {
        "status": "done", "inputs": [_row(project, storyboard)],
        "outputs": [_row(epdir, path) for path in images], "cost_cny": 0, "charge_ids": [],
        "origin": "codex_session", "completed_at": datetime.now(timezone.utc).isoformat()}
    same = all(real_stage_fresh(epdir, name, stages.get(name))
               and stages[name].get("inputs") == record["inputs"]
               and stages[name].get("outputs") == record["outputs"]
               for name, record in (("storyboard", storyboard_record), ("images", images_record)))
    if same:
        return {"status": "success", "passed": True, "summary": "分镜与画面未变化，已复用",
                "executed": [], "skipped": ["storyboard", "images"], "next_actions": [],
                "artifacts": [str(storyboard)]}
    with (epdir / "production/.manifest.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            current = load_yaml(manifest_path, {}) or {}
            if current.get("stages", {}).get("mix") != stages["mix"]:
                raise ValueError("混音阶段在分镜绑定期间发生变化")
            current["stages"]["storyboard"] = storyboard_record
            current["stages"]["images"] = images_record
            write_json(manifest_path, current)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return {"status": "success", "passed": True,
            "summary": f"分镜 {len(plan['shots'])} 镜与 {len(images)} 张图片已核验并登记",
            "executed": ["storyboard", "images"], "warnings": pacing["warnings"][:5],
            "next_actions": ["重跑 produce 做本地 FFmpeg 渲染"], "artifacts": [str(storyboard)]}


def advance(project: Path, epdir: Path) -> dict:
    project, epdir = Path(project).resolve(), Path(epdir).resolve()
    guard = guard_check(project, "visual", int(epdir.name[2:]))
    if not guard["passed"]:
        return {"status": "warning", "passed": False, "summary": "正式画面守卫未通过",
                "errors": guard["errors"], "next_actions": ["先完成对应人工确认"], "artifacts": []}
    return bind_existing(project, epdir)
