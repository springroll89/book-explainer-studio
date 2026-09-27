from __future__ import annotations
import difflib, re
from pathlib import Path
from .common import parse_draft, sha256_file, write_json

def _norm(text): return re.sub(r'\s+','',text).strip()
def generate(draft:Path, previous:Path|None=None, *, write_mapping:bool=True)->dict:
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
 if previous and write_mapping:
  stem=f'sentence_map_{Path(previous).stem}_to_{draft.stem}.json'; write_json(draft.parent/stem,{'from':str(previous),'to':str(draft),'mapping':mapping})
 return {'passed':True,'sentences':len(rows),'mapping':mapping,'output':str(draft.with_suffix('.sentences.json'))}


def _previous_version(draft: Path) -> Path | None:
    if draft.name == "final.md":
        from .common import latest_draft
        return latest_draft(draft.parent)
    match = re.fullmatch(r"draft_v(\d+)\.md", draft.name)
    if not match:
        return None
    number = int(match.group(1))
    candidates = [(int(m.group(1)), path) for path in draft.parent.glob("draft_v*.md")
                  if (m := re.fullmatch(r"draft_v(\d+)\.md", path.name)) and int(m.group(1)) < number]
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def parse_draft_file(draft: Path) -> dict:
    """Parse a saved script with its stable sentence IDs, creating a missing table."""
    import json

    draft = Path(draft)
    parsed = parse_draft(draft.read_text(encoding="utf-8"))
    sidecar = draft.with_suffix(".sentences.json")
    if not sidecar.is_file():
        generate(draft, _previous_version(draft))
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    stable = data.get("sentences", [])
    stale = data.get("draft_sha256") and data["draft_sha256"] != sha256_file(draft)
    mismatched = len(stable) != len(parsed["sentences"]) or any(
        row.get("text") != provisional["text"] or not row.get("id")
        for row, provisional in zip(stable, parsed["sentences"])
    )
    if stale or mismatched:
        generate(draft, draft, write_mapping=False)
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        stable = data.get("sentences", [])
        if len(stable) != len(parsed["sentences"]):
            raise ValueError(f"稳定句子表与稿件不一致：{sidecar}")
    mapping = {provisional["id"]: row["id"] for provisional, row in zip(parsed["sentences"], stable)}
    for provisional, row in zip(parsed["sentences"], stable):
        provisional["id"] = row["id"]
    for hook in parsed["hooks"]:
        hook["sentence_id"] = mapping.get(hook["sentence_id"], "")
    return parsed
