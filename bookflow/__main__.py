"""CLI entry point: python -m bookflow --help."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from . import __version__
from .common import ROOT, atomic_write, find_project, load_config, load_yaml, sha256_file, source_generation, write_json, write_yaml


def new_project(slug: str, title: str, author: str, genre: str, *, root: Path | None = None,
                profile: str | None = None) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise ValueError("slug 需为小写字母、数字或连字符")
    if profile is not None and profile not in ("story", "explainer"):
        raise ValueError("profile 必须是 story 或 explainer")
    project = (root if root is not None else ROOT) / "projects" / slug
    if project.exists():
        raise ValueError("项目目录已存在，拒绝覆盖")
    project.mkdir(parents=True)
    for directory in ("source/raw", "analysis/chapter_notes", "plan", "episodes/ep01/review", "approvals", "release"):
        (project / directory).mkdir(parents=True, exist_ok=True)
    config = {"book": {"title": title, "author": author, "slug": slug, "edition": ""},
              "genre": {"primary": genre, "secondary": []}, "voice": "style/voices/default.md",
              "profile": profile or ("story" if genre in ("suspense", "emotional") else "explainer"),
              "audience": "希望听懂一本书、尚未读过原文的成年观众", "platform": "待用户确定",
              "depth": {"goal": "待通过风格校准明确", "required_layers": [2], "series_layers": [3]},
              "format": {"episode_minutes": [10, 15], "speech_rate_cpm": 240},
              "approvals": {"verify_transcript": True, "minor_change_ratio": 0.03},
              "cost": {"per_episode_cny": 20},
              "studio_created_version": __version__, "studio_version": __version__,
              "producers": {"render": "ffmpeg"},
              "drafting": {"mode": "full_season_review"},
              "preview_limits": {"max_draft_episodes_before_g2": 1, "max_draft_episodes_before_g3": 2},
              "visual_pacing": {"output": {"aspect": "16:9", "width": 1920, "height": 1080}}}
    write_yaml(project / "project.yaml", config)
    write_yaml(project / "analysis/external_sources.yaml", {"sources": []})
    write_yaml(project / "release/compliance.yaml", {"copyright": {"status": "", "note": ""},
        "ai_content_label": {"explicit_label": "", "platform_setting": "", "checked_rules": ""},
        "likeness_and_assets": {"note": ""}, "platform": {"name": "", "rules_checked_at": ""}, "reviewer": ""})
    atomic_write(project / "feedback.md", "# 用户反馈\n\n尚未收到风格确认。\n")
    from .doctor import check as doctor_check
    from .flow import write as write_progress
    progress = write_progress(project)
    return {"project": str(project), "next": f"./run.sh ingest projects/{slug} <原文路径>",
            "doctor": doctor_check(project, full=False), "state": progress}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="书籍精讲本地工作流 · 首版")
    sub = p.add_subparsers(dest="command", required=True)

    def copy_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("draft", type=Path)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--format", choices=["md", "docx", "both"], default="md",
                             help="编辑稿格式，默认 md；both 保留旧版 Word 兼容性")

    def import_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("docx", type=Path, help="修改后的 .md、.markdown 或 .docx 文件")
        command.add_argument("--baseline", type=Path, required=True)

    new = sub.add_parser("new", help="创建书目项目，不覆盖已有文件")
    new.add_argument("slug")
    new.add_argument("--title", required=True)
    new.add_argument("--author", default="待确认")
    new.add_argument("--genre", choices=["suspense", "popsci", "emotional", "nonfiction", "history"], default="suspense")
    new.add_argument("--profile", choices=["story", "explainer"], help="按实际作品指定档位；默认按题材选择")
    ing = sub.add_parser("ingest", help="不可变导入 txt/md/epub/docx")
    ing.add_argument("project", type=Path)
    ing.add_argument("files", nargs="+", type=Path)
    for command, help_text in [("coverage", "生成机器查漏候选，不覆盖人工结论"), ("check-plan", "检查分集结构与证据"), ("status", "显示当前文件、检查和人工确认状态")]:
        q = sub.add_parser(command, help=help_text)
        q.add_argument("project", type=Path)
    next_step = sub.add_parser("next", help="按当前文件和确认记录计算唯一下一步，并写入进度页")
    next_step.add_argument("project", type=Path)
    next_step.add_argument("--json", action="store_true", help="输出 JSON 供脚本调用")
    next_step.add_argument("--read-only", action="store_true", help="仅预检，不写进度文件或工作室版本")
    stage_check = sub.add_parser("check", help="运行当前阶段的确定性检查；拆书阶段会更新机器查漏报告")
    stage_check.add_argument("project", type=Path)
    doctor = sub.add_parser("doctor", help="只读检查环境和书目项目")
    doctor.add_argument("project", nargs="?", type=Path)
    doctor.add_argument("--light", action="store_true", help="只检查运行时与项目配置")
    lessons = sub.add_parser("lessons", help="登记并查看本机教训收件箱")
    lesson_actions = lessons.add_subparsers(dest="action", required=True)
    for action in ("inbox", "triage", "report", "context"):
        q = lesson_actions.add_parser(action)
        q.add_argument("project", type=Path)
    observe = lesson_actions.add_parser("observe", help="按用户原话登记明确纠正，不作自动归类")
    observe.add_argument("project", type=Path)
    observe.add_argument("--quote", required=True)
    observe.add_argument("--evidence", required=True, help="本条消息在会话中的定位标记")
    proposal = lesson_actions.add_parser("propose", help="保存待用户确认的分诊提案，不改规则")
    proposal.add_argument("project", type=Path)
    proposal.add_argument("lesson_id")
    proposal.add_argument("--kind", required=True, choices=["book_fact", "book_style", "general_craft", "process_bug", "one_off"])
    proposal.add_argument("--destination", default="")
    proposal.add_argument("--before", default="")
    proposal.add_argument("--after", default="")
    proposal.add_argument("--check", default="")
    proposal.add_argument("--rationale", required=True)
    accept = lesson_actions.add_parser("accept", help="用户确认完整提案后标为已分诊，不应用修改")
    accept.add_argument("project", type=Path)
    accept.add_argument("--quote", required=True)
    apply_lesson = lesson_actions.add_parser("apply", help="验证并单独提交一条已确认的共享教训")
    apply_lesson.add_argument("project", type=Path)
    apply_lesson.add_argument("lesson_id")
    revert_lesson = lesson_actions.add_parser("revert", help="核对独立撤回口令后恢复一条共享教训")
    revert_lesson.add_argument("project", type=Path)
    revert_lesson.add_argument("lesson_id")
    revert_lesson.add_argument("--quote", required=True)
    guard = sub.add_parser("guard", help="检查阶段闸门与预览上限")
    guard.add_argument("project", type=Path)
    guard.add_argument("action")
    guard.add_argument("--ep", type=int)
    approve = sub.add_parser("approve", help="记录用户在聊天中发出的四类确认或撤回口令")
    approve.add_argument("project", type=Path)
    approve.add_argument("gate", choices=["方案", "文案", "样片", "成片", "plan", "script", "sample", "release"])
    approve.add_argument("--eps", help="集号或范围，如 1-16、1,3,5")
    approve.add_argument("--quote", required=True, help="用户最近一条确认口令的原话")
    approve.add_argument("--session", choices=["Codex", "Claude Code"], default="Codex")
    final = sub.add_parser("final", help="把用户已认可的草稿冻结为 final.md（不等于文案确认）")
    final_actions = final.add_subparsers(dest="action", required=True)
    final_freeze = final_actions.add_parser("freeze", help="默认取每集最新 draft_vN.md")
    final_freeze.add_argument("project", type=Path)
    final_freeze.add_argument("--eps", required=True, help="集号或范围，如 2-16、1,3,5")
    final_freeze.add_argument("--from", dest="source", help="单集指定版本，如 6 或 draft_v6.md")
    final_freeze.add_argument("--replace", action="store_true", help="已有不同 final.md 时另存旧版后替换")
    voices = sub.add_parser("voices", help="对白说话人标注与全季角色音色")
    voices_actions = voices.add_subparsers(dest="action", required=True)
    voices_scaffold = voices_actions.add_parser("scaffold", help="为定稿里的每处引号生成待填说话人清单（不覆盖）")
    voices_scaffold.add_argument("project", type=Path)
    voices_scaffold.add_argument("--eps", required=True)
    voices_check = voices_actions.add_parser("check", help="全季汇总：缺标注、缺音色、可复用、未出场")
    voices_check.add_argument("project", type=Path)
    voices_check.add_argument("--eps", help="默认全部计划集数")
    voices_set = voices_actions.add_parser("set", help="写入用户选定的人物音色并升级音色表版本")
    voices_set.add_argument("project", type=Path)
    voices_set.add_argument("speaker", help="人物稳定 ID，如 P31")
    voices_set.add_argument("--voice-id")
    voices_set.add_argument("--pool", help="改用已确认的共用音色池")
    voices_set.add_argument("--name")
    dev = sub.add_parser("dev", help="迁移与排查用内部命令")
    dev_commands = dev.add_subparsers(dest="dev_command", required=True)
    migrate_approvals = dev_commands.add_parser("migrate-approvals", help="迁移旧批准并保留原始记录")
    migrate_approvals.add_argument("project", type=Path)
    copy_arguments(dev_commands.add_parser("edit-copy", help="旧编辑包入口，仅供迁移排查"))
    import_arguments(dev_commands.add_parser("import-edits", help="旧改稿导入入口，仅供迁移排查"))
    dev_feedback_context = dev_commands.add_parser("feedback-context", help="旧风格上下文入口，仅供迁移排查")
    dev_feedback_context.add_argument("project", type=Path)
    cont = sub.add_parser("continuity", help="工作连续性")
    cs = cont.add_subparsers(dest="action", required=True)
    cu = cs.add_parser("update"); cu.add_argument("project", type=Path); cu.add_argument("ep", type=int); cu.add_argument("--draft", type=Path, required=True)
    cc = cs.add_parser("context"); cc.add_argument("project", type=Path); cc.add_argument("ep", type=int)
    ck = cs.add_parser("check"); ck.add_argument("project", type=Path)
    recap = sub.add_parser("recap", help="维护统一前情表，或显式迁移旧工作前情与正式账本")
    recap_actions = recap.add_subparsers(dest="action", required=True)
    recap_migrate = recap_actions.add_parser("migrate", help="默认只读；--write 才创建 episodes/recap.yaml")
    recap_migrate.add_argument("project", type=Path)
    recap_migrate.add_argument("--write", action="store_true")
    recap_working = recap_actions.add_parser("update-working", help="按显式实际内容登记当前草稿，待语义复核")
    recap_working.add_argument("project", type=Path)
    recap_working.add_argument("ep", type=int)
    recap_working.add_argument("--draft", type=Path, required=True)
    recap_working.add_argument("--actual", type=Path, required=True)
    recap_final = recap_actions.add_parser("snapshot-final", help="在有效文案确认后保存定稿前情快照")
    recap_final.add_argument("project", type=Path)
    recap_final.add_argument("ep", type=int)
    recap_final.add_argument("--actual", type=Path, help="定稿纯口播变化时必须提供当前实际内容")
    recap_check = recap_actions.add_parser("check", help="只读核对统一前情表的候选版本与语义复核记录")
    recap_check.add_argument("project", type=Path)
    recap_context = recap_actions.add_parser("context", help="读取已复核的前序实际内容")
    recap_context.add_argument("project", type=Path)
    recap_context.add_argument("ep", type=int)
    recap_inspect = recap_actions.add_parser("inspect", help="只读列出候选当前指纹，不显示正文")
    recap_inspect.add_argument("project", type=Path)
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
        if action == "cues":
            sp.add_argument("--migrate", action="store_true", help="先备份现有声音清单，再显式迁移旧 sound_plan.yaml")
    imp = snd.add_parser("import", help="登记手工生成的本地音频")
    imp.add_argument("episode", type=Path)
    imp.add_argument("files", nargs="+", type=Path)
    sfx = sub.add_parser("sfx", help="本机共享音效库：只索引已有文件，不调用付费生成")
    sfx_actions = sfx.add_subparsers(dest="action", required=True)
    sfx_search = sfx_actions.add_parser("search", help="搜索已试听通过且哈希有效的音效")
    sfx_search.add_argument("terms", nargs="+")
    sfx_search.add_argument("--class", dest="sound_class", choices=["ambience", "event", "process", "design", "music"])
    sfx_search.add_argument("--library", type=Path)
    sfx_harvest = sfx_actions.add_parser("harvest", help="成片或样片确认后，把本集新生成的音效收入共享库（accepted）")
    sfx_harvest.add_argument("project", type=Path)
    sfx_harvest.add_argument("--ep", type=int, required=True)
    sfx_add = sfx_actions.add_parser("add", help="复制并登记已有音效；accepted 须有本集确认和制作清单")
    sfx_add.add_argument("project", type=Path)
    sfx_add.add_argument("episode")
    sfx_add.add_argument("source", type=Path)
    sfx_add.add_argument("--desc", required=True)
    sfx_add.add_argument("--tags", required=True, help="逗号分隔的中文标签")
    sfx_add.add_argument("--class", dest="sound_class", required=True,
                         choices=["ambience", "event", "process", "design", "music"])
    sfx_add.add_argument("--status", choices=["pending", "accepted", "rejected"], default="pending")
    sfx_add.add_argument("--loop", action="store_true")
    sfx_add.add_argument("--prompt", default="")
    sfx_add.add_argument("--model", default="")
    sfx_add.add_argument("--cost-cny", type=float, default=0)
    sfx_add.add_argument("--library", type=Path)
    sfx_stats = sfx_actions.add_parser("stats", help="统计有效音效与已登记复用")
    sfx_stats.add_argument("--library", type=Path)
    archive = sub.add_parser("archive", help="分阶段核对并归档已确认成片；取回用 archive restore")
    archive.add_argument("target", help="书目项目路径，或 restore")
    archive.add_argument("project_or_episode", help="集号；restore 时为书目项目路径")
    archive.add_argument("episode", nargs="?", help="restore 时的集号")
    archive.add_argument("--only", choices=["audio", "video", "image"], help="restore 时按媒体类别取回")
    src = sub.add_parser("source", help="原文批次迁移")
    sc = src.add_subparsers(dest="action", required=True)
    sm=sc.add_parser("migrate"); sm.add_argument("project", type=Path); sm.add_argument("--to", required=True)
    sw=sc.add_parser("switch"); sw.add_argument("project", type=Path); sw.add_argument("--to", required=True)
    test = sub.add_parser("test", help="运行自动测试（默认完整集）")
    test.add_argument("--fast", action="store_true", help="只跑文档/技能卫生、配置与基础规则快速集")
    sub.add_parser("selftest", help="在临时目录运行无模型的文字与静音媒体自检")
    adopt = sub.add_parser("adopt-legacy", help="收编旧流程已做完的一集成品（不等于样片或成片确认）")
    adopt.add_argument("project", type=Path)
    adopt.add_argument("--ep", type=int, required=True)
    for kind, example in (("mix", "混音"), ("subtitles", "字幕 .srt"), ("storyboard", "分镜"), ("video", "成片视频")):
        adopt.add_argument(f"--{kind}", required=True, help=f"本集目录内的{example}文件，如 production/av1_build_v3/…")
    adopt.add_argument("--note", required=True, help="一句说明，如“旧流程 V3，用户已审看”")
    adopt.add_argument("--replace", action="store_true", help="替换已收编的另一组成品")
    produce = sub.add_parser("produce", help="按清单推进本集音画阶段；正式配音需显式开启付费调用")
    produce.add_argument("project", help="书目项目路径；只读检查时写 check")
    produce.add_argument("episode", help="集号 ep03；只读检查时写项目路径")
    produce.add_argument("check_episode", nargs="?", help="produce check 时的集号")
    produce.add_argument("--until", choices=("cues", "voice", "sfx", "mix", "subs", "storyboard", "images", "render"))
    produce.add_argument("--from", dest="from_stage", choices=("cues", "voice", "sfx", "mix", "subs", "storyboard", "images", "render"))
    produce.add_argument("--test-mode", action="store_true", help="仅限标记 test_fixture_only 的隔离示范项目")
    produce.add_argument("--allow-paid", action="store_true", help="允许正式配音（豆包 2.0）与缺失音效（豆包音频 1.0）的付费调用，每次一条；仍须通过守卫与预算")
    jobs = sub.add_parser("jobs", help="生成、领取、验收与串行执行分集任务卡")
    jobs_actions = jobs.add_subparsers(dest="action", required=True)
    jobs_plan = jobs_actions.add_parser("plan")
    jobs_plan.add_argument("project", type=Path)
    jobs_plan.add_argument("--stage", required=True, choices=("draft", "audio", "visual"))
    jobs_claim = jobs_actions.add_parser("claim")
    jobs_claim.add_argument("project", type=Path)
    jobs_claim.add_argument("--stage", required=True, choices=("draft", "audio", "visual"))
    jobs_claim.add_argument("--session", required=True)
    jobs_done = jobs_actions.add_parser("done")
    jobs_done.add_argument("project", type=Path)
    jobs_done.add_argument("task_id")
    jobs_done.add_argument("--session", required=True)
    jobs_run = jobs_actions.add_parser("run")
    jobs_run.add_argument("project", type=Path)
    jobs_run.add_argument("--stage", required=True, choices=("draft", "audio"))
    jobs_run.add_argument("--test-mode", action="store_true")
    for command, help_text in [("lint", "稿件硬指标检查"), ("quotes", "引文与来源编号核对"), ("listener-input", "输出不带写作标记的句子ID口播")]:
        q = sub.add_parser(command, help=help_text)
        q.add_argument("draft", type=Path)
        q.add_argument("--output", type=Path)
    story_check = sub.add_parser("story-check", help="只读检查剧情全季年份、线索呼应和集长分布")
    story_check.add_argument("project", type=Path)
    rev = sub.add_parser("review-check", help="检查独立审校与核心收获是否完整")
    rev.add_argument("draft", type=Path)
    rev.add_argument("--report", type=Path, required=True)
    ex = sub.add_parser("export", help="导出效果预览，或已确认定稿交付包")
    ex.add_argument("draft", type=Path)
    ex.add_argument("--review", type=Path)
    mode = ex.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--deliver", action="store_true")
    edit = sub.add_parser("edit", help="导出人工编辑稿或导入改稿差异")
    edit_actions = edit.add_subparsers(dest="action", required=True)
    copy_arguments(edit_actions.add_parser("copy", help="生成 Markdown 或 Word 编辑稿及原稿快照"))
    import_arguments(edit_actions.add_parser("import", help="导入 Markdown 或 DOCX 改稿并保留差异"))
    instruction = edit_actions.add_parser("instruction", help="登记用户原话及其精确文案替换，供 next 核验沿用")
    instruction.add_argument("project", type=Path)
    instruction.add_argument("ep", type=int)
    instruction.add_argument("--quote", required=True, help="用户明确要求修改的原话")
    instruction.add_argument("--replace", nargs=2, action="append", required=True,
                              metavar=("改前", "改后"), help="逐字替换；可重复填写多条")
    assistant_change = edit_actions.add_parser("assistant-change", help="登记助手小改及语义风险核查，等待用户过目")
    assistant_change.add_argument("project", type=Path)
    assistant_change.add_argument("ep", type=int)
    assistant_change.add_argument("--replace", nargs=2, action="append", required=True,
                                   metavar=("改前", "改后"), help="逐字替换；可重复填写多条")
    assistant_change.add_argument("--risk-review", required=True, help="说明为何不涉及身份、情节事实或结尾")
    assistant_change.add_argument("--no-identity-change", action="store_true", required=True)
    assistant_change.add_argument("--no-plot-fact-change", action="store_true", required=True)
    assistant_change.add_argument("--no-ending-change", action="store_true", required=True)
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
            q.add_argument("--approved-by", help="兼容旧接口，仅记录确认者名称；仍须有效文案确认")
            q.add_argument("--review", type=Path, required=True)
    return p


def dispatch(args) -> dict | str:
    from . import ledger, planning, review, source
    from .exporting import export_episode
    from .quality import audit_story_season, lint, listener_input, verify_quotes
    cmd = args.command
    if cmd == "test":
        import subprocess
        suite = (["tests.test_hygiene", "tests.test_rules_index", "tests.test_doctor", "tests.test_versioning"]
                 if args.fast else ["discover", "-s", "tests"])
        return {"passed": subprocess.run([sys.executable, "-m", "unittest", *suite], cwd=ROOT).returncode == 0,
                "suite": "fast" if args.fast else "full"}
    if cmd == "selftest":
        from .selftest import run
        return run()
    if cmd == "check":
        from .checks import run
        return run(args.project)
    if cmd == "produce":
        from . import produce
        if args.project == "check":
            if not args.check_episode or args.test_mode or args.from_stage or args.allow_paid:
                raise ValueError("produce check 用法：produce check <项目> ep03 [--until 阶段]")
            return produce.check(Path(args.episode), args.check_episode, until=args.until)
        if args.check_episode:
            raise ValueError("produce 用法：produce <项目> ep03 [--until 阶段] [--from 阶段]")
        return produce.run(Path(args.project), args.episode, test_mode=args.test_mode,
                           until=args.until, from_stage=args.from_stage, allow_paid=args.allow_paid)
    if cmd == "jobs":
        from . import jobs
        if args.action == "plan":
            return jobs.plan(args.project, args.stage)
        if args.action == "claim":
            return jobs.claim(args.project, args.stage, args.session)
        if args.action == "done":
            return jobs.done(args.project, args.task_id, args.session)
        return jobs.run(args.project, args.stage, test_mode=args.test_mode)
    if cmd == "guard":
        from .guard import check; return check(args.project, args.action, args.ep)
    if cmd == "doctor":
        from .doctor import check
        return check(args.project, full=not args.light)
    if cmd == "lessons":
        from . import lessons
        if args.action == "context":
            from .feedback import feedback_context
            return feedback_context(args.project)
        if args.action == "observe":
            return lessons.observe(args.project, args.quote, args.evidence)
        if args.action == "propose":
            return lessons.propose(args.project, args.lesson_id, kind=args.kind, destination=args.destination,
                                   before=args.before, after=args.after, check=args.check, rationale=args.rationale)
        if args.action == "accept":
            return lessons.accept(args.project, args.quote)
        if args.action == "apply":
            return lessons.apply(args.project, args.lesson_id)
        if args.action == "revert":
            return lessons.revert(args.project, args.lesson_id, args.quote)
        if args.action == "report":
            return lessons.report(args.project)
        return lessons.inbox(args.project) if args.action == "inbox" else lessons.triage(args.project)
    if cmd == "next":
        from .flow import derive, write
        state = derive(args.project) if args.read_only else write(args.project)
        if args.read_only:
            state["artifacts"] = []
        if args.json:
            return state
        return (f"《{state['book']}》当前阶段：{state['stage']}（{state['stage_index']}/15）\n"
                f"下一步：{state['next_step']}（{state['actor']}）\n"
                f"需要你：{'；'.join(state['needs_you']) if state['needs_you'] else '无'}\n"
                f"改动待过目：{'；'.join(state['change_pending']) if state['change_pending'] else '无'}\n"
                f"下游影响：{'；'.join(state['downstream_impacts']) if state['downstream_impacts'] else '无'}\n"
                f"阻塞：{'；'.join(state['blockers']) if state['blockers'] else '无'}\n"
                f"教训收件箱：{state['lessons_pending']} 条待分诊")
    if cmd == "approve":
        from . import approvals
        gate = approvals.CONFIRMATIONS.get(args.gate, args.gate)
        episodes = _episode_list(args.eps) if args.eps else None
        return approvals.record_confirmation(args.project, gate, args.quote, episodes, args.session)
    if cmd == "adopt-legacy":
        from .legacy_media import KINDS, adopt as adopt_legacy
        return adopt_legacy(args.project, args.ep, files={kind: getattr(args, kind) for kind in KINDS},
                            note=args.note, replace=args.replace)
    if cmd == "voices":
        from . import voice_script
        if args.action == "scaffold":
            rows = [voice_script.scaffold(args.project, ep) for ep in _episode_list(args.eps)]
            return {"passed": True, "episodes": rows,
                    "summary": "；".join(row["summary"] for row in rows),
                    "next_actions": ["逐条填写 speaker（人物 ID 或 narrator），然后运行 voices check"]}
        if args.action == "check":
            from .flow import _planned, _legacy_episodes
            episodes = _episode_list(args.eps) if args.eps else _planned(Path(args.project).resolve())
            return voice_script.season(args.project, episodes, skip=_legacy_episodes(Path(args.project).resolve(), episodes))
        return voice_script.set_voice(args.project, args.speaker, voice_id=args.voice_id, pool=args.pool, name=args.name)
    if cmd == "final":
        from .finalize import freeze_many
        return freeze_many(args.project, _episode_list(args.eps), source=args.source, replace=args.replace)
    if cmd == "dev" and args.dev_command == "migrate-approvals":
        from .approvals import migrate_legacy
        return migrate_legacy(args.project)
    if cmd == "dev" and args.dev_command in ("edit-copy", "import-edits"):
        from .feedback import create_copy, import_edits
        return (create_copy(args.draft, args.output, args.format) if args.dev_command == "edit-copy"
                else import_edits(args.docx, args.baseline))
    if cmd == "dev" and args.dev_command == "feedback-context":
        from .feedback import feedback_context
        return feedback_context(args.project)
    if cmd == "continuity":
        from . import continuity
        if args.action == "update": return continuity.update(args.project, args.ep, args.draft)
        if args.action == "context": return continuity.context(args.project, args.ep)
        return continuity.check(args.project)
    if cmd == "recap":
        from . import recap
        if args.action == "migrate":
            return recap.migrate(args.project, write=args.write)
        if args.action == "update-working":
            return recap.update_working(args.project, args.ep, args.draft, load_yaml(args.actual, {}))
        if args.action == "snapshot-final":
            return recap.snapshot_final(args.project, args.ep,
                                        load_yaml(args.actual, {}) if args.actual else None)
        if args.action == "check":
            return recap.check(args.project)
        if args.action == "inspect":
            return recap.inspect(args.project)
        return recap.context(args.project, args.ep)
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
        return sound.run(args.action, args.episode, getattr(args, "files", None), migrate=getattr(args, "migrate", False))
    if cmd == "sfx":
        from . import sfx_library
        if args.action == "search":
            return sfx_library.search(args.terms, library=args.library, sound_class=args.sound_class)
        if args.action == "stats":
            return sfx_library.stats(library=args.library)
        if args.action == "harvest":
            from .sfx_generate import harvest
            return harvest(args.project, args.ep)
        return sfx_library.add(args.project, args.episode, args.source, desc=args.desc,
                               tags=[tag.strip() for tag in args.tags.split(",")], sound_class=args.sound_class,
                               status=args.status, loop=args.loop, prompt=args.prompt, model=args.model,
                               cost_cny=args.cost_cny, library=args.library)
    if cmd == "archive":
        from . import archive
        if args.target == "restore":
            if not args.episode:
                raise ValueError("archive restore 用法：archive restore <项目> ep03 [--only audio]")
            return archive.restore(Path(args.project_or_episode), args.episode, only=args.only)
        if args.episode or args.only:
            raise ValueError("archive 用法：archive <项目> ep03；--only 仅用于 restore")
        return archive.run(Path(args.target), args.project_or_episode)
    if cmd == "source" and args.action in ("migrate", "switch"):
        from . import migration
        return migration.migrate(args.project, args.to) if args.action == "migrate" else migration.switch(args.project, args.to)
    if cmd in ("names-check", "name-change"):
        from .names import change_plan, check_project
        result = check_project(args.project) if cmd == "names-check" else change_plan(args.project, args.entity, args.name)
        if cmd == "name-change" and args.output:
            write_json(args.output, result)
        return result
    if cmd == "edit":
        if args.action == "instruction":
            from .approvals import record_user_edit_instruction
            return record_user_edit_instruction(args.project, args.ep, args.quote, args.replace)
        if args.action == "assistant-change":
            from .approvals import record_assistant_edit
            return record_assistant_edit(args.project, args.ep, args.replace, args.risk_review,
                                         no_identity_change=args.no_identity_change,
                                         no_plot_fact_change=args.no_plot_fact_change,
                                         no_ending_change=args.no_ending_change)
        from .feedback import create_copy, import_edits
        return (create_copy(args.draft, args.output, args.format) if args.action == "copy"
                else import_edits(args.docx, args.baseline))
    if cmd == "new":
        return new_project(args.slug, args.title, args.author, args.genre, profile=args.profile)
    if cmd == "ingest":
        return source.ingest(args.project, args.files)
    if cmd == "coverage":
        return planning.coverage(args.project)
    if cmd == "check-plan":
        return planning.check_plan(args.project)
    if cmd == "story-check":
        return audit_story_season(args.project)
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
        return ledger.stamp(args.project, args.ep, args.approved_by or "本机用户", args.review)
    if cmd == "export":
        return export_episode(args.draft, args.preview, args.review)
    if cmd == "status":
        project = args.project.resolve()
        if not (project / "project.yaml").exists():
            raise ValueError("找不到 project.yaml")
        from .states import derive as derive_status
        from .flow import write as write_progress
        derived = derive_status(project)
        progress = write_progress(project)
        drafts = sorted(project.glob("episodes/ep*/draft_v*.md"))
        observed = "created"
        for present, stage in [(bool(source_generation(project)), "ingested"),
                               ((project / "analysis/book_brief.md").exists(), "analyzed"),
                               ((project / "plan/episodes.yaml").exists(), "planned"),
                               (bool(drafts), "drafted"),
                               (bool(list(project.glob("episodes/ep*/preview/index.html"))), "preview_awaiting_human")]:
            if present:
                observed = stage
        return {"project": str(project), "observed_stage": observed, "derived": derived,
                "declared_state": load_yaml(project / "status.yaml"), "state": progress,
                "source_generation": source_generation(project),
                "analysis_exists": (project / "analysis/book_brief.md").exists(),
                "plan_exists": (project / "plan/episodes.yaml").exists(),
                "drafts": [str(x.relative_to(project)) for x in drafts],
                "ledger": ledger.check(project)}
    raise ValueError("未知命令")


def _record_cli_failure(args, message: str) -> dict | None:
    # Expected findings from read-only diagnostics are not script failures.
    if args.command in {"lessons", "story-check", "check"}:
        return None
    candidate = None
    for key in ("project", "draft", "episode", "baseline"):
        value = getattr(args, key, None)
        if value is None:
            continue
        path = Path(value).resolve()
        found = path if (path / "project.yaml").is_file() else find_project(path)
        if found is not None:
            candidate = found
            break
    if candidate is None:
        return None
    try:
        from .lessons import record_script_error
        return record_script_error(candidate, args.command, message)
    except (OSError, ValueError, yaml.YAMLError):
        return {"status": "warning", "summary": "命令失败已报告，但教训收件箱写入失败；请检查本机目录权限和格式"}


def _episode_list(text: str) -> list[int]:
    episodes: list[int] = []
    for part in str(text).replace("，", ",").split(","):
        part = part.strip()
        if "-" in part:
            start, end = map(int, part.split("-", 1))
            if start < 1 or end < start:
                raise ValueError("--eps 范围无效")
            episodes.extend(range(start, end + 1))
        elif part:
            if int(part) < 1:
                raise ValueError("--eps 集号必须为正整数")
            episodes.append(int(part))
    if not episodes:
        raise ValueError("--eps 不能为空")
    return list(dict.fromkeys(episodes))


def main() -> int:
    args = parser().parse_args()
    try:
        result = dispatch(args)
        if (isinstance(result, dict) and (result.get("errors") or result.get("passed") is False)
                and not (args.command == "voices" and args.action == "check"
                         and result.get("status") == "warning")):
            lesson = _record_cli_failure(args, "；".join(map(str, result.get("errors") or [result.get("summary", "命令失败")])))
            if lesson is not None:
                result["lesson_intake"] = lesson
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and (result.get("errors") or result.get("passed") is False) else 0
    except (ValueError, OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        from .lessons import safe_error
        message = safe_error(str(exc))
        lesson = _record_cli_failure(args, f"{type(exc).__name__}: {message}")
        payload = {"errors": [message]}
        if lesson is not None:
            payload["lesson_intake"] = lesson
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
