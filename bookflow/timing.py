from __future__ import annotations

import json
from pathlib import Path

from .common import latest_draft, load_config, parse_draft, sha256_file, write_json
from .sentences import generate as generate_sentences


def _stable_sentences(draft: Path) -> list[dict]:
    sidecar = draft.with_suffix(".sentences.json")
    if not sidecar.is_file():
        previous = latest_draft(draft.parent) if draft.name == "final.md" else None
        generate_sentences(draft, previous if previous != draft else None)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    rows = data.get("sentences", [])
    if not rows or any(not row.get("id") for row in rows):
        raise ValueError(f"稳定句子表无效：{sidecar}")
    return rows


def import_timing(epdir, audio, timestamps=None):
    epdir = Path(epdir)
    draft = latest_draft(epdir, prefer_final=True)
    if draft is None:
        raise ValueError(f"找不到稿件：{epdir}")
    audio = Path(audio)
    if not audio.is_file():
        raise ValueError(f"找不到音频：{audio}")
    parsed = parse_draft(draft.read_text(encoding="utf-8"))
    stable = _stable_sentences(draft)
    if len(stable) != len(parsed["sentences"]):
        raise ValueError("稳定句子表与当前稿件句数不一致，请先重新生成句子表")
    rows = []
    if timestamps is not None:
        source_path = Path(timestamps)
        if not source_path.is_file():
            raise ValueError(f"时间戳文件不存在：{source_path}")
        data = json.loads(source_path.read_text(encoding="utf-8"))
        imported = data.get("sentences", []) if isinstance(data, dict) else data
        if not isinstance(imported, list) or not imported:
            raise ValueError(f"时间戳文件没有句子：{source_path}")
        by_id = {str(row.get("id") or row.get("sentence_id")): row for row in imported if isinstance(row, dict)}
        use_ids = all(row["id"] in by_id for row in stable)
        if not use_ids and len(imported) != len(stable):
            raise ValueError("时间戳句数与稳定句子表不一致")
        for index, sentence in enumerate(stable):
            raw = by_id[sentence["id"]] if use_ids else imported[index]
            start = raw.get("startTime", raw.get("start"))
            end = raw.get("endTime", raw.get("end"))
            if start is None or end is None or float(end) < float(start):
                raise ValueError(f"第 {index + 1} 句时间戳无效")
            row = {"id": sentence["id"], "text": sentence["text"],
                   "startTime": float(start), "endTime": float(end)}
            if isinstance(raw.get("words"), list):
                row["words"] = raw["words"]
            rows.append(row)
        source = "timestamps"
    else:
        cpm = float(load_config(epdir).get("format", {}).get("speech_rate_cpm", 240))
        if cpm <= 0:
            raise ValueError("format.speech_rate_cpm 必须大于 0")
        t = 0.0
        for sentence, parsed_sentence in zip(stable, parsed["sentences"]):
            duration = parsed_sentence["chars"] * 60 / cpm
            rows.append({"id": sentence["id"], "text": sentence["text"],
                         "startTime": round(t, 3), "endTime": round(t + duration, 3)})
            t += duration
        source = "estimate"
    output = epdir / "production/timing.json"
    write_json(output, {"draft": str(draft), "draft_sha256": sha256_file(draft),
                        "audio": str(audio.resolve()), "audio_sha256": sha256_file(audio),
                        "sentences": rows, "source": source})
    return {"passed": True, "output": str(output), "source": source, "sentences": len(rows)}
