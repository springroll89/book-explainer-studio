import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bookflow.__main__ import dispatch, parser
from bookflow.common import ROOT, atomic_write, write_yaml
from bookflow.lessons import inbox


class CliTests(unittest.TestCase):
    def test_test_command_selects_fast_or_full_suite_and_propagates_failure(self):
        for flags, expected in (([], "full"), (["--fast"], "fast")):
            for code in (0, 1):
                with self.subTest(flags=flags, code=code), \
                     patch("subprocess.run", return_value=SimpleNamespace(returncode=code)) as run:
                    result = dispatch(parser().parse_args(["test", *flags]))
                    self.assertEqual(result, {"passed": code == 0, "suite": expected})
                    command = run.call_args.args[0]
                    self.assertEqual("discover" in command, expected == "full")
                    self.assertEqual("tests.test_hygiene" in command, expected == "fast")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'bookflow', *map(str, args)],
                              cwd=ROOT, text=True, capture_output=True, check=False)

    def test_unreviewed_draft_can_be_previewed_but_not_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'projects/book'
            write_yaml(p / 'project.yaml', {'book': {'title': '试稿'},
                       'format': {'episode_minutes': [0.02, 1], 'speech_rate_cpm': 240}})
            source = p / 'raw.txt'
            atomic_write(source, '第一章\n\n他拿起钥匙。门却已经开了。\n')
            imported = self.run_cli('ingest', p, source)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            draft = p / 'episodes/ep01/draft_v1.md'
            atomic_write(draft, '---\nepisode: 1\n---\n门怎么开了？[钩子]\n〔据 p00001〕他手里还拿着钥匙。')
            output = self.run_cli('export', draft, '--preview')
            self.assertEqual(output.returncode, 0, output.stderr)
            report = json.loads(output.stdout)
            self.assertFalse(report['review'])
            page = (draft.parent / 'preview/index.html').read_text(encoding='utf-8')
            self.assertIn('待确认', page)
            self.assertIn('未完成或待修订', page)
            self.assertFalse((p / 'series_ledger.yaml').exists())
            denied = self.run_cli('export', draft, '--deliver')
            self.assertEqual(denied.returncode, 1)
            self.assertFalse((draft.parent / 'deliver').exists())
            failures = [item for item in inbox(p)['items'] if item['source'] == 'script_error']
            self.assertEqual(len(failures), 1)
            self.assertIn('正式打包', failures[0]['what_happened'])
            status = self.run_cli('status', p)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)['observed_stage'], 'preview_awaiting_human')
            self.assertFalse((p / 'status.yaml').exists())
            self.assertTrue((p / 'state.json').is_file())
            self.assertTrue((p / '进度.md').is_file())

    def test_story_check_findings_do_not_write_lesson_inbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary) / 'projects/book'
            write_yaml(p / 'project.yaml', {'profile': 'story', 'book': {'title': '夹具'}})
            write_yaml(p / 'plan/episodes.yaml', {'episodes': [{'ep': 1}, {'ep': 2}]})
            atomic_write(p / 'episodes/ep01/draft_v1.md', '2024年，他回来了。\n')
            atomic_write(p / 'episodes/ep02/draft_v1.md', '二〇二四年，他走了。\n')
            output = self.run_cli('story-check', p)
            self.assertEqual(output.returncode, 1, output.stderr)
            self.assertIn('year_style', [item['rule'] for item in json.loads(output.stdout)['items']])
            self.assertFalse((p.parent.parent / 'lessons/inbox.yaml').exists())

    def test_legacy_status_file_is_preserved_by_status_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'projects/book'
            write_yaml(project / 'project.yaml', {'book': {'title': '旧书目'}})
            legacy = project / 'status.yaml'
            write_yaml(legacy, {'user_note': '保留原状态'})
            original = legacy.read_bytes()
            result = self.run_cli('status', project)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(legacy.read_bytes(), original)
            self.assertTrue((project / 'state.json').is_file())


if __name__ == '__main__':
    unittest.main()
