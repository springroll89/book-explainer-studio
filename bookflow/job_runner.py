"""A bounded, fresh Codex session for one judgement-only queue phase."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import ROOT

CODEX_TIMEOUT_SEC = 5400
_ENV_KEYS = ("PATH", "HOME", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


def _run_cli(command: list[str], prompt: str, stream, *, cwd: Path,
             environment: dict[str, str], timeout_sec: int) -> int:
    # A timed-out Codex may have spawned media tools; terminate the whole group.
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stream,
                               stderr=subprocess.DEVNULL, text=True, cwd=cwd,
                               env=environment, start_new_session=True)
    try:
        process.communicate(input=prompt, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return process.returncode


def _paths(project: Path, episode: int, card_path: Path, phase: str) -> tuple[Path, Path, Path]:
    project = Path(project).resolve()
    if type(episode) is not int or episode < 1 or not (project / "project.yaml").is_file():
        raise ValueError("执行器需要有效书目项目与正整数集号")
    epdir = project / "episodes" / f"ep{episode:02d}"
    stage = "draft" if phase == "draft" else "audio"
    expected = project / "jobs" / f"{stage}-ep{episode:02d}.yaml"
    if (not epdir.is_dir() or epdir.resolve() != epdir or expected.resolve() != expected
            or card_path.is_symlink() or Path(card_path).resolve() != expected):
        raise ValueError("执行器的本集目录或任务卡不属于指定书目")
    if not expected.is_file():
        raise ValueError("执行器找不到当前任务卡")
    return project, epdir, expected


def _cue_prompt(project: Path, episode: int, card_path: Path) -> str:
    python = shlex.quote(sys.executable)
    quoted_project = shlex.quote(str(project))
    return ("你在执行书籍精讲工作室的一张音频任务卡，本次只处理 cues 判断阶段。\n"
            f"只读取任务卡 {card_path}；先执行 `{python} -m bookflow next {quoted_project} --read-only --json` 核对当前阶段，"
            "再按任务卡和项目规则检查前置条件。\n"
            f"本次只允许修改 {project / 'episodes' / f'ep{episode:02d}'} 内的文件；"
            "音色表、人物表、其他集、任务卡和批准记录只读。不要运行 approve，"
            "不要启动 TTS、音效、生图或其他付费服务。\n"
            "有现成人工 cue 表时不得覆盖。需要用户决定、守卫不通过或证据不足时说明原因并停止。"
            f"完成后执行 `{python} -m bookflow produce {quoted_project} ep{episode:02d} --until cues`；"
            "父执行器会核验产物并交回任务卡。")


def _audio_prompt(project: Path, episode: int, card_path: Path) -> str:
    python = shlex.quote(sys.executable)
    quoted_project = shlex.quote(str(project))
    return ("你在执行书籍精讲工作室的一张音频任务卡，本次只接续已校验的 cues 和 voice，"
            "处理不付费的 sfx 素材绑定、mix、subs；不处理其他集。\n"
            f"只读取任务卡 {card_path}；先执行 `{python} -m bookflow next {quoted_project} --read-only --json` 核对阶段。"
            f"只允许修改 {project / 'episodes' / f'ep{episode:02d}'} 内的文件；"
            "音色表、人物表、其他集、任务卡和批准记录只读。不要运行 approve。\n"
            "不要调用配音、音效或生图模型及任何付费服务，不要使用 --allow-paid；"
            "需要新配音、新音效、用户决定或适配器尚未接入时停止并如实说明。"
            f"使用 `{python} -m bookflow produce {quoted_project} ep{episode:02d} --until subs` "
            "逐步推进；父执行器会核验产物并交回任务卡。")


def _draft_prompt(project: Path, episode: int, card_path: Path) -> str:
    python = shlex.quote(sys.executable)
    quoted_project = shlex.quote(str(project))
    epdir = project / "episodes" / f"ep{episode:02d}"
    return ("你在执行书籍精讲工作室的一张全季初稿任务卡，只处理本集文字。\n"
            f"先读取 {card_path} 与 {ROOT / '.agents/skills/draft-episode/SKILL.md'}，"
            f"运行 `{python} -m bookflow next {quoted_project} --read-only --json`、"
            f"`{python} -m bookflow guard {quoted_project} draft --ep {episode}`；"
            "按 skill 读取计划、原文、有效反馈、人名与可用的前序实际工作稿。"
            "前集尚未完成时按计划的揭示/隐瞒边界写，并标明依赖待补；不能称计划为已播出内容。\n"
            f"只允许在 {epdir} 内新增文件，初稿另存 draft_vN.md，不得修改或覆盖已有稿件；"
            "项目配置、原文、人物表、连续性表、其他集、任务卡和批准记录只读。"
            "不要运行 approve，不要调用配音、音效、生图或其他付费服务。"
            "守卫不通过、事实依据不足或需要用户决定时停止并如实说明。"
            "父执行器会独立核验稿件、生成编辑包并交回任务卡；不要自己改任务卡。")


def _invoke(project: Path, episode: int, card_path: Path, *, phase: str,
            timeout_sec: int) -> dict:
    """Never persist model output; completion is not evidence of valid media."""
    if phase not in {"cues", "audio", "draft"}:
        raise ValueError("未知的 Codex 单卡阶段")
    project, epdir, card_path = _paths(project, episode, card_path, phase)
    if type(timeout_sec) is not int or not 1 <= timeout_sec < 7200:
        raise ValueError("Codex 单卡超时必须小于任务卡两小时租约")
    executable = shutil.which("codex")
    if not executable:
        return {"passed": False, "reason": "未找到 codex CLI；未启动自动会话"}
    environment = {name: os.environ[name] for name in _ENV_KEYS if name in os.environ}
    environment["PYTHONPATH"] = str(ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [executable, "exec", "--ephemeral", "--ignore-user-config", "--sandbox",
               "workspace-write", "--json", "-C", str(epdir), "-"]
    try:
        prompt = {"cues": _cue_prompt, "audio": _audio_prompt, "draft": _draft_prompt}[phase](project, episode, card_path)
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stream:
            returncode = _run_cli(command, prompt, stream, cwd=epdir,
                                  environment=environment, timeout_sec=timeout_sec)
            stream.seek(0)
            thread_id = None
            finished = False
            failed = False
            while line := stream.readline(2_000_001):
                if len(line) > 2_000_000 and not line.endswith("\n"):
                    return {"passed": False, "reason": "Codex 单条事件过大；未核验任何产物"}
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    return {"passed": False, "reason": "Codex 事件流格式无效；未核验任何产物"}
                if not isinstance(event, dict):
                    return {"passed": False, "reason": "Codex 事件流格式无效；未核验任何产物"}
                if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                    thread_id = event["thread_id"]
                if event.get("type") in {"turn.failed", "error"}:
                    failed = True
                if event.get("type") == "turn.completed":
                    finished = True
    except subprocess.TimeoutExpired:
        return {"passed": False, "reason": "Codex 单卡会话超时；须核对现有产物及任务状态，不能盲目重试"}
    except OSError:
        return {"passed": False, "reason": "无法启动 Codex 单卡会话；未核验任何产物"}
    if returncode != 0 or not thread_id or not finished or failed:
        return {"passed": False, "reason": "Codex 单卡会话未正常完成；须核验文件后再继续",
                "thread_id": thread_id}
    return {"passed": True, "reason": "Codex 会话已结束；仍须由父执行器核验产物",
            "thread_id": thread_id}


def invoke_cues(project: Path, episode: int, card_path: Path,
                *, timeout_sec: int = CODEX_TIMEOUT_SEC) -> dict:
    return _invoke(project, episode, card_path, phase="cues", timeout_sec=timeout_sec)


def invoke_audio(project: Path, episode: int, card_path: Path,
                 *, timeout_sec: int = CODEX_TIMEOUT_SEC) -> dict:
    return _invoke(project, episode, card_path, phase="audio", timeout_sec=timeout_sec)


def invoke_draft(project: Path, episode: int, card_path: Path,
                 *, timeout_sec: int = CODEX_TIMEOUT_SEC) -> dict:
    return _invoke(project, episode, card_path, phase="draft", timeout_sec=timeout_sec)
