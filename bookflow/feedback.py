"""Offline human editing copies, evidence-preserving diffs and style context."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from xml.etree import ElementTree as ET

from .common import ROOT, atomic_write, find_project, load_yaml, parse_draft, sha256_file, source_generation, write_json, write_yaml

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DC = "http://purl.org/dc/elements/1.1/"
NS = {"w": W, "dc": DC}


def tag(name: str) -> str:
    return f"{{{W}}}{name}"


def el(parent, name: str, text: str | None = None, **attrs):
    node = ET.SubElement(parent, tag(name), {tag(k): str(v) for k, v in attrs.items()})
    node.text = text
    return node


def xml(node) -> bytes:
    return ET.tostring(node, encoding="utf-8", xml_declaration=True)


def compact(text: str) -> str:
    # Spaces inside a name or foreign phrase remain significant; paragraph breaks do not.
    return text.replace("\n", "").replace("\r", "")


def make_blocks(parsed: dict) -> list[dict]:
    blocks = []
    for number, text in parsed["lines"]:
        sentences = [s for s in parsed["sentences"] if s["line"] == number]
        section = sentences[0]["section"] if sentences else ""
        if blocks and blocks[-1]["section"] == section and len(blocks[-1]["text"]) + len(text) <= 135:
            block = blocks[-1]
            block["text"] += text
        else:
            block = {"id": f"b{len(blocks)+1:03d}", "section": section, "text": text,
                     "source_lines": [], "source_sentence_ids": []}
            blocks.append(block)
        block["source_lines"].append(number)
        block["source_sentence_ids"].extend(s["id"] for s in sentences)
    offset = 0
    for block in blocks:
        block.update(start=offset, end=offset + len(block["text"]))
        offset = block["end"]
    return blocks


def write_docx(path: Path, title: str, identity: str, blocks: list[dict]) -> None:
    """Minimal OOXML keeps the editing CLI dependency-free (apart from existing YAML)."""
    document = ET.Element(tag("document"))
    body = el(document, "body")

    def paragraph(text, style):
        p = el(body, "p")
        el(el(p, "pPr"), "pStyle", val=style)
        t = el(el(p, "r"), "t", text)
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    paragraph("口播稿 · 人工编辑版", "Title")
    paragraph(title, "Subtitle")
    paragraph("直接修改下面的正文即可；不用开启修订。想说明理由，可以在相应文字上加批注。", "Instructions")
    paragraph("改好后另存为 .docx 发回来。小节标题不参与口播；请保留标题样式，正文沿用正文样式。", "Instructions")
    previous = None
    for block in blocks:
        if block["section"] != previous and block["section"]:
            paragraph(block["section"], "Heading1")
        paragraph(block["text"], "Normal")
        previous = block["section"]
    section = el(body, "sectPr")
    footer_ref = el(section, "footerReference", type="default")
    footer_ref.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "rId2")
    el(section, "pgSz", w=12240, h=15840)
    el(section, "pgMar", top=1080, right=1080, bottom=1080, left=1080, header=540, footer=540)
    styles = ET.Element(tag("styles"))
    for name, size, bold in [("Normal", 32, False), ("Title", 44, True), ("Subtitle", 30, False),
                             ("Instructions", 22, False), ("Heading1", 34, True)]:
        style = el(styles, "style", type="paragraph", styleId=name)
        el(style, "name", val={"Heading1": "heading 1", "Normal": "Normal"}.get(name, name))
        if name == "Normal":
            style.set(tag("default"), "1")
        props = el(style, "pPr")
        el(props, "spacing", before=260 if name == "Heading1" else 0,
           after=160 if name == "Normal" else 120, line=360, lineRule="auto")
        el(props, "widowControl")
        if name != "Normal":
            el(props, "keepNext")
        if name == "Heading1":
            el(props, "outlineLvl", val=0)
        run = el(style, "rPr")
        el(run, "rFonts", ascii="Arial", hAnsi="Arial", eastAsia="PingFang SC")
        el(run, "sz", val=size)
        el(run, "szCs", val=size)
        el(run, "color", val="000000" if name != "Instructions" else "555555")
        if bold:
            el(run, "b")
    footer = ET.Element(tag("ftr"))
    p = el(footer, "p")
    el(el(p, "pPr"), "jc", val="center")
    el(el(p, "r"), "t", "编辑稿 · ")
    el(el(el(p, "fldSimple", instr="PAGE"), "r"), "t", "1")
    cp = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    core = ET.Element(f"{{{cp}}}coreProperties")
    ET.SubElement(core, f"{{{DC}}}identifier").text = identity
    ET.SubElement(core, f"{{{DC}}}title").text = title
    relns = "http://schemas.openxmlformats.org/package/2006/relationships"

    def relationships(items):
        # Office package metadata needs the conventional default namespace for LO detection.
        root = ET.Element("Relationships", xmlns=relns)
        for rid, kind, target in items:
            ET.SubElement(root, "Relationship", Id=rid, Type=kind, Target=target)
        return xml(root)

    office = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    types = ET.Element("Types", xmlns=ct)
    for ext, mime in [("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("xml", "application/xml")]:
        ET.SubElement(types, "Default", Extension=ext, ContentType=mime)
    for part, kind in [("document", "document.main"), ("styles", "styles"), ("footer1", "footer")]:
        ET.SubElement(types, "Override", PartName=f"/word/{part}.xml", ContentType=f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml")
    ET.SubElement(types, "Override", PartName="/docProps/core.xml", ContentType="application/vnd.openxmlformats-package.core-properties+xml")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in {
            "[Content_Types].xml": xml(types), "word/document.xml": xml(document),
            "word/styles.xml": xml(styles), "word/footer1.xml": xml(footer), "docProps/core.xml": xml(core),
            "_rels/.rels": relationships([("rId1", office + "officeDocument", "word/document.xml"),
                ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml")]),
            "word/_rels/document.xml.rels": relationships([("rId1", office + "styles", "styles.xml"), ("rId2", office + "footer", "footer1.xml")]),
        }.items():
            archive.writestr(name, data)


def write_markdown(path: Path, title: str, blocks: list[dict]) -> None:
    """Write a lightweight editing copy whose visible body is easy to diff."""
    lines = [f"# {title}", "", "> 直接修改下面的正文；小节标题用于定位，不参与口播。", ""]
    previous = None
    for block in blocks:
        if block["section"] != previous and block["section"]:
            lines += [f"## {block['section']}", ""]
        lines += [block["text"], ""]
        previous = block["section"]
    atomic_write(path, "\n".join(lines).rstrip() + "\n")


def read_markdown(path: Path) -> dict:
    """Read an editing Markdown copy, ignoring its title and instructions."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            text = text[end + 4:]
    headings, paragraphs = [], []
    current = ""
    pending = []
    def flush():
        if pending:
            value = "".join(pending).strip()
            if value:
                paragraphs.append(value)
            pending.clear()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--") or stripped.startswith(">"):
            flush()
            continue
        if stripped.startswith("## "):
            flush()
            current = stripped[3:].strip()
            headings.append(current)
            continue
        if stripped.startswith("# "):
            flush()
            continue
        pending.append(stripped)
    flush()
    return {"identity": "", "paragraphs": paragraphs, "headings": headings,
            "comments": {}, "tracked_changes": False}


