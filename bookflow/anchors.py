from __future__ import annotations
import json,re
from pathlib import Path
from .common import latest_draft, load_yaml, write_json

def _sentences(epdir):
 episode=Path(epdir)
 final=episode/'final.sentences.json'
 draft=latest_draft(episode)
 source=final if final.is_file() else (draft.with_suffix('.sentences.json') if draft else None)
 if source is None or not source.is_file(): return {}
 d=json.loads(source.read_text()); return {x['id']:x for x in d.get('sentences',[])}


def _structured_anchors(value):
 if isinstance(value,dict):
  sid=value.get('sentence_id') or value.get('sentenceId')
  if sid: yield str(sid).lower(),str(value.get('text_sha') or value.get('textSha') or '')
  for child in value.values(): yield from _structured_anchors(child)
 elif isinstance(value,list):
  for child in value: yield from _structured_anchors(child)


def check(epdir,against=None):
 p=Path(epdir); current=_sentences(p); rows=[]
 for f in sorted((p/'production').rglob('*')) if (p/'production').exists() else []:
  if not f.is_file() or f.suffix not in ('.yaml','.yml','.json','.md') or f.name.startswith('anchors_report') or {'_reports','_history'} & set(f.parts): continue
  anchors=[]
  if f.suffix in ('.yaml','.yml','.json'):
   try:
    data=json.loads(f.read_text(encoding='utf-8')) if f.suffix=='.json' else load_yaml(f,{})
    anchors=list(_structured_anchors(data))
   except (ValueError,OSError):
    continue
  else:
   text=f.read_text(encoding='utf-8',errors='ignore')
   anchors=[(m.group(1).lower(),'') for m in re.finditer(r'(?:sentence_id|sentenceId)\s*[:=]\s*["\']?([sS]\d{3,})',text)]
  for sid,sha in anchors:
   state='ok' if sid in current and (not sha or str(current[sid].get('text_sha','')).startswith(sha)) else ('text_changed' if sid in current else 'orphaned')
   rows.append({'file':str(f),'sentence_id':sid,'status':state})
 errors=[x for x in rows if x['status']=='orphaned']; changed=[x for x in rows if x['status']=='text_changed']
 out={'passed':not errors and not changed,'errors':[f"{x['file']}: {x['sentence_id']} orphaned" for x in errors],'warnings':[f"{x['file']}: {x['sentence_id']} text_changed" for x in changed],'anchors':rows}
 reports=p/'production/_reports'
 if reports.is_symlink() or not reports.resolve().is_relative_to(p.resolve()):
  raise ValueError('自动报告目录必须位于本集 production/_reports，且不能是符号链接')
 write_json(reports/'anchors_report.json',out); return out

def migrate(epdir):
 p=Path(epdir); maps=sorted(p.glob('sentence_map_*.json')); mapping={}
 if maps:
  d=json.loads(maps[-1].read_text()); mapping={x.get('old_id'):x.get('new_id') for x in d.get('mapping',[]) if x.get('relation') in ('same','edited') and x.get('new_id')}
 result={'migrated':[],'manual':[]}
 for f in sorted((p/'production').rglob('*')) if (p/'production').exists() else []:
  if not f.is_file() or f.suffix not in ('.yaml','.yml','.json','.md') or {'_reports','_history'} & set(f.parts): continue
  text=f.read_text(encoding='utf-8'); new=text
  for old,newid in mapping.items(): new=re.sub(rf'(?P<a>(?:sentence_id|sentenceId)\s*[:=]\s*["\']?){re.escape(old)}',rf'\g<a>{newid}',new)
  if new!=text: f.write_text(new,encoding='utf-8'); result['migrated'].append(str(f))
 return {'passed':True,**result}
