"""Per-episode audition sheet for the sound confirmation.

``production/audition_sheet.md`` lists, for the current mix, where every sound
effect plays (start–end, class, description, anchor sentence, source) and when
each character speaks. The sheet records the mix and timing hashes it was
built from, so a sound confirmation can require that the user listened with
the matching sheet.
"""
from __future__ import annotations

import re
from pathlib import Path

from .common import atomic_write, load_yaml, sha256_file

CLASS_LABELS = {"ambience": "环境声", "event": "事件音", "process": "过程声", "design": "设计音", "music": "音乐"}
SCOPE_LABELS = {"project": "本集素材", "sfx_library": "音效库"}
_HEADER = re.compile(r"<!-- audition mix_sha256=([0-9a-f]{64}) timing_sha256=([0-9a-f]{64}) -->")


def _stamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def _files(project: Path, ep: int) -> tuple[Path, Path, Path]:
    from .approvals import sound_paths
    epdir = Path(project).resolve() / "episodes" / f"ep{ep:02d}"
    configured = sound_paths(project)
    return epdir, epdir / configured["mix"], epdir / configured["timing"]


def sheet_path(project: Path, ep: int) -> Path:
    return _files(project, ep)[0] / "production/audition_sheet.md"


def sheet_state(project: Path, ep: int) -> dict:
    _, mix, timing = _files(project, ep)
    path = sheet_path(project, ep)
    if not path.is_file():
        return {"state": "missing", "path": str(path)}
    match = _HEADER.search(path.read_text(encoding="utf-8").split("\n", 1)[0])
    if (not match or not mix.is_file() or not timing.is_file()
            or match.groups() != (sha256_file(mix), sha256_file(timing))):
        return {"state": "stale", "path": str(path)}
    return {"state": "current", "path": str(path)}


def _cell(value: object) -> str:
    text = str(value or "").replace("|", "／").replace("\n", " ").strip()
    return text or "—"


def build(project: Path, ep: int) -> dict:
    project = Path(project).resolve()
    epdir, mix, timing_path = _files(project, ep)
    if not mix.is_file() or not timing_path.is_file():
        return {"passed": False, "status": "error", "summary": f"第 {ep} 集还没有混音和最终时间表",
                "errors": ["先让 produce 完成 mix 阶段"], "artifacts": []}
    timing = load_yaml(timing_path, {}) or {}
    if not isinstance(timing, dict) or timing.get("audio_sha256") != sha256_file(mix):
        return {"passed": False, "status": "error", "summary": "最终时间表不属于当前混音",
                "errors": ["重跑 produce 的 mix 阶段后再生成试听单"], "artifacts": []}
    sentences = {row.get("id"): row for row in timing.get("sentences") or [] if isinstance(row, dict)}
    warnings = []
    timeline = timing.get("cue_timeline")
    if not isinstance(timeline, list):
        report = load_yaml(epdir / "production/_reports/mix_qc.json", {}) or {}
        timeline = report.get("cue_timeline") if isinstance(report, dict) else None
        if not isinstance(timeline, list):
            timeline = []
            warnings.append("混音时间表没有音效时间线；重跑 mix 后再生成试听单")
        else:
            warnings.append("音效时间线取自混音报告（旧版时间表）；重跑 mix 可绑定到时间表")
    cues = {row.get("cue_id"): row for row in
            (load_yaml(epdir / "production/sound_cues.yaml", {}) or {}).get("cues") or [] if isinstance(row, dict)}
    duration = float(timing.get("duration_sec") or 0)
    lines = [f"<!-- audition mix_sha256={sha256_file(mix)} timing_sha256={sha256_file(timing_path)} -->",
             f"# 第 {ep} 集试听单", "",
             f"- 试听文件：`{mix.relative_to(project)}`（{_stamp(duration)}）",
             "- 听感重点：口播是否清楚、音效是否压字、停顿是否自然、人物音色是否贴合。",
             "- 听完后：没问题单独回复「拍板声音」；有问题直接说时间点和想怎么改。", "",
             f"## 音效时间线（{len(timeline)} 条）", ""]
    if timeline:
        lines += ["| # | 起–止 | 类别 | 描述 | 锚点句 | 来源 |", "|---|---|---|---|---|---|"]
        for index, row in enumerate(sorted(timeline, key=lambda item: float(item.get("start_sec", 0))), 1):
            cue = cues.get(row.get("cue_id"), {})
            anchor = sentences.get(row.get("sentence_id") or (cue.get("anchor") or {}).get("sentence_id"), {})
            text = str(anchor.get("text", ""))
            text = text if len(text) <= 28 else text[:27] + "…"
            place = "句前" if (row.get("placement") or cue.get("placement") or "before") == "before" else "句后"
            source = "新生成" if cue.get("asset_origin") == "generated" else SCOPE_LABELS.get(row.get("asset_scope"), "素材")
            lines.append(f"| {index} | {_stamp(row.get('start_sec', 0))}–{_stamp(row.get('end_sec', 0))} | "
                         f"{CLASS_LABELS.get(cue.get('sound_class'), _cell(cue.get('sound_class')))} | "
                         f"{_cell(cue.get('description'))}（{row.get('cue_id')}） | {place}：{_cell(text)} | {source} |")
    else:
        lines.append("本集没有音效。")
    lines += ["", "## 对白时段", ""]
    script = epdir / "production/voice_script.yaml"
    if script.is_file() and (epdir / "final.md").is_file():
        from .voice_script import NARRATOR, _names, load_cast, quote_sentences
        names = _names(project, load_cast(project))
        mapping = quote_sentences(epdir / "final.md")
        rows = [row for row in (load_yaml(script, {}) or {}).get("quotes") or []
                if isinstance(row, dict) and row.get("speaker") not in (None, "", NARRATOR)]
        if rows:
            lines += ["| 起–止（所在句） | 说话人 | 台词 |", "|---|---|---|"]
            for row in rows:
                spans = [sentences[sid] for sid in mapping.get(row.get("n"), []) if sid in sentences]
                if not spans:
                    continue
                start = min(float(item["startTime"]) for item in spans)
                end = max(float(item["endTime"]) for item in spans)
                speaker = row["speaker"]
                lines.append(f"| {_stamp(start)}–{_stamp(end)} | {speaker} {names.get(speaker, '')} | "
                             f"“{_cell(row.get('text'))}” |")
        else:
            lines.append("本集对白都由旁白读出。")
    else:
        lines.append("本集没有说话人标注，全部由旁白读出。")
    path = sheet_path(project, ep)
    atomic_write(path, "\n".join(lines).rstrip() + "\n")
    return {"passed": True, "status": "success" if not warnings else "warning",
            "summary": f"第 {ep} 集试听单：{len(timeline)} 条音效", "warnings": warnings,
            "path": str(path), "artifacts": [str(path), str(mix)],
            "next_actions": ["把试听单和混音发给用户试听；用户单独回复「拍板声音」后再做分镜"]}
