"""Speaker attribution for dialogue and the season-wide voice cast check.

Each episode that uses character voices keeps ``production/voice_script.yaml``
bound to its ``final.md``. Every quotation “…” in the spoken text is listed in
order with the speaker who says it (a character ID from the voice cast, or
``narrator`` for retold words and scare quotes). Text outside quotes is always
narration. Synthesis then splits each paragraph into narrator/character runs;
sentence timing is rebuilt from the fragments, so subtitles stay per sentence.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from .common import count_chars, load_yaml, parse_draft, sha256_file, write_yaml

OPEN, CLOSE = "“", "”"
NARRATOR = "narrator"


def cast_path(project: Path) -> Path:
    return Path(project) / "production/voice_cast.yaml"


def load_cast(project: Path) -> dict:
    cast = load_yaml(cast_path(project), {}) or {}
    if not isinstance(cast, dict):
        raise ValueError("production/voice_cast.yaml 必须是映射")
    return cast


def required(project: Path) -> bool:
    """Books whose cast lists characters voice dialogue with those characters."""
    characters = load_cast(project).get("characters")
    return isinstance(characters, dict) and bool(characters)


def resolve(cast: dict, speaker: str, *, ep: int | None = None, text: str = "",
            override: str | None = None) -> tuple[str | None, str]:
    """Return (voice_id, problem) for a speaker key."""
    if speaker == NARRATOR:
        narrator = cast.get("narrator") if isinstance(cast.get("narrator"), dict) else {}
        if narrator.get("status") == "confirmed" and isinstance(narrator.get("voice_id"), str) and narrator["voice_id"]:
            return narrator["voice_id"], ""
        return None, "旁白音色未确认"
    characters = cast.get("characters") if isinstance(cast.get("characters"), dict) else {}
    entry = characters.get(speaker)
    if not isinstance(entry, dict):
        return None, "missing"
    if entry.get("status") != "confirmed":
        return None, "unconfirmed"
    if override:
        rows = cast.get("episode_line_overrides") or []
        if not any(isinstance(row, dict) and row.get("status") == "confirmed" and row.get("episode") == ep
                   and row.get("character_id") == speaker and row.get("voice_id") == override
                   and isinstance(row.get("text_anchor"), str) and row["text_anchor"].strip("“”") in text
                   for row in rows):
            return None, "override_unconfirmed"
        return override, ""
    if isinstance(entry.get("voice_id"), str) and entry["voice_id"]:
        return entry["voice_id"], ""
    pool_name = entry.get("voice_pool")
    pools = cast.get("shared_voice_pools") if isinstance(cast.get("shared_voice_pools"), dict) else {}
    pool = pools.get(pool_name) if isinstance(pool_name, str) else None
    if isinstance(pool, dict) and pool.get("status") == "confirmed" and isinstance(pool.get("voice_id"), str) and pool["voice_id"]:
        return pool["voice_id"], ""
    return None, "pool_unconfirmed"


def _groups(parsed: dict) -> list[list[tuple[int, str]]]:
    groups: list[list[tuple[int, str]]] = []
    for line, text in parsed["lines"]:
        if groups and line == groups[-1][-1][0] + 1:
            groups[-1].append((line, text))
        else:
            groups.append([(line, text)])
    return groups


def analyse(final: Path) -> dict:
    """Quotes in order plus, per group, sentence spans labelled by quote ordinal (0 = narration)."""
    parsed = parse_draft(Path(final).read_text(encoding="utf-8"))
    quotes: list[dict] = []
    groups = []
    problems: list[str] = []
    sentence_index = 0
    for group in _groups(parsed):
        current = 0  # quote ordinal while inside “…”; quotes never cross a blank line
        lines = []
        for number, text in group:
            labels = []
            for char in text:
                if char == OPEN:
                    if current:
                        problems.append(f"第 {number} 行引号嵌套或未闭合")
                    quotes.append({"n": len(quotes) + 1, "text": "", "line": number})
                    current = len(quotes)
                    labels.append(0)
                elif char == CLOSE:
                    if not current:
                        problems.append(f"第 {number} 行有多余的右引号")
                    labels.append(0)
                    current = 0
                else:
                    if current:
                        quotes[current - 1]["text"] += char
                    labels.append(current)
            spans, offset = [], 0
            for part in re.split(r"(?<=[。！？!?])", text):
                start = offset
                offset += len(part)
                if not count_chars(part):
                    continue
                stripped = part.strip()
                begin = start + part.index(stripped)
                spans.append((begin, begin + len(stripped), stripped))
            lines.append({"line": number, "labels": labels, "spans": spans})
            sentence_index += len(spans)
        if current:
            problems.append(f"第 {group[-1][0]} 行所在段落的引号未闭合")
        groups.append(lines)
    if sentence_index != len(parsed["sentences"]):
        problems.append("句子切分与定稿句子表不一致")
    return {"quotes": quotes, "groups": groups, "problems": problems, "parsed": parsed}


def script_path(epdir: Path) -> Path:
    return Path(epdir) / "production/voice_script.yaml"


def check_episode(project: Path, ep: int, *, cast: dict | None = None) -> dict:
    project = Path(project).resolve()
    epdir = project / "episodes" / f"ep{ep:02d}"
    final = epdir / "final.md"
    cast = load_cast(project) if cast is None else cast
    if not final.is_file():
        return {"ep": ep, "state": "no_final", "errors": [f"第 {ep} 集缺少 final.md"], "speakers": {}}
    info = analyse(final)
    errors = list(info["problems"])
    path = script_path(epdir)
    if not info["quotes"]:
        return {"ep": ep, "state": "no_dialogue" if not errors else "invalid", "errors": errors,
                "speakers": {}, "quotes": 0}
    if not path.is_file():
        return {"ep": ep, "state": "missing", "errors": errors + [f"第 {ep} 集缺少对白说话人标注"],
                "speakers": {}, "quotes": len(info["quotes"])}
    data = load_yaml(path, {}) or {}
    rows = data.get("quotes") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("final_sha256") != sha256_file(final):
        errors.append(f"第 {ep} 集说话人标注不是按当前 final.md 做的，需重新核对")
    if not isinstance(rows, list) or len(rows) != len(info["quotes"]):
        errors.append(f"第 {ep} 集标注条数与定稿引号数（{len(info['quotes'])}）不一致")
        rows = rows if isinstance(rows, list) else []
    speakers: dict[str, dict] = {}
    unassigned = 0
    for quote, row in zip(info["quotes"], rows):
        if not isinstance(row, dict) or row.get("n") != quote["n"] or row.get("text") != quote["text"]:
            errors.append(f"第 {ep} 集第 {quote['n']} 处引号的标注与定稿文字不一致")
            continue
        speaker = row.get("speaker")
        if not isinstance(speaker, str) or not speaker.strip():
            unassigned += 1
            continue
        voice, problem = resolve(cast, speaker, ep=ep, text=quote["text"], override=row.get("voice_override"))
        item = speakers.setdefault(speaker, {"lines": 0, "problems": set()})
        item["lines"] += 1
        if problem:
            item["problems"].add(problem)
    if unassigned:
        errors.append(f"第 {ep} 集还有 {unassigned} 处引号未标说话人")
    for item in speakers.values():
        item["problems"] = sorted(item["problems"])
    state = "invalid" if errors else "ok"
    return {"ep": ep, "state": state, "errors": errors, "speakers": speakers, "quotes": len(info["quotes"])}


def _names(project: Path, cast: dict) -> dict[str, str]:
    names = {}
    data = load_yaml(Path(project) / "analysis/characters.yaml", {}) or {}
    rows = data.get("characters", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = [dict(value, id=key) if isinstance(value, dict) else {"id": key} for key, value in rows.items()]
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            names[row["id"]] = str(row.get("name") or row.get("name_zh") or row.get("display_name") or "")
    for key, value in (cast.get("characters") or {}).items():
        if isinstance(value, dict) and value.get("name"):
            names[key] = str(value["name"])
    return names


PROFILE_FIELDS = {"gender": ("gender", "性别"), "age_group": ("age_group", "年龄段"),
                  "personality": ("personality", "性格"), "summary": ("summary", "intro", "简介")}
AGE_GROUPS = ("儿童", "青年", "中年", "老年", "未知")


def profiles(project: Path) -> dict[str, dict]:
    """Casting fields per character ID from analysis/characters.yaml (gender, age group, personality, intro)."""
    data = load_yaml(Path(project) / "analysis/characters.yaml", {}) or {}
    rows = data.get("characters", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = [dict(value, id=key) if isinstance(value, dict) else {"id": key} for key, value in rows.items()]
    result = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        profile = {}
        for field, keys in PROFILE_FIELDS.items():
            value = next((row[key] for key in keys if isinstance(row.get(key), str) and row[key].strip()), "")
            profile[field] = value.strip() if isinstance(value, str) else ""
        result[row["id"]] = profile
    return result


def quote_sentences(final: Path) -> dict[int, list[str]]:
    """Map each quote ordinal to the stable sentence IDs it falls in."""
    info = analyse(final)
    stable = load_yaml(Path(final).with_suffix(".sentences.json"), {}) or {}
    ids = [row.get("id") for row in stable.get("sentences", [])] if isinstance(stable, dict) else []
    result: dict[int, list[str]] = {}
    index = 0
    for lines in info["groups"]:
        for line in lines:
            for begin, end, _text in line["spans"]:
                if index >= len(ids):
                    return result
                for label in sorted(set(line["labels"][begin:end]) - {0}):
                    result.setdefault(label, []).append(ids[index])
                index += 1
    return result


def _voice_label(cast: dict, speaker: str) -> str:
    entry = (cast.get("characters") or {}).get(speaker)
    if not isinstance(entry, dict):
        return "未定"
    status = "已确认" if entry.get("status") == "confirmed" else "待确认"
    if entry.get("voice_id"):
        return f"{entry['voice_id']}（{status}）"
    if entry.get("voice_pool"):
        return f"共用音色池 {entry['voice_pool']}（{status}）"
    return "未定"


def casting_sheet(project: Path, episodes: list[int], *, skip: set[int] | None = None) -> dict:
    """Write production/voice_casting_sheet.md: every labelled speaker with profile, lines and voice."""
    project = Path(project).resolve()
    cast = load_cast(project)
    names, profile = _names(project, cast), profiles(project)
    usage: dict[str, dict] = {}
    unlabelled = []
    for ep in episodes:
        if skip and ep in skip:
            continue
        epdir = project / "episodes" / f"ep{ep:02d}"
        path = script_path(epdir)
        if not (epdir / "final.md").is_file():
            continue
        if not path.is_file():
            if analyse(epdir / "final.md")["quotes"]:
                unlabelled.append(ep)
            continue
        for row in (load_yaml(path, {}) or {}).get("quotes") or []:
            speaker = row.get("speaker") if isinstance(row, dict) else None
            if not isinstance(speaker, str) or not speaker.strip() or speaker == NARRATOR:
                continue
            entry = usage.setdefault(speaker, {"episodes": [], "lines": []})
            if ep not in entry["episodes"]:
                entry["episodes"].append(ep)
            entry["lines"].append(str(row.get("text", "")))
    order = sorted(usage, key=lambda key: (-len(usage[key]["lines"]), key))
    rows = []
    for speaker in order:
        entry, info = usage[speaker], profile.get(speaker, {})
        samples = sorted(dict.fromkeys(entry["lines"]), key=len, reverse=True)[:3]
        confusable = [other for other in order if other != speaker
                      and set(usage[other]["episodes"]) & set(entry["episodes"])
                      and info.get("gender") and info.get("gender") == profile.get(other, {}).get("gender")
                      and info.get("age_group") and info.get("age_group") == profile.get(other, {}).get("age_group")]
        rows.append({"id": speaker, "name": names.get(speaker, ""), **{field: info.get(field, "") for field in PROFILE_FIELDS},
                     "episodes": entry["episodes"], "line_count": len(entry["lines"]), "samples": samples,
                     "voice": _voice_label(cast, speaker), "confusable": confusable})
    missing_profile = [row["id"] for row in rows if not all(row[field] for field in PROFILE_FIELDS)]
    bad_age = [row["id"] for row in rows if row["age_group"] and row["age_group"] not in AGE_GROUPS]

    def cell(value: str) -> str:
        return (value or "未填").replace("|", "／").replace("\n", " ")

    lines = ["# 选音色单", "", f"音色表版本：{cast.get('revision', '未建')}；按台词量排序。"
             "在「当前音色」一栏未定或待确认的人物，请回复人物 ID 和选定的 voice ID。", ""]
    if unlabelled:
        lines += [f"> 注意：第 {'、'.join(map(str, unlabelled))} 集还没标说话人，这些集的人物未计入。", ""]
    lines += ["| ID | 称呼 | 性别 | 年龄段 | 性格 | 简介 | 出场集 | 句数 | 当前音色 | 同场易混 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        mixed = "、".join(f"{other} {names.get(other, '')}".strip() for other in row["confusable"]) or "—"
        lines.append(f"| {row['id']} | {cell(row['name'])} | {cell(row['gender'])} | {cell(row['age_group'])} | "
                     f"{cell(row['personality'])} | {cell(row['summary'])} | {'、'.join(map(str, row['episodes']))} | "
                     f"{row['line_count']} | {cell(row['voice'])} | {mixed} |")
    lines += ["", "## 代表台词", ""]
    for row in rows:
        lines.append(f"**{row['id']} {row['name']}**")
        lines += [f"- “{sample}”" for sample in row["samples"]] or ["- （无）"]
        lines.append("")
    target = project / "production/voice_casting_sheet.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    warnings = []
    if missing_profile:
        warnings.append("characters.yaml 缺少性别/年龄段/性格/简介：" + "、".join(missing_profile))
    if bad_age:
        warnings.append("年龄段只用 儿童/青年/中年/老年：" + "、".join(bad_age))
    if unlabelled:
        warnings.append("未标说话人的集：" + "、".join(map(str, unlabelled)))
    return {"passed": not warnings, "status": "success" if not warnings else "warning",
            "summary": f"选音色单列出 {len(rows)} 个说话人物", "path": str(target), "rows": rows,
            "warnings": warnings, "artifacts": [str(target)]}


def season(project: Path, episodes: list[int], *, skip: set[int] | None = None) -> dict:
    """One view of the whole season: which scripts are missing and which speakers need a voice."""
    project = Path(project).resolve()
    cast = load_cast(project)
    names = _names(project, cast)
    profile = profiles(project)
    rows, missing_scripts, invalid = [], [], []
    usage: dict[str, dict] = {}
    for ep in episodes:
        if skip and ep in skip:
            rows.append({"ep": ep, "state": "skipped_legacy"})
            continue
        row = check_episode(project, ep, cast=cast)
        rows.append({key: value for key, value in row.items() if key != "speakers"})
        if row["state"] == "missing":
            missing_scripts.append(ep)
        elif row["state"] == "invalid":
            invalid.append(ep)
        for speaker, item in row["speakers"].items():
            entry = usage.setdefault(speaker, {"episodes": [], "lines": 0, "problems": set()})
            entry["episodes"].append(ep)
            entry["lines"] += item["lines"]
            entry["problems"].update(item["problems"])
    needs_voice, reused = [], []
    for speaker, entry in sorted(usage.items()):
        record = {"id": speaker, "name": names.get(speaker, ""), "episodes": entry["episodes"],
                  "lines": entry["lines"], **profile.get(speaker, {})}
        if entry["problems"]:
            needs_voice.append({**record, "problems": sorted(entry["problems"])})
        elif speaker != NARRATOR:
            reused.append(record)
    unused = sorted(set((cast.get("characters") or {})) - set(usage))
    ok = not missing_scripts and not invalid and not needs_voice
    return {"passed": ok, "status": "success" if ok else "warning",
            "summary": "全季说话人标注与音色齐全" if ok else "全季说话人标注或角色音色尚未齐全",
            "missing_scripts": missing_scripts, "invalid_scripts": invalid,
            "needs_voice": needs_voice, "reused": reused,
            "cast_without_lines": [{"id": key, "name": names.get(key, "")} for key in unused],
            "cast_revision": cast.get("revision"), "episodes": rows}


def scaffold(project: Path, ep: int) -> dict:
    """Write a template listing every quote with an empty speaker; never overwrite."""
    project = Path(project).resolve()
    epdir = project / "episodes" / f"ep{ep:02d}"
    final = epdir / "final.md"
    if not final.is_file():
        raise ValueError(f"第 {ep} 集缺少 final.md；先冻结定稿")
    info = analyse(final)
    if info["problems"]:
        raise ValueError("；".join(info["problems"]))
    path = script_path(epdir)
    if path.exists():
        return {"passed": True, "ep": ep, "status": "exists", "path": str(path),
                "summary": f"第 {ep} 集已有说话人标注，未覆盖；核对后运行 voices check"}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, {"schema": 1, "final_sha256": sha256_file(final),
                      "note": "逐条填写 speaker：音色表中的人物 ID，或 narrator（转述、强调用引号）",
                      "quotes": [{"n": quote["n"], "text": quote["text"], "speaker": None}
                                 for quote in info["quotes"]]})
    return {"passed": True, "ep": ep, "status": "created", "path": str(path), "quotes": len(info["quotes"]),
            "summary": f"第 {ep} 集共 {len(info['quotes'])} 处引号待标说话人"}


def set_voice(project: Path, speaker: str, *, voice_id: str | None = None, pool: str | None = None,
              name: str | None = None) -> dict:
    """Record a user-chosen voice for one character and bump the cast revision."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", speaker or "") or speaker == NARRATOR:
        raise ValueError("人物 ID 无效；用 characters.yaml 里的稳定 ID，如 P31")
    if bool(voice_id) == bool(pool):
        raise ValueError("--voice-id 与 --pool 二选一")
    cast = load_cast(project)
    if not cast:
        raise ValueError("缺少 production/voice_cast.yaml")
    if pool and not isinstance((cast.get("shared_voice_pools") or {}).get(pool), dict):
        raise ValueError(f"音色表里没有共用音色池 {pool}")
    characters = cast.setdefault("characters", {})
    entry = dict(characters.get(speaker) or {})
    before = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    if name:
        entry["name"] = name
    entry.pop("voice_id" if pool else "voice_pool", None)
    entry["voice_pool" if pool else "voice_id"] = pool or voice_id
    entry["status"] = "confirmed"
    if json.dumps(entry, ensure_ascii=False, sort_keys=True) == before:
        return {"passed": True, "status": "unchanged", "summary": f"{speaker} 的音色未变化"}
    characters[speaker] = entry
    cast["revision"] = int(cast.get("revision") or 0) + 1
    cast["updated"] = date.today().isoformat()
    write_yaml(cast_path(project), cast)
    return {"passed": True, "status": "updated", "revision": cast["revision"],
            "summary": f"已写入 {speaker} 的音色，音色表升到第 {cast['revision']} 版",
            "next_actions": ["运行 voices check 确认全季说话人都有音色"]}


