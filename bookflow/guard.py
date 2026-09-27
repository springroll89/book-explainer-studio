"""Stage gate checks based on the four passphrase confirmations.

The gate reads ``approvals/log.yaml`` through ``confirmation_state``. Old
terminal records (G1–G4/AV1/RELEASE files) are no longer an approval source:
a project that still has them unmigrated is stopped with the migration
command instead of silently reading them.
"""
from __future__ import annotations
from pathlib import Path
from .approvals import confirmation_state
from .common import full_season_review, load_yaml

TEXT_ACTIONS = ('outline', 'draft', 'review', 'edit-copy')
MEDIA_ACTIONS = ('sound-plan', 'visual', 'media-generate', 'export-preview')
DELIVER_ACTIONS = ('export-deliver', 'export-delivery')
LABELS = {'plan': '方案', 'script': '文案', 'sample': '样片', 'release': '成片'}
SAMPLE_EPISODE = 1  # record_confirmation binds the pilot sample to episode 1


def required_confirmations(action: str, ep: int | None = None, *, batch: bool = False) -> list[tuple[str, int | None]]:
    """Return (gate, episode) pairs that must be ``passed`` for this action."""
    action = action.replace('_', '-').lower()
    if action in TEXT_ACTIONS:
        if batch or action == 'review' or ep is None or ep <= 2:
            return [('plan', None)]
        # Episode-by-episode mode: later drafts follow the confirmed first-episode text.
        return [('plan', None), ('script', 1)]
    if action in MEDIA_ACTIONS:
        needed = [('plan', None), ('script', ep)]
        if ep is not None and ep != SAMPLE_EPISODE:
            needed.append(('sample', SAMPLE_EPISODE))
        return needed
    if action in DELIVER_ACTIONS:
        return [('script', ep), ('release', ep)]
    return []


def _label(gate: str, ep: int | None) -> str:
    return LABELS[gate] + (f" 第{ep}集" if ep is not None else "")


def unmigrated_legacy(project: Path) -> list[str]:
    folder = Path(project) / 'approvals'
    if not folder.is_dir():
        return []
    names = []
    for path in sorted(folder.glob('*.yaml')):
        if path.name == 'log.yaml':
            continue
        data = load_yaml(path, {})
        if isinstance(data, dict) and data.get('gate'):
            names.append(path.name)
    return names


def check(project: Path, action: str, ep: int | None = None) -> dict:
    p = Path(project); action = action.replace('_', '-').lower()
    batch = full_season_review(p) and action in TEXT_ACTIONS
    needed = required_confirmations(action, ep, batch=batch)
    required = [_label(gate, gate_ep) for gate, gate_ep in needed]
    states, errors, warnings, checks = {}, [], [], {}
    legacy = unmigrated_legacy(p)
    if legacy:
        errors.append(f"项目仍有 {len(legacy)} 个未迁移的旧终端批准记录；先运行 "
                      f"./run.sh dev migrate-approvals {p} 迁移，再按新口令确认")
    for gate, gate_ep in needed:
        try:
            state = confirmation_state(p, gate, gate_ep)['state']
        except ValueError as exc:
            state = 'error'
            errors.append(f"确认记录无法读取：{exc}")
        states[_label(gate, gate_ep)] = state
    errors.extend(f"{name}确认未通过（当前状态：{state}）" for name, state in states.items()
                  if state not in ('passed', 'error'))
    if batch:
        from .planning import check_plan
        from .continuity import context, check as continuity_check
        try:
            plan = check_plan(p)
            errors.extend(plan.get('errors', [])); warnings.extend(plan.get('warnings', []))
            checks['plan'] = not plan.get('errors')
            data = load_yaml(p / 'plan/episodes.yaml', {})
            episodes = data if isinstance(data, list) else data.get('episodes', [])
            if ep is not None and (type(ep) is not int or ep < 1 or ep not in [e.get('ep') for e in episodes if isinstance(e, dict)]):
                errors.append('集号不在当前分集计划中')
            continuity = context(p, ep, allow_missing=True) if ep is not None else continuity_check(p)
            errors.extend(continuity.get('errors', [])); warnings.extend(continuity.get('warnings', []))
            checks['continuity'] = continuity
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f'来源、计划或工作连续性检查未完成：{exc}')
        warnings.append('全季统一改稿模式：本次仅放行文字草稿与审阅，文案确认状态保持原样。')
    return {'passed': not errors, 'action': action, 'ep': ep, 'required_confirmations': required,
            'confirmation_states': states, 'errors': errors, 'warnings': warnings,
            'drafting_mode': 'full_season_review' if batch else 'episode_review', 'checks': checks}
