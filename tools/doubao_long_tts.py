#!/usr/bin/env python3
"""Submit and query long-form speech synthesis without exposing credentials."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path.home() / '.tts_config.json'
RESOURCE_ID = 'seed-tts-2.0'


def load_credentials(config_path: Path) -> tuple[str, str]:
    env_key = os.environ.get('DOUBAO_API_KEY', '').strip()
    data = {}
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding='utf-8'))
    api_key = env_key or str(data.get('api_key', '')).strip()
    host = str(data.get('host', 'openspeech.bytedance.com')).strip()
    if not api_key:
        raise RuntimeError('未找到 DOUBAO_API_KEY 或本机语音配置中的 api_key。')
    return api_key, host


def post_json(url: str, api_key: str, payload: dict, request_id: str, resource_id: str = RESOURCE_ID) -> tuple[dict, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'X-Api-Key': api_key,
            'X-Api-Resource-Id': resource_id,
            'X-Api-Request-Id': request_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode('utf-8')), dict(response.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'接口 HTTP {exc.code}：{detail[:500]}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'接口连接失败：{exc.reason}') from exc


def load_spoken(draft: Path) -> str:
    sys.path.insert(0, str(ROOT))
    from bookflow.common import parse_draft

    parsed = parse_draft(draft.read_text(encoding='utf-8'))
    text = parsed['spoken'].strip()
    if not text:
        raise RuntimeError('稿件没有可合成的纯口播文本。')
    return text


def submit(args: argparse.Namespace) -> None:
    api_key, host = load_credentials(args.config)
    text = load_spoken(args.draft)
    request_id = str(uuid.uuid4())
    unique_id = str(uuid.uuid4())
    payload = {
        'user': {'uid': str(args.uid), 'unique_id': unique_id},
        'req_params': {
            'text': text,
            'model': 'seed-tts-2.0-standard',
            'speaker': args.speaker,
            'audio_params': {
                'format': 'mp3',
                'sample_rate': 24000,
                'bit_rate': 160000,
                'enable_timestamp': True,
            },
            'disable_markdown_filter': False,
            'disable_emoji_filter': True,
        },
    }
    response, headers = post_json(f'https://{host}/api/v3/tts/submit', api_key, payload, request_id, args.resource_id)
    code = response.get('code')
    if code != 20000000:
        raise RuntimeError(f'提交失败 code={code} message={response.get("message", "")[:300]}')
    data = response.get('data') or {}
    task_id = data.get('task_id')
    if not task_id:
        raise RuntimeError('提交成功但响应中没有 task_id。')
    job = {
        'task_id': task_id,
        'request_id': request_id,
        'resource_id': args.resource_id,
        'speaker': args.speaker,
        'draft': str(args.draft),
        'text_chars': len(text),
        'submitted_at': int(time.time()),
        'submit_code': code,
        'submit_message': data.get('message'),
        'logid_present': bool(headers.get('X-Tt-Logid')),
    }
    args.job.parent.mkdir(parents=True, exist_ok=True)
    args.job.write_text(json.dumps(job, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'submitted', 'task_id': task_id, 'text_chars': len(text), 'job_file': str(args.job)}, ensure_ascii=False))


def query(args: argparse.Namespace) -> None:
    api_key, host = load_credentials(args.config)
    job = json.loads(args.job.read_text(encoding='utf-8'))
    task_id = job['task_id']
    last = None
    for attempt in range(args.attempts):
        request_id = str(uuid.uuid4())
        response, _ = post_json(f'https://{host}/api/v3/tts/query', api_key, {'task_id': task_id}, request_id, job.get('resource_id', RESOURCE_ID))
        code = response.get('code')
        data = response.get('data') or {}
        status = data.get('task_status')
        last = {'code': code, 'task_status': status, 'message': data.get('message', '')}
        if status == 2:
            audio_url = data.get('audio_url')
            if not audio_url:
                raise RuntimeError('任务成功但没有 audio_url。')
            args.output.parent.mkdir(parents=True, exist_ok=True)
            try:
                with urllib.request.urlopen(audio_url, timeout=120) as response_audio:
                    audio = response_audio.read()
            except urllib.error.URLError as exc:
                raise RuntimeError(f'音频下载失败：{exc.reason}') from exc
            if not audio:
                raise RuntimeError('下载到的音频为空。')
            args.output.write_bytes(audio)
            metadata = {
                **job,
                'status': 'success',
                'query_code': code,
                'task_status': status,
                'req_text_length': data.get('req_text_length'),
                'synthesize_text_length': data.get('synthesize_text_length'),
                'url_expire_time': data.get('url_expire_time'),
                'duration_source': 'returned sentences timestamps',
                'sentences': data.get('sentences', []),
                'output': str(args.output),
                'output_bytes': len(audio),
            }
            args.metadata.parent.mkdir(parents=True, exist_ok=True)
            args.metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps({'status': 'success', 'output': str(args.output), 'bytes': len(audio), 'req_text_length': data.get('req_text_length'), 'synthesize_text_length': data.get('synthesize_text_length')}, ensure_ascii=False))
            return
        if status == 3:
            raise RuntimeError(f'合成失败 code={code} message={data.get("message", "")[:300]}')
        if attempt + 1 < args.attempts:
            time.sleep(args.interval)
    print(json.dumps({'status': 'running', 'task_id': task_id, 'last': last}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    submit_parser = sub.add_parser('submit', parents=[common])
    submit_parser.add_argument('--draft', type=Path, required=True)
    submit_parser.add_argument('--job', type=Path, required=True)
    submit_parser.add_argument('--speaker', required=True)
    submit_parser.add_argument('--resource-id', choices=('seed-tts-2.0', 'seed-icl-2.0'), default=RESOURCE_ID)
    submit_parser.add_argument('--uid', default='bookflow')
    query_parser = sub.add_parser('query', parents=[common])
    query_parser.add_argument('--job', type=Path, required=True)
    query_parser.add_argument('--output', type=Path, required=True)
    query_parser.add_argument('--metadata', type=Path, required=True)
    query_parser.add_argument('--attempts', type=int, default=12)
    query_parser.add_argument('--interval', type=int, default=5)
    args = parser.parse_args()
    submit(args) if args.command == 'submit' else query(args)


if __name__ == '__main__':
    main()
