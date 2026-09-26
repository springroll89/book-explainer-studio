import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from bookflow.common import write_yaml, write_json, atomic_write
from bookflow.sentences import generate
from bookflow.continuity import update, context, check as continuity_check
from bookflow.pacing import check as pacing_check

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p=Path(self.tmp.name); write_yaml(self.p/'project.yaml',{'format':{'episode_minutes':[0.01,5],'speech_rate_cpm':240},'visual_pacing':{'opening':{'window_sec':30,'min_hard_changes':2},'min_hold_sec':1.5,'max_static_sec':15}})
        gen='a'*64; write_json(self.p/'source/current.json',{'generation':gen})
        atomic_write(self.p/'source/imports'/gen/'paragraphs.jsonl','{"id":"p00001","chapter":"ch01","text":"原文。"}\n')
    def draft(self,text):
        d=self.p/'episodes/ep01/draft_v1.md'; d.parent.mkdir(parents=True); atomic_write(d,'---\nepisode: 1\n---\n'+text); return d
    def test_sentence_id_inherits_edit(self):
        d1=self.draft('第一句。第二句。'); generate(d1)
        d2=d1.parent/'draft_v2.md'; atomic_write(d2,'第一句。第二个句子。'); r=generate(d2,d1)
        self.assertEqual(r['mapping'][0]['relation'],'same'); self.assertEqual(r['mapping'][1]['relation'],'edited')
    def test_continuity_working_context(self):
        d=self.draft('第一句。'); update(self.p,1,d); self.assertTrue(context(self.p,2)['passed']); d.write_text(d.read_text()+'第二句。'); self.assertTrue(continuity_check(self.p)['invalid'])
    def test_guard_rejects_without_gate(self):
        out=subprocess.run([sys.executable,'-m','bookflow','guard',str(self.p),'sound-plan','--ep','1'],capture_output=True,text=True); self.assertEqual(out.returncode,1)

if __name__=='__main__': unittest.main()
