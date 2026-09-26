"""Shared file formats and narration parsing. No model or network calls."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PID = re.compile(r"p\d{5,}")
REF = re.compile(r"^(p\d{5,})(?:[-–~](p\d{5,}))?$")
QUOTE = re.compile(r"〔引\s*((?:p\d{5,}|Q\d+))(?:[-–~]((?:p\d{5,}|Q\d+)))?〕\s*「([^」]*)」", re.S)
TAG = re.compile(r"〔(?:引\s*p\d{5,}(?:[-–~]p\d{5,})?|据\s*[^〕]+|外\s*[^〕]+|延伸|线索\s*[^〕]+)〕")
PROD = re.compile(r"\[(画面|BGM|音效|字幕|镜头|备注)[:：]([^\]]*)\]")
PUNCT = re.compile(r"[\s，。！？、；：“”‘’「」『』（）()《》〈〉【】…—\-,.!?;:\"'·~～/\[\]]")


def count_chars(text: str) -> int:
    return len(PUNCT.sub("", text))


def atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".bookflow-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_yaml(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ({} if default is None else default) if data is None else data


def write_yaml(path: Path, data: Any) -> None:
    atomic_write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        out[key] = deep_merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def find_project(path: Path) -> Path | None:
    path = Path(path).resolve()
    for item in [path, *path.parents]:
        if (item / "project.yaml").is_file():
            return item
    return None


def load_config(project: Path | None = None) -> dict:
    config = load_yaml(ROOT / "config/defaults.yaml")
    if project:
        actual = find_project(project)
        if actual:
            config = deep_merge(config, load_yaml(actual / "project.yaml"))
    return config


def full_season_review(project: Path) -> bool:
    """Explicit drafting policy; existing projects retain their current workflow."""
    return load_config(project).get("drafting", {}).get("mode") == "full_season_review"


def source_generation(project: Path) -> str:
    manifest = Path(project) / "source/current.json"
    return json.loads(manifest.read_text(encoding="utf-8")).get("generation", "") if manifest.exists() else ""


def source_dir(project: Path) -> Path:
    generation = source_generation(project)
    if not generation or not re.fullmatch(r"[a-f0-9]{16,64}", generation):
        raise ValueError("尚未导入原文，或 source/current.json 格式无效")
    return Path(project) / "source/imports" / generation


def read_paragraphs(project: Path) -> dict:
    result = {}
    for line in (source_dir(project) / "paragraphs.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            result[item["id"]] = item
    return result


def read_chapters(project: Path) -> list:
    return json.loads((source_dir(project) / "chapters.json").read_text(encoding="utf-8"))


def expand_evidence(ref: str, paragraphs: dict) -> list[str]:
    match = REF.fullmatch(str(ref).strip())
    if not match:
        raise ValueError(f"证据格式无效：{ref}")
    start, end = match.group(1), match.group(2) or match.group(1)
    if start not in paragraphs or end not in paragraphs:
        raise ValueError(f"证据段落不存在：{ref}")
    lo, hi = int(start[1:]), int(end[1:])
    if lo > hi:
        raise ValueError(f"证据范围倒置：{ref}")
    # Read existing identifiers rather than materializing an unbounded alleged range.
    ids = sorted((pid for pid in paragraphs if lo <= int(pid[1:]) <= hi), key=lambda x: int(x[1:]))
    if len(ids) != hi - lo + 1:
        raise ValueError(f"证据范围不连续：{ref}")
    return ids


def validate_evidence(evidence: Any, paragraphs: dict, external: Any = None) -> list[str]:
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list) or not evidence:
        return ["缺少证据列表"]
    sources = external or {}
    if isinstance(sources, dict) and "sources" in sources:
        sources = sources["sources"]
    if isinstance(sources, list):
        sources = {item.get("id"): item for item in sources if isinstance(item, dict)}
    errors = []
    for ref in evidence:
        if isinstance(ref, str) and ref.startswith("ext:"):
            item = sources.get(ref[4:], {}) if isinstance(sources, dict) else {}
            if item.get("status") != "verified" or not all(item.get(k) for k in ("url", "title", "accessed_at")):
                errors.append(f"书外证据未核实或缺来源信息：{ref}")
            elif not re.match(r"^https?://", str(item["url"])):
                errors.append(f"书外来源URL无效：{ref}")
        else:
            try:
                expand_evidence(ref, paragraphs)
            except ValueError as exc:
                errors.append(str(exc))
    return errors


def parse_draft(text: str) -> dict:
    meta: dict = {}
    body = text.replace("\r\n", "\n")
    offset_lines = 0
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", body, re.S)
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            raise ValueError("稿件 front matter 必须是映射")
        offset_lines = match.group(0).count("\n")
        body = body[match.end():]
    body = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), body, flags=re.S)
    quote_body = PROD.sub(lambda m: "\n" * m.group(0).count("\n"), body)
    quotes = [dict(line=offset_lines + quote_body[:m.start()].count("\n") + 1,
                   start=m.group(1), end=m.group(2) or m.group(1), text=m.group(3)) for m in QUOTE.finditer(quote_body)]
    lines, sentences, hooks, production, sections = [], [], [], [], []
    total = commentary_chars = 0
    commentary = False
    section = ""
    evidence = []
    for number, raw in enumerate(body.splitlines(), offset_lines + 1):
        line = raw.strip()
        if not line:
            commentary = False
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            section = heading.group(1)
            sections.append({"title": section, "offset": total})
            commentary = False
            continue
        for tag in PROD.finditer(line):
            production.append({"offset": total, "section": section, "kind": tag.group(1), "text": tag.group(2)})
        line = PROD.sub("", line)
        for tag in re.finditer(r"〔(?:据|引)\s*([^〕]+)〕", line):
            evidence.extend(re.split(r"[,，\s]+", tag.group(1).strip()))
        evidence.extend("ext:" + tag.group(1).strip() for tag in re.finditer(r"〔外\s*([^〕]+)〕", line))
        commentary = commentary or "〔延伸〕" in line
        while "[钩子]" in line:
            pos = line.index("[钩子]")
            hooks.append({"offset": total + count_chars(TAG.sub("", line[:pos])), "line": number})
            line = line[:pos] + line[pos + len("[钩子]"):]
        clean = TAG.sub("", line).replace("「", "").replace("」", "").strip()
        if not clean:
            continue
        lines.append((number, clean))
        for part in re.split(r"(?<=[。！？!?])", clean):
            if not count_chars(part):
                continue
            size = count_chars(part)
            sentences.append({"id": f"s{len(sentences)+1:03d}", "text": part.strip(), "line": number,
                              "offset": total, "chars": size, "section": section})
            total += size
            if commentary:
                commentary_chars += size
    for hook in hooks:
        matched = next((s for s in sentences if s["offset"] < hook["offset"] <= s["offset"] + s["chars"]), None)
        hook["sentence_id"] = matched["id"] if matched else (sentences[0]["id"] if sentences else "")
    return {"meta": meta, "spoken": "\n".join(t for _, t in lines), "lines": lines,
            "sentences": sentences, "hooks": hooks, "quotes": quotes, "total_chars": total,
            "commentary_chars": commentary_chars, "cited_pids": sorted(set(evidence)),
            "production": production, "sections": sections, "body": body}


def fmt_time(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
