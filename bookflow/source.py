"""Read source books into immutable, content-addressed imports."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from .common import atomic_write, write_json


PARSER_VERSION = "bookflow-source-1"
CHAPTER = re.compile(
    r"^(?:#{1,6}\s+.+|第[零〇一二三四五六七八九十百千万两\d]+[章回节卷部篇集].{0,60}"
    r"|chapter\s+[\wIVXLC]+.{0,60}"
    r"|(?:序章|序言|自序|前言|引言|序|楔子|引子|尾声|后记|跋|终章|番外)(?:[：: ].{0,50})?)$",
    re.IGNORECASE,
)


def _decode(raw: bytes, path: Path) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "gb18030", "big5")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            if "\x00" in text:
                continue
            return text
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文本编码：{path}；请另存为 UTF-8 后导入")


class _BookHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"head", "script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {"p", "div", "section", "li", "blockquote", "tr"}:
            self.parts.append("\n\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n# ")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"head", "script", "style"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if not self.hidden and tag in {
            "p", "div", "section", "li", "blockquote", "tr", "h1", "h2", "h3", "h4", "h5", "h6"
        }:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(re.sub(r"\s+", " ", data))


def _read_epub(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError(f"EPUB 缺少有效的 package 路径：{path}")
        package_path = rootfile.attrib["full-path"]
        package = ElementTree.fromstring(archive.read(package_path))
        items = {item.get("id"): item for item in package.findall("./{*}manifest/{*}item")}
        texts = []
        for ref in package.findall("./{*}spine/{*}itemref"):
            item = items.get(ref.get("idref"))
            if item is None:
                raise ValueError(f"EPUB spine 引用了不存在的内容：{path}")
            href = item.get("href", "").split("#", 1)[0]
            member = posixpath.normpath(posixpath.join(posixpath.dirname(package_path), unquote(href)))
            parser = _BookHTML()
            parser.feed(_decode(archive.read(member), path))
            parser.close()
            texts.append("".join(parser.parts))
        if not texts:
            raise ValueError(f"EPUB 没有可读取的正文 spine：{path}")
        return "\n\n".join(texts)


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines = []
    for paragraph in document.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t")).strip()
        if not text:
            continue
        style = paragraph.find(f"{namespace}pPr/{namespace}pStyle")
        style_name = style.get(namespace + "val", "") if style is not None else ""
        if re.match(r"(?:heading|标题)[1-6]$", style_name, re.IGNORECASE):
            text = "# " + text
        lines.append(text)
    return "\n\n".join(lines)


def _read_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        return _decode(path.read_bytes(), path)
    if suffix == ".epub":
        return _read_epub(path)
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pdf":
        raise ValueError("PDF 请先转换为 TXT；可用 pdftotext -layout，扫描版需要先 OCR")
    if suffix == ".mobi":
        raise ValueError("MOBI 请先用 Calibre 的 ebook-convert 转换为 EPUB 或 TXT")
    raise ValueError(f"不支持 {suffix or '无扩展名'} 格式：{path}；支持 TXT、Markdown、EPUB、DOCX")


def _split_book(texts: list[str]) -> tuple[list[dict], list[dict]]:
    sections: list[tuple[str, list[str]]] = []
    title, body, pending = "正文", [], []
    saw_heading = False

    def flush_paragraph() -> None:
        if pending:
            body.append(" ".join(pending).strip())
            pending.clear()

    def flush_section() -> None:
        nonlocal body
        flush_paragraph()
        if body:
            sections.append((title, body))
            body = []

    for text in texts:
        lines = text.splitlines()
        has_blank_paragraphs = any(not line.strip() for line in lines[1:-1])
        for raw in lines:
            line = raw.strip()
            if not line:
                flush_paragraph()
            elif len(line) <= 100 and CHAPTER.fullmatch(line):
                flush_section()
                title = re.sub(r"^#{1,6}\s+", "", line)
                saw_heading = True
            else:
                pending.append(line)
                if not has_blank_paragraphs:
                    flush_paragraph()
        flush_paragraph()
    flush_section()
    if not saw_heading and len(sections) == 1:
        grouped: list[tuple[str, list[str]]] = []
        chunk, size = [], 0
        for paragraph in sections[0][1]:
            chunk.append(paragraph)
            size += len(re.sub(r"\s", "", paragraph))
            if size >= 6000:
                grouped.append((f"第{len(grouped) + 1}部分", chunk))
                chunk, size = [], 0
        if chunk:
            grouped.append(("正文" if not grouped else f"第{len(grouped) + 1}部分", chunk))
        sections = grouped
    paragraphs, chapters = [], []
    for number, (chapter_title, content) in enumerate(sections, 1):
        chapter_id = f"ch{number:02d}"
        ids = []
        for paragraph in content:
            pid = f"p{len(paragraphs) + 1:05d}"
            paragraphs.append({"id": pid, "chapter": chapter_id, "text": paragraph})
            ids.append(pid)
        chapters.append({"id": chapter_id, "title": chapter_title, "paragraphs": ids,
                         "chars": sum(len(re.sub(r"\s", "", p)) for p in content)})
    if not paragraphs:
        raise ValueError("原文为空或只有标题，未生成正文；已有原文不会被修改")
    return paragraphs, chapters


def _current_generation(project: Path) -> str | None:
    pointer = project / "source" / "current.json"
    if not pointer.exists():
        return None
    data = json.loads(pointer.read_text(encoding="utf-8"))
    generation = data.get("generation") if isinstance(data, dict) else None
    if not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{64}", generation):
        raise ValueError("source/current.json 损坏；请检查原文索引，未修改已有数据")
    return generation


@contextmanager
def _import_lock(source: Path):
    source.mkdir(parents=True, exist_ok=True)
    # Keep the lock inode in place; unlinking it can let concurrent importers lock different files.
    with (source / ".import.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("本项目已有原文导入正在提交，请稍后重试") from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def ingest(project: Path, files: list[Path]) -> dict:
    """Import all inputs successfully before atomically selecting their generation."""
    project = Path(project)
    if not files:
        raise ValueError("至少提供一个原文文件")
    texts, inputs = [], []
    for file in files:
        path = Path(file)
        text = _read_source(path).replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise ValueError(f"原文文件为空：{path}")
        texts.append(text)
        inputs.append({"path": str(path.resolve()), "name": path.name,
                       "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    # Optional language declaration for foreign-language projects. This is a
    # conservative sanity check: mixed books and CJK names are allowed.
    project_cfg = {}
    cfg_path = project / "project.yaml"
    if cfg_path.exists():
        try:
            import yaml
            project_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            project_cfg = {}
    declared = (project_cfg.get("source", {}) or {}).get("language")
    if not declared:
        declared = (project_cfg.get("book", {}) or {}).get("language", "")
    if declared and str(declared).lower().startswith(("de", "en", "fr", "es", "it")):
        sample = "".join(texts)
        latin = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]", sample))
        cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
        if cjk > latin * 2 and cjk > 50:
            raise ValueError(f"project.yaml 声明原文语言为 {declared}，但导入文本主要为中文；请核对 translation_mode")
    digest_input = json.dumps({"parser": PARSER_VERSION, "texts": texts}, ensure_ascii=False).encode("utf-8")
    generation = hashlib.sha256(digest_input).hexdigest()
    current = _current_generation(project)
    target = project / "source" / "imports" / generation
    if current == generation:
        for name in ("paragraphs.jsonl", "chapters.json", "manifest.json"):
            if not (target / name).is_file():
                raise ValueError(f"当前原文版本缺少 {name}；请检查导入数据")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        return {**manifest, "status": "unchanged"}
    if current is not None:
        raise ValueError("本项目已有不同的原文版本；为避免段落证据错绑，请新建项目后导入")
    paragraphs, chapters = _split_book(texts)
    manifest = {"generation": generation, "parser_version": PARSER_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(), "sources": inputs,
                "chapters": len(chapters), "paragraphs": len(paragraphs),
                "chars": sum(chapter["chars"] for chapter in chapters)}
    with _import_lock(project / "source"):
        latest = _current_generation(project)
        if latest is not None and latest != generation:
            raise ValueError("导入期间原文版本已变化；未覆盖当前原文，请重新检查项目")
        if latest == generation:
            return {**json.loads((target / "manifest.json").read_text(encoding="utf-8")), "status": "unchanged"}
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=target.parent))
        try:
            atomic_write(staging / "paragraphs.jsonl", "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in paragraphs))
            write_json(staging / "chapters.json", chapters)
            write_json(staging / "manifest.json", manifest)
            # A completed orphan may remain after a crash before current.json was updated.
            if target.exists():
                for name in ("paragraphs.jsonl", "chapters.json"):
                    if (target / name).read_bytes() != (staging / name).read_bytes():
                        raise ValueError("同一原文版本的缓存不一致，未修改当前原文")
                manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            else:
                os.replace(staging, target)
            write_json(project / "source" / "current.json", {"generation": generation})
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {**manifest, "status": "imported"}
