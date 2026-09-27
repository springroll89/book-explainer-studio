"""Derive project status from files, checks and approval records."""
from __future__ import annotations
from pathlib import Path
from .common import full_season_review, latest_draft, load_yaml, parse_draft, sha256_file, source_generation, write_yaml
from .approvals import confirmation_state
from .ledger import check as ledger_check
from .quality import lint, verify_quotes

def _latest(epdir:Path):
    return latest_draft(epdir)

def derive(project:Path)->dict:
    p=Path(project); source=source_generation(p); batch=full_season_review(p)
    plan=p/'plan/episodes.yaml'; analysis=p/'analysis'
    notes=load_yaml(p/'notes.yaml',{}) or {}
    workflow_notes=notes.get('workflow',{}) if isinstance(notes,dict) else {}
    season_review_complete=workflow_notes.get('season_review_status')=='completed'
    eps=[]; epdirs=sorted((p/'episodes').glob('ep*')) if (p/'episodes').exists() else []
    data=load_yaml(plan,{}) or {}; planned=data if isinstance(data,list) else data.get('episodes',[])
    expected=[e['ep'] for e in planned if isinstance(e,dict) and type(e.get('ep')) is int]
    numbers=sorted(set(expected) | {int(e.name[2:]) for e in epdirs if e.name[2:].isdigit()})
    confirmations={'plan':confirmation_state(p,'plan')['state'],
                   'sample':confirmation_state(p,'sample',1)['state']}
    for kind in ('script','release'):
        confirmations[kind]={str(ep):confirmation_state(p,kind,ep)['state'] for ep in numbers}
    ledger_states={x['ep']:x['passed'] for x in ledger_check(p).get('episodes',[])}
    for epdir in epdirs:
        if not epdir.name[2:].isdigit(): continue
        ep=int(epdir.name[2:]); latest=_latest(epdir); item={'ep':ep,'status':'planned' if plan.exists() else 'planned','flags':[]}
        if (epdir/'outline.md').exists(): item['status']='outlined'
        if latest:
            item['draft']=str(latest.relative_to(p)); item['draft_sha256']=sha256_file(latest); item['status']='drafted'
            try:
                q=lint(latest); quotes=verify_quotes(latest)
                if q.get('passed') and quotes.get('passed'): item['status']='drafted'
            except Exception as exc: item['flags'].append('check_error:'+str(exc))
            if 'working' in str(parse_draft(latest.read_text(encoding='utf-8'))['meta'].get('continuity_basis','')): item['flags'].append('preview')
        if (epdir/'final.md').exists():
            meta=parse_draft((epdir/'final.md').read_text(encoding='utf-8'))['meta']
            if meta.get('status')=='final': item['status']='final_candidate'
        item['confirmations']={kind:confirmations[kind][str(ep)] for kind in ('script','release')}
        if ep==1: item['confirmations']['sample']=confirmations['sample']
        if item['confirmations']['script']=='passed': item['status']='approved'
        if ledger_states.get(ep): item['status']='ledgered'
        if batch and not season_review_complete and item['status']=='drafted': item['flags'].append('awaiting_season_review')
        if not batch and item['status']=='drafted' and ep>2 and confirmations['script'].get('1')!='passed': item['flags'].append('on_hold')
        if any(s in ('invalidated','revoked') for s in item['confirmations'].values()): item['flags'].append('needs_recheck')
        if item['flags']: item['status_with_flags']=item['status']+'+'+'+'.join(item['flags'])
        eps.append(item)
    out={'_notice':'自动生成，请勿手改；用户备注请写入 notes.yaml。','generated_by':'./run.sh status','auto_generated':True,'project':str(p),'source_generation':source,'stage':'created','confirmations':confirmations,'episodes':eps,'artifacts':{},'next_steps':[],'awaiting_human':[]}
    if source: out['stage']='ingested'
    if (analysis/'book_brief.md').exists(): out['stage']='analyzed'
    if plan.exists(): out['stage']='planned'
    if eps: out['stage']='drafted'
    if any(x.get('status') in ('reviewed','final_candidate','approved','ledgered') for x in eps): out['stage']='reviewed'
    out['drafting_mode']='full_season_review' if batch else 'episode_review'
    if batch:
        drafted={x['ep'] for x in eps if x.get('draft')}
        missing=[ep for ep in expected if ep not in drafted]
        out['season_drafts']={'planned':len(expected),'drafted':len(drafted.intersection(expected)),'missing_episodes':missing,'complete':bool(expected) and not missing}
        if confirmations['plan']!='passed': out['awaiting_human'].append('请阅读方案并回复“拍板方案”')
        if missing: out['next_steps'].append('继续生成全季剩余文案：'+', '.join(map(str,missing)))
        elif expected:
            recap=p/'episodes/recap.yaml'
            if recap.exists() or recap.is_symlink():
                from .recap import check
                complete=check(p,required_episodes=expected)['passed']
            else:
                from .continuity import context
                complete=context(p,max(expected)+1)['complete']
            out['season_drafts']['continuity_complete']=complete
            if not complete: out['next_steps'].append('按实际全季稿件补录或复核工作连续性')
            if not season_review_complete:
                out['awaiting_human'].append('统一阅读、修改全季文案并反馈；本轮不要求逐集批准')
        else: out['next_steps'].append('完成可核查的全季分集计划')
        out['deferred_confirmations']={g:s for g,s in confirmations.items() if g!='plan'}
    else:
        if any(x.get('flags') for x in eps): out['awaiting_human'].append('处理需要复查或暂停的集数')
        labels={'plan':'方案','script':'文案','sample':'样片','release':'成片'}
        for kind,state in confirmations.items():
            scopes=state.items() if isinstance(state,dict) else [(None,state)]
            for ep,status in scopes:
                if status!='passed': out['awaiting_human'].append(f'待{labels[kind]}确认'+(f'：第 {ep} 集' if ep else ''))
    out['legacy_stage']='preview_awaiting_human' if eps else out['stage']
    return out

def write(project:Path)->dict:
    data=derive(project); write_yaml(Path(project)/'status.yaml',data); return data
