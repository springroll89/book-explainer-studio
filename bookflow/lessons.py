"""Local lesson intake, triage, and guarded shared-rule application."""
from __future__ import annotations

import fcntl
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .common import atomic_write, load_yaml, write_yaml

SOURCES = {"user_message", "edit_round", "rework", "script_error"}
KINDS = {"book_fact", "book_style", "general_craft", "process_bug", "one_off"}
TRIGGERS = re.compile(r"不对|改成|别再|不要再|不要用|统一写作|记住|以后|所有书|后续的书|每次|这类|更新\s*skill", re.I)
GENERAL = re.compile(r"以后|所有书|后续的书|每次|这类|skill", re.I)


def safe_error(message: str) -> str:
    """Retain an actionable error while removing common credential and user-path forms."""
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", str(message))
    value = re.sub(r"(?i)\b(api[_-]?key|token|password|secret)\s*([=:])\s*\S+",
                   r"\1\2[REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
    value = re.sub(r"/" + r"Users/[^/\s]+", "/" + "Users/<local>", value)
    return value[:1000]


def _path(project: Path) -> Path:
    project = Path(project).resolve()
    if project.parent.name != "projects" or not (project / "project.yaml").is_file():
        raise ValueError("教训收件箱需要有效的 projects/<书目> 项目路径")
    return project.parent.parent / "lessons/inbox.yaml"


def _load(path: Path) -> list[dict]:
    rows = load_yaml(path, []) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("lessons/inbox.yaml 格式无效；请先备份并修复，不能静默跳过")
    return rows


def pending_count(project: Path) -> int:
    return sum(row.get("status") == "new" for row in _load(_path(project)))


def inbox(project: Path) -> dict:
    path = _path(project)
    rows = _load(path)
    pending = sum(row.get("status") == "new" for row in rows)
    return {"status": "warning" if pending else "success", "passed": True,
            "summary": f"教训收件箱：{pending} 条待分诊、共 {len(rows)} 条", "items": rows,
            "next_actions": ["运行 lessons triage，逐条准备归宿和检查后交你确认"] if pending else [],
            "artifacts": [str(path)] if path.is_file() else []}


def record(project: Path, source: str, quote: str, evidence: str, what_happened: str) -> dict:
    if source not in SOURCES:
        raise ValueError("教训来源必须是 user_message、edit_round、rework 或 script_error")
    if not evidence.strip() or not what_happened.strip() or (source == "user_message" and not quote.strip()):
        raise ValueError("教训缺少证据、事件说明或用户原话；不要补造内容")
    path = _path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.parent / ".inbox.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            rows = _load(path)
            book = Path(project).name
            existing = next((row for row in rows if row.get("book") == book and row.get("source") == source
                             and row.get("evidence") == evidence and row.get("quote", "") == quote), None)
            if existing:
                return {"status": "warning", "passed": True, "summary": "同一证据已登记，未重复写入",
                        "id": existing["id"], "next_actions": [], "artifacts": [str(path)]}
            now = datetime.now().astimezone()
            prefix = "L" + now.strftime("%Y%m%d") + "-"
            sequence = max((int(row["id"][len(prefix):]) for row in rows
                            if isinstance(row.get("id"), str) and row["id"].startswith(prefix)
                            and row["id"][len(prefix):].isdigit()), default=0) + 1
            item = {"id": f"{prefix}{sequence:02d}", "at": now.isoformat(timespec="seconds"),
                    "book": book, "source": source, "quote": quote, "evidence": evidence,
                    "what_happened": what_happened, "status": "new"}
            write_yaml(path, [*rows, item])
            return {"status": "success", "passed": True, "summary": "教训已登记，等待阶段交界分诊",
                    "id": item["id"], "next_actions": ["阶段交界运行 lessons triage，先展示归宿和配套检查"],
                    "artifacts": [str(path)]}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def observe(project: Path, quote: str, evidence: str) -> dict:
    """Register a real user correction; non-corrections leave the inbox unchanged."""
    if not TRIGGERS.search(quote):
        return {"status": "warning", "passed": True, "summary": "未发现明确纠正或跨项目要求，未登记",
                "next_actions": [], "artifacts": []}
    return record(project, "user_message", quote, evidence, "用户纠正或提出跨项目要求；原因与归宿待分诊")


def record_script_error(project: Path, command: str, message: str) -> dict:
    evidence = f"cli:{command}:{time.time_ns()}"
    return record(project, "script_error", "", evidence, safe_error(message))


def record_edit_round(project: Path, round_dir: Path, report: dict) -> dict:
    changes = report.get("changes", [])
    comments = report.get("comments", {})
    if not changes and not comments and not report.get("headings_changed"):
        return {"status": "warning", "passed": True, "summary": "本轮没有文字、批注或标题改动，未登记教训",
                "next_actions": [], "artifacts": []}
    kinds = Counter(change.get("kind", "unknown") for change in changes)
    summary = "人工改稿导入：" + "、".join(f"{kind} {count} 处" for kind, count in sorted(kinds.items()))
    summary += f"；批注 {len(comments)} 条；待人工语义分诊"
    evidence = str((round_dir / "diff.json").relative_to(project))
    return record(project, "edit_round", "", evidence, summary)


def propose(project: Path, lesson_id: str, *, kind: str, destination: str,
            before: str, after: str, check: str, rationale: str) -> dict:
    """Save an assistant proposal, not a triage decision or permission to edit."""
    if kind not in KINDS:
        raise ValueError("教训类型无效；须选本书事实、本书风格、通用手艺、流程缺陷或一次性")
    if not rationale.strip() or (kind != "one_off" and (not check.strip() or not after.strip())):
        raise ValueError("提案缺少判断依据、配套检查或拟修改文字")
    if kind != "one_off":
        relative = Path(destination)
        if (not destination.strip() or relative.is_absolute() or ".." in relative.parts
                or ".git" in relative.parts):
            raise ValueError("归宿路径必须是项目或工作室内的单个相对文件")
        if kind in {"book_fact", "book_style"}:
            allowed = relative.parts[:1] in (("analysis",), ("feedback",))
        else:
            allowed = relative.parts[:1] in (("style",), ("bookflow",), ("tools",), ("docs",),
                                             ("roles",), ("config",), (".agents",))
        if not allowed or len(relative.parts) < 2:
            raise ValueError("归宿路径与教训类型不符，不能写到项目或工作室范围以外")
    elif destination.strip():
        raise ValueError("一次性事项只归档，不指定规则文件")
    path = _path(project)
    if not path.parent.is_dir():
        raise ValueError("教训收件箱不存在，无法提议未知条目")
    lock = path.parent / ".inbox.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            rows = _load(path)
            target = next((row for row in rows if row.get("id") == lesson_id and row.get("book") == Path(project).name), None)
            if not target or target.get("status") != "new":
                raise ValueError("找不到本书待分诊条目；不要修改已处理记录")
            target["proposal"] = {"kind": kind, "destination": destination, "before": before,
                                  "after": after, "check": check, "rationale": rationale}
            write_yaml(path, rows)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return {"status": "warning", "passed": True, "summary": "分诊提案已保存，仍待用户确认",
            "id": lesson_id, "next_actions": ["向用户展示全部提案的类型、归宿、改前改后和检查；确认前保持 new"],
            "artifacts": [str(path)]}


def accept(project: Path, quote: str, *, verify_transcript: bool = True) -> dict:
    """Mark fully proposed items triaged only after an explicit user reply."""
    if quote.strip() != "可以":
        return {"status": "error", "passed": False, "summary": "分诊确认须为用户独立回复“可以”",
                "errors": ["确认原话不匹配"], "next_actions": ["展示提案并等待用户明确回复"], "artifacts": []}
    if verify_transcript:
        from .approvals import _session_user_message
        latest, warning = _session_user_message()
        if warning or latest is None:
            return {"status": "error", "passed": False, "summary": "无法核对用户的独立分诊确认，未更改收件箱",
                    "errors": [warning or "会话原话不可读"],
                    "next_actions": ["核对本机会话记录后重试；不要代填“可以”"], "artifacts": []}
        if latest.strip() != quote.strip():
            return {"status": "error", "passed": False, "summary": "最新用户消息与分诊确认原话不一致",
                    "errors": ["不能代填用户确认"], "next_actions": ["等待用户独立回复“可以”后重试"], "artifacts": []}
    path = _path(project)
    if not path.parent.is_dir():
        return {"status": "warning", "passed": True, "summary": "没有待确认的分诊条目",
                "next_actions": [], "artifacts": []}
    lock = path.parent / ".inbox.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            rows = _load(path)
            pending = [row for row in rows if row.get("status") == "new"]
            if not pending:
                return {"status": "warning", "passed": True, "summary": "没有待确认的分诊条目",
                        "next_actions": [], "artifacts": [str(path)] if path.is_file() else []}
            unproposed = [row.get("id") for row in pending if not isinstance(row.get("proposal"), dict)]
            if unproposed:
                return {"status": "error", "passed": False,
                        "summary": "仍有条目未准备完整提案", "errors": ["、".join(map(str, unproposed))],
                        "next_actions": ["先为每条 new 教训补齐类型、归宿、改前改后和检查，再展示给用户"],
                        "artifacts": [str(path)]}
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            for row in pending:
                row.update(status="triaged", accepted_quote=quote, accepted_at=stamp)
            write_yaml(path, rows)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return {"status": "warning", "passed": True, "summary": f"{len(pending)} 条分诊已确认；尚未应用规则修改",
            "next_actions": ["后续按已确认提案逐条实施、测试并提交；目前不要声称已应用"],
            "artifacts": [str(path)]}


def triage(project: Path) -> dict:
    """Suggest only a scope; never claim that a destination or edit is approved."""
    path = _path(project)
    pending = [row for row in _load(path) if row.get("status") == "new"]
    proposals = []
    for row in pending:
        proposal = row.get("proposal") if isinstance(row.get("proposal"), dict) else None
        if row.get("source") == "script_error":
            scope = "process_candidate"
        elif GENERAL.search(str(row.get("quote", ""))):
            scope = "general_candidate"
        else:
            scope = "needs_classification"
        proposals.append({"id": row.get("id"), "source": row.get("source"), "quote": row.get("quote", ""),
                          "evidence": row.get("evidence", ""), "suggested_scope": scope,
                          "proposal": proposal, "kind": proposal.get("kind") if proposal else None,
                          "destination": proposal.get("destination") if proposal else None,
                          "before": proposal.get("before") if proposal else None,
                          "after": proposal.get("after") if proposal else None,
                          "check": proposal.get("check") if proposal else None})
    return {"status": "warning" if pending else "success", "passed": True,
            "summary": f"待分诊 {len(pending)} 条；候选范围不是用户批准或可直接应用的修改",
            "items": proposals,
            "next_actions": ["助手补全类型、唯一归宿、改前改后和检查，一次性展示待你确认；未确认前保持 new"] if pending else [],
            "artifacts": [str(path)] if path.is_file() else []}


def report(project: Path) -> dict:
    """List applied rules that lack a specific automated check."""
    path = _path(project)
    root = path.parent.parent
    pending = []
    for row in _load(path):
        proposal = row.get("proposal") if isinstance(row.get("proposal"), dict) else {}
        check = proposal.get("check")
        missing_check = (isinstance(check, str) and check.startswith("tests/")
                         and not (root / check).is_file())
        if row.get("status") == "applied" and (check in (None, "", "unverified") or missing_check):
            pending.append({"id": row.get("id"), "destination": proposal.get("destination"),
                            "commit": row.get("commit"), "check": "unverified" if not missing_check else check,
                            "reason": "missing_check" if missing_check else "unverified"})
    return {"status": "warning" if pending else "success", "passed": True,
            "summary": f"已应用且未验证的共享规则：{len(pending)} 条", "items": pending,
            "next_actions": ["为每条规则补自动检查并重新审阅"] if pending else [],
            "artifacts": [str(path)] if path.is_file() else []}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"Git 操作失败：{' '.join(args[:2])}；请在工作室仓库核对 Git 状态后重试")
    return result.stdout.rstrip("\n")