def create_copy(draft: Path, output: Path, format: str = "both") -> dict:
    draft, output = draft.resolve(), output.resolve()
    project = find_project(draft)
    if not project:
        raise ValueError("稿件不在书目项目中")
    if output.exists():
        raise ValueError("编辑目录已存在；请使用新目录，避免覆盖人工修改")
    parsed = parse_draft(draft.read_text(encoding="utf-8"))
    blocks = make_blocks(parsed)
    if not blocks:
        raise ValueError("稿件没有口播正文")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".edit-", dir=output.parent) as tmp:
        stage = Path(tmp) / "copy"
        stage.mkdir()
        identity = "bookflow:" + str(uuid.uuid4())
        title = parsed["meta"].get("title_working", draft.stem)
        docx = stage / "口播稿_请在这里修改.docx"
        markdown = stage / "口播稿_请在这里修改.md"
        if format not in {"md", "docx", "both"}:
            raise ValueError("编辑稿格式必须为 md、docx 或 both")
        if format in {"docx", "both"}:
            write_docx(docx, title, identity, blocks)
        if format in {"md", "both"}:
            write_markdown(markdown, title, blocks)
        shutil.copyfile(draft, stage / "original.md")
        write_json(stage / "baseline.json", {
            "schema": 1, "identity": identity, "project": str(project),
            "draft": str(draft), "draft_sha256": sha256_file(draft),
            "source_generation": source_generation(project), "episode": parsed["meta"].get("episode"),
            "editor_format": format,
            "docx_sha256": sha256_file(docx) if docx.is_file() else None,
            "markdown_sha256": sha256_file(markdown) if markdown.is_file() else None,
            "blocks": blocks,
        })
        stage.rename(output)
    result = {"baseline": str(output / "baseline.json"), "blocks": len(blocks), "status": "awaiting_human_edit"}
    if (output / docx.name).is_file():
        result["docx"] = str(output / docx.name)
    if (output / markdown.name).is_file():
        result["markdown"] = str(output / markdown.name)
    return result


