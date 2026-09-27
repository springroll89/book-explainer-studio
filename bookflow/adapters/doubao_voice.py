"""Doubao 2.0 asynchronous narration protocol; no implicit retries or disk writes.

The submit/query wire shape follows the studio's previously successful local
long-TTS jobs. Live provider behaviour must still be validated before release.
"""
from __future__ import annotations

import json
import ipaddress
import math
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from collections.abc import Callable

from ..common import count_chars

BASE_URL = "https://openspeech.bytedance.com/api/v3/tts"
SUCCESS_CODE = 20000000
MAX_RESPONSE = 2_000_000
MAX_AUDIO = 50_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _audio_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or port not in {None, 443}:
        return False
    if host.lower() == "localhost" or host.lower().endswith(".localhost") or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


class ProviderUncertain(ValueError):
    """The caller cannot prove whether a paid submit happened; never auto-resubmit."""


class DoubaoVoiceClient:
    def __init__(self, *, api_key: str | None = None, opener: Callable | None = None):
        key = api_key if api_key is not None else os.environ.get("DOUBAO_API_KEY")
        if not isinstance(key, str) or not key.strip() or "\r" in key or "\n" in key:
            raise ValueError("未设置 DOUBAO_API_KEY；不提交付费配音")
        self._key = key
        self._open = opener or urllib.request.build_opener(_NoRedirect).open

    def __repr__(self) -> str:
        return "DoubaoVoiceClient(api_key=<redacted>)"

    def _post(self, endpoint: str, payload: dict, request_id: str, resource_id: str) -> dict:
        if endpoint not in {"submit", "query"} or resource_id not in {"seed-tts-2.0", "seed-icl-2.0"}:
            raise ValueError("豆包配音端点或资源版本无效")
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", request_id):
            raise ValueError("豆包请求 ID 必须为已持久化的 UUID")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{BASE_URL}/{endpoint}", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Api-Key": self._key,
                     "X-Api-Resource-Id": resource_id, "X-Api-Request-Id": request_id})
        try:
            with self._open(request, timeout=60) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ProviderUncertain(f"豆包 {endpoint} 请求结果未知；保留原任务并人工核对，不能重发") from exc
        if len(raw) > MAX_RESPONSE:
            raise ProviderUncertain(f"豆包 {endpoint} 响应超出大小限制；不能重发")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderUncertain(f"豆包 {endpoint} 响应无法解析；不能重发") from exc
        if not isinstance(data, dict):
            raise ProviderUncertain(f"豆包 {endpoint} 响应格式无效；不能重发")
        return data

    def submit(self, *, text: str, speaker: str, request_id: str,
               resource_id: str = "seed-tts-2.0", model: str = "seed-tts-2.0-standard") -> str:
        """Return task ID. Caller must journal `request_id` before invoking this method."""
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise ValueError("配音段落为空或过长")
        if not isinstance(speaker, str) or not speaker.strip() or len(speaker) > 200:
            raise ValueError("配音音色 ID 无效")
        if model not in {"seed-tts-2.0-standard", "seed-tts-2.0-expressive"}:
            raise ValueError("当前只支持已核对的豆包 2.0 模型")
        payload = {"user": {"uid": "bookflow", "unique_id": request_id},
                   "req_params": {"text": text, "model": model, "speaker": speaker,
                                  "audio_params": {"format": "mp3", "sample_rate": 24000,
                                                   "bit_rate": 160000, "enable_timestamp": True},
                                  "disable_markdown_filter": False, "disable_emoji_filter": True}}
        result = self._post("submit", payload, request_id, resource_id)
        data = result.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if result.get("code") != SUCCESS_CODE or not isinstance(task_id, str) or not task_id.strip():
            raise ProviderUncertain("豆包 submit 未返回可查询任务 ID；不能自动重发")
        return task_id

    def query(self, *, task_id: str, request_id: str, resource_id: str = "seed-tts-2.0") -> dict:
        """Query the same task; this never creates another synthesis request."""
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
            raise ValueError("豆包任务 ID 无效")
        result = self._post("query", {"task_id": task_id}, request_id, resource_id)
        data = result.get("data")
        if result.get("code") != SUCCESS_CODE or not isinstance(data, dict):
            raise ProviderUncertain("豆包 query 未返回可确认的任务状态；保留原任务")
        state = data.get("task_status")
        if type(state) is not int:
            raise ProviderUncertain("豆包 query 返回未知任务状态；保留原任务")
        if state == 3:
            return {"state": "failed", "task_id": task_id}
        if state in {0, 1}:
            return {"state": "running", "task_id": task_id}
        if state != 2:
            raise ProviderUncertain("豆包 query 返回未知任务状态；保留原任务")
        audio_url, sentences = data.get("audio_url"), data.get("sentences")
        if not _audio_url(audio_url) or not isinstance(sentences, list):
            raise ProviderUncertain("豆包任务完成但缺少 HTTPS 音频或时间戳；保留原任务")
        chars = data.get("req_text_length")
        if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
            chars = None
        return {"state": "done", "task_id": task_id, "audio_url": audio_url,
                "sentences": sentences, "billable_chars": chars}

    def download_audio(self, url: str) -> bytes:
        """Fetch a provider-supplied HTTPS asset without auth headers or redirects."""
        if not _audio_url(url):
            raise ValueError("豆包音频地址不是允许的 HTTPS 主机")
        request = urllib.request.Request(url, method="GET")
        try:
            with self._open(request, timeout=120) as response:
                audio = response.read(MAX_AUDIO + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ProviderUncertain("豆包音频下载未完成；只查询原 task_id，不重发合成") from exc
        if not audio or len(audio) > MAX_AUDIO:
            raise ProviderUncertain("豆包音频为空或过大；保留原 task_id")
        return audio


def _times(row: dict) -> tuple[float, float]:
    try:
        start, end = float(row["startTime"]), float(row["endTime"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("豆包句子时间戳缺失或无效") from exc
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end - start < 0.02:
        raise ValueError("豆包句子时间戳倒置或过短")
    return start, end


def align_timestamps(provider_rows: object, expected: list[dict]) -> list[dict]:
    """Use provider evidence only; reject text normalization or boundary ambiguity."""
    if not isinstance(provider_rows, list) or not provider_rows or not expected:
        raise ValueError("豆包未提供可核对的句子时间戳")
    if len(provider_rows) == len(expected) and all(
        isinstance(row, dict) and count_chars(str(row.get("text", ""))) == count_chars(item["text"])
        for row, item in zip(provider_rows, expected)
    ):
        times = [_times(row) for row in provider_rows]
    else:
        words = [word for row in provider_rows if isinstance(row, dict)
                 for word in (row.get("words") if isinstance(row.get("words"), list) else [])
                 if isinstance(word, dict)]
        if not words or "".join(str(word.get("word", "")) for word in words) == "":
            raise ValueError("豆包分句与定稿不同且没有字级时间戳")
        if "".join(re.sub(r"\W|_", "", str(word.get("word", ""))) for word in words) != "".join(
            re.sub(r"\W|_", "", item["text"]) for item in expected):
            raise ValueError("豆包字级文本与定稿不一致；不能推算字幕时间")
        times = []
        consumed = 0
        cursor = 0
        for item in expected:
            needed = len(re.sub(r"\W|_", "", item["text"]))
            start_index = cursor
            while cursor < len(words) and consumed < needed:
                consumed += len(re.sub(r"\W|_", "", str(words[cursor].get("word", ""))))
                cursor += 1
            if consumed != needed or cursor == start_index:
                raise ValueError("豆包字级时间戳跨越定稿句界，不能安全切分")
            first, last = words[start_index], words[cursor - 1]
            times.append(_times({"startTime": first.get("startTime"), "endTime": last.get("endTime")}))
            consumed = 0
        if cursor != len(words):
            raise ValueError("豆包字级时间戳有多余内容")
    result = []
    previous = 0.0
    for item, (start, end) in zip(expected, times):
        if start + 0.001 < previous:
            raise ValueError("豆包句子时间戳重叠或逆序")
        result.append({"id": item["id"], "text": item["text"],
                       "startTime": start, "endTime": end})
        previous = end
    return result
