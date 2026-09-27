"""Record the studio build used by a book without rewriting unrelated YAML."""
from __future__ import annotations

import fcntl
import json
import re
from pathlib import Path

import yaml

from . import __version__
from .common import atomic_write

VERSION_LINE = re.compile(r"(?m)^(?:studio_version|'studio_version'|\"studio_version\")[ \t]*:[^\r\n]*(?:\r?\n|$)")


def touch(project: Path) -> bool:
    """Update only the current version; unknown legacy creation versions stay unknown."""
    project = Path(project).resolve()
    path = project / "project.yaml"
    if not path.is_file() or path.is_symlink():
        return False
    with (project / ".studio-version.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            raw = path.read_bytes().decode("utf-8")
            config = yaml.safe_load(raw)
            if not isinstance(config, dict) or not isinstance(config.get("book"), dict):
                return False
            if config.get("studio_version") == __version__:
                return False
            matches = list(VERSION_LINE.finditer(raw))
            if len(matches) > 1 or (config.get("studio_version") is not None and not matches):
                return False
            newline = "\r\n" if "\r\n" in raw else "\n"
            line = f"studio_version: {json.dumps(__version__)}"
            if matches:
                match = matches[0]
                ending = "\r\n" if match.group().endswith("\r\n") else "\n" if match.group().endswith("\n") else ""
                updated = raw[:match.start()] + line + ending + raw[match.end():]
            else:
                updated = raw + ("" if not raw or raw.endswith(("\n", "\r")) else newline) + line + newline
            atomic_write(path, updated)
            return True
        except (OSError, UnicodeError, yaml.YAMLError):
            return False
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
