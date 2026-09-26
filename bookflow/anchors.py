from __future__ import annotations
import json,re
from pathlib import Path
from .common import load_yaml, write_json

def _sentences(epdir):
 files=sorted(Path(epdir).glob('draft_v*.sentences.json'))
 if not files: return {}
 d=json.loads(files[-1].read_text()); return {x['id']:x for x in d.get('sentences',[])}
def check(epdir,against=None):
 p=Path(epdir); current=_sentences(p); rows=[]
 for f in sorted((p/'production').rglob('*')) if (p/'production').exists() else []:
  if not f.is_file() or f.suffix not in ('.yaml','.yml','.json','.md'): continue
  text=f.read_text(encoding='utf-8',errors='ignore')
  for m in re.finditer(r'(?:sentence_id|sentenceId)\s*[:=]\s*["\']?([sS]\d{3,})',text):
   sid=m.group(1).lower(); sha_m=re.search(r'text_sha\s*[:=]\s*["\']?([0-9a-f]{16,64})',text[max(0,m.start()-160):m.end()+160])
   state='ok' if sid in current and (not sha_m or current[sid]['text_sha'].startswith(sha_m.group(1))) else ('text_changed' if sid in current else 'orphaned')
   rows.append({'file':str(f),'sentence_id':sid,'status':state})
 errors=[x for x in rows if x['status']=='orphaned']; changed=[x for x in rows if x['status']=='text_changed']
 out={'passed':not errors and not changed,'errors':[f"{x['file']}: {x['sentence_id']} orphaned" for x in errors],'warnings':[f"{x['file']}: {x['sentence_id']} text_changed" for x in changed],'anchors':rows}
 write_json(p/'production/anchors_report.json',out); return out

def migrate(epdir):
 p=Path(epdir); maps=sorted(p.glob('sentence_map_*.json')); mapping={}
 if maps:
  d=json.loads(maps[-1].read_text()); mapping={x.get('old_id'):x.get('new_id') for x in d.get('mapping',[]) if x.get('relation') in ('same','edited') and x.get('new_id')}
 result={'migrated':[],'manual':[]}
 for f in sorted((p/'production').rglob('*')) if (p/'production').exists() else []:
  if not f.is_file() or f.suffix not in ('.yaml','.yml','.json','.md'): continue
  text=f.read_text(encoding='utf-8'); new=text
  for old,newid in mapping.items(): new=re.sub(rf'(?P<a>(?:sentence_id|sentenceId)\s*[:=]\s*["\']?){re.escape(old)}',rf'\g<a>{newid}',new)
  if new!=text: f.write_text(new,encoding='utf-8'); result['migrated'].append(str(f))
 return {'passed':True,**result}
