from __future__ import annotations
import difflib, json
import sys
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
 mapping=[]; used=set()
 for oid,o in old.items():
  best=None; score=0
  for nid,n in new.items():
   if nid in used: continue
   s=difflib.SequenceMatcher(None,o.get('text',''),n.get('text','')).ratio()
   if s>score: best,score=nid,s
  if best and score>=0.8: used.add(best); rel='same' if score==1 else 'edited'; mapping.append({'old_pid':oid,'new_pid':best,'relation':rel,'similarity':round(score,3)})
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
 cur.write_text(json.dumps({'generation':to_generation,'switched_at':datetime.now(timezone.utc).isoformat()},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 return {'passed':True,'generation':to_generation,'backup':str(backup)}