def read_docx(path: Path) -> dict:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效 DOCX") from exc
    with archive:
        if sum(i.file_size for i in archive.infolist()) > 50_000_000:
            raise ValueError("编辑稿解压后超过 50 MB，请去掉无关图片后另存")
        def read(name):
            try:
                return ET.fromstring(archive.read(name))
            except (KeyError, ET.ParseError) as exc:
                raise ValueError(f"DOCX 结构无效：{name}") from exc
        root = read("word/document.xml")
        style_names, style_parents = {}, {}
        if "word/styles.xml" in archive.namelist():
            for style in read("word/styles.xml").findall("w:style", NS):
                sid = style.get(tag("styleId"), "")
                name = style.find("w:name", NS)
                parent = style.find("w:basedOn", NS)
                style_names[sid] = name.get(tag("val"), sid) if name is not None else sid
                style_parents[sid] = parent.get(tag("val"), "") if parent is not None else ""

        def style_kind(sid):
            seen = set()
            while sid and sid not in seen:
                seen.add(sid)
                name = style_names.get(sid, sid).lower().replace(" ", "")
                if name in ("title", "subtitle", "instructions"):
                    return "metadata"
                if name.startswith("heading"):
                    return "heading"
                sid = style_parents.get(sid, "")
            return "body"
        core = read("docProps/core.xml") if "docProps/core.xml" in archive.namelist() else None
        identity = core.findtext("dc:identifier", default="", namespaces=NS) if core is not None else ""
        comments = {}
        if "word/comments.xml" in archive.namelist():
            for c in read("word/comments.xml").findall("w:comment", NS):
                comments[c.get(tag("id"))] = {"text": "\n".join("".join(p.itertext()) for p in c.findall("w:p", NS)),
                    "author": c.get(tag("author"), ""), "anchors": []}
        if root.find(".//w:txbxContent", NS) is not None or root.find(".//w:altChunk", NS) is not None:
            raise ValueError("发现文本框或嵌入文档，无法可靠提取；请将内容放入普通正文段落")
        paragraphs, headings, active = [], [], set()
        tracked = any(root.find(f".//w:{name}", NS) is not None for name in ("ins", "del", "moveFrom", "moveTo"))
        def walk(node):
            if node.tag in (tag("del"), tag("moveFrom"), tag("pPr"), tag("rPr")):
                return ""
            if node.tag == tag("commentRangeStart"):
                active.add(node.get(tag("id")))
            if node.tag == tag("commentRangeEnd"):
                active.discard(node.get(tag("id")))
            if node.tag == tag("t"):
                text = node.text or ""
                for cid in active:
                    if cid in comments:
                        comments[cid]["anchors"].append(text)
                return text
            if node.tag == tag("tab"):
                return "\t"
            if node.tag in (tag("br"), tag("cr")):
                return "\n"
            return "".join(walk(child) for child in node)
        body = root.find("w:body", NS)
        if body is None:
            raise ValueError("DOCX 没有正文")
        def visit(node):
            if node.tag in (tag("del"), tag("moveFrom")):
                return
            if node.tag == tag("p"):
                style = node.find("w:pPr/w:pStyle", NS)
                style_id = style.get(tag("val"), "Normal") if style is not None else "Normal"
                text = walk(node).strip()
                kind = style_kind(style_id)
                if kind == "metadata":
                    return
                if kind == "heading":
                    headings.append(text)
                elif text:
                    paragraphs.append(text)
                return
            for child in node:
                visit(child)
        visit(body)
        for comment in comments.values():
            comment["anchor"] = "".join(comment.pop("anchors"))
        return {"identity": identity, "paragraphs": paragraphs, "headings": headings,
                "comments": comments, "tracked_changes": tracked}


