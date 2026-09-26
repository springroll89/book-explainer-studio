import tempfile
import unittest
from pathlib import Path

from bookflow.common import parse_draft, validate_evidence, write_json, atomic_write, write_yaml
from bookflow.quality import verify_quotes


class ParsingTests(unittest.TestCase):
    def test_hook_anchors_to_actual_sentence_and_production_never_spoken(self):
        text = "[画面：雨夜]\n他一直在等。为什么门却开了？[钩子]\n〔延伸〕我觉得他想走。\n但他还没走。\n\n门关了。"
        parsed = parse_draft(text)
        self.assertEqual(parsed['hooks'][0]['sentence_id'], 's002')
        self.assertNotIn('雨夜', parsed['spoken'])
        self.assertNotIn('延伸', parsed['spoken'])
        self.assertEqual(parsed['commentary_chars'], 11)

    def test_external_fact_needs_verified_source(self):
        self.assertTrue(validate_evidence(['ext:E01'], {}, {'sources': [{'id': 'E01', 'status': 'pending'}]}))
        self.assertEqual(validate_evidence(['ext:E01'], {}, {'sources': [{'id': 'E01', 'status': 'verified',
                         'url': 'https://example.org/article', 'title': '证据', 'accessed_at': '2026-09-15'}]}), [])

    def test_quotes_must_follow_original_order(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            write_yaml(p / 'project.yaml', {})
            gen = 'a' * 64
            write_json(p / 'source/current.json', {'generation': gen})
            atomic_write(p / 'source/imports' / gen / 'paragraphs.jsonl',
                         '{"id":"p00001","chapter":"ch01","text":"他先开门。随后关灯。最后离开。"}\n')
            draft = p / 'draft.md'
            atomic_write(draft, '〔引 p00001〕「他先开门。……最后离开。」')
            self.assertTrue(verify_quotes(draft)['passed'])
            atomic_write(draft, '〔引 p00001〕「最后离开。……他先开门。」')
            self.assertFalse(verify_quotes(draft)['passed'])
            atomic_write(draft, '「他先开门。」')
            self.assertFalse(verify_quotes(draft)['passed'])
            atomic_write(draft, '[画面：写着「欢迎回家」的门牌]\n〔引 p00001〕「他先开门。」')
            self.assertTrue(verify_quotes(draft)['passed'])
            atomic_write(draft, '---\nsource_generation: ' + 'b' * 64 + '\n---\n〔引 p00001〕「他先开门。」')
            self.assertFalse(verify_quotes(draft)['passed'])


if __name__ == '__main__':
    unittest.main()
