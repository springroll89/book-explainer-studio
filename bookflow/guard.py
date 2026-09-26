from __future__ import annotations
from pathlib import Path
from .approvals import gate_state
from .common import full_season_review, load_yaml

TEXT_ACTIONS = ('outline', 'draft', 'review', 'edit-copy')

def required_gates(action:str, ep:int|None=None):
    action=action.replace('_','-').lower()
    if action in ('outline','draft'):
        if ep is None or ep<=1: return ['G1']
        if ep==2: return ['G1','G2']
        return ['G1','G2','G3']
    if action=='review': return ['G1']
    if action in ('sound-plan','visual','media-generate'):
        return ['G1','G2','G3'] if ep in (None,1) else ['G1','G2','G3','AV1']
    if action=='export-preview': return ['G1','G2','G3'] if ep in (None,1) else ['G1','G2','G3','AV1']
    if action in ('export-deliver','export-delivery'): return ['G4','RELEASE']
    return []

def check(project:Path, action:str, ep:int|None=None)->dict:
    p=Path(project); action=action.replace('_','-').lower()
    batch=full_season_review(p) and action in TEXT_ACTIONS
    req=['G1'] if batch else required_gates(action,ep)
    states={}
    for gate in req:
        gate_ep = ep if gate in ('G3','G4','AV1') else None
        if gate in ('G3','AV1') and ep and ep>1:
            # G3 calibrates the first episode; AV1 approves its pilot for later production.
            gate_ep = 1
        states[gate]=gate_state(p,gate,gate_ep)
    errors=[f"{g} 未通过（当前状态：{s}）" for g,s in states.items() if s!='passed']
    warnings=[]; checks={}
    if batch:
        from .planning import check_plan
        from .continuity import context, check as continuity_check
        try:
            plan=check_plan(p)
            errors.extend(plan.get('errors',[])); warnings.extend(plan.get('warnings',[]))
            checks['plan']=not plan.get('errors')
            data=load_yaml(p/'plan/episodes.yaml',{})
            episodes=data if isinstance(data,list) else data.get('episodes',[])
            if ep is not None and (type(ep) is not int or ep<1 or ep not in [e.get('ep') for e in episodes if isinstance(e,dict)]):
                errors.append('集号不在当前分集计划中')
            continuity=context(p,ep,allow_missing=True) if ep is not None else continuity_check(p)
            errors.extend(continuity.get('errors',[])); warnings.extend(continuity.get('warnings',[]))
            checks['continuity']=continuity
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f'来源、计划或工作连续性检查未完成：{exc}')
        warnings.append('全季统一改稿模式：本次仅放行文字草稿与审阅，G2/G3/G4 状态保持原样。')
    return {'passed':not errors,'action':action,'ep':ep,'required_gates':req,'gate_states':states,'errors':errors,'warnings':warnings,'drafting_mode':'full_season_review' if batch else 'episode_review','checks':checks}
