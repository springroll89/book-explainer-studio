"""Deterministic diagnostics; semantic quality is assessed separately."""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from .common import (PROD, ROOT, count_chars, expand_evidence, find_project, fmt_time,
                     load_config, load_yaml, parse_draft, read_paragraphs, source_generation, validate_evidence)


def lint(path: Path) -> dict:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    draft = parse_draft(raw)
    cfg = load_config(find_project(path))
    rules = cfg.get("lint", {})
    rate = cfg["format"]["speech_rate_cpm"]
    if not isinstance(rate, (float, int)) or rate <= 0:
        raise ValueError("语速必须为正数")
    seconds = draft["total_chars"] * 60 / rate
    lo, hi = cfg["format"]["episode_minutes"]
    items = []

    def add(level, rule, message, line=None):
        items.append({"level": level, "rule": rule, "message": message, "line": line})

    project = find_project(path)
    if project:
        from .names import check_text
        for hit in check_text(project, draft["spoken"]):
            add("error", "deprecated_name", f"旧称 {hit['old']} 应统一为 {hit['replacement']}（{hit['entity_id']}）")

    if not draft["total_chars"]:
        add("error", "empty", "稿件没有口播正文")
    elif not lo * 60 * 0.95 <= seconds <= hi * 60 * 1.05:
        add("error", "duration", f"估算 {fmt_time(seconds)}，目标 {lo}–{hi} 分钟")
    if re.search(r"\bTODO\b|待核实|待补充", raw, re.I):
        add("error", "unresolved", "稿件仍有待核实或待补充事项（包括隐藏备注）")
    if re.search(r"〔[^〕]*〕|\[钩子[^\]]*\]", draft["spoken"]):
        add("error", "unknown_marker", "正文中存在无法识别的标记")
    lengths = []
    for sentence in draft["sentences"]:
        n, text, line = sentence["chars"], sentence["text"], sentence["line"]
        lengths.append(n)
        if n > rules.get("sentence_error", 55):
            add("error", "sentence", f"单句 {n} 字：{text[:36]}", line)
        elif n > rules.get("sentence_warn", 35):
            add("warn", "sentence", f"单句 {n} 字，建议拆开", line)
        if any(count_chars(part) > rules.get("clause_warn", 20) for part in re.split(r"[，,:：；;、]", text)):
            add("warn", "clause", f"连续片段较长，请试读：{text[:36]}", line)
        if re.search(r"[（(].*?[）)]|——|/|\d+%|[A-Za-z]{2,}", text):
            add("warn", "audio", f"确认符号、数字或外文念法：{text[:36]}", line)
    for spec in load_yaml(ROOT / "style/banned.yaml").get("rules", []):
        body = draft["spoken"]
        window = spec.get("window", 120)
        if spec.get("scope") == "opening":
            body = body[:window]
        elif spec.get("scope") == "ending":
            body = body[-window:]
        hits = [m.group() for pattern in spec.get("patterns", []) for m in re.finditer(pattern, body)]
        if len(hits) > spec.get("max", 0):
            add(spec.get("level", "warn"), f"phrase:{spec['id']}", f"命中 {len(hits)} 次：{'、'.join(hits[:5])}")
    hooks = draft["hooks"]
    if not hooks:
        add("error", "hook", "缺少钩子标记，尚不能进行独立对齐")
    else:
        points = [h["offset"] * 60 / rate for h in hooks]
        if points[0] > rules.get("first_hook_sec", 30):
            add("error", "opening", f"第一个钩子在 {fmt_time(points[0])}")
        for start, end in zip(points, points[1:] + [seconds]):
            if end - start > rules.get("hook_gap_sec", 90):
                add("warn", "hook_gap", f"{fmt_time(start)}–{fmt_time(end)} 没有钩子标记")
    quote_chars = sum(count_chars(q["text"]) for q in draft["quotes"])
    denominator = draft["total_chars"] or 1
    ratio = quote_chars / denominator
    if ratio > cfg["quote"]["max_ratio"]:
        add("error", "quote_ratio", f"引用占比 {ratio:.1%}，超出项目编辑上限")
    for quote in draft["quotes"]:
        if count_chars(quote["text"]) > cfg["quote"]["max_single_chars"]:
            add("error", "quote_length", "单处引用超出项目编辑上限", quote["line"])
    commentary = draft["commentary_chars"] / denominator
    cmin, cmax = cfg["depth"]["commentary_ratio"]
    if not cmin <= commentary <= cmax:
        add("warn", "commentary", f"标注的个人解读占 {commentary:.1%}；仅供观察，不代表精讲深度")
    stats = {"chars": draft["total_chars"], "estimated_seconds": round(seconds), "estimated_time": fmt_time(seconds),
             "speech_rate_cpm": rate, "sentences": len(lengths), "quote_ratio": round(ratio, 4),
             "commentary_ratio": round(commentary, 4), "hooks": len(hooks),
             "sentence_mean": round(statistics.mean(lengths), 1) if lengths else 0,
             "sentence_median": statistics.median(lengths) if lengths else 0}
    return {"passed": not any(i["level"] == "error" for i in items), "stats": stats, "items": items,
            "errors": [i["message"] for i in items if i["level"] == "error"],
            "warnings": [i["message"] for i in items if i["level"] == "warn"]}


