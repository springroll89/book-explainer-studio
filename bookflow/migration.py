from __future__ import annotations
import difflib, hashlib, json
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from .common import load_yaml, read_paragraphs, source_generation, write_json, atomic_write

def migrate(project,to_generation):
 p=Path(project); old=read_paragraphs(p); target=p/'source/imports'/to_generation/'paragraphs.jsonl'
 if not target.exists(): return {'passed':False,'errors':[f'目标原文批次不存在：{to_generation}']}
 new={}
 for line in target.read_text(encoding='utf-8').splitlines():
  if line.strip():
   x=json.loads(line); new[x['id']]=x
 mapping=[]; used=set(); exact={}
 old_items=list(old.items()); new_items=list(new.items())
 def digest(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()
 by_hash=defaultdict(deque)
 for nid,row in new_items: by_hash[digest(row.get('text',''))].append(nid)
 for oid,row in old_items:
  candidates=by_hash[digest(row.get('text',''))]
  if candidates:
   exact[oid]=candidates.popleft(); used.add(exact[oid])
 for index,(oid,o) in enumerate(old_items):
  if oid in exact:
   mapping.append({'old_pid':oid,'new_pid':exact[oid],'relation':'same','similarity':1.0})
   continue
  best=None; score=0.0
  center=round(index*len(new_items)/max(1,len(old_items)))
  for nid,n in new_items[max(0,center-24):min(len(new_items),center+25)]:
   if nid in used: continue
   old_text=o.get('text',''); new_text=n.get('text','')
   if not old_text or not new_text or min(len(old_text),len(new_text))/max(len(old_text),len(new_text))<0.6: continue
   similarity=difflib.SequenceMatcher(None,old_text,new_text).ratio()
   if similarity>score: best,score=nid,similarity
  if best and score>=0.8:
   used.add(best); mapping.append({'old_pid':oid,'new_pid':best,'relation':'edited','similarity':round(score,3)})
  else: mapping.append({'old_pid':oid,'new_pid':None,'relation':'deleted'})
 for nid in new:
  if nid not in used: mapping.append({'old_pid':None,'new_pid':nid,'relation':'new'})
 outdir=p/'source/imports'/to_generation; write_json(outdir/'pid_map.json',{'from':source_generation(p),'to':to_generation,'mapping':mapping}); atomic_write(outdir/'migration_report.md','# 原文段落迁移报告\n\n'+json.dumps(mapping,ensure_ascii=False,indent=2)+'\n')
 return {'passed':True,'output':str(outdir/'migration_report.md'),'mapping':mapping}

def switch(project,to_generation):
 p=Path(project); report=p/'source/imports'/to_generation/'pid_map.json'
 if not report.exists(): return {'passed':False,'errors':['请先运行 source migrate 生成迁移报告']}
 if not sys.stdin.isatty(): return {'passed':False,'errors':['source switch 需要用户在交互终端确认']}
 if input(f'请输入“切换原文 {to_generation[:8]}”以确认：').strip()!=f'切换原文 {to_generation[:8]}': return {'passed':False,'errors':['确认短语不匹配，未切换']}
 cur=p/'source/current.json'; backup=p/'source/backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')/'current.json'; backup.parent.mkdir(parents=True,exist_ok=True)
 if cur.exists(): backup.write_bytes(cur.read_bytes())
 atomic_write(cur,json.dumps({'generation':to_generation,'switched_at':datetime.now(timezone.utc).isoformat()},ensure_ascii=False,indent=2)+'\n')
 return {'passed':True,'generation':to_generation,'backup':str(backup)}
