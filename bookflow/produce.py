"""Stage manifest and resumable offline test adapter for episode production."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .approvals import confirmation_state
from .common import load_config, load_yaml, sha256_file, write_json
from .media_fixture import outputs as fixture_outputs
from .media_fixture import run_stage as run_fixture_stage
from .voice_paths import cache_paths

STAGES = ("cues", "voice", "sfx", "mix", "subs", "storyboard", "images", "render")
DEPENDENCIES = {"voice": ("cues",), "sfx": ("cues",), "mix": ("voice", "sfx"),
                "subs": ("mix",), "storyboard": ("mix",), "images": ("storyboard",),
                "render": ("mix", "subs", "images")}
LINKED_INPUTS = {"sfx": ("cues",), "mix": ("voice", "sfx"), "subs": ("mix",),
                 "storyboard": ("mix",), "images": ("storyboard",),
                 "render": ("mix", "subs", "images")}


def _linked_inputs(project: Path, epdir: Path, stage: str, record: dict, stages: dict) -> bool:
    inputs = {(row.get("path"), row.get("sha256")) for row in record.get("inputs", [])
              if isinstance(row, dict) and row.get("scope", "project") == "project"}
    for upstream in LINKED_INPUTS.get(stage, ()):
        source = stages.get(upstream, {})
        outputs = source.get("outputs", []) if isinstance(source, dict) else []
        if not isinstance(outputs, list) or not any(
            isinstance(row, dict) and isinstance(row.get("path"), str)
            and (str((epdir / row["path"]).relative_to(project)), row.get("sha256")) in inputs
            for row in outputs
        ):
            return False
    return True


def _episode(project: Path, episode: str | int) -> tuple[Path, Path]:
    project = Path(project).resolve()
    match = re.fullmatch(r"(?:ep)?(0*[1-9][0-9]*)", str(episode))
    if not (project / "project.yaml").is_file() or not match:
        raise ValueError("produce 需要有效书目项目和正整数集号，例如 ep03")
    epdir = project / "episodes" / f"ep{int(match.group(1)):02d}"
    final = epdir / "final.md"
    if not final.is_file():
        raise ValueError("本集缺少 final.md；不能从草稿生成正式音画")
    return epdir, final


def _inputs(final: Path, stage: str) -> list[Path]:
    production = final.parent / "production"
    voice_segments = cache_paths(final.parent)[0] if stage == "mix" else production / "voice_segments.json"
    table = {
        "cues": [final],
        "voice": [final, final.parent.parent.parent / "production/voice_cast.yaml"],
        "sfx": [production / "sound_cues.yaml"],
        "mix": [final, production / "voice.wav", voice_segments,
                production / "timing.json", production / "sfx.wav", production / "sound_cues.yaml"],
        "subs": [final, production / "timing_actual.json", production / "final_mix.wav"],
        "storyboard": [final, production / "timing_actual.json"],
        "images": [production / "storyboard.yaml"],
        "render": [production / "placeholder.png", production / "final_mix.wav",
                   production / "subtitles.srt", production / "storyboard.yaml"],
    }
    return table[stage]


def _input_digest(final: Path, stage: str) -> str:
    digest = hashlib.sha256()
    for path in _inputs(final, stage):
        if not path.is_file():
            raise ValueError(f"{stage} 缺少输入 {path.name}；先完成前置阶段")
        digest.update(str(path.relative_to(final.parent.parent.parent)).encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def _manifest(path: Path) -> dict:
    data = load_yaml(path, {}) or {}
    if not isinstance(data, dict) or not isinstance(data.get("stages", {}), dict):
        raise ValueError("production/manifest.json 格式错误；先备份并修复，不覆盖旧记录")
    return data


def _stage_config_digest(epdir: Path, stage: str) -> str:
    config = load_config(epdir)
    keys = {"mix": ("producers", "mix", "sound_design"),
            "subs": ("producers", "subs"),
            "render": ("producers", "render", "visual_pacing")}[stage]
    relevant = {key: config.get(key) for key in keys}
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _fresh(final: Path, stage: str, record: dict, digest: str) -> bool:
    if not isinstance(record, dict) or record.get("status") != "done" or record.get("input_sha256") != digest:
        return False
    expected = fixture_outputs(final, stage)
    saved = record.get("outputs", [])
    if not isinstance(saved, list) or len(saved) != len(expected):
        return False
    for path, row in zip(expected, saved):
        if (not isinstance(row, dict) or row.get("path") != str(path.relative_to(final.parent))
                or not path.is_file() or row.get("sha256") != sha256_file(path)):
            return False
    return True


def _test_fixture(project: Path, config: dict | None = None) -> bool:
    project = Path(project).resolve()
    config = config if config is not None else load_yaml(project / "project.yaml", {}) or {}
    approvals = config.get("approvals", {}) if isinstance(config, dict) else {}
    marker = project.parent.parent / ".bookflow-selftest"
    return (project.name == "selftest-fixture" and isinstance(approvals, dict)
            and approvals.get("test_fixture_only") is True and marker.is_file()
            and marker.read_text(encoding="utf-8") == "test_fixture_only\n")


def _preflight(project: Path, episode: str | int, test_mode: bool) -> tuple[Path, Path, dict]:
    epdir, final = _episode(project, episode)
    config = load_yaml(Path(project) / "project.yaml", {}) or {}
    if test_mode and not _test_fixture(project, config):
        raise ValueError("--test-mode 仅限标为 test_fixture_only 的隔离示范项目，拒绝写入真实书目")
    number = int(epdir.name[2:])
    if confirmation_state(project, "script", number)["state"] != "passed":
        raise ValueError("本集文案确认未通过或已失效；先核对 final.md 与确认记录")
    return epdir, final, config


def check(project: Path, episode: str | int, *, until: str | None = None) -> dict:
    """Read-only report; checking never creates a manifest or calls paid services."""
    if until is not None and until not in STAGES:
        raise ValueError("produce check --until 指定了未知媒体阶段")
    epdir, final = _episode(project, episode)
    manifest_path = epdir / "production/manifest.json"
    manifest = _manifest(manifest_path)
    rows = []
    readiness: dict[str, bool] = {}
    real = manifest.get("mode") == "real"
    fixture = manifest.get("mode") == "test" and manifest.get("test_fixture_only") is True and _test_fixture(project)
    stages_to_check = STAGES[:STAGES.index(until) + 1] if until else STAGES
    for stage in stages_to_check:
        record = manifest.get("stages", {}).get(stage, {})
        if real:
            from .media_manifest import real_stage_fresh
            ready = real_stage_fresh(epdir, stage, record)
            reason = "fresh" if ready else "missing_or_changed"
        elif fixture:
            try:
                digest = _input_digest(final, stage)
                ready = _fresh(final, stage, record, digest) if isinstance(record, dict) else False
                reason = "fresh" if ready else "missing_or_changed"
            except ValueError as exc:
                ready, reason = False, str(exc)
        else:
            ready, reason = False, "missing_or_invalid_manifest_mode"
        if ready and any(not readiness.get(name, False) for name in DEPENDENCIES.get(stage, ())):
            ready, reason = False, "upstream_changed"
        if real and ready and not _linked_inputs(Path(project).resolve(), epdir, stage, record,
                                                 manifest.get("stages", {})):
            ready, reason = False, "unlinked_inputs"
        if real and ready and stage in {"cues", "voice", "sfx", "mix", "subs", "render"} and record.get("config_sha256"):
            if stage == "cues":
                from .cues_stage import cues_digest
                expected_config = cues_digest(load_config(epdir))
            elif stage == "voice":
                from .voice_plan import narration_digest
                expected_config = narration_digest(load_config(epdir))
            elif stage == "sfx":
                from .sfx_stage import sfx_digest
                expected_config = sfx_digest(load_config(epdir))
            else:
                expected_config = _stage_config_digest(epdir, stage)
            if record["config_sha256"] != expected_config:
                ready, reason = False, "config_changed"
        readiness[stage] = ready
        rows.append({"stage": stage, "ready": ready, "reason": reason})
    ready = all(row["ready"] for row in rows)
    from .cost import estimate_episode
    budget = estimate_episode(Path(project), int(epdir.name[2:]))
    return {"status": "success" if ready else "warning", "passed": True,
            "summary": "全部媒体阶段有效" if ready else "媒体阶段仍有缺项或输入已变化",
            "stages": rows, "budget": budget,
            "next_actions": [] if ready else ["运行 produce 推进下一阶段，或先补齐缺失输入"],
            "artifacts": [str(manifest_path)] if manifest_path.is_file() else []}


def _run_real_local(project: Path, epdir: Path, stage: str) -> dict:
    """Advance a local stage only when every upstream real artifact is verified."""
    if stage == "mix":
        from .adapters.ffmpeg_audio import inputs, mix as execute
    elif stage == "subs":
        from .adapters.subtitles import inputs, build as execute
    elif stage == "render":
        from .adapters.ffmpeg_render import inputs, render as execute
    else:
        raise ValueError("当前阶段没有已接入的正式本地适配器")

    production = epdir / "production"
    manifest_path = production / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("正式制作缺少媒体清单；先完成前序制作阶段")
    with (production / ".manifest.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            manifest = _manifest(manifest_path)
            if manifest.get("mode") != "real":
                raise ValueError("正式本地阶段只能接续 mode: real 的媒体清单")
            previous_stage = STAGES[STAGES.index(stage) - 1]
            prior = check(project, int(epdir.name[2:]), until=previous_stage)
            invalid = next((row for row in prior["stages"] if not row["ready"]), None)
            if invalid:
                return {"status": "warning", "passed": False,
                        "summary": f"正式 {stage} 前序阶段 {invalid['stage']} 尚未通过产物校验",
                        "errors": [invalid["reason"]], "next_actions": ["先修复或重新登记前序阶段的正式产物"],
                        "artifacts": [str(manifest_path)]}
            plan = inputs(epdir)
            targets = ([plan["output"], plan["timing_output"]] if stage == "mix"
                       else [plan["output"]])
            old = manifest.get("stages", {}).get(stage, {})
            old_outputs = old.get("outputs", []) if isinstance(old, dict) else []
            existing = [target for target in targets if target.exists()]
            owned = bool(existing) and all(
                target.is_file() and any(isinstance(row, dict)
                and row.get("path") == str(target.relative_to(epdir))
                and row.get("sha256") == sha256_file(target) for row in old_outputs)
                for target in existing)
            if existing and not owned:
                raise ValueError(f"现有 {stage} 产物未由媒体清单同哈希绑定，拒绝覆盖人工或外部制作文件")
            result = execute(epdir, replace_owned=owned)
            produced = result.get("outputs", [result.get("output")])
            manifest["stages"][stage] = {
                "status": "done", "inputs": result["inputs"],
                "outputs": [{"path": str(path.relative_to(epdir)), "sha256": sha256_file(path)}
                            for path in produced],
                "config_sha256": _stage_config_digest(epdir, stage),
                "probe": result["probe"],
                "cost_cny": 0, "charge_ids": [],
                "completed_at": datetime.now(timezone.utc).isoformat()}
            write_json(manifest_path, manifest)
            review = ([f"观看成片和字幕抽帧：{result['report_frame']}"] if stage == "render"
                      else ["人工试听混音，机器响度检查不能代替听感验收"] if stage == "mix" else [])
            artifacts = [str(manifest_path), *(str(path) for path in produced)]
            if result.get("report_frame"):
                artifacts.append(str(result["report_frame"]))
            if result.get("report"):
                artifacts.append(str(result["report"]))
            return {"status": "warning" if review else "success", "passed": True,
                    "summary": f"正式 {stage} 本地阶段完成；已校验输入与输出",
                    "executed": [stage], "probe": result["probe"],
                    "next_actions": review, "artifacts": artifacts}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run(project: Path, episode: str | int, *, test_mode: bool = False,
        until: str | None = None, from_stage: str | None = None,
        allow_paid: bool = False) -> dict:
    """Run the next stage, or a bounded range; paid real adapters are not silently invoked."""
    if until is not None and until not in STAGES:
        raise ValueError("--until 指定了未知媒体阶段")
    if from_stage is not None and from_stage not in STAGES:
        raise ValueError("--from 指定了未知媒体阶段")
    if from_stage and until and STAGES.index(from_stage) > STAGES.index(until):
        raise ValueError("--from 不能晚于 --until")
    if test_mode and allow_paid:
        raise ValueError("--test-mode 不能同时启用 --allow-paid")
    epdir, final, _ = _preflight(Path(project).resolve(), episode, test_mode)
    if not test_mode:
        project = Path(project).resolve()
        end = STAGES.index(until) if until else len(STAGES) - 1
        if from_stage:
            start = STAGES.index(from_stage)
        else:
            rows = check(project, episode, until=until)["stages"]
            first = next((row for row in rows if not row["ready"]), None)
            if first is None:
                return {"status": "success", "passed": True, "summary": "指定范围内的正式媒体阶段已有效",
                        "executed": [], "skipped": [row["stage"] for row in rows],
                        "next_actions": [], "artifacts": [str(epdir / "production/manifest.json")]}
            start = STAGES.index(first["stage"])
        if STAGES[start] == "cues":
            from .cues_stage import advance
            from .cost import estimate_episode
            result = advance(project, epdir)
            result["budget"] = estimate_episode(project, int(epdir.name[2:]))
            return result
        if STAGES[start] == "voice" and allow_paid:
            from .voice_stage import advance
            return advance(project, epdir)
        if STAGES[start] == "sfx":
            from .sfx_stage import advance
            return advance(project, epdir)
        if STAGES[start] in {"mix", "subs", "render"}:
            executed, skipped, artifacts = [], [], []
            last = None
            for index in range(start, end + 1):
                stage = STAGES[index]
                if stage not in {"mix", "subs", "render"}:
                    current = {row["stage"]: row for row in check(project, episode, until=stage)["stages"]}
                    if current[stage]["ready"]:
                        skipped.append(stage)
                        continue
                    return {"status": "warning", "passed": False,
                            "summary": f"正式制作停在需助手处理的 {stage} 阶段",
                            "executed": executed, "skipped": skipped,
                            "next_actions": [f"先完成 {stage}，再继续 produce"], "artifacts": artifacts}
                current = {row["stage"]: row for row in check(project, episode, until=stage)["stages"]}
                if not from_stage and current[stage]["ready"]:
                    skipped.append(stage)
                    continue
                last = _run_real_local(project, epdir, stage)
                if not last["passed"]:
                    last["executed"] = executed
                    last["skipped"] = skipped
                    return last
                executed.append(stage)
                artifacts.extend(last.get("artifacts", []))
                if until is None:
                    break
            if last is None:
                return {"status": "success", "passed": True, "summary": "指定范围内的正式媒体阶段已有效",
                        "executed": [], "skipped": skipped, "next_actions": [],
                        "artifacts": [str(epdir / "production/manifest.json")]}
            last["executed"], last["skipped"] = executed, skipped
            last["artifacts"] = list(dict.fromkeys(artifacts))
            return last
        from .cost import estimate_episode
        budget = estimate_episode(Path(project), int(epdir.name[2:]))
        if not budget["passed"]:
            return {"status": "warning", "passed": False,
                    "summary": "正式制作费用预检未通过；没有调用付费服务或修改媒体",
                    "errors": budget["blockers"], "budget": budget,
                    "next_actions": budget["next_actions"], "artifacts": []}
        if STAGES[start] == "voice" and not allow_paid:
            return {"status": "warning", "passed": False,
                    "summary": "正式配音已接入，但未启用付费调用；没有提交豆包任务",
                    "budget": budget,
                    "next_actions": ["核对守卫、音色和预算后，显式使用 --allow-paid 继续"],
                    "artifacts": []}
        return {"status": "error", "passed": False,
                "summary": "所需的正式生成/判断适配器尚未接入；未调用音效或生图服务，也未修改本集媒体",
                "errors": ["正式音效生成尚未接入；本地混音、字幕和渲染可接续已核验产物"],
                "budget": budget,
                "next_actions": ["预算已通过；仍须接入正式适配器才可开始付费制作"],
                "artifacts": []}
    production = epdir / "production"
    production.mkdir(parents=True, exist_ok=True)
    manifest_path = production / "manifest.json"
    lock_path = production / ".manifest.lock"
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            manifest = _manifest(manifest_path)
            if manifest and manifest.get("test_fixture_only") is not True:
                raise ValueError("已有非测试媒体清单；测试模式拒绝覆盖")
            manifest.update(schema=1, test_fixture_only=True, mode="test")
            stages = manifest.setdefault("stages", {})
            start = STAGES.index(from_stage) if from_stage else 0
            end = STAGES.index(until) if until else (len(STAGES) - 1 if from_stage else None)
            for stage in STAGES[:start]:
                digest = _input_digest(final, stage)
                if not _fresh(final, stage, stages.get(stage, {}), digest):
                    raise ValueError(f"--from {from_stage} 的前置阶段 {stage} 无效；先从该阶段恢复")
            executed, skipped = [], []
            for index in range(start, len(STAGES) if end is None else end + 1):
                stage = STAGES[index]
                digest = _input_digest(final, stage)
                if not from_stage and _fresh(final, stage, stages.get(stage, {}), digest):
                    skipped.append(stage)
                    continue
                try:
                    result = run_fixture_stage(final, stage)
                except (OSError, ValueError) as exc:
                    stages[stage] = {"status": "failed", "input_sha256": digest,
                                     "reason": str(exc), "cost_cny": 0}
                    write_json(manifest_path, manifest)
                    return {"status": "error", "passed": False, "summary": f"测试制作停在 {stage}",
                            "errors": [str(exc)], "executed": executed, "skipped": skipped,
                            "next_actions": [f"修复 {stage} 的环境或输入后重跑 produce；已完成阶段会跳过"],
                            "artifacts": [str(manifest_path)]}
                stages[stage] = {"status": "done", "input_sha256": digest,
                                 "outputs": [{"path": str(path.relative_to(epdir)), "sha256": sha256_file(path)}
                                             for path in result["outputs"]],
                                 "cost_cny": 0, "completed_at": datetime.now(timezone.utc).isoformat()}
                if result.get("probe"):
                    manifest["probe"] = result["probe"]
                write_json(manifest_path, manifest)
                executed.append(stage)
                if end is None:
                    break
            complete = True
            for stage in STAGES:
                try:
                    stage_digest = _input_digest(final, stage)
                    if not _fresh(final, stage, stages.get(stage, {}), stage_digest):
                        complete = False
                        break
                except ValueError:
                    complete = False
                    break
            return {"status": "success" if complete else "warning", "passed": True,
                    "summary": "测试媒体全阶段完成" if complete else "测试媒体已推进到下一阶段",
                    "executed": executed, "skipped": skipped, "probe": manifest.get("probe") if complete else None,
                    "next_actions": [] if complete else ["继续运行 produce 执行下一未完成阶段"],
                    "artifacts": [str(manifest_path), str(production / "final.mp4")]
                    if complete else [str(manifest_path)]}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
