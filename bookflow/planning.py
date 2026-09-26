"""Deterministic coverage candidates and evidence-aware episode plan checks."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from .common import (
    expand_evidence,
    load_config,
    load_yaml,
    read_chapters,
    read_paragraphs,
    source_generation,
    validate_evidence,
    write_json,
)


LAYERS = {1, 2, 3, 4}
PID = re.compile(r"(?<![\w])p\d+(?![\w])")
AUTHOR_CHAPTER = re.compile(r"序|前言|引言|后记|跋|作者的话|致读者|Vorwort|Nachwort|Danksagung|Acknowledgments|Preface", re.IGNORECASE)
STOP = set("的了是在我你他她它们这那不也就都着过吗呢吧啊么把被给让和很还又再没要会能个说道得地之与及其而或")
COMMON_TERMS = {"一个", "自己", "什么", "知道", "已经", "时候", "开始", "起来", "出来", "地方", "事情", "问题", "声音", "眼睛", "第一", "第二"}
LATIN_STOP = set("""aber alle allem allen aller alles also auch auf aus bei bis dann dass dein deine dem den der des die dies diese dieser dieses doch dort durch ein eine einem einen einer eines er es etwas für ganz gegen gewesen hatte hatten haben habe hier hinter ich ihm ihn ihnen ihr ihre ihren ihrer im in ist jetzt kann kein keine konnte konnten man mehr mein meine mich mir mit muss nach nicht nichts noch nun nur oder ohne schon sehr sein seine seinen seiner sich sie sind sollte um und uns unter über vom von vor war waren was wenn werde werden wie wieder wir wird wurde wurden zum zur zwei sagte sagt sagen fragte fragt fragen wollte wollten würde würden hätte hätten the and that this with from have has had not was were into you your they their them she her his him been would could should""".split())
LATIN_STOP.update("""weil immer denn seinem ihrem können könnte könnten müssten musste mussten worden wäre wären ging gehen ging gingen selbst wissen wusste wussten paar gemacht machen damit stand stehen erwiderte seit deshalb warum dich ließ liess nein anderen anderem andere diesem diesen heute kurz sogar erst zwar wohl etwas wenig viele vielen sehr denn wirklich noch einmal plötzlich wieder zurück gerade fast dabei dabei darum darauf daran davon dazu dafür darüber darunter inzwischen mittlerweile überhaupt vielleicht schließlich schliesslich eigentlich antwortete nickte lächelte schaute blickte sah sahen sagte beiden beiden gegenübersetzt""".split())


def _load_threads(project: Path, errors: list[str]) -> list[dict]:
    data = load_yaml(project / "analysis" / "threads.yaml", [])
    if isinstance(data, dict):
        data = data.get("threads", [])
    if not isinstance(data, list):
        errors.append("analysis/threads.yaml 必须是线索列表或包含 threads 列表")
        return []
    threads, ids = [], set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            errors.append("每条线索必须包含非空字符串 id")
            continue
        if item["id"] in ids:
            errors.append(f"线索 id 重复：{item['id']}")
        ids.add(item["id"])
        threads.append(item)
    return threads


def _term_candidates(paragraphs: dict, analysis_text: str, chapter_count: int) -> list[dict]:
    counts: Counter = Counter()
    chapters: dict[str, set] = defaultdict(set)
    first = {}
    display = {}
    analysis_folded = analysis_text.casefold()
    total = sum(len(p["text"]) for p in paragraphs.values())
    threshold = max(4, total // 25000)
    for pid, paragraph in paragraphs.items():
        for segment in re.findall(r"[\u4e00-\u9fff]+", paragraph["text"]):
            for length in (2, 3, 4):
                for offset in range(len(segment) - length + 1):
                    term = segment[offset:offset + length]
                    if STOP.intersection(term) or term in COMMON_TERMS:
                        continue
                    counts[term] += 1
                    chapters[term].add(paragraph["chapter"])
                    first.setdefault(term, pid)
        for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{4,}", paragraph["text"]):
            term = word.casefold()
            if term in LATIN_STOP:
                continue
            counts[term] += 1
            chapters[term].add(paragraph["chapter"])
            first.setdefault(term, pid)
            display.setdefault(term, word)
    minimum_chapters = 2 if chapter_count > 2 else 1
    candidates = [term for term, count in counts.items()
                  if count >= threshold and len(chapters[term]) >= minimum_chapters and term.casefold() not in analysis_folded]
    retained = []
    for term in sorted(candidates, key=lambda value: (-len(value), -counts[value], value)):
        if any(term in longer and counts[longer] >= counts[term] * 0.7 for longer in retained):
            continue
        retained.append(term)
    return [{"term": display.get(term, term), "count": counts[term], "chapters": sorted(chapters[term]), "first": first[term]}
            for term in sorted(retained, key=lambda value: (-counts[value], value))[:40]]


def coverage(project: Path) -> dict:
    """Regenerate machine findings without changing the separate human review."""
    project = Path(project)
    paragraphs, chapters = read_paragraphs(project), read_chapters(project)
    errors, warnings = [], []
    analysis = project / "analysis"
    if not paragraphs or not chapters:
        errors.append("原文段落或章节为空，请先成功导入原文")
    for chapter in chapters:
        note = analysis / "chapter_notes" / f"{chapter['id']}.md"
        if not note.is_file():
            errors.append(f"{chapter['id']}（{chapter['title']}）缺少章节笔记")
            continue
        references = set(PID.findall(note.read_text(encoding="utf-8")))
        for pid in sorted(references - paragraphs.keys()):
            errors.append(f"{chapter['id']} 笔记引用的 {pid} 不存在")
        if not references.intersection(chapter["paragraphs"]):
            errors.append(f"{chapter['id']} 笔记没有引用本章段落")
    threads = _load_threads(project, errors)
    hits: set[str] = set()
    for thread in threads:
        tid, setup, payoff = thread["id"], thread.get("setup"), thread.get("payoff")
        if not isinstance(setup, list) or not setup:
            errors.append(f"线索 {tid} 缺少 setup 段落列表")
            setup = []
        errors.extend(f"线索 {tid} setup：{error}" for error in validate_evidence(setup, paragraphs))
        if payoff == "none" or payoff == ["none"]:
            if not thread.get("note"):
                warnings.append(f"线索 {tid} 声明不回收，但未在 note 说明")
            payoff = []
        elif not payoff:
            warnings.append(f"线索 {tid} 未找到回收；原著不回收时写 payoff: none 并说明")
            payoff = []
        elif not isinstance(payoff, list):
            errors.append(f"线索 {tid} payoff 应为段落列表或 none")
            payoff = []
        if payoff:
            errors.extend(f"线索 {tid} payoff：{error}" for error in validate_evidence(payoff, paragraphs))
        for ref in setup + payoff:
            try:
                hits.update(paragraphs[pid]["chapter"] for pid in expand_evidence(ref, paragraphs))
            except (ValueError, TypeError, KeyError):
                pass  # The validation error above retains the invalid reference.
    for chapter in chapters:
        if chapter["id"] not in hits:
            warnings.append(f"{chapter['id']} 没有线索落点，请抽读确认是否遗漏转折或论证环节")
    brief_path = analysis / "book_brief.md"
    brief = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
    if not brief.strip():
        errors.append("缺少非空的 analysis/book_brief.md")
    brief_references = set(PID.findall(brief))
    for chapter in chapters:
        if AUTHOR_CHAPTER.search(chapter["title"]) and not brief_references.intersection(chapter["paragraphs"]):
            warnings.append(f"作者自述章节 {chapter['id']}（{chapter['title']}）未被全书简报引用")
    analysis_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(analysis.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".yaml", ".yml"}
        and not path.name.startswith("coverage_")
    )
    report = {"generation": source_generation(project), "errors": errors, "warnings": warnings,
              "candidates": _term_candidates(paragraphs, analysis_text, len(chapters)),
              "note": "高频词仅为抽查候选；中文采用字组，拉丁字母词采用大小写归一，未做词形还原。本报告不代表人工审计通过。人工结论保存于 coverage_review.yaml。"}
    write_json(analysis / "coverage_machine.json", report)
    return report


def _strings(value: object, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{label} 必须是字符串列表（无内容时写 []）")
        return []
    return value


def _positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def check_plan(project: Path) -> dict:
    project = Path(project)
    errors, warnings = [], []
    cfg = load_config(project)
    paragraphs, chapters = read_paragraphs(project), read_chapters(project)
    chapter_map = {chapter["id"]: chapter for chapter in chapters}
    data = load_yaml(project / "plan" / "episodes.yaml", {})
    if isinstance(data, list):
        data = {"episodes": data}
    if not isinstance(data, dict) or not isinstance(data.get("episodes"), list) or not data["episodes"]:
        return {"errors": ["plan/episodes.yaml 必须包含非空的 episodes 列表"], "warnings": []}
    episodes = data["episodes"]
    external = load_yaml(project / "analysis" / "external_sources.yaml", {"sources": []})
    depth = cfg.get("depth", {})
    required_layers = depth.get("required_layers", [2])
    series_required = depth.get("series_layers", [3])
    for name, values in (("required_layers", required_layers), ("series_layers", series_required)):
        if not isinstance(values, list) or any(type(value) is not int or value not in LAYERS for value in values):
            errors.append(f"depth.{name} 必须是 1–4 的整数列表")
    required_layers = set(required_layers) if isinstance(required_layers, list) and all(type(v) is int for v in required_layers) else set()
    series_required = set(series_required) if isinstance(series_required, list) and all(type(v) is int for v in series_required) else set()
    formatting = cfg.get("format", {})
    limits, cpm = formatting.get("episode_minutes", [10, 15]), formatting.get("speech_rate_cpm", 240)
    valid_budget = isinstance(limits, list) and len(limits) == 2 and all(_positive_number(n) for n in limits) and limits[0] <= limits[1] and _positive_number(cpm)
    if not valid_budget:
        errors.append("format 的 episode_minutes 或 speech_rate_cpm 配置无效")
    threads = _load_threads(project, errors)
    thread_ids = {thread["id"] for thread in threads}
    setup_episodes: dict[str, list[int]] = defaultdict(list)
    payoff_episodes: dict[str, list[int]] = defaultdict(list)
    covered: set[str] = set()
    covered_paragraphs: set[str] = set()
    series_layers: set[int] = set()
    information = []
    takeaway_ids = set()
    genres = {"suspense", "popsci", "emotional", "nonfiction", "history"}
    for number, episode in enumerate(episodes, 1):
        tag = f"ep{number:02d}"
        if not isinstance(episode, dict):
            errors.append(f"{tag} 必须是映射")
            continue
        if type(episode.get("ep")) is not int or episode["ep"] != number:
            errors.append(f"{tag} 编号必须按顺序为 {number}")
        for key in ("title_working", "core_question"):
            if not isinstance(episode.get(key), str) or not episode[key].strip():
                errors.append(f"{tag} 缺少非空的 {key}")
        if episode.get("genre_mode") not in genres:
            errors.append(f"{tag} genre_mode 必须是 {', '.join(sorted(genres))} 之一")
        target = episode.get("target_chars")
        if not _positive_number(target):
            errors.append(f"{tag} target_chars 必须是正数")
        elif valid_budget and not limits[0] * cpm * 0.95 <= target <= limits[1] * cpm * 1.05:
            errors.append(f"{tag} target_chars={target} 不在 {limits[0]}–{limits[1]} 分钟字数预算内")
        covers = _strings(episode.get("covers"), f"{tag} covers", errors)
        if not covers:
            errors.append(f"{tag} covers 不能为空")
        for ref in covers:
            chapter_id = ref.removesuffix(".md")
            if chapter_id in chapter_map:
                covered.add(chapter_id)
                covered_paragraphs.update(chapter_map[chapter_id]["paragraphs"])
            else:
                try:
                    ids = expand_evidence(ref, paragraphs)
                    covered_paragraphs.update(ids)
                    covered.update(paragraphs[pid]["chapter"] for pid in ids)
                except (ValueError, TypeError, KeyError) as error:
                    errors.append(f"{tag} covers {ref} 无效：{error}")
        takeaways = episode.get("takeaways")
        if not isinstance(takeaways, list) or not takeaways:
            errors.append(f"{tag} takeaways 必须是非空列表")
            takeaways = []
        layers: set[int] = set()
        core_count = 0
        for item in takeaways:
            if not isinstance(item, dict):
                errors.append(f"{tag} 每条 takeaway 必须包含 id、layer、text、evidence")
                continue
            tid = item.get("id")
            if not isinstance(tid, str) or not tid.strip():
                errors.append(f"{tag} takeaway 缺少 id")
            elif tid in takeaway_ids:
                errors.append(f"{tag} takeaway id 重复：{tid}")
            else:
                takeaway_ids.add(tid)
            if not isinstance(item.get("text"), str) or not item["text"].strip():
                errors.append(f"{tag} takeaway {tid} 缺少 text")
            core = item.get("core", True)
            if type(core) is not bool:
                errors.append(f"{tag} takeaway {tid} core 必须是布尔值")
                core = True
            if core:
                core_count += 1
            layer = item.get("layer")
            if type(layer) is not int or layer not in LAYERS:
                errors.append(f"{tag} takeaway {tid} layer 必须是 1–4")
            elif core:
                layers.add(layer)
            evidence = item.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                errors.append(f"{tag} takeaway {tid} 缺少 evidence 列表")
            else:
                errors.extend(f"{tag} takeaway {tid}：{error}" for error in validate_evidence(evidence, paragraphs, external))
        if not core_count:
            errors.append(f"{tag} 至少需要一条 core: true 的核心 takeaway")
        if required_layers - layers:
            errors.append(f"{tag} 核心 takeaways 缺少深度层 {sorted(required_layers - layers)}")
        series_layers.update(layers)
        for field, mapping in (("threads_setup", setup_episodes), ("threads_payoff", payoff_episodes)):
            for tid in _strings(episode.get(field, []), f"{tag} {field}", errors):
                if tid not in thread_ids:
                    errors.append(f"{tag} {field} 引用了不存在的线索 {tid}")
                mapping[tid].append(number)
        reveal = _strings(episode.get("reveal"), f"{tag} reveal", errors)
        withhold = _strings(episode.get("withhold"), f"{tag} withhold", errors)
        for item in set(reveal).intersection(withhold):
            errors.append(f"{tag} 同时要求揭示和隐瞒「{item}」")
        information.append((number, set(reveal), set(withhold)))
        if number < len(episodes) and not episode.get("cliffhanger"):
            warnings.append(f"{tag} 非末集，未填写 cliffhanger")
        if number > 1 and not episode.get("recap"):
            warnings.append(f"{tag} 未填写前情回顾 recap")
    if series_required - series_layers:
        errors.append(f"系列核心 takeaways 缺少深度层 {sorted(series_required - series_layers)}")
    for thread in threads:
        tid = thread["id"]
        setup, payoff = setup_episodes[tid], payoff_episodes[tid]
        if payoff and (not setup or min(payoff) < min(setup)):
            errors.append(f"线索 {tid} 在埋下之前回收")
        if thread.get("importance") == "high":
            if not setup:
                errors.append(f"high 级线索 {tid} 没有安排埋点")
            if thread.get("payoff") not in ("none", ["none"]) and not payoff:
                errors.append(f"high 级线索 {tid} 没有安排回收")
    for number, revealed, _ in information:
        for later, _, withheld in information:
            if later > number:
                for item in sorted(revealed.intersection(withheld)):
                    errors.append(f"ep{number:02d} 已揭示「{item}」，ep{later:02d} 却仍要求隐瞒")
    skipped: set[str] = set()
    skip_items = data.get("skipped", [])
    if not isinstance(skip_items, list):
        errors.append("skipped 必须是包含 chapter 和 reason 的列表")
        skip_items = []
    for item in skip_items:
        if not isinstance(item, dict) or not isinstance(item.get("chapter"), str):
            errors.append("每条 skipped 必须包含 chapter 和 reason")
            continue
        chapter_id = item["chapter"].removesuffix(".md")
        if chapter_id not in chapter_map:
            errors.append(f"skipped 引用了不存在的章节 {chapter_id}")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            errors.append(f"skipped {chapter_id} 缺少理由")
        if chapter_id in covered:
            errors.append(f"{chapter_id} 同时出现在 covers 和 skipped")
        skipped.add(chapter_id)
    for chapter in chapters:
        chapter_id = chapter["id"]
        if chapter_id not in covered | skipped:
            errors.append(f"{chapter_id}（{chapter['title']}）既未覆盖也未说明跳过理由")
        elif chapter_id in covered:
            missing = set(chapter["paragraphs"]) - covered_paragraphs
            if missing:
                warnings.append(f"{chapter_id} 尚有 {len(missing)} 段未在 covers 范围内，请确认取舍")
    return {"errors": errors, "warnings": warnings}