def episode_units(project: Path, ep: int, final: Path, stable: list[dict]) -> list[dict]:
    """Paragraph groups split into same-speaker runs; fragments keep sentence IDs."""
    project = Path(project).resolve()
    cast = load_cast(project)
    info = analyse(final)
    if info["problems"]:
        raise ValueError("；".join(info["problems"]))
    speakers = [None] * (len(info["quotes"]) + 1)
    speakers[0] = (NARRATOR, None)
    if info["quotes"]:
        checked = check_episode(project, ep, cast=cast)
        if checked["state"] != "ok" or any(item["problems"] for item in checked["speakers"].values()):
            raise ValueError("本集对白说话人标注或角色音色未齐全；先运行 voices check")
        rows = load_yaml(script_path(Path(final).parent), {})["quotes"]
        for quote, row in zip(info["quotes"], rows):
            speakers[quote["n"]] = (row["speaker"], row.get("voice_override"))
    ids = iter(row["id"] for row in stable)
    units: list[dict] = []
    for lines in info["groups"]:
        runs: list[dict] = []
        for line in lines:
            for begin, end, text in line["spans"]:
                sid = next(ids)
                labels = line["labels"][begin:end]
                pieces: list[list] = []
                for offset, label in enumerate(labels):
                    if pieces and pieces[-1][0] == label:
                        pieces[-1][1] += text[offset]
                    else:
                        pieces.append([label, text[offset]])
                merged: list[list] = []
                for label, chunk in pieces:  # punctuation-only pieces join their neighbour
                    if merged and not count_chars(merged[-1][1]):
                        merged[-1] = [label, merged[-1][1] + chunk]
                    elif merged and not count_chars(chunk):
                        merged[-1][1] += chunk
                    else:
                        merged.append([label, chunk])
                for label, chunk in merged:
                    key, override = speakers[label]
                    quote_text = info["quotes"][label - 1]["text"] if label else ""
                    voice, problem = resolve(cast, key, ep=ep, text=quote_text, override=override)
                    if problem:
                        raise ValueError(f"说话人 {key} 的音色不可用：{problem}")
                    if runs and runs[-1]["voice"] == voice:
                        if runs[-1]["line"] != line["line"]:
                            runs[-1]["text"] += "\n"
                        runs[-1]["text"] += chunk
                        runs[-1]["fragments"].append({"id": sid, "text": chunk})
                        runs[-1]["line"] = line["line"]
                    else:
                        runs.append({"speaker_key": key, "voice": voice, "text": chunk, "line": line["line"],
                                     "fragments": [{"id": sid, "text": chunk}]})
        units.extend(runs)
    return units


def unit_sha(text: str, voice: str) -> str:
    """Every multi-voice unit binds its actual voice, including narration."""
    payload = f"{voice}\n{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def voices_digest(units: list[dict], cast: dict) -> str:
    """Only the voices this episode actually uses, plus the engine, key the paid cache."""
    used = sorted({unit["voice"] for unit in units})
    return hashlib.sha256(json.dumps({"voices": used, "engine": cast.get("engine")},
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()
