"""Check that reusable studio files do not contain per-book material."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

import yaml

SHARED_DIRS = ("bookflow", "tools", ".agents/skills", "docs", "roles", "style", "config")
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".sh", ".json", ".html"}
PATTERNS = {
    "absolute_path": re.compile(r"/" + r"(?:Users|home)/|[A-Za-z]:\\Users\\"),
    "book_path": re.compile(r"(?:projects|图书库)/(?![<{(])[\w\u4e00-\u9fff-]+"),
    "fixed_episode_asset": re.compile(r"\b(?:[a-z][a-z0-9_]*_)?ep\d{2}_[a-z0-9_-]*v\d+\b", re.I),
}
INLINE_LINK = re.compile(r"\[[^\]\n]+\]\((<[^>\n]+>|[^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)")
REMOTE_LINK = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
LEGACY_WORKFLOW = re.compile(r"(?<![A-Za-z0-9_])(?:guard|G[1-4]|AV1|RELEASE|ChatCut)(?![A-Za-z0-9_])")
CURRENT_COUNT = re.compile(
    r"(?:当前|现有|共有|本文件是|本项目有|目前)[^。\n]{0,40}"
    r"\d+\s*(?:个|项)\s*(?:项目技能|技能|自动测试|测试)"
)


def _local_links(line: str) -> list[str]:
    targets = [match.group(1) for match in INLINE_LINK.finditer(line)]
    reference = REFERENCE_LINK.match(line)
    if reference:
        targets.append(reference.group(1))
    return [target.strip("<>") for target in targets]


def _prose_paragraphs(lines: list[str]) -> list[tuple[int, str]]:
    """Normalize prose across line wraps without treating examples as rules."""
    paragraphs: list[tuple[int, str]] = []
    parts: list[str] = []
    start = 0
    fenced = False

    def flush() -> None:
        nonlocal parts
        normalized = re.sub(r"\s+", "", "".join(parts))
        if len(normalized) >= 40:
            paragraphs.append((start, normalized))
        parts = []

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced or not stripped or stripped.startswith(("#", "|", ">", "<!--")):
            flush()
            continue
        if re.match(r"(?:[-*+]\s+|\d+[.)]\s+)", stripped):
            flush()
            stripped = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", stripped)
        if not parts:
            start = number
        parts.append(stripped)
    flush()
    return paragraphs


def _book_catalog(projects_dir: Path) -> tuple[set[str], set[str]]:
    terms: set[str] = set()
    slugs: set[str] = set()
    for project in projects_dir.glob("*/"):
        if not project.is_dir():
            continue
        slugs.add(project.name)
        for source, keys in ((project / "project.yaml", ("title",)),
                             (project / "analysis/characters.yaml", ("name_zh", "spoken_name", "name_es", "name_de"))):
            if not source.is_file():
                continue
            try:
                data = yaml.safe_load(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
            if not isinstance(data, dict):
                continue
            rows = [data.get("book", {})] if source.name == "project.yaml" else data.get("characters", [])
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                for key in keys:
                    value = row.get(key)
                    if isinstance(value, str) and len(value.strip()) >= 2:
                        terms.add(value.strip())
    return terms, slugs


def audit(root: Path, *, tracked_only: bool = False, projects_dir: Path | None = None,
          include_paths: tuple[Path, ...] = ()) -> dict:
    """Return path/line findings only; never echo book text or local secrets."""
    root = Path(root).resolve()
    findings: list[dict] = []
    prose: dict[str, list[tuple[Path, int]]] = {}
    terms, slugs = _book_catalog(Path(projects_dir) if projects_dir is not None else root / "projects")
    tracked: set[Path] | None = None
    if tracked_only:
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                capture_output=True, check=True)
        tracked = {root / path.decode("utf-8") for path in result.stdout.split(b"\0") if path}
        tracked.update(Path(path).resolve() for path in include_paths)

    def issue(path: Path, rule: str, line: int = 0) -> None:
        findings.append({"path": str(path.relative_to(root)), "line": line, "rule": rule})

    def check_doc_links(path: Path, lines: list[str]) -> None:
        for number, line in enumerate(lines, 1):
            for target in _local_links(line):
                if target.startswith("file:"):
                    issue(path, "doc_link_outside_repo", number)
                    continue
                if target.startswith("#") or REMOTE_LINK.match(target):
                    continue
                name = unquote(target.split("#", 1)[0].split("?", 1)[0])
                if not name:
                    continue
                destination = (path.parent / name).resolve()
                if not destination.is_relative_to(root):
                    issue(path, "doc_link_outside_repo", number)
                elif not destination.exists():
                    issue(path, "broken_doc_link", number)

    for directory in SHARED_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if (path.is_symlink() or not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES
                    or (tracked is not None and path not in tracked)):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                issue(path, "unreadable_text")
                continue
            for number, line in enumerate(lines, 1):
                if directory == ".agents/skills" and LEGACY_WORKFLOW.search(line):
                    issue(path, "legacy_skill_workflow", number)
                for rule, pattern in PATTERNS.items():
                    if rule == "book_path":
                        is_code = path.suffix.lower() in {".py", ".sh", ".yaml", ".yml", ".toml", ".json"}
                        if any(is_code or match.group().split("/", 1)[1] in slugs
                               for match in pattern.finditer(line)):
                            issue(path, rule, number)
                    elif pattern.search(line):
                        issue(path, rule, number)
                if any(term in line for term in terms):
                    issue(path, "book_term", number)
            if path.suffix.lower() == ".md":
                check_doc_links(path, lines)
                for number, paragraph in _prose_paragraphs(lines):
                    prose.setdefault(paragraph, []).append((path, number))
                for number, line in enumerate(lines, 1):
                    if CURRENT_COUNT.search(line):
                        issue(path, "manual_current_count", number)
            if path.name == "SKILL.md" and len(lines) > 80:
                issue(path, "skill_over_80_lines", len(lines))
            if directory == "style" and path.suffix == ".md" and len(lines) > 200:
                issue(path, "craft_over_200_lines", len(lines))
    agents = root / "AGENTS.md"
    if agents.is_file() and (tracked is None or agents in tracked) and len(agents.read_text(encoding="utf-8").splitlines()) > 60:
        issue(agents, "agents_over_60_lines")
    if agents.is_file() and (tracked is None or agents in tracked):
        for number, paragraph in _prose_paragraphs(agents.read_text(encoding="utf-8").splitlines()):
            prose.setdefault(paragraph, []).append((agents, number))
    readme = root / "README.md"
    if readme.is_file() and (tracked is None or readme in tracked):
        lines = readme.read_text(encoding="utf-8").splitlines()
        check_doc_links(readme, lines)
        for number, paragraph in _prose_paragraphs(lines):
            prose.setdefault(paragraph, []).append((readme, number))
        for number, line in enumerate(lines, 1):
            if CURRENT_COUNT.search(line):
                issue(readme, "manual_current_count", number)
    for occurrences in prose.values():
        distinct = {}
        for path, number in occurrences:
            distinct.setdefault(path, number)
        for path, number in sorted(distinct.items())[1:]:
            issue(path, "duplicate_rule_text", number)
    findings.sort(key=lambda row: (row["path"], row["line"], row["rule"]))
    return {"status": "error" if findings else "success", "passed": not findings,
            "summary": f"共享层卫生检查：{len(findings)} 项问题", "findings": findings,
            "next_actions": ["按 path/line 修复共享文件；书目专属内容移到项目目录"] if findings else [],
            "artifacts": [str(root)]}
