from __future__ import annotations
import difflib, re
from pathlib import Path
from .common import parse_draft, sha256_file, write_json

def _norm(text): return re.sub(r'\s+','',text).strip()
def generate(draft:Path, previous:Path|None=None)->dict:
 draft=Path(draft); parsed=parse_draft(draft.read_text(encoding='utf-8')); old=[]
 if previous:
  op=Path(previous).with_suffix('.sentences.json')
  if op.exists(): old=__import__('json').loads(op.read_text()).get('sentences',[])
  elif Path(previous).exists(): old=parse_draft(Path(previous).read_text(encoding='utf-8'))['sentences']
 used=set(); maxnum=max([int(str(x.get('id','s0'))[1:]) for x in old if str(x.get('id',''))[1:].isdigit()] or [0]); rows=[]; mapping=[]
 for i,s in enumerate(parsed['sentences']):
  best=None; score=0
  for j,o in enumerate(old):
   if j in used: continue
   q=difflib.SequenceMatcher(None,_norm(s['text']),_norm(o.get('text',''))).ratio()
   if q>score: best,score=j,q
  if best is not None and score>=0.8:
   used.add(best); sid=old[best]['id']; rel='same' if _norm(s['text'])==_norm(old[best].get('text','')) else 'edited'
   mapping.append({'old_id':sid,'new_id':sid,'relation':rel,'similarity':round(score,3)})
  else:
   maxnum+=1; sid=f's{maxnum:03d}'; mapping.append({'old_id':None,'new_id':sid,'relation':'new'})
  rows.append({'id':sid,'text':s['text'],'text_sha':__import__('hashlib').sha256(s['text'].encode()).hexdigest(),'section':s.get('section',''),'order':i+1})
 for j,o in enumerate(old):
  if j not in used: mapping.append({'old_id':o.get('id'),'new_id':None,'relation':'deleted'})
 # Mark obvious split/merge operations for downstream anchor review.
 for i,o in enumerate(old):
  joined=''
  for k in range(i, min(i+3,len(old))):
   joined += _norm(old[k].get('text',''))
   for ni,n in enumerate(rows):
    if _norm(n['text'])==joined and k>i:
     for m in mapping:
      if m.get('old_id')==old[i].get('id') or m.get('old_id')==old[k].get('id'):
       m['relation']='merged'; m['new_id']=n['id']
 for i,o in enumerate(old):
  for ni in range(len(rows)-1):
   if _norm(o.get('text',''))==_norm(rows[ni]['text'])+_norm(rows[ni+1]['text']):
    for m in mapping:
     if m.get('old_id')==o.get('id'): m['relation']='split'; m['new_id']=rows[ni]['id']
    mapping.append({'old_id':o.get('id'),'new_id':rows[ni+1]['id'],'relation':'split'})
 out={'draft':str(draft),'draft_sha256':sha256_file(draft),'sentences':rows}
 write_json(draft.with_suffix('.sentences.json'),out)
 if previous:
  stem=f'sentence_map_{Path(previous).stem}_to_{draft.stem}.json'; write_json(draft.parent/stem,{'from':str(previous),'to':str(draft),'mapping':mapping})
 return {'passed':True,'sentences':len(rows),'mapping':mapping,'output':str(draft.with_suffix('.sentences.json'))}
