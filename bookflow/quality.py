"""Deterministic diagnostics; semantic quality is assessed separately."""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from .common import (PROD, ROOT, count_chars, expand_evidence, find_project, fmt_time,
                     latest_draft, load_config, load_yaml, parse_draft, read_paragraphs,
                     sha256_file, source_generation, validate_evidence)
from .sentences import parse_draft_file

LONG_DESCRIPTOR = re.compile(
    r"(?:那个|那位|这位|这个|穿着|戴着)[\u4e00-\u9fff]{4,16}?"
    r"(?:男人|女人|男孩|女孩|老人|孩子|警察|医生|司机|母亲|父亲|朋友|邻居|人)"
)
ARABIC_YEAR = re.compile(r"(?<!\d)[1-9]\d{3}年")
CHINESE_YEAR = re.compile(r"[一二三四五六七八九〇零]{4}年")


def _book_rule_scope(rule: dict, ep: int) -> bool:
    origin = rule.get("origin_ep")
    if type(origin) is not int or origin < 1:
        raise ValueError("书级 lint 规则缺少正整数 origin_ep；不能默认作用全书")
    applies = rule.get("applies_to")
    if applies is None:
        return ep == origin
    if applies == "all":
        targets = None
        broad = True
    elif isinstance(applies, list) and applies and all(type(item) is int and item > 0 for item in applies):
        targets = set(applies)
        broad = targets != {origin}
    elif (isinstance(applies, dict) and type(applies.get("from")) is int and applies["from"] > 0
          and type(applies.get("to")) is int and applies["to"] >= applies["from"]):
        targets = set(range(applies["from"], applies["to"] + 1))
        broad = targets != {origin}
    else:
        raise ValueError("书级 lint 规则 applies_to 必须是集号列表、{from,to} 或 all")
    if broad and not str(rule.get("scope_evidence", "")).strip():
        raise ValueError("跨集 lint 规则缺少用户原话依据 scope_evidence")
    if applies == "all" and not re.search(r"全季|所有|以后|每一集|每集|每次|全部", str(rule["scope_evidence"])):
        raise ValueError("全季 lint 规则的原话依据没有明确全季或以后适用意图")
    return targets is None or ep in targets


