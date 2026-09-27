"""Doubao audio 1.0 (seed-audio-1.0) sound-effect adapter.

Same request shape as ``tools/doubao_audio_gen.py``, which produced the first
episode's effects. The call is synchronous: one POST returns the audio. The
key is read from DOUBAO_API_KEY or ``~/.tts_config.json`` and never logged.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".tts_config.json"
MODEL = "seed-audio-1.0"


class SfxProviderError(RuntimeError):
    """The provider answered without usable audio (the request is settled as failed)."""


class SfxProviderUncertain(RuntimeError):
    """No trustworthy answer: the request may or may not have been billed."""


def prompt_for(cue: dict, suffix: str = "") -> str:
    """Build the generation prompt from the cue sheet; duration is stated in words."""
    parts = [str(cue.get("prompt") or cue.get("description") or "").strip()]
    tags = cue.get("tags")
    if isinstance(tags, list) and tags and not cue.get("prompt"):
        parts.append("，".join(str(tag) for tag in tags if str(tag).strip()))
    duration = float(cue.get("duration_sec") or 0)
    if duration:
        parts.append(f"时长约{duration:g}秒")
    if suffix:
        parts.append(suffix)
    return "，".join(part for part in parts if part)


class DoubaoSfxClient:
    def __init__(self, *, config: Path = DEFAULT_CONFIG, timeout: int = 180):
        data = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        self.key = os.environ.get("DOUBAO_API_KEY", "").strip() or str(data.get("api_key", "")).strip()
        self.host = str(data.get("host", "openspeech.bytedance.com")).strip()
        self.timeout = timeout
        if not self.key:
            raise SfxProviderError("未找到 DOUBAO_API_KEY 或本机语音配置中的 api_key")

    def generate(self, *, prompt: str, request_id: str, sample_rate: int = 48000) -> dict:
        payload = {"model": MODEL, "text_prompt": prompt,
                   "audio_config": {"format": "mp3", "sample_rate": sample_rate, "pitch_rate": 0,
                                    "speech_rate": 0, "loudness_rate": 0},
                   "watermark": {}}
        request = urllib.request.Request(
            f"https://{self.host}/api/v3/tts/create", method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Api-Key": self.key,
                     "X-Api-Request-Id": request_id})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise SfxProviderError(f"音效接口拒绝请求 HTTP {exc.code}") from None
            raise SfxProviderUncertain(f"音效接口 HTTP {exc.code}，结果未知") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise SfxProviderUncertain("音效接口连接中断或返回无法解析，结果未知") from None
        encoded = result.get("audio") if isinstance(result, dict) else None
        if not encoded:
            code = result.get("code") if isinstance(result, dict) else None
            raise SfxProviderError(f"音效生成失败 code={code}")
        audio = base64.b64decode(encoded)
        if not audio:
            raise SfxProviderError("音效接口返回空音频")
        duration = result.get("duration")
        return {"audio": audio, "duration_sec": float(duration) if isinstance(duration, (int, float)) else None}
