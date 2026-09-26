import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bookflow.common import ROOT, atomic_write, write_yaml


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'bookflow', *map(str, args)],
                              cwd=ROOT, text=True, capture_output=True, check=False)

    def test_unreviewed_draft_can_be_previewed_but_not_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
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
            status = self.run_cli('status', p)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)['observed_stage'], 'preview_awaiting_human')


if __name__ == '__main__':
    unittest.main()