def verify_quotes(path: Path) -> dict:
    path = Path(path)
    project = find_project(path)
    if not project:
        raise ValueError("稿件必须放在包含 project.yaml 的项目内")
    parsed = parse_draft(path.read_text(encoding="utf-8"))
    paragraphs = read_paragraphs(project)
    external = load_yaml(project / "analysis/external_sources.yaml")
    errors = []
    declared_generation = parsed['meta'].get('source_generation')
    if declared_generation and declared_generation != source_generation(project):
        errors.append("稿件声明的原文版本与当前项目不一致，需重新核对依据")
    if parsed["cited_pids"]:
        errors.extend(validate_evidence(parsed["cited_pids"], paragraphs, external))
    quote_body = PROD.sub("", parsed["body"])
    if quote_body.count("「") != len(parsed["quotes"]) or quote_body.count("」") != len(parsed["quotes"]):
        errors.append("引文缺少有效的〔引 p编号〕标记或引号不配对")
    for quote in parsed["quotes"]:
        ref = quote["start"] if quote["start"] == quote["end"] else f"{quote['start']}-{quote['end']}"
        if str(quote['start']).startswith('Q'):
            bank=load_yaml(project/'analysis/quote_bank.yaml',{}) or {}; entries=bank.get('quotes',bank if isinstance(bank,list) else [])
            item=next((x for x in entries if isinstance(x,dict) and x.get('id')==quote['start']),None)
            if not item: errors.append(f"L{quote['line']} 引文 {ref} 不在 quote_bank.yaml")
            elif item.get('status')!='confirmed': errors.append(f"L{quote['line']} 引文 {ref} 尚未确认")
            elif item.get('zh')!=quote['text']: errors.append(f"L{quote['line']} 引文 {ref} 中文与引文库不一致")
            continue
        try:
            ids = expand_evidence(ref, paragraphs)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        source = re.sub(r"\s+", "", "".join(paragraphs[pid]["text"] for pid in ids))
        parts = [re.sub(r"\s+", "", x) for x in re.split(r"……|…|\.{3}", quote["text"]) if x.strip()]
        cursor = 0
        valid = bool(parts)
        for part in parts:
            at = source.find(part, cursor)
            if at < 0:
                valid = False
                break
            cursor = at + len(part)
        if not valid:
            errors.append(f"L{quote['line']} 引文与 {ref} 不符（只允许按原顺序省略）")
    return {"passed": not errors, "errors": errors, "warnings": [], "quotes_checked": len(parsed["quotes"]),
            "evidence_checked": len(parsed["cited_pids"])}


def listener_input(path: Path) -> str:
    path = Path(path)
    draft = parse_draft(path.read_text(encoding="utf-8"))
    cfg = load_config(find_project(path))
    rate = cfg["format"]["speech_rate_cpm"]
    return "\n".join(f"[{s['id']} {fmt_time(s['offset'] * 60 / rate)}] {s['text']}" for s in draft["sentences"]) + "\n"
