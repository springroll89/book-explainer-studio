"""Read-only checks for every current season draft."""
from __future__ import annotations

from pathlib import Path

import yaml

from .common import latest_draft
from .quality import lint, verify_quotes


def check_drafts(project: Path, episodes: list[int]) -> dict:
    project = Path(project).resolve()
    errors: list[str] = []
    checked: list[int] = []
    for ep in episodes:
        draft = latest_draft(project / "episodes" / f"ep{ep:02d}")
        if draft is None:
            errors.append(f"episodes/ep{ep:02d}/：缺少当前初稿")
            continue
        relative = draft.relative_to(project)
        checked.append(ep)
        try:
            script = lint(draft)
            quotes = verify_quotes(draft)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            errors.append(f"{relative}：检查失败：{exc}")
            continue
        for item in script.get("items", []):
            if item.get("level") == "error":
                location = f"{relative}:{item['line']}" if type(item.get("line")) is int else str(relative)
                errors.append(f"{location}：{item['message']}")
        for message in quotes.get("errors", []):
            errors.append(f"{relative}：{message}")
    return {"passed": not errors, "errors": errors, "episodes": checked}