def _sentence_exception(project: Path | None, path: Path, sentence: dict) -> str | None:
    if project is None:
        return None
    match = re.fullmatch(r"ep0*([1-9][0-9]*)", path.parent.name)
    if not match:
        return None
    data = load_yaml(project / "feedback/sentence_exceptions.yaml", {}) or {}
    rows = data.get("exceptions", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("episode")) is not int
                or row["episode"] != int(match.group(1))
                or row.get("sentence_id") != sentence["id"]
                or row.get("sentence") != sentence["text"]
                or not isinstance(row.get("id"), str) or not row["id"].strip()
                or not isinstance(row.get("evidence"), str) or not row["evidence"].strip()
                or not isinstance(row.get("audio"), str) or not row["audio"].strip()
                or not isinstance(row.get("audio_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["audio_sha256"])):
            continue
        audio = Path(row["audio"])
        if audio.is_absolute():
            continue
        audio_path = (project / audio).resolve()
        if (audio_path.is_relative_to(project.resolve()) and audio_path.is_file()
                and sha256_file(audio_path) == row["audio_sha256"]):
            return row["id"]
    return None


def lint(path: Path) -> dict:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    draft = parse_draft(raw)
    project = find_project(path)
    cfg = load_config(project)
    story = cfg.get("profile") == "story"
    rules = cfg.get("lint", {})
    rate = cfg["format"]["speech_rate_cpm"]
    if not isinstance(rate, (float, int)) or rate <= 0:
        raise ValueError("语速必须为正数")
    seconds = draft["total_chars"] * 60 / rate
    lo, hi = cfg["format"]["episode_minutes"]
    items = []

    def add(level, rule, message, line=None):
        items.append({"level": level, "rule": rule, "message": message, "line": line})

    if project:
        from .names import check_text
        for hit in check_text(project, draft["spoken"]):
            add("error", "deprecated_name", f"旧称 {hit['old']} 应统一为 {hit['replacement']}（{hit['entity_id']}）")

    if not draft["total_chars"]:
        add("error", "empty", "稿件没有口播正文")
    elif not lo * 60 * 0.95 <= seconds <= hi * 60 * 1.05:
        add("warn" if story else "error", "duration", f"估算 {fmt_time(seconds)}，目标 {lo}–{hi} 分钟")
    if re.search(r"\bTODO\b|待核实|待补充", raw, re.I):
        add("error", "unresolved", "稿件仍有待核实或待补充事项（包括隐藏备注）")
    if re.search(r"〔[^〕]*〕|\[钩子[^\]]*\]", draft["spoken"]):
        add("error", "unknown_marker", "正文中存在无法识别的标记")
    lengths = []
    for sentence in draft["sentences"]:
        n, text, line = sentence["chars"], sentence["text"], sentence["line"]
        lengths.append(n)
        if "〇" in text:
            add("error", "year_zero_pronunciation", "口播中的“〇”请写作“零”，避免年份被配音误读", line)
        if n > rules.get("sentence_error", 55):
            exception = _sentence_exception(project, path, sentence)
            if exception:
                add("warn", "sentence_exception", f"单句 {n} 字，按 {exception} 保留，仍需人工试听：{text[:36]}", line)
            else:
                add("error", "sentence", f"单句 {n} 字：{text[:36]}", line)
        elif n > rules.get("sentence_warn", 35):
            add("warn", "sentence", f"单句 {n} 字，建议拆开", line)
        if any(count_chars(part) > rules.get("clause_warn", 20) for part in re.split(r"[，,:：；;、]", text)):
            add("warn", "clause", f"连续片段较长，请试读：{text[:36]}", line)
        if re.search(r"[（(].*?[）)]|——|/|\d+%|[A-Za-z]{2,}", text):
            add("warn", "audio", f"确认符号、数字或外文念法：{text[:36]}", line)
    specs = list(load_yaml(ROOT / "style/banned.yaml").get("rules", []))
    book_rules = project / "feedback/lint_rules.yaml" if project else None
    if book_rules is not None and book_rules.is_file():
        data = load_yaml(book_rules, {}) or {}
        rows = data.get("rules") if isinstance(data, dict) else None
        match = re.fullmatch(r"ep([1-9][0-9]*)", path.parent.name)
        episode = int(match.group(1)) if match else draft["meta"].get("episode")
        if not isinstance(rows, list):
            add("error", "book_lint_rules", "feedback/lint_rules.yaml 必须包含 rules 列表")
        elif type(episode) is not int or episode < 1:
            add("error", "book_lint_rules", "书级 lint 规则需要可核对的正整数集号")
        elif match and draft["meta"].get("episode") is not None and draft["meta"]["episode"] != episode:
            add("error", "book_lint_rules", "稿件元数据集号与目录集号不一致")
        else:
            for spec in rows:
                if (not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not spec["id"].strip()
                        or type(spec.get("max")) is not int or spec["max"] < 0
                        or spec.get("level") not in ("warn", "error")
                        or spec.get("scope") not in (None, "opening", "ending")
                        or ("window" in spec and (type(spec["window"]) is not int or spec["window"] < 1))
                        or not isinstance(spec.get("patterns"), list) or not spec["patterns"]
                        or any(not isinstance(pattern, str) or not pattern for pattern in spec["patterns"])):
                    add("error", "book_lint_rules", "书级 lint 规则需包含 id、level、max、非空 patterns 和有效 scope")
                    continue
                try:
                    for pattern in spec["patterns"]:
                        re.compile(pattern)
                except re.error:
                    add("error", "book_lint_rules", f"规则 {spec['id']} 包含无效正则表达式")
                    continue
                try:
                    if _book_rule_scope(spec, episode):
                        specs.append(spec)
                except ValueError as exc:
                    add("error", "book_lint_rules", f"规则 {spec['id']}：{exc}")
    for spec in specs:
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
    if not story and not hooks:
        add("error", "hook", "缺少钩子标记，尚不能进行独立对齐")
    elif not story:
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
    if not story and not cmin <= commentary <= cmax:
        add("warn", "commentary", f"标注的个人解读占 {commentary:.1%}；仅供观察，不代表精讲深度")
    dialogue_chars = sum(count_chars(match.group(1)) for match in re.finditer(r"“([^”]*)”", draft["spoken"], re.S))
    dialogue_ratio = dialogue_chars / denominator
    if story:
        baseline = load_yaml(ROOT / "config/story_baseline.yaml", {}) or {}
        ratio_baseline = baseline.get("dialogue_ratio", {}) if isinstance(baseline, dict) else {}
        floor = ratio_baseline.get("warn_below") if isinstance(ratio_baseline, dict) else None
        if isinstance(floor, (int, float)) and not isinstance(floor, bool) and dialogue_ratio < floor:
            label = "候选基线（未获用户定稿批准）" if baseline.get("status") == "provisional_not_user_approved" else "故事基线"
            add("warn", "dialogue_ratio", f"直接对白占比 {dialogue_ratio:.1%} 低于{label} {floor:.1%}；请核对场景需要，不为达标硬加对白")
        without_dialogue = re.sub(r"“[^”]*”|「[^」]*」", "", PROD.sub("", draft["body"]), flags=re.S)
        for phrase in ("我们知道", "上一集", "上集", "下一期", "小说里", "这本书"):
            if phrase in without_dialogue:
                add("warn", "meta_narration", f"旁白出现元话语：{phrase}")
        repeated = {match.group() for match in LONG_DESCRIPTOR.finditer(without_dialogue)}
        for phrase in sorted(repeated):
            count = without_dialogue.count(phrase)
            if count >= 3:
                add("warn", "long_descriptor", f"长描述性称呼「{phrase}」出现 {count} 次；核对能否改用名字")
    stats = {"chars": draft["total_chars"], "estimated_seconds": round(seconds), "estimated_time": fmt_time(seconds),
             "speech_rate_cpm": rate, "sentences": len(lengths), "quote_ratio": round(ratio, 4),
             "commentary_ratio": round(commentary, 4), "dialogue_ratio": round(dialogue_ratio, 4), "hooks": len(hooks),
             "sentence_mean": round(statistics.mean(lengths), 1) if lengths else 0,
             "sentence_median": statistics.median(lengths) if lengths else 0}
    return {"passed": not any(i["level"] == "error" for i in items), "stats": stats, "items": items,
            "errors": [i["message"] for i in items if i["level"] == "error"],
            "warnings": [i["message"] for i in items if i["level"] == "warn"]}


def audit_story_season(project: Path, *, prefer_final: bool = True) -> dict:
    """Read-only cross-episode checks on final scripts or current working drafts."""
    project = Path(project).resolve()
    if not (project / "project.yaml").is_file():
        raise ValueError("story-check 需要有效书目项目")
    profile = load_config(project).get("profile")
    plan = load_yaml(project / "plan/episodes.yaml", {}) or {}
    planned = plan.get("episodes") if isinstance(plan, dict) else plan
    if (not isinstance(planned, list) or not planned
            or any(not isinstance(row, dict) or type(row.get("ep")) is not int or row["ep"] < 1
                   for row in planned)):
        raise ValueError("story-check 需要含有效集号的 plan/episodes.yaml")
    numbers = [row["ep"] for row in planned]
    if len(numbers) != len(set(numbers)):
        raise ValueError("story-check 的分集编号不能重复")
    items, episodes, spoken = [], [], {}

    def add(level: str, rule: str, message: str) -> None:
        items.append({"level": level, "rule": rule, "message": message})

    for number in numbers:
        script = latest_draft(project / "episodes" / f"ep{number:02d}", prefer_final=prefer_final)
        if script is None:
            add("error", "missing_episode", f"ep{number:02d} 缺少定稿或工作稿；不能计算全季指标")
            continue
        parsed = parse_draft(script.read_text(encoding="utf-8"))
        spoken[number] = parsed["spoken"]
        episodes.append({"episode": number, "path": str(script.relative_to(project)),
                         "chars": parsed["total_chars"]})
    all_spoken = "\n".join(spoken.values())
    if ARABIC_YEAR.search(all_spoken) and CHINESE_YEAR.search(all_spoken):
        add("error", "year_style", "全季同时使用阿拉伯数字与中文数字年份；请统一写法")

    threads_data = load_yaml(project / "analysis/threads.yaml", []) or []
    threads = threads_data.get("threads", []) if isinstance(threads_data, dict) else threads_data
    if not isinstance(threads, list):
        raise ValueError("analysis/threads.yaml 的 threads 必须是列表")
    for thread in threads:
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            continue
        thread_id = thread["id"]
        targets = [(row["ep"], field) for row in planned for field in ("threads_setup", "threads_payoff")
                   if isinstance(row.get(field), list) and thread_id in row[field]]
        if not targets:
            continue
        key = thread.get("spoken_key")
        if not isinstance(key, str) or not key.strip():
            if len({ep for ep, _ in targets}) > 1:
                add("warn", "spoken_key", f"线索 {thread_id} 跨集埋收但缺少 spoken_key；不能核对同词呼应")
            continue
        key = key.strip()
        for ep, field in targets:
            if ep in spoken and key not in spoken[ep]:
                action = "埋点" if field == "threads_setup" else "回收"
                add("warn", "spoken_key", f"线索 {thread_id} 在 ep{ep:02d} {action}未出现 spoken_key「{key}」")

    stats = {"episodes_planned": len(numbers), "episodes_checked": len(episodes)}
    if len(episodes) == len(numbers) and len(episodes) >= 3:
        lengths = [row["chars"] for row in episodes]
        mean = statistics.mean(lengths)
        if mean > 0:
            cv = statistics.pstdev(lengths) / mean
            stats["episode_chars_cv"] = round(cv, 4)
            baseline = load_yaml(ROOT / "config/story_baseline.yaml", {}) or {}
            limits = baseline.get("episode_chars", {}) if isinstance(baseline, dict) else {}
            floor = limits.get("uniform_warn_below") if isinstance(limits, dict) else None
            if isinstance(floor, (int, float)) and not isinstance(floor, bool) and 0 <= floor <= 1 and cv < floor:
                label = "候选基线（未获用户定稿批准）" if baseline.get("status") == "provisional_not_user_approved" else "故事基线"
                add("warn", "episode_length_distribution",
                    f"全季集长变异系数 {cv:.1%} 低于{label} {floor:.1%}；请检查是否机械凑齐字数")
    errors = [item["message"] for item in items if item["level"] == "error"]
    warnings = [item["message"] for item in items if item["level"] == "warn"]
    return {"passed": not errors, "status": "error" if errors else "warning" if warnings else "success",
            "summary": f"剧情全季检查：{len(episodes)}/{len(numbers)} 集，错误 {len(errors)}，提醒 {len(warnings)}",
            "project_profile": profile,
            "episodes": episodes, "stats": stats, "items": items,
            "errors": errors, "warnings": warnings}


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
    if load_config(project).get("profile") == "story":
        if quote_body.count("“") != quote_body.count("”"):
            errors.append("场景对白的中文双引号不配对")
        for paragraph in re.split(r"\n\s*\n", quote_body):
            if re.search(r"“[^”]+”", paragraph, re.S) and not re.search(r"〔据\s*[^〕]*p\d{5,}[^〕]*〕", paragraph):
                errors.append("场景对白所在段缺少〔据 p编号〕原文依据")
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
    draft = parse_draft_file(path)
    cfg = load_config(find_project(path))
    rate = cfg["format"]["speech_rate_cpm"]
    return "\n".join(f"[{s['id']} {fmt_time(s['offset'] * 60 / rate)}] {s['text']}" for s in draft["sentences"]) + "\n"
