"""Provider protocol tests use fake transport; no billable request is sent."""
import json
import unittest
from uuid import uuid4

from bookflow.adapters.doubao_voice import (DoubaoVoiceClient, ProviderUncertain,
                                            align_timestamps)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self, limit):
        return self.payload[:limit]


class DoubaoVoiceTests(unittest.TestCase):
    def test_submit_and_query_keep_key_out_of_results(self):
        calls = []
        replies = [FakeResponse({"code": 20000000, "data": {"task_id": "task-123"}}),
                   FakeResponse({"code": 20000000, "data": {"task_status": 1}}),
                   FakeResponse({"code": 20000000, "data": {"task_status": 2,
                                 "audio_url": "https://example.com/audio.mp3", "req_text_length": 4,
                                 "sentences": [{"text": "你好。", "startTime": 0.1, "endTime": 0.8}]}})]

        def open_fake(request, *, timeout):
            calls.append((request, timeout))
            return replies.pop(0)

        client = DoubaoVoiceClient(api_key="test-key", opener=open_fake)
        request_id = str(uuid4())
        self.assertEqual(client.submit(text="你好。", speaker="test-speaker", request_id=request_id), "task-123")
        submitted = json.loads(calls[0][0].data.decode())
        self.assertEqual(submitted["req_params"]["audio_params"]["enable_timestamp"], True)
        self.assertEqual(submitted["user"]["unique_id"], request_id)
        self.assertEqual(calls[0][0].full_url, "https://openspeech.bytedance.com/api/v3/tts/submit")
        self.assertNotIn("test-key-not-a-real-secret", repr(client))
        self.assertEqual(client.query(task_id="task-123", request_id=str(uuid4()))["state"], "running")
        done = client.query(task_id="task-123", request_id=str(uuid4()))
        self.assertEqual((done["state"], done["billable_chars"]), ("done", 4))
        self.assertEqual(len(calls), 3)

    def test_uncertain_submit_never_retries_or_leaks_provider_body(self):
        calls = []

        def failed_open(request, *, timeout):
            calls.append(request)
            raise OSError("private-provider-response")

        client = DoubaoVoiceClient(api_key="private-key", opener=failed_open)
        with self.assertRaises(ProviderUncertain) as caught:
            client.submit(text="测试。", speaker="speaker", request_id=str(uuid4()))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private-provider-response", str(caught.exception))
        self.assertNotIn("private-key", str(caught.exception))

    def test_missing_task_id_is_uncertain_not_retryable(self):
        client = DoubaoVoiceClient(api_key="test", opener=lambda *args, **kwargs:
                                   FakeResponse({"code": 20000000, "data": {}}))
        with self.assertRaisesRegex(ProviderUncertain, "不能自动重发"):
            client.submit(text="测试。", speaker="speaker", request_id=str(uuid4()))

    def test_exact_and_word_level_alignment(self):
        expected = [{"id": "s001", "text": "你好。"}, {"id": "s002", "text": "世界！"}]
        direct = [{"text": "你好", "startTime": 0.1, "endTime": 0.8},
                  {"text": "世界", "startTime": 0.9, "endTime": 1.7}]
        self.assertEqual([row["id"] for row in align_timestamps(direct, expected)], ["s001", "s002"])
        word_level = [{"text": "你好世界", "words": [
            {"word": "你", "startTime": 0.1, "endTime": 0.3},
            {"word": "好，", "startTime": 0.3, "endTime": 0.8},
            {"word": "世", "startTime": 0.9, "endTime": 1.2},
            {"word": "界", "startTime": 1.2, "endTime": 1.7}]}]
        aligned = align_timestamps(word_level, expected)
        self.assertEqual(aligned[0]["endTime"], 0.8)
        self.assertEqual(aligned[1]["startTime"], 0.9)
        with self.assertRaisesRegex(ValueError, "没有字级"):
            align_timestamps([{"text": "别的句子", "words": []}], expected)
        with self.assertRaisesRegex(ValueError, "跨越"):
            align_timestamps([{"text": "你好世界", "words": [
                {"word": "你好世", "startTime": 0.1, "endTime": 1.2},
                {"word": "界", "startTime": 1.2, "endTime": 1.7}]}], expected)


if __name__ == "__main__":
    unittest.main()
