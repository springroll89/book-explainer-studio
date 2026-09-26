from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from .common import full_season_review, load_yaml, parse_draft, sha256_file, source_generation, write_yaml
from .ledger import check as ledger_check
from .approvals import gate_state

def _path(project): return Path(project)/'episodes/working_continuity.yaml'
def _load(project):
 d=load_yaml(_path(project),{}) or {}; d.setdefault('episodes',[]); return d

def update(project,ep,draft):
 p=Path(project); draft=Path(draft); parsed=parse_draft(draft.read_text(encoding='utf-8')); d=_load(p)
 previous=context(p,ep,allow_missing=True)
 if previous['errors']: return {'passed':False,'errors':previous['errors']}
 meta=parsed['meta']
 if meta.get('episode',ep)!=ep or meta.get('source_generation',source_generation(p))!=source_generation(p):
  return {'passed':False,'errors':['稿件集号或原文批次与当前项目不一致']}
 entry={'ep':ep,'draft_path':str(draft.resolve().relative_to(p.resolve())),'draft_sha256':sha256_file(draft),'source_generation':source_generation(p),'generated_at':datetime.now(timezone.utc).isoformat(),'generated_by':'continuity update','status':'working','revealed':[],'threads_setup':[],'threads_payoff':[],'identities_known':{},'open_questions':[],'recap_points':[s['text'] for s in parsed['sentences'][-3:]],'actual_takeaways':[],'takeaways_delivered':[],'devices':{},'deviations_from_plan':[]}
 entry['dependency_hashes']={str(e['ep']):e.get('final_sha256') if e['basis']=='ledger' else e.get('draft_sha256') for e in previous['episodes']}
 entry['dependency_basis']={str(e['ep']):'ledger' if e['basis']=='ledger' else 'working' for e in previous['episodes']}
 d['episodes']=[x for x in d['episodes'] if not isinstance(x,dict) or x.get('ep')!=ep]+[entry]; d['episodes'].sort(key=lambda x:x.get('ep',0)); write_yaml(_path(p),d); return {'passed':True,'entry':entry}

def _working_problems(project,e):
 p=Path(project); draft=p/e.get('draft_path',''); problems=[]
 if not draft.is_file() or sha256_file(draft)!=e.get('draft_sha256'): problems.append('draft_sha256 不一致')
 if e.get('source_generation')!=source_generation(p): problems.append('source_generation 已变化')
 versions=[x for x in draft.parent.glob('draft_v*.md') if x.stem.split('_v')[-1].isdigit()]
 if versions and max(versions,key=lambda x:int(x.stem.split('_v')[-1])).resolve()!=draft.resolve(): problems.append('已有更新的草稿版本')
 dependencies=e.get('dependency_hashes',{})
 if not isinstance(dependencies,dict): return problems+['dependency_hashes 必须是映射']
 entries={str(x.get('ep')):x for x in _load(p)['episodes'] if isinstance(x,dict)}
 for number,digest in dependencies.items():
  if e.get('dependency_basis',{}).get(str(number))=='ledger':
   target=p/'episodes'/f'ep{int(number):02d}'/'final.md'
  else: target=p/entries.get(str(number),{}).get('draft_path','')
  if not target.is_file() or sha256_file(target)!=digest: problems.append(f'依赖第 {number} 集的版本已变化，须复核后重新登记')
 return problems

def context(project,ep,allow_missing=False):
 p=Path(project); d=_load(p); led=load_yaml(p/'series_ledger.yaml',{}) or {}; le={x.get('ep'):x for x in led.get('episodes',[]) if isinstance(x,dict)} if isinstance(led,dict) else {}; result=[]; errors=[]; missing=[]; pending_dependencies={}
 if type(ep) is not int or ep<1: return {'passed':False,'errors':['集号必须为正整数'],'episodes':[],'warnings':[]}
 ledger_states={x['ep']:x['passed'] for x in ledger_check(p).get('episodes',[])} if le else {}
 for n in range(1,ep):
  if n in le and ledger_states.get(n) and gate_state(p,'G4',n)=='passed':
   result.append({'ep':n,'basis':'ledger',**le[n]}); continue
  e=next((x for x in d['episodes'] if isinstance(x,dict) and x.get('ep')==n),None)
  if not e:
   missing.append(n)
   if not allow_missing: errors.append(f'第 {n} 集没有有效账本或工作连续性条目')
   continue
  problems=_working_problems(p,e)
  if problems: errors.append(f'第 {n} 集工作连续性已失效：'+ '；'.join(problems)); continue
  if full_season_review(p):
   dependencies=e.get('dependency_hashes',{})
   absent=[i for i in range(1,n) if not dependencies.get(str(i),dependencies.get(i))]
   if absent:
    pending_dependencies[n]=absent
    if not allow_missing: errors.append(f'第 {n} 集尚未绑定前序工作稿依赖：{absent}')
  result.append({'ep':n,'basis':'working@'+e['draft_sha256'][:8],**e})
 warnings=[f'第 {", ".join(map(str,missing))} 集工作连续性待补：并行初稿暂用计划限制信息揭示，整季汇总前必须按实际稿件补录并复核衔接。'] if allow_missing and missing else []
 if allow_missing and pending_dependencies: warnings.append(f'已有工作稿的前序依赖待补：{pending_dependencies}')
 return {'passed':not errors,'errors':errors,'warnings':warnings,'complete':not errors and not missing and not pending_dependencies,'missing_episodes':missing,'pending_dependencies':pending_dependencies,'episodes':result,'sources':[{'ep':x['ep'],'source':x['basis']} for x in result]}

def check(project):
 p=Path(project); d=_load(p); invalid=[]
 for e in d['episodes']:
  if not isinstance(e,dict): continue
  problems=_working_problems(p,e)
  if problems: invalid.append({'ep':e.get('ep'),'problems':problems})
 return {'passed':not invalid,'invalid':invalid,'errors':[] if not invalid else ['存在失效工作连续性条目']}
