"""Derive project status from files, checks and approval records."""
from __future__ import annotations
from pathlib import Path
from .common import full_season_review, latest_draft, load_yaml, parse_draft, sha256_file, source_generation, write_yaml
from .approvals import gate_state, list_valid
from .quality import lint, verify_quotes

def _latest(epdir:Path):
    return latest_draft(epdir)

def derive(project:Path)->dict:
    p=Path(project); source=source_generation(p); batch=full_season_review(p)
    plan=p/'plan/episodes.yaml'; analysis=p/'analysis'; approvals=list_valid(p)
    notes=load_yaml(p/'notes.yaml',{}) or {}
    workflow_notes=notes.get('workflow',{}) if isinstance(notes,dict) else {}
    season_review_complete=workflow_notes.get('season_review_status')=='completed'
    eps=[]; epdirs=sorted((p/'episodes').glob('ep*')) if (p/'episodes').exists() else []
    gates={g:gate_state(p,g,None) for g in ('G1','G2','G3','RELEASE')}
    # AV1 is the first-episode audio/visual pilot gate for the series.
    gates['AV1']=gate_state(p,'AV1',1)
    gates['G4']={str(int(e.name[2:])):gate_state(p,'G4',int(e.name[2:])) for e in epdirs if e.name[2:].isdigit()}
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
            if ep==1 and gate_state(p,'G3',ep)=='passed': item['status']='reviewed'
            if (epdir/'final.md').exists():
                meta=parse_draft((epdir/'final.md').read_text(encoding='utf-8'))['meta']
                if meta.get('status')=='final': item['status']='final_candidate'
            if gate_state(p,'G4',ep)=='passed': item['status']='approved'
            item['g4'] = gate_state(p,'G4',ep)
            item['av1'] = gate_state(p,'AV1',ep) if ep == 1 else None
            ledger=load_yaml(p/'series_ledger.yaml',{}) or {}; entries=ledger.get('episodes',[]) if isinstance(ledger,dict) else []
            if any(isinstance(x,dict) and x.get('ep')==ep and x.get('final_sha256')==sha256_file(epdir/'final.md') for x in entries if (epdir/'final.md').exists()): item['status']='ledgered'
            if 'working' in str(parse_draft(latest.read_text(encoding='utf-8'))['meta'].get('continuity_basis','')): item['flags'].append('preview')
            if any(a.get('gate')=='OVERRIDE' and a.get('valid') for a in approvals): item['flags'].append('preview_override')
            if batch and not season_review_complete and item['status'] in ('drafted','reviewed'): item['flags'].append('awaiting_season_review')
            if not batch and item['status'] in ('drafted','reviewed') and ep>2 and gates.get('G3')!='passed': item['flags'].append('on_hold')
        if gate_state(p,'G4',ep) in ('invalidated','revoked'): item['flags'].append('needs_recheck')
        if item['flags']: item['status_with_flags']=item['status']+'+'+'+'.join(item['flags'])
        eps.append(item)
    if (p/'release/compliance.yaml').exists(): compliance=load_yaml(p/'release/compliance.yaml',{}) or {}
    else: compliance={}
    out={'_notice':'自动生成，请勿手改；用户备注请写入 notes.yaml。','generated_by':'./run.sh status','auto_generated':True,'project':str(p),'source_generation':source,'stage':'created','gates':gates,'episodes':eps,'artifacts':{},'next_steps':[],'awaiting_human':[]}
    if source: out['stage']='ingested'
    if (analysis/'book_brief.md').exists(): out['stage']='analyzed'
    if plan.exists(): out['stage']='planned'
    if eps: out['stage']='drafted'
    if any(x.get('status') in ('reviewed','final_candidate','approved','ledgered') for x in eps): out['stage']='reviewed'
    out['drafting_mode']='full_season_review' if batch else 'episode_review'
    if batch:
        data=load_yaml(plan,{}) or {}; planned=data if isinstance(data,list) else data.get('episodes',[])
        expected=[e['ep'] for e in planned if isinstance(e,dict) and type(e.get('ep')) is int]
        drafted={x['ep'] for x in eps if x.get('draft')}
        missing=[ep for ep in expected if ep not in drafted]
        out['season_drafts']={'planned':len(expected),'drafted':len(drafted.intersection(expected)),'missing_episodes':missing,'complete':bool(expected) and not missing}
        if gates.get('G1')!='passed': out['awaiting_human'].append('用户确认拆书 G1')
        if missing: out['next_steps'].append('继续生成全季剩余文案：'+', '.join(map(str,missing)))
        elif expected:
            from .continuity import context
            continuity=context(p,max(expected)+1)
            out['season_drafts']['continuity_complete']=continuity['passed']
            if not continuity['passed']: out['next_steps'].append('按实际全季稿件补录或复核工作连续性')
            if not season_review_complete:
                out['awaiting_human'].append('统一阅读、修改全季文案并反馈；本轮不要求逐集批准')
        else: out['next_steps'].append('完成可核查的全季分集计划')
        out['deferred_gates']={g:s for g,s in gates.items() if g!='G1'}
    else:
        if any(x.get('flags') for x in eps): out['awaiting_human'].append('处理需要复查或暂停的集数')
        for g,s in gates.items():
            if s!='passed': out['awaiting_human'].append(f'用户执行 {g} 批准')
    out['legacy_stage']='preview_awaiting_human' if eps else out['stage']
    return out

def write(project:Path)->dict:
    data=derive(project); write_yaml(Path(project)/'status.yaml',data); return data
