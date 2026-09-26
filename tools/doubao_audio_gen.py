#!/usr/bin/env python3
"""Generate one custom sound with the audio model, keeping credentials local."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_CONFIG = Path.home() / '.tts_config.json'


def credentials(config: Path) -> tuple[str, str]:
    data = json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
    key = os.environ.get('DOUBAO_API_KEY', '').strip() or str(data.get('api_key', '')).strip()
    host = str(data.get('host', 'openspeech.bytedance.com')).strip()
    if not key:
        raise RuntimeError('未找到 DOUBAO_API_KEY 或本机语音配置中的 api_key。')
    return key, host


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    key, host = credentials(args.config)
    request_id = str(uuid.uuid4())
    payload = {
        'model': 'seed-audio-1.0',
        'text_prompt': args.prompt,
        'audio_config': {
            'format': 'mp3',
            'sample_rate': 48000,
            'pitch_rate': 0,
            'speech_rate': 0,
            'loudness_rate': 0,
        },
        'watermark': {},
    }
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'X-Api-Key': key,
        'X-Api-Request-Id': request_id,
    }
    req = urllib.request.Request(
        f'https://{host}/api/v3/tts/create',
        data=body,
        method='POST',
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            logid_present = bool(response.headers.get('X-Tt-Logid'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'接口 HTTP {exc.code}：{detail[:500]}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'接口连接失败：{exc.reason}') from exc

    encoded = result.get('audio')
    if not encoded:
        raise RuntimeError(f'生成失败 code={result.get("code")} message={str(result.get("message", ""))[:300]}')
    audio = base64.b64decode(encoded)
    if not audio:
        raise RuntimeError('解码后的音频为空。')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(audio)
    metadata = {
        'name': args.name,
        'model': 'seed-audio-1.0',
        'request_id': request_id,
        'prompt': args.prompt,
        'requested_duration_sec': args.duration,
        'duration_sec': result.get('duration'),
        'original_duration_sec': result.get('original_duration'),
        'output': str(args.output),
        'output_bytes': len(audio),
        'logid_present': logid_present,
        'status': 'success',
    }
    args.metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'success', 'name': args.name, 'duration_sec': result.get('duration'), 'original_duration_sec': result.get('original_duration'), 'bytes': len(audio), 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
