from __future__ import annotations
import json, math, re
from pathlib import Path
from .common import latest_draft, load_config, load_yaml, parse_draft, sha256_file, write_json, atomic_write

def _reports_dir(epdir):
 reports=Path(epdir)/'production/_reports'
 if reports.is_symlink() or not reports.resolve().is_relative_to(Path(epdir).resolve()):
  raise ValueError('自动报告目录必须位于本集 production/_reports，且不能是符号链接')
 return reports

def _shots(path):
 p=Path(path)
 candidates=[p/'production/storyboard.yaml',p/'production/storyboard.yml',p/'production/shots.yaml']
 for f in candidates:
  if f.exists():
   d=load_yaml(f,{}) or {}; return d.get('shots',d if isinstance(d,list) else [])
 js=sorted((p/'production').glob('storyboard*.json')) if (p/'production').exists() else []
 if js:
  try:
   d=json.loads(js[-1].read_text(encoding='utf-8'))
   if isinstance(d,dict): return d.get('shots',[])
   if isinstance(d,list): return d
  except (OSError, json.JSONDecodeError):
   pass
 md=sorted((p/'production').glob('storyboard*.md')) if (p/'production').exists() else []
 if md:
  rows=[]
  for line in md[-1].read_text(encoding='utf-8').splitlines():
   if not line.startswith('|') or line.count('|')<4 or '---' in line: continue
   c=[x.strip() for x in line.strip('|').split('|')]
   try:
    cell=c[1] if len(c)>1 else c[0]
    vals=[]
    for part in re.split(r'\s*[–-]\s*',cell):
     m=re.fullmatch(r'(\d+):(\d+(?:\.\d+)?)',part.strip())
     vals.append(float(m.group(1))*60+float(m.group(2)) if m else float(part))
    if len(vals)>=2: rows.append({'time_actual':{'start':vals[0],'end':vals[1]},'asset_id':c[3] if len(c)>3 else ''})
   except Exception: pass
  return rows
 return []

def budget(epdir):
 p=Path(epdir); draft=latest_draft(p,prefer_final=True)
 if draft is None: raise ValueError(f'找不到稿件：{p}')
 parsed=parse_draft(draft.read_text(encoding='utf-8')); cfg=load_config(p); preset=cfg.get('visual_pacing',{}); genre=cfg.get('genre',{}).get('primary','suspense'); avg=(preset.get('genre_presets',{}).get(genre,{}).get('avg_hold_sec') or preset.get('avg_hold_sec',[8,12])); avg_sec=sum(avg)/2
 rows=[]
 for sec in parsed.get('sections',[]):
  start=sec.get('offset',0); nexts=[x for x in parsed['sections'] if x.get('offset',0)>start]; end=nexts[0]['offset'] if nexts else parsed['total_chars']; dur=(end-start)*60/cfg['format']['speech_rate_cpm']; role='opening' if start==0 else 'setup'; target=(preset.get('segment_targets',{}).get(role,{}).get('avg') or avg); n=max(1,math.ceil(dur/(sum(target)/2))); rows.append({'section':sec.get('title',''),'segment_role':role,'seconds':round(dur,2),'recommended_shots':n,'recommended_new_assets':n})
 total=parsed['total_chars']*60/cfg['format']['speech_rate_cpm']; shots=max(1,math.ceil(total/avg_sec)); out={'episode':int(parsed['meta'].get('episode',1)),'estimated_seconds':round(total,2),'avg_hold_target':avg,'recommended_shots':shots,'recommended_assets':shots,'sections':rows}
 reports=_reports_dir(p); atomic_write(reports/'shot_budget.md','# 镜头预算\n\n'+json.dumps(out,ensure_ascii=False,indent=2)+'\n'); write_json(reports/'shot_budget.json',out); return {'passed':True,**out,'output':str(reports/'shot_budget.md')}

