#!/usr/bin/env python3
"""Align returned TTS sentence timestamps to the exact final draft sentences."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def norm(text: str) -> str:
    return re.sub(r"\\n|\s+", "", text or "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode", type=Path)
    ap.add_argument("--draft-sentences", type=Path, required=True)
    ap.add_argument("--tts", type=Path, required=True)
    args = ap.parse_args()
    draft = json.loads(args.draft_sentences.read_text(encoding="utf-8"))["sentences"]
    raw = json.loads(args.tts.read_text(encoding="utf-8"))
    tts = raw.get("sentences", raw)
    aligned = []
    j = 0
    for i, target in enumerate(draft, 1):
        wanted = norm(target["text"])
        parts = []
        start = None
        end = None
        words = []
        while j < len(tts) and len(norm("".join(parts))) < len(wanted):
            row = tts[j]
            parts.append(row.get("text", ""))
            start = row.get("startTime", start)
            end = row.get("endTime", end)
            words.extend(row.get("words", []) or [])
            j += 1
            got = norm("".join(parts))
            if got == wanted:
                break
            if not wanted.startswith(got):
                raise SystemExit(f"无法对齐第 {i} 句：{target['text']!r} / {''.join(parts)!r}")
        if norm("".join(parts)) != wanted:
            raise SystemExit(f"第 {i} 句未完整对齐：{target['text']!r} / {''.join(parts)!r}")
        aligned.append({"id": target["id"], "text": target["text"], "startTime": start, "endTime": end, "words": words})
    if j != len(tts):
        raise SystemExit(f"TTS 还剩 {len(tts)-j} 条未绑定到最终稿")
    out = {
        "draft": str(next(args.episode.glob("draft_v*.md"))),
        "draft_sha256": None,
        "audio": raw.get("output"),
        "audio_sha256": None,
        "sentences": aligned,
        "source": "tts_timestamps_aligned_to_final_script",
        "raw_tts_metadata": str(args.tts),
    }
    (args.episode / "production/timing.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "episode": int(args.episode.name[2:]), "sentences": len(aligned), "raw_sentences": len(tts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
