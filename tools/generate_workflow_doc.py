"""Update the generated stage overview in docs/WORKFLOW.md."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bookflow.common import atomic_write
from bookflow.flow import WORKFLOW_END, WORKFLOW_START, render_workflow_overview


def update(*, check: bool = False) -> bool:
    path = ROOT / "docs/WORKFLOW.md"
    current = path.read_text(encoding="utf-8")
    if current.count(WORKFLOW_START) != 1 or current.count(WORKFLOW_END) != 1:
        raise ValueError("docs/WORKFLOW.md 的生成区标记缺失或重复")
    start = current.find(WORKFLOW_START)
    end = current.find(WORKFLOW_END)
    if start < 0 or end < start:
        raise ValueError("docs/WORKFLOW.md 缺少有效的生成区标记")
    end += len(WORKFLOW_END)
    expected = render_workflow_overview()
    updated = current[:start] + expected + current[end:]
    if check:
        return updated == current
    if updated != current:
        atomic_write(path, updated)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查文档是否与阶段定义同步")
    args = parser.parse_args()
    try:
        current = update(check=args.check)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"workflow 文档更新失败：{exc}", file=sys.stderr)
        return 2
    if not current:
        print("docs/WORKFLOW.md 的生成区已过期；运行 tools/generate_workflow_doc.py 更新。", file=sys.stderr)
        return 1
    print("docs/WORKFLOW.md 阶段总览已同步。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
