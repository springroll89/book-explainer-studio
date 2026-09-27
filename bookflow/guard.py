"""Stage gate checks based on the passphrase confirmations.

The gate reads ``approvals/log.yaml`` through ``confirmation_state``. Old
terminal records (G1–G4/AV1/RELEASE files) are no longer an approval source:
a project that still has them unmigrated is stopped with the migration
command instead of silently reading them.
"""
from __future__ import annotations
from pathlib import Path
import yaml
from .approvals import confirmation_state
from .common import full_season_review, load_yaml

TEXT_ACTIONS = ('outline', 'draft', 'review', 'edit-copy')
MEDIA_ACTIONS = ('sound-plan', 'visual', 'media-generate', 'export-preview')
DELIVER_ACTIONS = ('export-deliver', 'export-delivery')
LABELS = {'plan': '方案', 'script': '文案', 'style': '画风', 'characters': '定妆', 'sound': '声音',
          'sample': '样片', 'release': '成片'}
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
        if action == 'visual':
            # Storyboards start only after the look, the character sheet and this episode's sound.
            needed += [('style', None), ('characters', None), ('sound', ep)]
        return needed
    if action in DELIVER_ACTIONS:
        return [('script', ep), ('release', ep)]
    return []


def design_required(project: Path) -> bool:
    """Style and character sheets gate visuals, except for projects whose sample
    was confirmed before these confirmations existed and never recorded them."""
    from .approvals import read_log
    if any(row.get('gate') in ('style', 'characters') for row in read_log(project)):
        return True
    return confirmation_state(project, 'sample', SAMPLE_EPISODE)['state'] != 'passed'


def sound_required(project: Path, ep: int) -> bool:
    """A separate sound confirmation is needed before visuals unless the episode's
    adopted legacy media, its current sample or its current release already covers it."""
    from .flow import _legacy_episodes
    if ep in _legacy_episodes(Path(project).resolve(), [ep]):
        return False
    if ep == SAMPLE_EPISODE and confirmation_state(project, 'sample', ep)['state'] == 'passed':
        return False
    return confirmation_state(project, 'release', ep)['state'] != 'passed'


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
    if action == 'visual':
        try:
            skip = set() if design_required(p) else {'style', 'characters'}
            if ep is not None and not sound_required(p, ep):
                # Adopted legacy audio is judged together with the sample.
                skip.add('sound')
        except ValueError:
            skip = set()
        needed = [item for item in needed if item[0] not in skip]
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
        try:
            plan = check_plan(p)
            errors.extend(plan.get('errors', [])); warnings.extend(plan.get('warnings', []))
            checks['plan'] = not plan.get('errors')
            data = load_yaml(p / 'plan/episodes.yaml', {})
            episodes = data if isinstance(data, list) else data.get('episodes', [])
            if ep is not None and (type(ep) is not int or ep < 1 or ep not in [e.get('ep') for e in episodes if isinstance(e, dict)]):
                errors.append('集号不在当前分集计划中')
            recap_path = p / 'episodes/recap.yaml'
            if recap_path.exists() or recap_path.is_symlink():
                # recap.yaml supersedes working_continuity.yaml; a legacy file kept by
                # migration must not block drafting once the recap exists.
                from .recap import drafting_state
                continuity = drafting_state(p, ep)
            else:
                from .continuity import context, check as continuity_check
                continuity = context(p, ep, allow_missing=True) if ep is not None else continuity_check(p)
            errors.extend(continuity.get('errors', [])); warnings.extend(continuity.get('warnings', []))
            checks['continuity'] = continuity
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            errors.append(f'来源、计划或工作连续性检查未完成：{exc}')
        warnings.append('全季统一改稿模式：本次仅放行文字草稿与审阅，文案确认状态保持原样。')
    return {'passed': not errors, 'action': action, 'ep': ep, 'required_confirmations': required,
            'confirmation_states': states, 'errors': errors, 'warnings': warnings,
            'drafting_mode': 'full_season_review' if batch else 'episode_review', 'checks': checks}