def _git_blob(root: Path, revision: str, destination: str, *, allow_missing: bool = False) -> bytes | None:
    result = subprocess.run(["git", "show", f"{revision}:{destination}"], cwd=root,
                            capture_output=True, check=False)
    if result.returncode:
        if allow_missing:
            return None
        raise ValueError("目标提交中的共享规则文件无法读取，停止撤回")
    return result.stdout


def _verify_workspace(root: Path, project: Path, check: str, target: Path) -> list[str]:
    """Run the proposed check, the full suite, hygiene, and the current selftest."""
    from .hygiene import audit
    if not audit(root, tracked_only=True, projects_dir=project.parent, include_paths=(target,))["passed"]:
        return ["共享层卫生检查未通过；修改已恢复，先核对书目词或路径泄漏"]
    commands = []
    if check != "unverified":
        if check == "selftest":
            commands.append([sys.executable, "-m", "bookflow", "selftest"])
        elif re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", check) and (root / check).is_file():
            commands.append([sys.executable, "-m", "unittest", check[:-3].replace("/", ".")])
        else:
            return ["提案检查必须是存在的 tests/test_*.py、自检 selftest 或 unverified"]
    commands.extend(([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                     [sys.executable, "-m", "bookflow", "selftest"]))
    for command in commands:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
        if result.returncode:
            return [f"检查未通过：{' '.join(command[2:])}；修改已恢复，可在工作室目录单独重跑"]
    return []


def _rule_target(root: Path, destination: str) -> Path:
    relative = Path(destination)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts or len(relative.parts) < 2:
        raise ValueError("共享规则归宿路径无效，停止应用")
    if relative.suffix.lower() not in {".md", ".py", ".yaml", ".yml", ".toml", ".txt", ".sh", ".json", ".html"}:
        raise ValueError("共享规则只能应用到可审阅的文本文件")
    target = root / relative
    if not target.resolve().is_relative_to(root) or any(path.is_symlink() for path in (target, *target.parents)
                                                    if path.is_relative_to(root) and path != root):
        raise ValueError("共享规则归宿经过符号链接或越出工作室，停止应用")
    return target


def apply(project: Path, lesson_id: str) -> dict:
    """Apply one accepted shared lesson; never commit ignored book material."""
    path = _path(project)
    root = path.parent.parent
    lock = path.parent / ".inbox.lock"
    if not lock.parent.is_dir():
        raise ValueError("教训收件箱不存在，停止应用")
    with lock.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            rows = _load(path)
            item = next((row for row in rows if row.get("id") == lesson_id and row.get("book") == Path(project).name), None)
            if item is None or item.get("status") != "triaged" or not isinstance(item.get("proposal"), dict):
                raise ValueError("只有本书已确认、未应用的完整分诊提案才能执行 apply")
            if not item.get("accepted_at") or item.get("accepted_quote") != "可以":
                raise ValueError("缺少已核对的用户分诊确认，停止应用")
            proposal = item["proposal"]
            kind = proposal.get("kind")
            if kind == "one_off":
                item.update(status="rejected", applied_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                write_yaml(path, rows)
                return {"status": "success", "passed": True, "summary": "一次性事项已归档，未修改共享规则",
                        "id": lesson_id, "next_actions": [], "artifacts": [str(path)]}
            if kind not in {"general_craft", "process_bug"}:
                return {"status": "error", "passed": False,
                        "summary": "本书专属教训不能提交到公开工作室仓库",
                        "errors": ["本书归宿位于被忽略的 projects/；尚无独立的本书版本提交机制"],
                        "next_actions": ["保留已确认提案，先建立本书本地版本与回退方案，再执行应用"],
                        "artifacts": [str(path)]}
            destination = str(proposal.get("destination", ""))
            target = _rule_target(root, destination)
            if not destination.startswith(("style/", "bookflow/", "tools/", "docs/", "roles/", "config/", ".agents/")):
                raise ValueError("共享规则归宿不在允许的工作室目录")
            if _git(root, "rev-parse", "--show-toplevel") != str(root):
                raise ValueError("项目目录与 Git 工作室根目录不一致，停止应用")
            if _git(root, "status", "--porcelain", "--untracked-files=normal"):
                raise ValueError("工作树含未提交改动；先人工处理，避免混入教训提交")
            if target.exists() and not target.is_file():
                raise ValueError("规则归宿不是普通文件，停止应用")
            before = str(proposal.get("before", ""))
            after = str(proposal.get("after", ""))
            check = str(proposal.get("check", ""))
            if not after.strip() or not check.strip():
                raise ValueError("提案缺少改后文本或检查，停止应用")
            original = target.read_text(encoding="utf-8") if target.is_file() else None
            if before:
                if original is None or original.count(before) != 1:
                    raise ValueError("改前文本在唯一归宿中未恰好匹配一次；先重新分诊，不能盲改")
                updated = original.replace(before, after, 1)
            else:
                if original is not None and after in original:
                    raise ValueError("拟新增规则已存在；先核对重复条目")
                updated = (original or "").rstrip("\n") + ("\n\n" if original else "") + after.rstrip("\n") + "\n"
            changelog = root / "CHANGELOG.md"
            changelog_original = changelog.read_text(encoding="utf-8") if changelog.is_file() else None
            if changelog_original and lesson_id in changelog_original:
                raise ValueError("CHANGELOG 已包含该教训编号；请核对是否已经提交")
            changelog_text = (changelog_original or "# 变更记录\n").rstrip("\n") + f"\n\n- {lesson_id}：更新 `{destination}`；检查：`{check}`。\n"
            committed = False
            try:
                atomic_write(target, updated)
                atomic_write(changelog, changelog_text)
                errors = _verify_workspace(root, project, check, target)
                if errors:
                    return {"status": "error", "passed": False, "summary": "教训修改未通过验证，未提交",
                            "errors": errors, "next_actions": ["修复提案或对应测试后重试 apply"],
                            "artifacts": [str(path)]}
                changed = {line[3:] for line in _git(root, "status", "--porcelain", "--untracked-files=normal").splitlines()}
                if changed != {destination, "CHANGELOG.md"}:
                    raise ValueError("验证后工作树出现非本教训文件改动；停止提交，请单独检查这些改动")
                _git(root, "diff", "--check")
                _git(root, "add", "--", destination, "CHANGELOG.md")
                _git(root, "diff", "--cached", "--check")
                _git(root, "commit", "-m", f"Lesson {lesson_id}: update shared rule")
                committed = True
            except (OSError, ValueError):
                if _git(root, "diff", "--cached", "--name-only"):
                    _git(root, "restore", "--staged", "--", destination, "CHANGELOG.md")
                raise
            finally:
                if not committed:
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        atomic_write(target, original)
                    if changelog_original is None:
                        changelog.unlink(missing_ok=True)
                    else:
                        atomic_write(changelog, changelog_original)
            commit = _git(root, "rev-parse", "HEAD")
            item.update(status="applied", applied_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        commit=commit)
            try:
                write_yaml(path, rows)
            except OSError:
                return {"status": "warning", "passed": False,
                        "summary": "共享教训已提交，但本机收件箱状态写入失败",
                        "id": lesson_id, "commit": commit,
                        "next_actions": ["不要再次应用；核对该提交后修复收件箱并登记 applied 状态"],
                        "artifacts": [str(target), str(changelog), str(path)]}
            return {"status": "success", "passed": True, "summary": "共享教训已应用并单独提交",
                    "id": lesson_id, "commit": commit,
                    "next_actions": [f"如需撤销，核对后续依赖后独立回复‘撤回 {lesson_id}’"],
                    "artifacts": [str(target), str(changelog), str(path)]}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def revert(project: Path, lesson_id: str, quote: str, *, verify_transcript: bool = True) -> dict:
    """Undo only a known lesson commit whose rule target has not changed since."""
    expected = f"撤回 {lesson_id}"
    if quote.strip() != expected:
        raise ValueError(f"撤回口令须是独立的“{expected}”")
    if verify_transcript:
        from .approvals import _session_user_message
        latest, warning = _session_user_message()
        if warning or latest is None or latest.strip() != quote.strip():
            raise ValueError("无法核对用户最新独立撤回口令，未修改规则或 Git 历史")
    path = _path(project)
    root = path.parent.parent
    if not path.parent.is_dir():
        raise ValueError("教训收件箱不存在，停止撤回")
    lock = path.parent / ".inbox.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            rows = _load(path)
            item = next((row for row in rows if row.get("id") == lesson_id and row.get("book") == Path(project).name), None)
            if not item or item.get("status") != "applied" or not isinstance(item.get("proposal"), dict):
                raise ValueError("只能撤回本书已应用且未撤回的教训")
            commit = str(item.get("commit", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ValueError("教训记录缺少有效 Git 提交号，停止撤回")
            destination = str(item["proposal"].get("destination", ""))
            target = _rule_target(root, destination)
            if _git(root, "rev-parse", "--show-toplevel") != str(root):
                raise ValueError("项目目录与 Git 工作室根目录不一致，停止撤回")
            if _git(root, "status", "--porcelain", "--untracked-files=normal"):
                raise ValueError("工作树含未提交改动；先人工处理，避免撤回覆盖其他工作")
            if _git(root, "show", "-s", "--format=%s", commit) != f"Lesson {lesson_id}: update shared rule":
                raise ValueError("提交标题与教训编号不符，停止撤回")
            ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                                      cwd=root, capture_output=True, check=False)
            if ancestor.returncode:
                raise ValueError("教训提交不在当前分支历史中，停止撤回")
            changed_files = set(_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines())
            if changed_files != {destination, "CHANGELOG.md"}:
                raise ValueError("教训提交包含额外文件，停止自动撤回")
            committed_blob = _git_blob(root, commit, destination)
            if not target.is_file() or target.read_bytes() != committed_blob:
                raise ValueError("归宿文件在教训提交后又被修改；停止自动撤回，先人工核对后续依赖")
            previous_blob = _git_blob(root, commit + "^", destination, allow_missing=True)
            changelog = root / "CHANGELOG.md"
            changelog_original = changelog.read_text(encoding="utf-8")
            if lesson_id not in changelog_original:
                raise ValueError("CHANGELOG 未找到该教训编号，停止撤回")
            original = committed_blob.decode("utf-8")
            previous = previous_blob.decode("utf-8") if previous_blob is not None else None
            check = str(item["proposal"].get("check", "unverified"))
            committed = False
            try:
                if previous is None:
                    target.unlink()
                else:
                    atomic_write(target, previous)
                atomic_write(changelog, changelog_original.rstrip("\n") +
                             f"\n\n- {lesson_id}：撤回共享规则提交 `{commit[:12]}`。\n")
                errors = _verify_workspace(root, project, check, target)
                if errors:
                    return {"status": "error", "passed": False, "summary": "撤回未通过验证，原文件已恢复",
                            "errors": errors, "next_actions": ["核对依赖和失败检查后再决定如何撤回"],
                            "artifacts": [str(path)]}
                changed = {line[3:] for line in _git(root, "status", "--porcelain", "--untracked-files=normal").splitlines()}
                if changed != {destination, "CHANGELOG.md"}:
                    raise ValueError("验证后出现非本教训文件改动，停止提交撤回")
                _git(root, "add", "-A", "--", destination, "CHANGELOG.md")
                _git(root, "diff", "--cached", "--check")
                _git(root, "commit", "-m", f"Revert lesson {lesson_id}")
                committed = True
            except (OSError, ValueError):
                if _git(root, "diff", "--cached", "--name-only"):
                    _git(root, "restore", "--staged", "--", destination, "CHANGELOG.md")
                raise
            finally:
                if not committed:
                    atomic_write(target, original)
                    atomic_write(changelog, changelog_original)
            revert_commit = _git(root, "rev-parse", "HEAD")
            item.update(status="rejected", reverted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        revert_commit=revert_commit)
            try:
                write_yaml(path, rows)
            except OSError:
                return {"status": "warning", "passed": False, "summary": "撤回已提交，但本机收件箱状态写入失败",
                        "id": lesson_id, "revert_commit": revert_commit,
                        "next_actions": ["不要再次撤回；核对提交后修复收件箱状态"],
                        "artifacts": [str(target), str(changelog), str(path)]}
            return {"status": "success", "passed": True, "summary": "共享教训已撤回并单独提交",
                    "id": lesson_id, "revert_commit": revert_commit, "next_actions": [],
                    "artifacts": [str(target), str(changelog), str(path)]}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
