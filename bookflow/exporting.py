"""Inspectable local preview and gated delivery export."""
from __future__ import annotations

import html
from pathlib import Path

from .common import atomic_write, find_project, fmt_time, load_config, load_yaml, parse_draft, sha256_file, source_generation
from .quality import lint, listener_input, verify_quotes


def export_episode(path: Path, preview: bool = True, review_path: Path | None = None) -> dict:
    from . import review
    path = Path(path).resolve()
    project = find_project(path)
    if project is None:
        raise ValueError("找不到 project.yaml")
    draft = parse_draft(path.read_text(encoding="utf-8"))
    ep = int(draft["meta"].get("episode", 1))
    cfg = load_config(project)
    story = cfg.get("profile") == "story"
    quality, quotes = lint(path), verify_quotes(path)
    review_result = {"passed": False, "errors": ["尚未提交事实审校" if story else "尚未提交完整独立审校"], "warnings": []}
    if review_path:
        review_result = review.evaluate(project, ep, path, load_yaml(review_path))
    if not preview:
        if path.name != "final.md" or draft["meta"].get("status") != "final":
            raise ValueError("正式打包需要 final.md 定稿候选")
        if not quality["passed"] or not quotes["passed"] or not review_result["passed"]:
            raise ValueError("正式打包前必须通过稿件、引用和独立审校检查")
        compliance_path = project / "release" / "compliance.yaml"
        if not compliance_path.is_file():
            raise ValueError("正式交付缺少 release/compliance.yaml")
        compliance = load_yaml(compliance_path, {}) or {}
        required = [compliance.get("copyright",{}).get("status"), compliance.get("copyright",{}).get("note"),
                    compliance.get("ai_content_label",{}).get("explicit_label"), compliance.get("ai_content_label",{}).get("platform_setting"),
                    compliance.get("ai_content_label",{}).get("checked_rules"), compliance.get("likeness_and_assets",{}).get("note"),
                    compliance.get("platform",{}).get("name"), compliance.get("platform",{}).get("rules_checked_at"), compliance.get("reviewer")]
        if any(not str(x or "").strip() for x in required):
            raise ValueError("正式交付前必须完整填写 release/compliance.yaml")
        from .approvals import confirmation_state
        if confirmation_state(project, "script", ep)["state"] != "passed":
            raise ValueError("正式交付需要当前纯口播一致的文案确认记录")
        if confirmation_state(project, "release", ep)["state"] != "passed":
            raise ValueError("正式交付需要当前成片、字幕与合规文件一致的成片确认记录")
        recap_path = project / "episodes/recap.yaml"
        if recap_path.exists() or recap_path.is_symlink():
            from .recap import final_snapshot_state
            state = final_snapshot_state(project, list(range(1, ep + 1)))
            if not state["passed"]:
                raise ValueError("本集或前序前情表定稿快照未有效复核：" + "；".join(state["errors"]))
            continuity_label = "前情快照：已复核"
        else:
            from .ledger import context as ledger_context
            state = ledger_context(project, ep + 1)
            if state.get("errors") or state.get("passed") is False:
                raise ValueError("本集或前序旧账本未有效入账：" + str(state.get("errors", state)))
            continuity_label = "旧账本：已确认"
    output = path.parent / ("preview" if preview else "deliver")
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / "voiceover.txt", draft["spoken"] + "\n")
    atomic_write(output / "listener_input.txt", listener_input(path))
    plan = load_yaml(project / "plan/episodes.yaml")
    episodes = plan if isinstance(plan, list) else plan.get("episodes", [])
    entry = next((item for item in episodes if int(item.get("ep", 0)) == ep), {})
    takes = entry.get("takeaways") or []
    if not isinstance(takes, list):
        raise ValueError("本集计划的 takeaways 必须是列表")
    question = str(entry.get("core_question") or "")
    rate = cfg["format"]["speech_rate_cpm"]
    shot = ["# 画面清单", "", "时间按字数估算，实际配音后需重新对齐。", "", "| 估算时间 | 节段 | 类型 | 建议 |", "|---|---|---|---|"]
    for item in draft["production"]:
        values = [fmt_time(item["offset"] * 60 / rate), item["section"], item["kind"], item["text"]]
        shot.append("| " + " | ".join(str(x).replace("|", "／") for x in values) + " |")
    atomic_write(output / "shotlist.md", "\n".join(shot) + "\n")
    state_label = "效果预览 · 等待用户定调与终审" if preview else "已确认定稿 · 正式交付"
    qa = ["# 质检说明", "", f"状态：{state_label}", f"稿件 SHA256：`{sha256_file(path)}`",
          f"原文版本：`{source_generation(project)}`", "", f"口播：{quality['stats']['chars']} 字；估算 {quality['stats']['estimated_time']}（未实测音频）。",
          f"脚本检查：{'通过' if quality['passed'] else '未通过'}；引文/依据检查：{'通过' if quotes['passed'] else '未通过'}；独立审校：{'通过' if review_result['passed'] else '未通过或未完成'}。",
          "", "## 本集叙事问题与策划参考" if story else "## 本集核心收获", ""]
    if story and question:
        qa.append(f"- 本集问题：{question}")
    qa.extend(f"- {take.get('id', '')}：{take.get('text', '')}；依据：{', '.join(take.get('evidence', []))}" for take in takes)
    qa += ["", "## 剩余问题与提示", ""]
    for kind, report in [("稿件", quality), ("引文", quotes), ("审校", review_result)]:
        qa.extend(f"- {kind}：{message}" for message in report.get("errors", []) + report.get("warnings", []))
    if preview:
        qa.append("- 尚未获得用户风格认可或终审确认；预览不会创建定稿前情快照或旧账本记录。")
    atomic_write(output / "qa_summary.md", "\n".join(qa) + "\n")
    title = draft["meta"].get("title_working", cfg.get("book", {}).get("title", "精讲预览"))
    paragraphs = []
    current = None
    buf = []
    for sentence in draft["sentences"]:
        if sentence["section"] != current:
            if buf:
                paragraphs.append("<p>" + html.escape("".join(buf)) + "</p>")
                buf = []
            current = sentence["section"]
            if current:
                paragraphs.append("<h2>" + html.escape(current) + "</h2>")
        if buf and sum(len(s) for s in buf) > 160:
            paragraphs.append("<p>" + html.escape("".join(buf)) + "</p>")
            buf = []
        buf.append(sentence["text"])
    if buf:
        paragraphs.append("<p>" + html.escape("".join(buf)) + "</p>")
    take_html = (f"<li><b>本集问题</b> {html.escape(question)}</li>" if story and question else "") + "".join(f"<li><b>{html.escape(str(t.get('id','')))}</b> {html.escape(t.get('text',''))}<small>依据 {html.escape(', '.join(t.get('evidence', [])))}</small></li>" for t in takes)
    notices = "".join(f"<li>{html.escape(str(x))}</li>" for x in quality["errors"] + review_result["errors"])
    package = path.parent / "package.md"
    package_text = package.read_text(encoding="utf-8") if package.exists() else "标题、封面和配音建议尚待编辑补充。"
    css = """
*{box-sizing:border-box}body{margin:0;background:#f3f1ec;color:#263832;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}header{padding:26px 5vw;border-bottom:1px solid #d5d9cf;display:flex;justify-content:space-between;gap:12px}header b{letter-spacing:2px}header span{font-size:13px;color:#737d70}.layout{max-width:1210px;margin:44px auto;display:grid;grid-template-columns:1fr 300px;gap:34px;padding:0 24px}article{background:#fffdf8;padding:52px 58px;border:1px solid #e2e4db;border-radius:10px}h1{font-size:35px;line-height:1.4;margin:18px 0 26px}.badge{background:#e5edde;padding:7px 12px;display:inline-block;font-size:12px;border-radius:6px;color:#476543}.meta{color:#737d70;font-size:14px;padding-bottom:28px;border-bottom:1px solid #e6e6dc}article h2{font-size:18px;color:#5c745b;margin-top:38px}article p{font-family:'Songti SC','Noto Serif CJK SC',serif;font-size:21px;line-height:1.95;text-align:justify;margin:18px 0}.card{padding:24px;background:#e9ece3;border-radius:10px;margin-bottom:18px}.card h3{font-size:15px;margin:0 0 15px}.card ul{padding-left:18px;font-size:14px;line-height:1.9}.card li{margin-bottom:14px}.card small{display:block;color:#727d6e}.metric{font-size:30px;line-height:1.8}.card a{color:#416447}.sub{font-size:13px;color:#65735f;line-height:1.8}details{margin-top:20px;padding:20px;background:#f4f5ed}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.9 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}footer{max-width:1210px;margin:30px auto;padding:24px;color:#76806f;font-size:12px}@media(max-width:850px){.layout{grid-template-columns:1fr;margin:20px auto;gap:20px}article{padding:28px 24px}h1{font-size:28px}.card{margin-bottom:10px}.card ul{margin:0}article p{font-size:19px}}@media print{header,aside,footer,details{display:none}.layout{display:block;margin:0;padding:0}article{border:0;padding:0}body{background:white}}
"""
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}｜精讲工作室</title><style>{css}</style><header><b>书籍精讲工作室</b><span>FIRST EDITION / 01 · 本地工作流</span></header><main class="layout"><article><div class="badge">{state_label}</div><h1>{html.escape(title)}</h1><div class="meta">第 {ep} 集 · {html.escape(cfg.get('book',{}).get('title',''))} · 口播阅读版</div>{''.join(paragraphs)}<details><summary>标题、封面与制作建议</summary><pre>{html.escape(package_text)}</pre></details><details><summary>查看质检说明</summary><pre>{html.escape(chr(10).join(qa))}</pre></details></article><aside><div class="card"><h3>这一集听完，带走什么</h3><ul>{take_html}</ul></div><div class="card"><h3>口播概况</h3><div class="metric">{quality['stats']['estimated_time']}</div><div class="sub">{quality['stats']['chars']} 字 · 按 {rate} 字/分钟估算<br>实际配音与人工试听尚待完成</div></div><div class="card"><h3>验收状态</h3><div class="sub">脚本：{'通过' if quality['passed'] else '有错误'}<br>引用：{'通过' if quotes['passed'] else '有错误'}<br>独立审校：{'通过' if review_result['passed'] else '未完成或待修订'}<br>{'人工定调与终审：待确认' if preview else continuity_label}</div><ul>{notices}</ul></div><div class="card"><h3>文件</h3><div class="sub"><a href="voiceover.txt">纯口播稿</a> · <a href="shotlist.md">画面清单</a><br><a href="qa_summary.md">质检说明</a></div></div></aside></main><footer>本地生成，无外部字体、统计或网络请求。脚本用于核查可量化问题，内容质量以独立审稿和实际试听为准。</footer></html>"""
    if story:
        page = page.replace("<h3>这一集听完，带走什么</h3>", "<h3>本集问题与线索</h3>", 1)
    atomic_write(output / "index.html", page)
    return {"status": "preview_awaiting_human" if preview else "delivered", "output": str(output),
            "files": [str(output / name) for name in ("index.html", "voiceover.txt", "listener_input.txt", "shotlist.md", "qa_summary.md")],
            "quality": quality["passed"], "quotes": quotes["passed"], "review": review_result["passed"]}
