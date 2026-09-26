"""CLI entry point: python -m bookflow --help."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from .common import ROOT, atomic_write, find_project, load_config, load_yaml, sha256_file, source_generation, write_json, write_yaml


def new_project(slug: str, title: str, author: str, genre: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise ValueError("slug 需为小写字母、数字或连字符")
    project = ROOT / "projects" / slug
    if project.exists():
        raise ValueError("项目目录已存在，拒绝覆盖")
    project.mkdir(parents=True)
    for directory in ("source/raw", "analysis/chapter_notes", "plan", "episodes/ep01/review", "approvals", "release"):
        (project / directory).mkdir(parents=True, exist_ok=True)
    config = {"book": {"title": title, "author": author, "slug": slug, "edition": ""},
              "genre": {"primary": genre, "secondary": []}, "voice": "style/voices/default.md",
              "audience": "希望听懂一本书、尚未读过原文的成年观众", "platform": "待用户确定",
              "depth": {"goal": "待通过风格校准明确", "required_layers": [2], "series_layers": [3]},
              "format": {"episode_minutes": [10, 15], "speech_rate_cpm": 240},
              "drafting": {"mode": "full_season_review"},
              "preview_limits": {"max_draft_episodes_before_g2": 1, "max_draft_episodes_before_g3": 2},
              "visual_pacing": {"output": {"aspect": "16:9", "width": 1920, "height": 1080}}}
    write_yaml(project / "project.yaml", config)
    write_yaml(project / "status.yaml", {"mode": "production", "stage": "created", "gates": {"analysis": "pending", "plan": "pending", "first_episode": "pending", "final": "pending"}})
    write_yaml(project / "analysis/external_sources.yaml", {"sources": []})
    write_yaml(project / "release/compliance.yaml", {"copyright": {"status": "", "note": ""},
        "ai_content_label": {"explicit_label": "", "platform_setting": "", "checked_rules": ""},
        "likeness_and_assets": {"note": ""}, "platform": {"name": "", "rules_checked_at": ""}, "reviewer": ""})
    atomic_write(project / "feedback.md", "# 用户反馈\n\n尚未收到风格确认。\n")
    return {"project": str(project), "next": f"./run.sh ingest projects/{slug} <原文路径>"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="书籍精讲本地工作流 · 首版")
    sub = p.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="创建书目项目，不覆盖已有文件")
    new.add_argument("slug")
    new.add_argument("--title", required=True)
    new.add_argument("--author", default="待确认")
    new.add_argument("--genre", choices=["suspense", "popsci", "emotional", "nonfiction", "history"], default="suspense")
    ing = sub.add_parser("ingest", help="不可变导入 txt/md/epub/docx")
    ing.add_argument("project", type=Path)
    ing.add_argument("files", nargs="+", type=Path)
    for command, help_text in [("coverage", "生成机器查漏候选，不覆盖人工结论"), ("check-plan", "检查分集结构与证据"), ("status", "显示当前文件、检查和人工确认状态")]:
        q = sub.add_parser(command, help=help_text)
        q.add_argument("project", type=Path)
    guard = sub.add_parser("guard", help="检查阶段闸门与预览上限")
    guard.add_argument("project", type=Path)
    guard.add_argument("action")
    guard.add_argument("--ep", type=int)
    approve = sub.add_parser("approve", help="用户交互式批准闸门（代理不可执行）")
    approve.add_argument("project", type=Path); approve.add_argument("gate"); approve.add_argument("--ep", type=int)
    revoke = sub.add_parser("revoke", help="用户交互式撤回批准")
    revoke.add_argument("project", type=Path); revoke.add_argument("gate"); revoke.add_argument("--ep", type=int); revoke.add_argument("--reason", required=True)
    override = sub.add_parser("override", help="用户交互式越界授权")
    override.add_argument("project", type=Path); override.add_argument("rule"); override.add_argument("--reason", required=True)
    cont = sub.add_parser("continuity", help="工作连续性")
    cs = cont.add_subparsers(dest="action", required=True)
    cu = cs.add_parser("update"); cu.add_argument("project", type=Path); cu.add_argument("ep", type=int); cu.add_argument("--draft", type=Path, required=True)
    cc = cs.add_parser("context"); cc.add_argument("project", type=Path); cc.add_argument("ep", type=int)
    ck = cs.add_parser("check"); ck.add_argument("project", type=Path)
    sent = sub.add_parser("sentences", help="生成稳定句子表")
    ss = sent.add_subparsers(dest="action", required=True)
    sg = ss.add_parser("generate"); sg.add_argument("draft", type=Path); sg.add_argument("--previous", type=Path)
    anc = sub.add_parser("anchors", help="检查或迁移句子锚点")
    ac = anc.add_subparsers(dest="action", required=True)
    ax = ac.add_parser("check"); ax.add_argument("episode", type=Path); ax.add_argument("--against", type=Path)
    am = ac.add_parser("migrate"); am.add_argument("episode", type=Path)
    timing = sub.add_parser("timing", help="导入真实语音时间")
    ti = timing.add_subparsers(dest="action", required=True)
    tim = ti.add_parser("import"); tim.add_argument("episode", type=Path); tim.add_argument("--audio", type=Path, required=True); tim.add_argument("--timestamps", type=Path)
    pacing = sub.add_parser("pacing", help="静态图视频节奏")
    ps = pacing.add_subparsers(dest="action", required=True)
    for action in ("budget", "check", "baseline"):
        pp=ps.add_parser(action); pp.add_argument("episode", type=Path)
    sound = sub.add_parser("sound", help="O13 声音设计、拼装与检查（不自动调用付费模型）")
    snd = sound.add_subparsers(dest="action", required=True)
    for action in ("cues", "estimate", "check", "assemble", "mix", "baseline", "generate"):
        sp = snd.add_parser(action)
        sp.add_argument("episode", type=Path)
    imp = snd.add_parser("import", help="登记手工生成的本地音频")
    imp.add_argument("episode", type=Path)
    imp.add_argument("files", nargs="+", type=Path)
    src = sub.add_parser("source", help="原文批次迁移")
    sc = src.add_subparsers(dest="action", required=True)
    sm=sc.add_parser("migrate"); sm.add_argument("project", type=Path); sm.add_argument("--to", required=True)
    sw=sc.add_parser("switch"); sw.add_argument("project", type=Path); sw.add_argument("--to", required=True)
    sub.add_parser("test", help="运行自动测试")
    for command, help_text in [("lint", "稿件硬指标检查"), ("quotes", "引文与来源编号核对"), ("listener-input", "输出不带写作标记的句子ID口播")]:
        q = sub.add_parser(command, help=help_text)
        q.add_argument("draft", type=Path)
        q.add_argument("--output", type=Path)
    rev = sub.add_parser("review-check", help="检查独立审校与核心收获是否完整")
    rev.add_argument("draft", type=Path)
    rev.add_argument("--report", type=Path, required=True)
    ex = sub.add_parser("export", help="导出效果预览，或已确认定稿交付包")
    ex.add_argument("draft", type=Path)
    ex.add_argument("--review", type=Path)
    mode = ex.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--deliver", action="store_true")
    ec = sub.add_parser("edit-copy", help="生成 Markdown 或 Word 人工编辑稿及原稿快照")
    ec.add_argument("draft", type=Path)
    ec.add_argument("--output", type=Path, required=True)
    ec.add_argument("--format", choices=["md", "docx", "both"], default="md",
                    help="编辑稿格式，默认 md；both 保留旧版 Word 兼容性")
    ie = sub.add_parser("import-edits", help="导入人工修改后的 Markdown 或 DOCX，保留差异")
    ie.add_argument("docx", type=Path, help="修改后的 .md、.markdown 或 .docx 文件")
    ie.add_argument("--baseline", type=Path, required=True)
    fc = sub.add_parser("feedback-context", help="读取有改稿证据的个人风格规则与待学习反馈")
    fc.add_argument("project", type=Path)
    nc = sub.add_parser("names-check", help="检查当前资料和稿件是否残留已停用译名")
    nc.add_argument("project", type=Path)
    np = sub.add_parser("name-change", help="生成指定人物改名的影响清单，不直接修改文件")
    np.add_argument("project", type=Path)
    np.add_argument("--entity", required=True)
    np.add_argument("--name", required=True)
    np.add_argument("--output", type=Path)
    lg = sub.add_parser("ledger", help="系列账本与版本依赖")
    ls = lg.add_subparsers(dest="action", required=True)
    for action in ["check", "context", "mark-stale", "stamp"]:
        q = ls.add_parser(action)
        q.add_argument("project", type=Path)
        if action != "check":
            q.add_argument("ep", type=int)
        if action == "mark-stale":
            q.add_argument("--reason", required=True)
        if action == "stamp":
            q.add_argument("--approved-by", help="兼容旧接口；新流程只认 approvals/G4 记录")
            q.add_argument("--review", type=Path, required=True)
    return p


def dispatch(args) -> dict | str:
    from . import ledger, planning, review, source
    from .exporting import export_episode
    from .quality import lint, listener_input, verify_quotes
    cmd = args.command
    if cmd == "test":
        import subprocess
        return {"passed": subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT).returncode == 0}
    if cmd == "guard":
        from .guard import check; return check(args.project, args.action, args.ep)
    if cmd in ("approve", "revoke", "override"):
        from . import approvals
        if cmd == "approve": return approvals.approve(args.project, args.gate, args.ep)
        if cmd == "revoke": return approvals.revoke(args.project, args.gate, args.ep, args.reason)
        return approvals.override(args.project, args.rule, args.reason)
    if cmd == "continuity":
        from . import continuity
        if args.action == "update": return continuity.update(args.project, args.ep, args.draft)
        if args.action == "context": return continuity.context(args.project, args.ep)
        return continuity.check(args.project)
    if cmd == "sentences":
        from . import sentences
        return sentences.generate(args.draft, args.previous)
    if cmd == "anchors":
        from . import anchors
        return anchors.check(args.episode, args.against) if args.action == "check" else anchors.migrate(args.episode)
    if cmd == "timing":
        from . import timing
        return timing.import_timing(args.episode, args.audio, args.timestamps)
    if cmd == "pacing":
        from . import pacing
        return getattr(pacing, args.action)(args.episode)
    if cmd == "sound":
        from . import sound
        return sound.run(args.action, args.episode, getattr(args, "files", None))
    if cmd == "source" and args.action in ("migrate", "switch"):
        from . import migration
        return migration.migrate(args.project, args.to) if args.action == "migrate" else migration.switch(args.project, args.to)
    if cmd in ("names-check", "name-change"):
        from .names import change_plan, check_project
        result = check_project(args.project) if cmd == "names-check" else change_plan(args.project, args.entity, args.name)
        if cmd == "name-change" and args.output:
            write_json(args.output, result)
        return result
    if cmd in ("edit-copy", "import-edits", "feedback-context"):
        from .feedback import create_copy, feedback_context, import_edits
        if cmd == "edit-copy":
            return create_copy(args.draft, args.output, args.format)
        if cmd == "import-edits":
            return import_edits(args.docx, args.baseline)
        return feedback_context(args.project)
    if cmd == "new":
        return new_project(args.slug, args.title, args.author, args.genre)
    if cmd == "ingest":
        return source.ingest(args.project, args.files)
    if cmd == "coverage":
        return planning.coverage(args.project)
    if cmd == "check-plan":
        return planning.check_plan(args.project)
    if cmd in ("lint", "quotes", "listener-input"):
        result = {"lint": lint, "quotes": verify_quotes, "listener-input": listener_input}[cmd](args.draft)
        if args.output:
            if isinstance(result, str):
                atomic_write(args.output, result)
            else:
                write_json(args.output, result)
        return result
    if cmd == "review-check":
        from .common import parse_draft
        project = find_project(args.draft)
        if not project:
            raise ValueError("稿件不在项目中")
        ep = int(parse_draft(args.draft.read_text(encoding="utf-8"))["meta"].get("episode", 1))
        return review.evaluate(project, ep, args.draft, load_yaml(args.report))
    if cmd == "ledger":
        if args.action == "check":
            return ledger.check(args.project)
        if args.action == "context":
            return ledger.context(args.project, args.ep)
        if args.action == "mark-stale":
            return ledger.mark_stale(args.project, args.ep, args.reason)
        if not args.approved_by:
            from .approvals import gate_state, list_valid
            if gate_state(args.project, "G4", args.ep) != "passed":
                return {"passed": False, "errors": ["缺少当前 final.md 哈希一致的 G4 用户批准记录"]}
            approved = next((x.get("approver") for x in list_valid(args.project) if x.get("gate")=="G4" and x.get("ep")==args.ep and x.get("valid")), "本机用户")
        else:
            approved = args.approved_by
        return ledger.stamp(args.project, args.ep, approved, args.review)
    if cmd == "export":
        return export_episode(args.draft, args.preview, args.review)
    if cmd == "status":
        project = args.project.resolve()
        if not (project / "project.yaml").exists():
            raise ValueError("找不到 project.yaml")
        from .states import write as derive_status
        derived = derive_status(project)
        drafts = sorted(project.glob("episodes/ep*/draft_v*.md"))
        observed = "created"
        for present, stage in [(bool(source_generation(project)), "ingested"),
                               ((project / "analysis/book_brief.md").exists(), "analyzed"),
                               ((project / "plan/episodes.yaml").exists(), "planned"),
                               (bool(drafts), "drafted"),
                               (bool(list(project.glob("episodes/ep*/preview/index.html"))), "preview_awaiting_human")]:
            if present:
                observed = stage
        return {"project": str(project), "observed_stage": observed, "derived": derived, "declared_state": load_yaml(project / "status.yaml"),
                "source_generation": source_generation(project),
                "analysis_exists": (project / "analysis/book_brief.md").exists(),
                "plan_exists": (project / "plan/episodes.yaml").exists(),
                "drafts": [str(x.relative_to(project)) for x in drafts],
                "ledger": ledger.check(project)}
    raise ValueError("未知命令")


def main() -> int:
    args = parser().parse_args()
    try:
        result = dispatch(args)
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and (result.get("errors") or result.get("passed") is False) else 0
    except (ValueError, OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        print(json.dumps({"errors": [str(exc)]}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