def compare(blocks: list[dict], paragraphs: list[str]) -> dict:
    before = "".join(b["text"] for b in blocks)
    after = compact("\n".join(paragraphs))
    changes = []
    for kind, i, j, k, l in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if kind == "equal":
            continue
        affected = [b["id"] for b in blocks if b["start"] < j and b["end"] > i]
        if not affected:
            affected = [b["id"] for b in blocks if b["start"] <= i <= b["end"]][:1]
        changes.append({"id": f"c{len(changes)+1:03d}", "kind": kind,
                        "blocks": affected, "before": before[i:j], "after": after[k:l],
                        "before_range": [i, j], "after_range": [k, l],
                        "context_before": before[max(0, i-35):i], "context_after": before[j:j+35]})
    return {"changes": changes, "text_changed": before != after,
            "paragraph_layout_changed": [b["text"] for b in blocks] != paragraphs,
            "revised_text": "\n\n".join(paragraphs) + "\n"}


def import_edits(docx: Path, baseline_path: Path) -> dict:
    docx, baseline_path = docx.resolve(), baseline_path.resolve()
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if sha256_file(baseline_path.parent / "original.md") != baseline["draft_sha256"]:
        raise ValueError("原稿快照校验失败，停止比较")
    original = parse_draft((baseline_path.parent / "original.md").read_text(encoding="utf-8"))
    if make_blocks(original) != baseline["blocks"]:
        raise ValueError("基线段落与原稿不符，停止比较")
    project = Path(baseline["project"])
    if not (project / "project.yaml").is_file():
        raise ValueError("基线所指书目项目不存在")
    is_markdown = docx.suffix.lower() in {".md", ".markdown"}
    edited = read_markdown(docx) if is_markdown else read_docx(docx)
    if edited["identity"] and edited["identity"] != baseline["identity"]:
        raise ValueError("编辑稿属于另一轮基线，停止比较")
    if not edited["paragraphs"]:
        raise ValueError("编辑稿正文为空，停止导入")
    report = compare(baseline["blocks"], edited["paragraphs"])
    from .names import feedback_candidates
    report["name_review_candidates"] = feedback_candidates(project, baseline["blocks"], report["changes"])
    original_headings = list(dict.fromkeys(b["section"] for b in baseline["blocks"] if b["section"]))
    warnings = []
    if not edited["identity"] and not is_markdown:
        warnings.append("文档身份标记丢失；本轮使用显式指定的基线，请核对是否同一集")
    if source_generation(project) != baseline["source_generation"]:
        warnings.append("原文版本已变化，须重新核对来源")
    if edited["tracked_changes"]:
        warnings.append("正文按接受文字插入/删除后的结果提取；修订作者归属和段落合并仍需人工核对")
    digest = sha256_file(docx)
    round_id = hashlib.sha256((baseline["identity"] + digest).encode()).hexdigest()[:20]
    destination = project / "feedback/rounds" / round_id
    if destination.exists():
        return {"round": str(destination), "status": "already_imported", "warnings": warnings}
    report.update(schema=1, round_id=round_id, episode=baseline.get("episode"),
                  baseline_identity=baseline["identity"], source_generation=baseline["source_generation"],
                  original_docx_sha256=baseline.get("docx_sha256"), edited_docx_sha256=digest if not is_markdown else None,
                  edited_file=str(docx), edited_file_sha256=digest,
                  comments=edited["comments"], headings_before=original_headings, headings_after=edited["headings"],
                  headings_changed=original_headings != edited["headings"], warnings=warnings,
                  status="pending_semantic_review", blocks=baseline["blocks"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feedback-", dir=destination.parent) as tmp:
        stage = Path(tmp) / "round"
        stage.mkdir()
        shutil.copyfile(docx, stage / ("edited.md" if is_markdown else "edited.docx"))
        shutil.copyfile(baseline_path, stage / "baseline.json")
        shutil.copyfile(baseline_path.parent / "original.md", stage / "original.md")
        atomic_write(stage / "revised.txt", report.pop("revised_text"))
        write_json(stage / "diff.json", report)
        lines = ["# 人工改稿对照", "", "状态：待语义归类、事实复核与新版审校；尚未定稿。", "",
                 f"正文差异 {len(report['changes'])} 处；批注 {len(report['comments'])} 条。", "",
                 "字符差异会把移位显示为删除和新增；学习前应合并阅读上下文。", ""]
        for c in report["changes"]:
            lines += [f"## {c['id']} · {'、'.join(c['blocks'])}", "",
                      "前文：" + c["context_before"], "", "改前：" + (c["before"] or "（此处新增）"), "",
                      "改后：" + (c["after"] or "（删除）"), "", "后文：" + c["context_after"], ""]
        for cid, c in report["comments"].items():
            lines += [f"## 批注 {cid}", "", "位置：" + c["anchor"], "", c["text"], ""]
        if report["name_review_candidates"]:
            lines += ["## 名称专项核对", ""]
            lines += [f"- {c['entity_id']} {c['current_name']}：差异 {', '.join(c['changes'])}，需核对是否改名并同步资料。"
                      for c in report["name_review_candidates"]]
        lines += ["## 结构与提示", "", f"段落布局变化：{report['paragraph_layout_changed']}", "",
                  f"标题变化：{report['headings_changed']}", "", *warnings]
        atomic_write(stage / "diff.md", "\n".join(lines) + "\n")
        write_yaml(stage / "learning.yaml", {"status": "pending_semantic_review", "round_id": round_id,
                   "items": [], "note": "仅保存真实修改；由 learn-feedback 结合上下文提炼，不能把模型推测当作用户理由。"})
        stage.rename(destination)
    return {"round": str(destination), "changes": len(report["changes"]), "comments": len(report["comments"]),
            "status": report["status"], "warnings": warnings}


def feedback_context(project: Path) -> dict:
    project = project.resolve()
    if not (project / "project.yaml").is_file():
        raise ValueError("书目项目不存在")
    active, candidates, errors = [], [], []
    for path, scope in [(ROOT / "style/personal.yaml", "workspace"), (project / "feedback/rules.yaml", "book")]:
        for rule in load_yaml(path).get("rules", []):
            label = rule.get("id", "无编号")
            if rule.get("scope") != scope or (scope == "workspace" and rule.get("kind") == "fact"):
                errors.append(f"{label}：适用范围无效；事实规则只属于本书")
                continue
            evidence = rule.get("evidence", [])
            valid = bool(evidence)
            for item in evidence:
                try:
                    relative = Path(item["report"])
                    report_path = (ROOT / relative).resolve()
                    if relative.is_absolute() or not report_path.is_relative_to(ROOT / "projects"):
                        raise ValueError("证据不在项目内")
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    if scope == "book" and not report_path.is_relative_to(project / "feedback/rounds"):
                        raise ValueError("本书规则引用其他书目")
                    if item.get("type") == "user_instruction":
                        if not isinstance(report, dict) or report.get("source") != "user_message":
                            raise ValueError("用户指令来源校验失败")
                        instructions = report.get("instructions")
                        if not isinstance(instructions, list) or not all(isinstance(i, dict) for i in instructions):
                            raise ValueError("用户指令记录无效")
                        if not isinstance(item.get("instruction"), str) or not item["instruction"].strip():
                            raise ValueError("用户指令编号无效")
                        matches = [i for i in instructions if i.get("id") == item["instruction"]]
                        if (len(matches) != 1 or not isinstance(matches[0].get("text"), str)
                                or not matches[0]["text"].strip() or sha256_file(report_path) != item["sha256"]):
                            raise ValueError("用户指令编号、正文或文件校验失败")
                    else:
                        ids = {c["id"] for c in report["changes"]} | {"comment:" + c for c in report["comments"]}
                        edited_path = report_path.parent / ("edited.docx" if (report_path.parent / "edited.docx").is_file() else "edited.md")
                        expected_digest = report.get("edited_file_sha256") or report.get("edited_docx_sha256")
                        if item["change"] not in ids or not edited_path.is_file() or sha256_file(edited_path) != expected_digest:
                            raise ValueError("证据编号或改稿校验失败")
                except (KeyError, ValueError, OSError, TypeError):
                    valid = False
            if not valid:
                errors.append(f"{label}：缺少可核对的真实改稿证据或用户指令，或证据校验失败")
                continue
            if rule.get("status") == "active":
                if not rule.get("instruction") or not rule.get("rationale"):
                    errors.append(f"{label}：缺少规则正文或生效依据")
                else:
                    active.append(rule)
            else:
                candidates.append(rule)
    pending = []
    for path in sorted((project / "feedback/rounds").glob("*/learning.yaml")):
        if load_yaml(path).get("status") != "reviewed":
            pending.append(str(path))
    from .names import check_project
    names = check_project(project)
    errors.extend(names["errors"])
    return {"active_rules": active, "candidate_rules": candidates, "pending_rounds": pending, "errors": errors,
            "name_registry": str(project / "analysis/characters.yaml"), "name_consistency": names,
            "instruction": "先处理真实反馈的归类；写稿应用 active_rules，候选不作强制要求。事实规则须回到原文核查。"}
