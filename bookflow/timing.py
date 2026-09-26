from __future__ import annotations
import json, hashlib
from pathlib import Path
from .common import parse_draft, sha256_file, write_json, find_project

def import_timing(epdir,audio,timestamps=None):
 p=Path(epdir); draft=sorted(p.glob('draft_v*.md'))[-1]; parsed=parse_draft(draft.read_text(encoding='utf-8')); rows=[]
 if timestamps and Path(timestamps).exists():
  d=json.loads(Path(timestamps).read_text()); rows=d.get('sentences',d) if isinstance(d,(dict,list)) else []
 if not rows:
  cpm=240; t=0
  for s in parsed['sentences']:
   dur=s['chars']*60/cpm; rows.append({'id':s['id'],'text':s['text'],'start':round(t,3),'end':round(t+dur,3)}); t+=dur
 out={'draft':str(draft),'draft_sha256':sha256_file(draft),'audio':str(Path(audio).resolve()),'audio_sha256':sha256_file(Path(audio)),'sentences':rows,'source':'timestamps' if timestamps else 'estimate'}
 write_json(p/'production/timing.json',out); return {'passed':True,'output':str(p/'production/timing.json'),'source':out['source'],'sentences':len(rows)}
