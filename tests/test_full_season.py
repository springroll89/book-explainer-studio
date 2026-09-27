import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookflow.__main__ import new_project
from bookflow.common import atomic_write, load_yaml, parse_draft, sha256_file, source_generation, write_yaml
from bookflow.continuity import context, update
from bookflow.guard import check
from bookflow.review import evaluate
from bookflow.source import ingest
from bookflow.states import derive


class FullSeasonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'book'
        self.cfg = {'drafting': {'mode': 'full_season_review'}}
        write_yaml(self.project / 'project.yaml', self.cfg)
        original = Path(self.temp.name) / 'source.txt'
        atomic_write(original, '第一章\n\n他拿起钥匙。门却已经开了。\n')
        ingest(self.project, [original])
        self.plan = {'episodes': [
            {'ep': ep, 'title_working': f'第 {ep} 集', 'genre_mode': 'suspense',
             'covers': ['ch01'], 'target_chars': 3000, 'core_question': '门为何打开？',
             'takeaways': [
                 {'id': f'K{ep}a', 'layer': 2, 'core': True, 'text': '动作暴露疑点', 'evidence': ['p00001']},
                 {'id': f'K{ep}b', 'layer': 3, 'core': True, 'text': '钥匙带来疑问', 'evidence': ['p00001']}],
             'reveal': [], 'withhold': [], 'recap': '前面的疑问', 'cliffhanger': '门为何打开？'}
            for ep in range(1, 4)]}
        write_yaml(self.project / 'plan/episodes.yaml', self.plan)
        self.gates = patch('bookflow.guard.confirmation_state',
                           side_effect=lambda p, g, ep=None: {'state': 'passed' if g == 'plan' else 'invalidated'})
        self.gates.start()
        self.addCleanup(self.gates.stop)

    def draft(self, ep, version=1):
        path = self.project / f'episodes/ep{ep:02d}/draft_v{version}.md'
        atomic_write(path, f'---\nepisode: {ep}\nstatus: draft\n---\n〔据 p00001〕门怎么开了？[钩子]\n他手里还拿着钥匙。')
        return path

    def report(self, ep, draft):
        dependency = {str(e['ep']): e['draft_sha256'] for e in context(self.project, ep)['episodes']}
        meta = {'draft_sha256': sha256_file(draft), 'source_generation': source_generation(self.project),
                'dependency_hashes': dependency}
        parsed = parse_draft(draft.read_text())
        listener = {'engaged': [{'sentence_id': parsed['sentences'][0]['id'],
                                'trigger': parsed['sentences'][0]['text'], 'why': '疑问明确'}],
                    'dropoff': [], 'takeaway': '钥匙与门构成疑问'}
        core = [{'id': f'K{ep}{suffix}', 'delivered': True, 'supported': True} for suffix in ('a', 'b')]
        roles = {}
        for role in ('fact', 'listener', 'deai'):
            original = {**meta, 'findings': []}
            if role == 'fact': original['core_takeaways'] = core
            if role == 'listener': original['listener'] = listener
            write_yaml(draft.parent / f'review/{role}.yaml', original)
            roles[role] = {'status': 'completed', 'independent': True, 'report': f'review/{role}.yaml'}
        return {**meta, 'core_takeaways': core, 'findings': [], 'listener': listener, 'reviewers': roles}

    def test_all_text_actions_allow_unapproved_full_season(self):
        for action in ('outline', 'draft', 'review', 'edit-copy'):
            with self.subTest(action=action):
                result = check(self.project, action, 3)
                self.assertTrue(result['passed'], result)
                self.assertEqual(result['required_confirmations'], ['方案'])
                self.assertFalse(result['checks']['continuity']['complete'])
                self.assertEqual(result['checks']['continuity']['missing_episodes'], [1, 2])
        self.assertFalse(context(self.project, 3)['passed'])

    def test_g1_and_media_gates_are_not_waived(self):
        with patch('bookflow.guard.confirmation_state', return_value={'state': 'pending'}):
            self.assertFalse(check(self.project, 'draft', 3)['passed'])
        for action in ('sound-plan', 'visual', 'media-generate', 'export-preview', 'export-deliver'):
            self.assertFalse(check(self.project, action, 3)['passed'], action)
        self.assertEqual(list((self.project / 'approvals').glob('*.yaml')), [])

    def test_invalid_plan_source_and_episode_still_block(self):
        self.assertFalse(check(self.project, 'draft', 4)['passed'])
        self.plan['episodes'][0]['takeaways'][0]['evidence'] = ['p99999']
        write_yaml(self.project / 'plan/episodes.yaml', self.plan)
        self.assertFalse(check(self.project, 'draft', 3)['passed'])
        (self.project / 'source/current.json').unlink()
        self.assertFalse(check(self.project, 'draft', 3)['passed'])

    def test_stale_and_superseded_working_drafts_still_block(self):
        first = self.draft(1)
        update(self.project, 1, first)
        self.assertTrue(check(self.project, 'draft', 3)['passed'])
        first.write_text(first.read_text() + '\n他把门推开。')
        self.assertFalse(check(self.project, 'draft', 3)['passed'])
        update(self.project, 1, first)
        self.draft(1, 2)
        self.assertFalse(check(self.project, 'draft', 3)['passed'])

    def test_episode_mode_later_drafts_follow_first_script_confirmation(self):
        write_yaml(self.project / 'project.yaml', {})
        result = check(self.project, 'draft', 3)
        self.assertFalse(result['passed'])
        self.assertEqual(result['required_confirmations'], ['方案', '文案 第1集'])

    def test_restamping_prior_working_draft_does_not_clear_later_dependency(self):
        first = self.draft(1)
        second = self.draft(2)
        update(self.project, 1, first)
        update(self.project, 2, second)
        self.assertTrue(context(self.project, 3)['passed'])
        first.write_text(first.read_text() + '\n他离开了。')
        update(self.project, 1, first)
        result = context(self.project, 3)
        self.assertFalse(result['passed'])
        self.assertTrue(any('依赖第 1 集' in e for e in result['errors']))
        self.assertFalse(check(self.project, 'draft', 3)['passed'])

    def test_parallel_continuity_requires_final_dependency_completion(self):
        update(self.project, 2, self.draft(2))
        update(self.project, 1, self.draft(1))
        self.assertFalse(context(self.project, 3)['passed'])
        self.assertFalse(context(self.project, 3, allow_missing=True)['complete'])
        update(self.project, 2, self.project / 'episodes/ep02/draft_v1.md')
        self.assertTrue(context(self.project, 3)['passed'])

    def test_new_project_explicitly_uses_full_season(self):
        with patch('bookflow.__main__.ROOT', Path(self.temp.name)):
            result = new_project('new-book', '书', '作者', 'suspense')
        self.assertEqual(load_yaml(Path(result['project']) / 'project.yaml')['drafting']['mode'], 'full_season_review')

    def test_status_waits_for_complete_season_not_g3(self):
        self.draft(1)
        result = derive(self.project)
        self.assertEqual(result['season_drafts']['missing_episodes'], [2, 3])
        self.assertTrue(any('继续生成' in item for item in result['next_steps']))
        for ep in range(1, 4):
            update(self.project, ep, self.draft(ep))
        result = derive(self.project)
        self.assertTrue(result['season_drafts']['complete'])
        self.assertTrue(result['season_drafts']['continuity_complete'])
        self.assertTrue(any('统一阅读' in item for item in result['awaiting_human']))
        self.assertFalse(any('G3' in item for item in result['awaiting_human']))
        self.assertFalse(any('on_hold' in item['flags'] for item in result['episodes']))
        self.assertEqual(result['gates']['G3'], 'pending')

    def test_status_stops_waiting_for_season_review_after_user_marks_it_complete(self):
        for ep in range(1, 4):
            update(self.project, ep, self.draft(ep))
        write_yaml(self.project / 'notes.yaml', {'workflow': {'season_review_status': 'completed'}})

        result = derive(self.project)

        self.assertTrue(result['season_drafts']['continuity_complete'])
        self.assertFalse(any('统一阅读' in item for item in result['awaiting_human']))
        self.assertFalse(any('awaiting_season_review' in item['flags'] for item in result['episodes']))

    def test_draft_review_accepts_working_hashes_but_final_does_not(self):
        update(self.project, 1, self.draft(1))
        draft = self.draft(2)
        report = self.report(2, draft)
        result = evaluate(self.project, 2, draft, report)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['review_mode'], 'full_season_draft')
        final = draft.parent / 'final.md'
        final.write_bytes(draft.read_bytes())
        result = evaluate(self.project, 2, final, report)
        self.assertFalse(result['passed'])
        self.assertTrue(any('当前定稿' in e for e in result['errors']))
        draft.write_text(draft.read_text().replace('status: draft', 'status: final'))
        report = self.report(2, draft)
        self.assertFalse(evaluate(self.project, 2, draft, report)['passed'])

    def test_draft_review_rejects_changed_prior_version_and_stale_role_hash(self):
        first = self.draft(1)
        update(self.project, 1, first)
        draft = self.draft(2)
        report = self.report(2, draft)
        role_path = draft.parent / 'review/fact.yaml'
        role = load_yaml(role_path)
        role['dependency_hashes']['1'] = 'old'
        write_yaml(role_path, role)
        self.assertFalse(evaluate(self.project, 2, draft, report)['passed'])
        self.report(2, draft)
        first.write_text(first.read_text() + '\n门开了。')
        self.assertFalse(evaluate(self.project, 2, draft, report)['passed'])


if __name__ == '__main__':
    unittest.main()
