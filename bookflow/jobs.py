"""File-backed episode work queue; no worker may silently edit shared project tables."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .common import full_season_review, latest_draft, load_yaml, parse_draft, sha256_file, write_yaml

STAGES = ("draft", "audio", "visual")
LEASE = timedelta(hours=2)
CARD_ID = re.compile(r"(draft|audio|visual)-ep(0*[1-9][0-9]*)\Z")
PROMPT = """# 单张任务卡执行提示词

只读取指定的 `jobs/<任务ID>.yaml`；先运行 `next <项目> --read-only --json` 核对当前阶段。
按任务卡所列阶段的 skill 和 `do` 执行，遵守只读文件清单及人工确认边界。
完成后运行 `jobs done <项目> <任务ID> --session <本会话ID>`；若需要用户决定，
把问题写入卡片 `needs_you` 并停下，不代填确认，也不修改共享音色表或人物表。
"""


def _project(project: Path) -> Path:
    project = Path(project).resolve()
    if not (project / "project.yaml").is_file():
        raise ValueError("jobs 需要有效书目项目路径")
    return project


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _locked(project: Path):
    directory = project / "jobs"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".queue.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _episodes(project: Path) -> list[int]:
    data = load_yaml(project / "plan/episodes.yaml", {}) or {}
    rows = data if isinstance(data, list) else data.get("episodes", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        raise ValueError("plan/episodes.yaml 的 episodes 必须是列表")
    numbers = [row.get("ep") for row in rows if isinstance(row, dict)]
    if not numbers or any(type(number) is not int or number < 1 for number in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("plan/episodes.yaml 缺少有效且不重复的集号")
    return sorted(numbers)


def _inputs(project: Path, stage: str, ep: int) -> list[Path]:
    episode = project / "episodes" / f"ep{ep:02d}"
    if stage == "draft":
        rows = [project / "project.yaml", project / "source/current.json",
                project / "plan/episodes.yaml", project / "analysis/characters.yaml",
                project / "analysis/book_brief.md"]
        optional = [project / "feedback/rules.yaml", project / "writing_direction.md"]
        recap_path = project / "episodes/recap.yaml"
        if not full_season_review(project) and not (recap_path.exists() or recap_path.is_symlink()):
            optional.append(project / "series_ledger.yaml")
        return rows + [path for path in optional if path.is_file()]
    if stage == "audio":
        return [episode / "final.md", project / "production/voice_cast.yaml",
                project / "project.yaml"]
    return [episode / "final.md", episode / "production/final_mix.wav",
            episode / "production/subtitles.srt", project / "analysis/characters.yaml"]


def _snapshot(project: Path, stage: str, ep: int) -> tuple[dict, str, list[str]]:
    inputs, missing = {}, []
    for path in _inputs(project, stage, ep):
        relative = str(path.relative_to(project))
        if path.is_file():
            row = {"path": relative, "sha256": sha256_file(path)}
            if path.name == "voice_cast.yaml":
                cast = load_yaml(path, {}) or {}
                row["version"] = cast.get("version") if isinstance(cast, dict) else None
            inputs[path.stem if path.name != "final.md" else "final"] = row
        else:
            missing.append(relative)
    recap_path = project / "episodes/recap.yaml"
    if stage == "draft" and ep > 1 and (recap_path.exists() or recap_path.is_symlink()):
        from .recap import context as recap_context, inspect as recap_inspect
        fingerprints = recap_inspect(project)["episodes"]
        data = load_yaml(recap_path, {}) or {}
        prior = sorted((row for row in data["episodes"] if isinstance(row, dict)
                        and type(row.get("ep")) is int and row["ep"] < ep),
                       key=lambda row: row["ep"])
        checked = recap_context(project, ep)
        payload = {"prior_rows": prior,
                   "current_fingerprints": [row for row in fingerprints if row["ep"] < ep],
                   "context_passed": checked["passed"], "context_errors": checked["errors"]}
        scoped_digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                 default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
        inputs["recap_context"] = {"path": "episodes/recap.yaml", "sha256": scoped_digest,
                                   "scope": f"before_ep{ep:02d}",
                                   "status": "reviewed_prior" if checked["passed"] else "planned_pending"}
    digest = hashlib.sha256(json.dumps({"inputs": inputs, "missing": missing},
                                       ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return inputs, digest, missing


def _card(project: Path, stage: str, ep: int) -> dict:
    inputs, digest, missing = _snapshot(project, stage, ep)
    task = {
        "draft": f"写第 {ep} 集初稿；以前序实际工作稿校对连续性，缺失依赖标待补",
        "audio": f"制作第 {ep} 集声音与字幕（produce ep{ep:02d} --until subs）",
        "visual": f"制作第 {ep} 集分镜、画面和成片（produce ep{ep:02d} --until render）",
    }[stage]
    if stage == "draft" and inputs.get("recap_context", {}).get("status") == "planned_pending":
        task = f"写第 {ep} 集初稿；前集前情未全部复核，只按分集计划的信息边界暂写，待全季汇总后补齐实际依赖"
    acceptance = {
        "draft": [f"lint episodes/ep{ep:02d}/draft_vN.md", f"quotes episodes/ep{ep:02d}/draft_vN.md"],
        "audio": [f"produce check ep{ep:02d} --until subs"],
        "visual": [f"produce check ep{ep:02d} --until render"],
    }[stage]
    read_only = ["production/voice_cast.yaml", "analysis/characters.yaml", "project.yaml"]
    if stage == "draft":
        read_only.extend(["source/current.json", "plan/episodes.yaml", "analysis/book_brief.md",
                          "episodes/working_continuity.yaml", "episodes/recap.yaml",
                          "feedback/rules.yaml", "status.yaml"])
    return {"id": f"{stage}-ep{ep:02d}", "stage": stage, "episode": ep,
            "status": "needs_you" if missing else "todo", "claimed_by": None,
            "inputs": inputs, "input_sha256": digest, "do": task,
            "read_only": read_only,
            "accept": acceptance, "needs_you": [f"缺少输入：{name}" for name in missing],
            "failure_reason": None, "updated_at": _now().isoformat()}


def _cards(directory: Path, stage: str | None = None) -> list[tuple[Path, dict]]:
    result = []
    for path in directory.glob("*-ep*.yaml"):
        match = CARD_ID.fullmatch(path.stem)
        if not match or stage and match.group(1) != stage:
            continue
        card = load_yaml(path, {}) or {}
        if not isinstance(card, dict) or card.get("id") != path.stem:
            raise ValueError(f"任务卡格式无效：{path.name}；先备份修复，不覆盖")
        result.append((path, card))
    return sorted(result, key=lambda item: (item[1].get("episode", 0), item[1].get("stage", "")))


def _report(summary: str, *, status: str = "success", cards: list[dict] | None = None,
            next_actions: list[str] | None = None, artifacts: list[str] | None = None, **extra) -> dict:
    return {"status": status, "passed": status != "error", "summary": summary,
            "cards": cards or [], "next_actions": next_actions or [], "artifacts": artifacts or [], **extra}


def plan(project: Path, stage: str) -> dict:
    project = _project(project)
    if stage not in STAGES:
        raise ValueError("jobs plan --stage 必须是 draft、audio 或 visual")
    from .versioning import touch
    touch(project)
    numbers = _episodes(project)
    changed, preserved = [], []
    with _locked(project) as directory:
        for ep in numbers:
            card = _card(project, stage, ep)
            path = directory / f"{card['id']}.yaml"
            previous = load_yaml(path, None) if path.is_file() else None
            if previous is not None:
                if not isinstance(previous, dict) or previous.get("id") != card["id"]:
                    raise ValueError(f"任务卡格式无效：{path.name}；先备份修复，不覆盖")
                if previous.get("input_sha256") == card["input_sha256"]:
                    if previous.get("status") != "done":
                        preserved.append(card["id"])
                        continue
                    try:
                        accepted, _ = _accept(project, previous)
                    except (OSError, ValueError, KeyError, TypeError):
                        accepted = False
                    if accepted:
                        preserved.append(card["id"])
                        continue
                history = directory / "history" / f"{card['id']}-{_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}.yaml"
                write_yaml(history, previous)
            write_yaml(path, card)
            changed.append(card["id"])
        prompt_path = directory / "_prompt.md"
        legacy_prompt = PROMPT.replace("next <项目> --read-only --json", "next <项目>")
        if not prompt_path.exists() or prompt_path.read_text(encoding="utf-8") == legacy_prompt:
            from .common import atomic_write
            atomic_write(prompt_path, PROMPT)
    return _report(f"{stage} 任务卡：新增或重排 {len(changed)}，保留 {len(preserved)}",
                   cards=[{"id": item, "status": "todo"} for item in changed],
                   next_actions=["运行 jobs claim 领取下一张任务卡"],
                   artifacts=[str(project / "jobs"), str(project / "jobs/_prompt.md")],
                   changed=changed, preserved=preserved)


def claim(project: Path, stage: str, session: str) -> dict:
    project = _project(project)
    if stage not in STAGES or not session or len(session) > 128:
        raise ValueError("jobs claim 需要有效 --stage 和非空 --session")
    with _locked(project) as directory:
        rows = _cards(directory, stage)
        for path, card in rows:
            if card.get("status") != "doing":
                continue
            owner = card.get("claimed_by") if isinstance(card.get("claimed_by"), dict) else {}
            try:
                claimed_at = datetime.fromisoformat(owner["at"])
                expired = _now() - claimed_at.astimezone(timezone.utc) > LEASE
            except (KeyError, TypeError, ValueError):
                expired = True
            if expired:
                card.update(status="todo", claimed_by=None, updated_at=_now().isoformat())
                write_yaml(path, card)
        for path, card in rows:
            if card.get("status") == "doing" and isinstance(card.get("claimed_by"), dict) and card["claimed_by"].get("session") == session:
                return _report("恢复本会话未交回的任务卡", cards=[card], artifacts=[str(path)])
        for path, card in rows:
            if card.get("status") == "todo":
                card.update(status="doing", claimed_by={"session": session, "at": _now().isoformat()},
                            updated_at=_now().isoformat())
                write_yaml(path, card)
                return _report("已领取任务卡", cards=[card], artifacts=[str(path)])
        pending = [row for _, row in rows if row.get("status") in ("failed", "needs_you", "doing")]
        return _report("没有可领取的任务卡", status="warning", cards=pending,
                       next_actions=["先处理失败、待用户决定或其他会话持有的任务卡"] if pending else [])


def _accept(project: Path, card: dict) -> tuple[bool, str]:
    stage, ep = card["stage"], card["episode"]
    _, digest, missing = _snapshot(project, stage, ep)
    if missing or digest != card.get("input_sha256"):
        return False, "输入已变化或缺失；先重跑 jobs plan，再检查下游产物"
    if card.get("needs_you"):
        return False, "任务卡仍有 needs_you；先由主会话汇总并解决"
    from .approvals import confirmation_state
    if stage == "draft" and confirmation_state(project, "plan")["state"] != "passed":
        return False, "方案确认无效；不能交回全季初稿任务"
    if stage == "draft":
        from .guard import check as guard_check
        guard = guard_check(project, "draft", ep)
        if not guard["passed"]:
            return False, "写稿守卫未通过：" + "；".join(guard["errors"])
    if stage in ("audio", "visual") and confirmation_state(project, "script", ep)["state"] != "passed":
        return False, "本集文案确认无效；不能交回音画任务"
    if stage == "draft":
        from .quality import lint, verify_quotes
        draft = latest_draft(project / "episodes" / f"ep{ep:02d}")
        if not draft:
            return False, "缺少本集初稿"
        parsed = parse_draft(draft.read_text(encoding="utf-8"))
        if parsed["meta"].get("episode") != ep:
            return False, "稿件元数据集号与任务卡不一致"
        if card.get("status") == "done" and card.get("output_draft_sha256") and (
                card.get("output_draft") != str(draft.relative_to(project))
                or card["output_draft_sha256"] != sha256_file(draft)):
            return False, "任务卡完成后稿件版本已变化；须重新验收"
        quality, quotes = lint(draft), verify_quotes(draft)
        if not quality["passed"] or not quotes["passed"]:
            return False, "稿件 lint 或 quotes 未通过"
        return True, "初稿及来源检查通过"
    from .produce import check as produce_check
    media = produce_check(project, ep, until="subs" if stage == "audio" else "render")
    if not all(row["ready"] for row in media["stages"]):
        bad = next(row for row in media["stages"] if not row["ready"])
        return False, f"{bad['stage']} 验收失败：{bad['reason']}"
    return True, "媒体产物及输入哈希检查通过"


def done(project: Path, task_id: str, session: str) -> dict:
    project = _project(project)
    if not CARD_ID.fullmatch(task_id) or not session:
        raise ValueError("jobs done 需要合法任务ID和 --session")
    with _locked(project) as directory:
        path = directory / f"{task_id}.yaml"
        card = load_yaml(path, {}) or {}
        if not isinstance(card, dict) or card.get("id") != task_id:
            raise ValueError("任务卡不存在或格式无效；先运行 jobs plan")
        if card.get("status") == "done":
            return _report("任务卡已完成，无须重复交回", cards=[card], artifacts=[str(path)])
        if card.get("status") != "doing" or not isinstance(card.get("claimed_by"), dict) or card["claimed_by"].get("session") != session:
            raise ValueError("只能由当前领用会话交回 doing 任务卡；先运行 jobs claim")
        try:
            passed, reason = _accept(project, card)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            passed, reason = False, str(exc)
        card.update(status="done" if passed else "needs_you" if card.get("needs_you") else "failed",
                    failure_reason=None if passed else reason, claimed_by=None, updated_at=_now().isoformat())
        if passed and card["stage"] == "draft":
            draft = latest_draft(project / "episodes" / f"ep{card['episode']:02d}")
            card["output_draft"] = str(draft.relative_to(project))
            card["output_draft_sha256"] = sha256_file(draft)
        write_yaml(path, card)
    return _report(reason, status="success" if passed else "warning", cards=[card],
                   next_actions=[] if passed else ["核对失败原因；输入变化时重跑 jobs plan，需用户决定时停下"],
                   artifacts=[str(path)])


def summary(project: Path) -> dict:
    project = _project(project)
    directory = project / "jobs"
    rows = _cards(directory) if directory.is_dir() else []
    counts = {stage: {status: 0 for status in ("todo", "doing", "done", "failed", "needs_you")}
              for stage in STAGES}
    requests = []
    for _, card in rows:
        stage, status = card.get("stage"), card.get("status")
        if stage in counts and status in counts[stage]:
            counts[stage][status] += 1
        if status == "needs_you":
            requests.extend(f"{card['id']}：{item}" for item in card.get("needs_you", []) or [card.get("failure_reason") or "待用户决定"])
    return {"counts": counts, "needs_you": requests, "cards": [row for _, row in rows]}


def run(project: Path, stage: str, *, test_mode: bool = False) -> dict:
    project = _project(project)
    if stage == "draft":
        if test_mode:
            raise ValueError("初稿任务不能用静音媒体测试模式冒充写稿；请用独立测试夹具验证")
        return _run_draft(project)
    if stage != "audio":
        raise ValueError("jobs run 当前支持 --stage draft 或 audio；visual 可手动 claim/done")
    if not test_mode:
        return _run_real(project, stage)
    from .produce import _preflight, run as produce_run
    numbers = _episodes(project)
    for ep in numbers:
        _preflight(project, ep, True)
    plan(project, stage)
    session = "bookflow-test-runner"
    state = summary(project)
    blocked = [card for card in state["cards"] if card["stage"] == stage and (
        card["status"] in ("failed", "needs_you") or
        (card["status"] == "doing" and (card.get("claimed_by") or {}).get("session") != session))]
    if blocked:
        return _report(f"{blocked[0]['id']} 需要先处理，自动队列已停止", status="warning", cards=blocked,
                       next_actions=["处理失败原因或 needs_you 后重跑 jobs plan / jobs run"],
                       artifacts=[str(project / "jobs")], completed=[])
    completed = []
    while True:
        got = claim(project, stage, session)
        if not got["cards"] or got["cards"][0].get("status") != "doing":
            break
        card = got["cards"][0]
        result = produce_run(project, card["episode"], test_mode=True, until="subs")
        if not result["passed"]:
            with _locked(project) as directory:
                path = directory / f"{card['id']}.yaml"
                current = load_yaml(path)
                current.update(status="failed", failure_reason=result["summary"], claimed_by=None,
                               updated_at=_now().isoformat())
                write_yaml(path, current)
            return _report(f"停在 {card['id']}：{result['summary']}", status="error",
                           cards=[current], next_actions=result.get("next_actions", []),
                           artifacts=result.get("artifacts", []), completed=completed)
        result = done(project, card["id"], session)
        if result["cards"][0]["status"] != "done":
            return _report(f"停在 {card['id']}：{result['summary']}", status="warning",
                           cards=result["cards"], next_actions=result["next_actions"],
                           artifacts=result["artifacts"], completed=completed)
        completed.append(card["id"])
    state = summary(project)
    pending = [card for card in state["cards"] if card["stage"] == stage and card["status"] != "done"]
    return _report(f"音频任务完成 {len(completed)} 张；剩余 {len(pending)} 张",
                   status="warning" if pending else "success", cards=pending,
                   next_actions=["处理未完成任务卡后重跑 jobs run"] if pending else [],
                   artifacts=[str(project / "jobs")], completed=completed)


def _shared_hashes(project: Path, card_path: Path, card: dict) -> dict[str, str | None]:
    names = card.get("read_only")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("任务卡的共享只读文件列表无效")
    inputs = card.get("inputs")
    if not isinstance(inputs, dict) or not all(isinstance(row, dict) and isinstance(row.get("path"), str)
                                                for row in inputs.values()):
        raise ValueError("任务卡的输入快照无效")
    result = {}
    for name in [*names, *(row["path"] for row in inputs.values()), str(card_path.relative_to(project))]:
        relative = Path(name)
        path = project / relative
        if (relative.is_absolute() or ".." in relative.parts or path.is_symlink()
                or not path.resolve().is_relative_to(project)):
            raise ValueError("任务卡的共享只读文件路径无效")
        result[name] = sha256_file(path) if path.is_file() else None
    return result


def _update_real_card(project: Path, task_id: str, session: str, *, status: str | None = None,
                      reason: str | None = None) -> dict:
    with _locked(project) as directory:
        path = directory / f"{task_id}.yaml"
        card = load_yaml(path, {}) or {}
        owner = card.get("claimed_by") if isinstance(card, dict) else None
        if (not isinstance(card, dict) or card.get("status") != "doing"
                or not isinstance(owner, dict) or owner.get("session") != session):
            raise ValueError("任务卡在自动执行期间已由其他会话改动；停止而不覆盖")
        if status is None:
            owner["at"] = _now().isoformat()
        else:
            card.update(status=status, claimed_by=None, failure_reason=reason)
            if status == "needs_you" and reason:
                card["needs_you"] = list(dict.fromkeys([*card.get("needs_you", []), reason]))
        card["updated_at"] = _now().isoformat()
        write_yaml(path, card)
    return card


@contextmanager
def _runner_lock(project: Path):
    directory = project / "jobs"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".runner.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _draft_versions(epdir: Path) -> dict[str, str]:
    result = {}
    for path in epdir.glob("draft_v*.md"):
        if not re.fullmatch(r"draft_v[1-9][0-9]*\.md", path.name):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("现有初稿不是普通文件；停止以保护历史版本")
        result[path.name] = sha256_file(path)
    return result


def _draft_baseline(project: Path, card: dict, session: str, *, fresh: bool) -> dict:
    with _locked(project) as directory:
        path = directory / f"{card['id']}.yaml"
        current = load_yaml(path, {}) or {}
        owner = current.get("claimed_by") if isinstance(current, dict) else None
        if (not isinstance(owner, dict) or current.get("status") != "doing"
                or owner.get("session") != session):
            raise ValueError("初稿任务卡已被其他会话改动")
        baseline = current.get("baseline_drafts")
        if baseline is None:
            if not fresh:
                raise ValueError("恢复中的初稿卡缺少旧稿快照；须人工核对，不能盲目重写")
            baseline = _draft_versions(project / "episodes" / f"ep{card['episode']:02d}")
            current["baseline_drafts"] = baseline
            current["updated_at"] = _now().isoformat()
            write_yaml(path, current)
        if (not isinstance(baseline, dict) or any(not isinstance(name, str)
                or not isinstance(digest, str) for name, digest in baseline.items())):
            raise ValueError("初稿旧版本快照无效；须人工核对")
        return current


def _new_draft(epdir: Path, baseline: dict[str, str]) -> Path | None:
    current = _draft_versions(epdir)
    if any(current.get(name) != digest for name, digest in baseline.items()):
        raise ValueError("Codex 会话修改或删除了已有初稿；停止并人工核对历史版本")
    created = [name for name in current if name not in baseline]
    if len(created) > 1:
        raise ValueError("同一张任务卡出现多份新初稿；须人工选择版本")
    if not created:
        return None
    draft = latest_draft(epdir)
    if draft is None or draft.name != created[0]:
        raise ValueError("新初稿不是本集最新版本；须人工核对")
    return draft


def _draft_edit_copy(epdir: Path, draft: Path) -> Path:
    from .feedback import create_copy

    version = re.fullmatch(r"draft_v([1-9][0-9]*)\.md", draft.name)
    if not version:
        raise ValueError("初稿版本名无效；不能生成编辑包")
    output = epdir / "human_edit" / f"v{version.group(1)}"
    if output.is_symlink() or not output.resolve().is_relative_to(epdir.resolve()):
        raise ValueError("编辑包路径越界；拒绝写入")
    if output.exists():
        baseline = load_yaml(output / "baseline.json", {}) or {}
        original = output / "original.md"
        if (not isinstance(baseline, dict) or baseline.get("draft_sha256") != sha256_file(draft)
                or not original.is_file() or sha256_file(original) != sha256_file(draft)):
            raise ValueError("现有编辑包与当前稿件不一致；拒绝覆盖人工编辑")
        return output
    create_copy(draft, output, format="md")
    return output


def _run_draft(project: Path) -> dict:
    from .job_runner import invoke_draft
    from .produce import _test_fixture

    if not full_season_review(project):
        return _report("自动初稿队列要求 drafting.mode: full_season_review", status="error", completed=[])
    if _test_fixture(project):
        return _report("测试夹具不能启动真实 Codex 初稿执行器", status="error", completed=[])
    if not shutil.which("codex"):
        return _report("未找到 codex CLI；没有领取任务或修改初稿", status="error", completed=[])
    try:
        with _runner_lock(project):
            return _run_draft_locked(project, invoke_draft)
    except BlockingIOError:
        return _report("已有任务队列执行器在运行；没有重复领取初稿卡", status="warning", completed=[])


def _run_draft_locked(project: Path, invoke_draft) -> dict:
    from .guard import check as guard_check

    plan(project, "draft")
    session = "bookflow-codex-draft-runner"
    state = summary(project)
    blocked = [card for card in state["cards"] if card["stage"] == "draft" and (
        card["status"] in {"failed", "needs_you"} or
        card["status"] == "doing" and
        (card.get("claimed_by") or {}).get("session") != session)]
    if blocked:
        return _report(f"{blocked[0]['id']} 尚未可领取，初稿队列已停止", status="warning",
                       cards=blocked, completed=[], next_actions=["先核对任务卡失败或待决定事项"])
    completed = []
    while True:
        got = claim(project, "draft", session)
        if not got["cards"] or got["cards"][0].get("status") != "doing":
            break
        card = got["cards"][0]
        ep = card["episode"]
        epdir = project / "episodes" / f"ep{ep:02d}"
        card_path = project / "jobs" / f"{card['id']}.yaml"
        try:
            guard = guard_check(project, "draft", ep)
            if not guard["passed"]:
                reason = "写稿守卫未通过：" + "；".join(guard["errors"])
                current = _update_real_card(project, card["id"], session, status="needs_you", reason=reason)
                return _report(reason, status="warning", cards=[current], completed=completed)
            if _snapshot(project, "draft", ep)[1] != card["input_sha256"]:
                raise ValueError("初稿卡输入在领取后变化；须重排任务")
            if epdir.is_symlink() or epdir.resolve() != epdir:
                raise ValueError("本集目录不是书目下的普通目录；拒绝启动初稿会话")
            epdir.mkdir(parents=True, exist_ok=True)
            card = _draft_baseline(project, card, session, fresh=got["summary"] == "已领取任务卡")
            draft = _new_draft(epdir, card["baseline_drafts"])
            if draft is None:
                _update_real_card(project, card["id"], session)
                before = _shared_hashes(project, card_path, card)
                execution = invoke_draft(project, ep, card_path)
                if _shared_hashes(project, card_path, card) != before:
                    raise ValueError("Codex 会话改动了任务卡或共享只读文件；停止并人工核对")
                if not execution["passed"]:
                    raise ValueError(execution["reason"])
                draft = _new_draft(epdir, card["baseline_drafts"])
            if draft is None:
                raise ValueError("Codex 会话没有新增本集初稿版本")
            accepted, reason = _accept(project, card)
            if not accepted:
                raise ValueError(reason)
            _draft_edit_copy(epdir, draft)
            result = done(project, card["id"], session)
            if result["cards"][0]["status"] != "done":
                return _report(f"停在 {card['id']}：{result['summary']}", status="warning",
                               cards=result["cards"], completed=completed,
                               next_actions=result["next_actions"])
            completed.append(card["id"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
            try:
                current = _update_real_card(project, card["id"], session,
                                            status="failed", reason=reason)
            except (OSError, ValueError, KeyError, TypeError):
                return _report(f"停在 {card['id']}：{reason}；任务卡也被并发改动，未覆盖现有文件",
                               status="error", completed=completed,
                               next_actions=["人工核对任务卡及稿件历史；不要盲目重试"])
            return _report(f"停在 {card['id']}：{reason}", status="error", cards=[current],
                           completed=completed, next_actions=["核对失败原因和旧稿版本后再决定是否重排任务"])
        try:
            plan(project, "draft")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _report(f"已完成 {card['id']}，后续初稿卡重排失败：{exc}", status="warning",
                           completed=completed, next_actions=["核对前情表和任务卡后重跑 jobs plan"])
    state = summary(project)
    pending = [card for card in state["cards"] if card["stage"] == "draft" and card["status"] != "done"]
    return _report(f"初稿任务完成 {len(completed)} 张；剩余 {len(pending)} 张；实际连续性与语义衔接仍待复核",
                   status="warning", cards=pending, completed=completed,
                   next_actions=["按全部实际初稿补录工作连续性、复核揭示与前情，再统一交付编辑包"],
                   artifacts=[str(project / "jobs")])


def _voice_until_ready(project: Path, ep: int, *, timeout_sec: int = 5400) -> dict:
    """Only the parent process may call the paid adapter; never repeat unknown submits."""
    from .produce import check as produce_check
    from .voice_stage import advance

    epdir = project / "episodes" / f"ep{ep:02d}"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if produce_check(project, ep, until="voice")["stages"][-1]["ready"]:
            return {"passed": True, "waiting": False}
        result = advance(project, epdir)
        if result.get("passed"):
            if produce_check(project, ep, until="voice")["stages"][-1]["ready"]:
                return {"passed": True, "waiting": False}
            return {"passed": False, "waiting": False,
                    "reason": "配音适配器报告完成，但正式 voice 清单未通过核验"}
        if result.get("progress") in {"submitted", "running"}:
            time.sleep(5)
            continue
        if result.get("progress") == "part_saved":
            continue
        return {"passed": False, "waiting": False,
                "needs_you": result.get("progress") in {
                    "guard_blocked", "budget_blocked", "request_unsettled", "provider_failed"},
                "reason": result.get("summary", "正式配音未完成")}
    return {"passed": False, "waiting": True,
            "reason": "原豆包任务仍待查询或配音未完成；保留任务卡和请求日志，稍后继续"}


def _run_real(project: Path, stage: str) -> dict:
    """One fresh, bounded Codex session per decision phase; parent verifies files."""
    from .approvals import confirmation_state
    from .cost import estimate_episode
    from .guard import check as guard_check
    from .job_runner import invoke_audio, invoke_cues
    from .produce import _test_fixture, check as produce_check

    if _test_fixture(project):
        return _report("测试夹具不能用真实 Codex 执行器；请显式使用 --test-mode",
                       status="error", completed=[])
    if not shutil.which("codex"):
        return _report("未找到 codex CLI；没有领取任务或修改媒体",
                       status="error", completed=[])
    try:
        with _runner_lock(project):
            return _run_real_locked(project, stage, confirmation_state, estimate_episode,
                                    guard_check, invoke_audio, invoke_cues, produce_check)
    except BlockingIOError:
        return _report("已有真实任务队列执行器在运行；没有重复领取或提交付费任务",
                       status="warning", completed=[])


def _run_real_locked(project: Path, stage: str, confirmation_state, estimate_episode,
                     guard_check, invoke_audio, invoke_cues, produce_check) -> dict:
    plan(project, stage)
    state = summary(project)
    session = "bookflow-codex-audio-runner"
    blocked = [card for card in state["cards"] if card["stage"] == stage and
               (card["status"] in {"failed", "needs_you"} or
                card["status"] == "doing" and
                (card.get("claimed_by") or {}).get("session") != session)]
    if blocked:
        return _report(f"{blocked[0]['id']} 尚未可领取，自动队列已停止", status="warning",
                       cards=blocked, completed=[], next_actions=["先核对任务卡失败或待决定事项"])
    completed = []
    while True:
        got = claim(project, stage, session)
        if not got["cards"] or got["cards"][0].get("status") != "doing":
            break
        card = got["cards"][0]
        ep = card["episode"]
        card_path = project / "jobs" / f"{card['id']}.yaml"
        try:
            if confirmation_state(project, "script", ep)["state"] != "passed":
                reason = "本集文案确认缺失或失效；不启动 Codex 或付费生成"
                current = _update_real_card(project, card["id"], session, status="needs_you", reason=reason)
                return _report(reason, status="warning", cards=[current], completed=completed)
            cues = produce_check(project, ep, until="cues")["stages"][0]
            if not cues["ready"]:
                guard = guard_check(project, "sound-plan", ep)
                if not guard["passed"]:
                    reason = "正式声音规划守卫未通过：" + "；".join(guard["errors"])
                    current = _update_real_card(project, card["id"], session, status="needs_you", reason=reason)
                    return _report(reason, status="warning", cards=[current], completed=completed)
                before = _shared_hashes(project, card_path, card)
                execution = invoke_cues(project, ep, card_path)
                if _shared_hashes(project, card_path, card) != before:
                    raise ValueError("Codex 会话改动了任务卡或共享只读文件；停止并人工核对")
                if not execution["passed"]:
                    raise ValueError(execution["reason"])
                cues = produce_check(project, ep, until="cues")["stages"][0]
                if not cues["ready"]:
                    raise ValueError(f"Codex 会话结束但 cues 未通过正式清单核验：{cues['reason']}")
                if confirmation_state(project, "script", ep)["state"] != "passed":
                    raise ValueError("cue 会话结束后文案确认已失效；停止后续付费阶段")
            budget = estimate_episode(project, ep)
            voice = produce_check(project, ep, until="voice")["stages"][-1]
            if not budget["passed"] and voice["ready"]:
                reason = "费用预检未通过：" + "；".join(budget["blockers"][:3])
                current = _update_real_card(project, card["id"], session, status="needs_you", reason=reason)
                return _report(reason, status="warning", cards=[current], completed=completed,
                               budget=budget)
            media_guard = guard_check(project, "media-generate", ep)
            if not media_guard["passed"]:
                reason = "正式媒体守卫未通过：" + "；".join(media_guard["errors"])
                current = _update_real_card(project, card["id"], session, status="needs_you", reason=reason)
                return _report(reason, status="warning", cards=[current], completed=completed,
                               budget=budget)
            if not voice["ready"]:
                from .voice_jobs import read as read_voice_jobs
                jobs = read_voice_jobs(project / "episodes" / f"ep{ep:02d}")["jobs"]
                queryable = any(row.get("status") in {"running", "provider_done"} and row.get("task_id")
                                for row in jobs)
                if not budget["passed"] and not queryable:
                    reason = "费用预检未通过：" + "；".join(budget["blockers"][:3])
                    current = _update_real_card(project, card["id"], session, status="needs_you", reason=reason)
                    return _report(reason, status="warning", cards=[current], completed=completed,
                                   budget=budget)
                _update_real_card(project, card["id"], session)
                voice_result = _voice_until_ready(project, ep)
                if voice_result["waiting"]:
                    current = _update_real_card(project, card["id"], session)
                    return _report(voice_result["reason"], status="warning", cards=[current],
                                   completed=completed, next_actions=["稍后重跑 jobs run；只查询原 task_id"])
                if not voice_result["passed"]:
                    if voice_result.get("needs_you"):
                        current = _update_real_card(project, card["id"], session,
                                                    status="needs_you", reason=voice_result["reason"])
                        return _report(voice_result["reason"], status="warning", cards=[current],
                                       completed=completed)
                    raise ValueError(voice_result["reason"])
                budget = estimate_episode(project, ep)
                if not budget["passed"]:
                    reason = "配音后费用预检未通过：" + "；".join(budget["blockers"][:3])
                    current = _update_real_card(project, card["id"], session, status="needs_you", reason=reason)
                    return _report(reason, status="warning", cards=[current], completed=completed,
                                   budget=budget)
            _update_real_card(project, card["id"], session)
            before = _shared_hashes(project, card_path, card)
            execution = invoke_audio(project, ep, card_path)
            if _shared_hashes(project, card_path, card) != before:
                raise ValueError("Codex 会话改动了任务卡或共享只读文件；停止并人工核对")
            if not execution["passed"]:
                raise ValueError(execution["reason"])
            result = done(project, card["id"], session)
            if result["cards"][0]["status"] != "done":
                return _report(f"停在 {card['id']}：{result['summary']}", status="warning",
                               cards=result["cards"], completed=completed,
                               next_actions=result["next_actions"])
            completed.append(card["id"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
            try:
                current = _update_real_card(project, card["id"], session,
                                            status="failed", reason=reason)
            except (OSError, ValueError, KeyError, TypeError):
                return _report(f"停在 {card['id']}：{reason}；任务卡也已被并发改动，未覆盖现有文件",
                               status="error", completed=completed,
                               next_actions=["人工核对任务卡和媒体文件；不要自动重试"])
            return _report(f"停在 {card['id']}：{reason}", status="error", cards=[current],
                           completed=completed, next_actions=["核对失败原因和现有媒体清单后再决定是否重排任务"])
    state = summary(project)
    pending = [card for card in state["cards"] if card["stage"] == stage and card["status"] != "done"]
    return _report(f"真实音频任务完成 {len(completed)} 张；剩余 {len(pending)} 张",
                   status="warning" if pending else "success", cards=pending, completed=completed,
                   next_actions=["处理未完成任务卡后重跑 jobs run"] if pending else [])