def check(epdir):
 p=Path(epdir); cfg=load_config(p).get('visual_pacing',{}); shots=_shots(p); errors=[]; warnings=[]; durations=[]; assets=[]; seen_assets={}; seen_images={}
 for i,s in enumerate(shots):
  t=s.get('time_actual') or s.get('time_est') or s.get('time') or {}; start=t.get('start',s.get('start',0)); end=t.get('end',s.get('end',start)); dur=float(end)-float(start); durations.append(dur); image=s.get('image'); aid=s.get('asset_id') or s.get('asset') or s.get('assetId') or image; assets.append(aid if isinstance(aid,str) else None)
  if not isinstance(aid,str) or not aid.strip(): errors.append(f'{i+1}: 缺少独立剧情图 asset_id 或 image')
  elif aid in seen_assets: errors.append(f'{i+1}: 剧情图 asset_id 与第 {seen_assets[aid]} 镜重复')
  else: seen_assets[aid]=i+1
  if isinstance(image,str) and image.strip():
   image_key=str((p/'production'/image).resolve())
   if image_key in seen_images: errors.append(f'{i+1}: 剧情图文件与第 {seen_images[image_key]} 镜重复')
   else: seen_images[image_key]=i+1
  min_hold=cfg.get('min_hold_sec',8); max_hold=cfg.get('max_static_sec',15)
  if dur<min_hold: errors.append(f'{i+1}: 画面停留 {dur:.2f}s 小于下限 {min_hold}s')
  if dur>max_hold: errors.append(f'{i+1}: 画面停留 {dur:.2f}s 超过上限 {max_hold}s')
  if s.get('fact_level')=='source' and not s.get('evidence'):
   errors.append(f'{i+1}: source 画面缺少 evidence')
  crop=s.get('crop')
  if isinstance(crop,dict) and crop.get('w') and crop.get('h') and min(float(crop['w']),float(crop['h']))<cfg.get('crop',{}).get('min_crop_scale',0.7):
   errors.append(f'{i+1}: 裁切区域低于分辨率下限')
  overlay=s.get('overlay') or {}
  if overlay.get('type')=='info' and len(str(overlay.get('text','')))>cfg.get('overlay',{}).get('info_max_chars',40): warnings.append(f'{i+1}: 信息图文字超过40字')
  if i>=2 and dur<3 and durations[i-1]<3 and durations[i-2]<3 and s.get('segment_role')!='opening': warnings.append(f'{i+1}: 连续短镜头过碎')
 window=cfg.get('opening',{}).get('window_sec',30)
 opening=sum(1 for s,t in zip(shots,durations) if (s.get('time_actual') or s.get('time_est') or {}).get('start',0)<window)
 hard=max(0,opening-1)
 internal_changes=0
 for i,s in enumerate(shots):
  t=s.get('time_actual') or s.get('time_est') or s.get('time') or {}
  start=float(t.get('start',s.get('start',0))); end=float(t.get('end',s.get('end',start)))
  for beat in s.get('strong_change_beats',[]):
   at=float(beat.get('time',-1)) if isinstance(beat,dict) else float(beat)
   if at<start or at>end:
    errors.append(f'{i+1}: 开场强变化时点 {at:.2f}s 超出所属镜头区间')
   elif at<window:
    internal_changes+=1
 hard+=internal_changes
 if hard<cfg.get('opening',{}).get('min_hard_changes',6): errors.append(f'开场30秒强变化 {hard} 次，少于 {cfg.get("opening",{}).get("min_hard_changes",6)} 次')
 out={'passed':not errors,'errors':errors,'warnings':warnings,'metrics':{'shots':len(shots),'assets':len(set(a for a in assets if a)),'reuse_factor':round(len(shots)/max(1,len(set(a for a in assets if a))),2),'average_hold':round(sum(durations)/len(durations),2) if durations else 0,'longest_hold':max(durations) if durations else 0,'opening_hard_changes':hard,'time_source':'time_actual' if any('time_actual' in s for s in shots) else 'time_est'}}
 reports=_reports_dir(p); write_json(reports/'pacing_report.json',out); atomic_write(reports/'pacing_report.md','# 节奏检查\n\n'+json.dumps(out,ensure_ascii=False,indent=2)+'\n'); atomic_write(reports/'pacing_timeline.html','<html><body><h1>画面节奏时间线</h1><pre>'+json.dumps(out,ensure_ascii=False,indent=2)+'</pre></body></html>'); return out

def baseline(epdir):
 p=Path(epdir); report_path=_reports_dir(p)/'pacing_report.json'
 if not report_path.is_file(): report_path=p/'production/pacing_report.json'
 report=load_yaml(report_path,{})
 from .approvals import confirmation_state
 project=p
 while project != project.parent and not (project/'project.yaml').exists(): project=project.parent
 ep=int(p.name[2:]) if p.name.startswith('ep') and p.name[2:].isdigit() else 1
 if confirmation_state(project,'sample',ep)['state']!='passed':
  return {'passed':False,'errors':['pacing baseline 需要有效的样片确认记录']}
 if not report: return {'passed':False,'errors':['请先运行 pacing check']}
 output=p/'production/pacing_baseline.json'; metrics=report.get('metrics',{})
 write_json(output,{'episode':ep,'report_sha256':sha256_file(report_path),'metrics':metrics})
 return {'passed':True,'baseline':metrics,'output':str(output)}
