"""Read-only environment and project health checks; never expose credentials."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from .common import ROOT


def check(project: Path | None = None, *, full: bool = True) -> dict:
    """Report actionable health checks without creating files or contacting services."""
    project = Path(project).resolve() if project is not None else None
    checks: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    next_actions: list[str] = []

    def add(name: str, status: str, message: str, remedy: str = "") -> None:
        checks.append({"id": name, "status": status, "message": message})
        if status == "error":
            errors.append(message)
        elif status == "warning":
            warnings.append(message)
        if status != "success" and remedy and remedy not in next_actions:
            next_actions.append(remedy)

    if sys.version_info >= (3, 11):
        add("python", "success", "Python 版本满足 3.11+")
    else:
        add("python", "error", "Python 版本低于 3.11", "安装 Python 3.11+ 并重建虚拟环境后重跑 doctor")
    add("pyyaml", "success", f"PyYAML 可导入（{yaml.__version__}）")

    if project is not None:
        config_path = project / "project.yaml"
        if not config_path.is_file():
            add("project_config", "error", "缺少 project.yaml", "先运行 new 建立项目，或核对传入的项目路径")
            config = {}
        else:
            try:
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                config = None
            if not isinstance(config, dict):
                add("project_config", "error", "project.yaml 格式无效，必须是 YAML 映射", "修复 project.yaml 语法后重跑 doctor")
                config = {}
            else:
                book = config.get("book")
                title = book.get("title") if isinstance(book, dict) else None
                if not isinstance(title, str) or not title.strip():
                    add("project_config", "error", "project.yaml 缺少非空的 book.title", "补全书名后重跑 doctor")
                else:
                    add("project_config", "success", "project.yaml 的书名有效")
                if full:
                    missing_fields = []
                    for key in ("author", "slug"):
                        if not isinstance(book, dict) or not str(book.get(key, "")).strip():
                            missing_fields.append(f"book.{key}")
                    for key in ("audience", "platform", "voice"):
                        if not str(config.get(key, "")).strip():
                            missing_fields.append(key)
                    genre = config.get("genre")
                    if not isinstance(genre, dict) or not str(genre.get("primary", "")).strip():
                        missing_fields.append("genre.primary")
                    if missing_fields:
                        add("project_fields", "warning", "project.yaml 待补字段：" + "、".join(missing_fields),
                            "补全项目基本信息后重跑 doctor")
                    else:
                        add("project_fields", "success", "project.yaml 基本字段齐全")
                    version = config.get("studio_version")
                    if not isinstance(version, str) or not version.strip():
                        add("studio_version", "warning", "尚未记录最近使用的工作室版本",
                            "运行 next 写入当前版本；旧书目的创建版本保持未知")
                    else:
                        add("studio_version", "success", "已记录最近使用的工作室版本")
                profile = config.get("profile")
                if profile is not None and profile not in ("story", "explainer"):
                    add("profile", "error", "project.yaml 的 profile 必须是 story 或 explainer", "修正 profile 后重跑 doctor")
                elif full and profile is None:
                    add("profile", "warning", "项目尚未声明 story/explainer 档位", "确认本书档位后在 project.yaml 填写 profile")
                formatting = config.get("format", {})
                if isinstance(formatting, dict) and "speech_rate_cpm" in formatting:
                    rate = formatting["speech_rate_cpm"]
                    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
                        add("speech_rate", "error", "project.yaml 的 format.speech_rate_cpm 必须大于 0", "修正语速后重跑 doctor")
        if not project.is_dir() or not os.access(project, os.W_OK):
            add("project_writable", "error", "项目目录不存在或不可写", "核对项目目录及文件系统权限后重跑 doctor")
        else:
            add("project_writable", "success", "项目目录可写")

    if full:
        personal = ROOT / "style/personal.yaml"
        if personal.is_file():
            add("personal_style", "success", "本机私有风格文件存在（未上传；不代表已有备份）")
        else:
            add("personal_style", "warning", "缺少可选的 style/personal.yaml；已有私有偏好可能未迁入",
                "有私有偏好时从备份恢复；将该文件与 projects 一起备份到已配置的私人 OneDrive，勿提交公开仓库")
        for executable in ("ffmpeg", "ffprobe"):
            if shutil.which(executable):
                add(executable, "success", f"{executable} 可执行")
            else:
                add(executable, "error", f"缺少 {executable}，无法完成媒体检查与渲染",
                    f"安装 {executable} 并确认在 PATH 后重跑 doctor；不要开始媒体制作")
        font_ok = any(path.is_file() for path in (
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/System/Library/Fonts/STHeiti Medium.ttc"),
            Path("/System/Library/Fonts/STHeiti Light.ttc"),
            Path("/Library/Fonts/NotoSansCJK-Regular.ttc"),
        ))
        if not font_ok and shutil.which("fc-match"):
            try:
                matched = subprocess.run(["fc-match", "-f", "%{family}", "PingFang SC"],
                                         capture_output=True, text=True, timeout=3, check=True).stdout
                font_ok = any(name in matched for name in ("PingFang", "Noto Sans CJK", "Source Han"))
            except (OSError, subprocess.SubprocessError):
                pass
        if font_ok:
            add("chinese_font", "success", "可找到中文字体")
        else:
            add("chinese_font", "warning", "未能确认中文字体，字幕渲染可能缺字", "安装中文字体后用测试字幕抽帧检查")

        has_key = bool(os.environ.get("DOUBAO_API_KEY", "").strip())
        if not has_key:
            local_config = Path.home() / ".tts_config.json"
            if local_config.is_file():
                try:
                    data = json.loads(local_config.read_text(encoding="utf-8"))
                    has_key = isinstance(data, dict) and bool(str(data.get("api_key", "")).strip())
                except (OSError, UnicodeError, ValueError):
                    add("doubao_config", "warning", "本机语音配置不可读", "修复本机语音配置，或设置 DOUBAO_API_KEY 后重跑 doctor")
        if has_key:
            add("doubao_key", "success", "豆包凭据可读取（内容未显示；未连接服务验证）")
        else:
            add("doubao_key", "warning", "未找到可读取的豆包凭据", "生成配音或音效前设置 DOUBAO_API_KEY，不要把密钥写入仓库")
        add("image_generation", "warning", "Codex 内置生图权限无法从本地脚本验证，未调用生成服务",
            "在生图阶段于 Codex 会话中试生成一张测试图，确认权限和结果")

        session_root = Path.home()
        session_ok = any(path.is_dir() and os.access(path, os.R_OK) for path in (
            session_root / ".codex/sessions", session_root / ".claude/projects"))
        if session_ok:
            add("session_logs", "success", "可读取至少一种本机会话记录目录")
        else:
            add("session_logs", "warning", "未找到可读取的本机会话记录目录，聊天确认原话核对可能降级",
                "在当前会话中确认记录目录可读；不可读时保留警告，不声称已核对原话")
        disk_target = project if project and project.exists() else ROOT
        try:
            free = shutil.disk_usage(disk_target).free
        except OSError:
            add("disk_space", "warning", "无法读取剩余磁盘空间", "检查项目所在磁盘是否已挂载后重跑 doctor")
        else:
            if free < 5 * 1024**3:
                add("disk_space", "warning", "剩余磁盘空间低于 5 GiB，媒体制作可能失败", "清理空间并保留原件后重跑 doctor")
            else:
                add("disk_space", "success", "剩余磁盘空间至少 5 GiB")
        if project is not None:
            cast = project / "production/voice_cast.yaml"
            if cast.is_file():
                add("voice_cast", "success", "项目音色表存在")
            else:
                add("voice_cast", "warning", "尚未建立 production/voice_cast.yaml",
                    "进入付费配音前建立并确认音色表")
            current = project / "source/current.json"
            source_ready = False
            if not current.is_file():
                add("source_batch", "warning", "尚未导入原文批次", "运行 ingest 并抽查原文")
            else:
                try:
                    pointer = json.loads(current.read_text(encoding="utf-8"))
                    generation = pointer.get("generation") if isinstance(pointer, dict) else None
                    valid_generation = isinstance(generation, str) and re.fullmatch(r"[a-f0-9]{64}", generation)
                except (OSError, UnicodeError, ValueError):
                    valid_generation = False
                    generation = None
                if not valid_generation:
                    add("source_batch", "error", "source/current.json 不是有效的原文批次指针",
                        "检查原文批次指针和备份；修复后重跑 doctor，不要覆盖原文")
                else:
                    source_dir = project / "source/imports" / generation
                    missing = [name for name in ("paragraphs.jsonl", "chapters.json", "manifest.json")
                               if not (source_dir / name).is_file()]
                    if missing:
                        add("source_batch", "error", "当前原文批次缺少：" + "、".join(missing),
                            "核对 source/imports 中的当前批次与备份，不要直接重导覆盖")
                    else:
                        source_ready = True
                        add("source_batch", "success", "当前原文批次文件齐全")
            if source_ready:
                required = ("source_validation.json", "intake_report.md", "book_brief.md",
                            "characters.yaml", "threads.yaml", "coverage_machine.json", "coverage_review.yaml")
                missing = [name for name in required if not (project / "analysis" / name).is_file()]
                add("analysis_files", "warning" if missing else "success",
                    "分析阶段缺少：" + "、".join(missing) if missing else "分析阶段基础文件齐全",
                    "补齐分析与独立审阅后重跑 doctor" if missing else "")
                plan = project / "plan/episodes.yaml"
                if not plan.is_file():
                    add("plan_files", "warning", "尚未建立 plan/episodes.yaml", "完成分集方案后重跑 doctor")
                else:
                    try:
                        plan_data = yaml.safe_load(plan.read_text(encoding="utf-8"))
                        episodes = plan_data if isinstance(plan_data, list) else plan_data.get("episodes")
                        planned = sorted({row["ep"] for row in episodes
                                          if isinstance(row, dict) and type(row.get("ep")) is int and row["ep"] > 0})
                    except (OSError, UnicodeError, yaml.YAMLError, AttributeError, TypeError):
                        planned = []
                    if not planned:
                        add("plan_files", "error", "plan/episodes.yaml 缺少有效集数",
                            "修复分集方案后重跑 doctor")
                    else:
                        add("plan_files", "success", f"分集方案列出 {len(planned)} 集")
                        missing_drafts = [ep for ep in planned if not any(
                            (project / "episodes" / f"ep{ep:02d}").glob("draft_v*.md"))
                            and not (project / "episodes" / f"ep{ep:02d}" / "final.md").is_file()]
                        add("draft_files", "warning" if missing_drafts else "success",
                            f"尚无初稿的集数：{missing_drafts}" if missing_drafts else "各集均有初稿或定稿",
                            "继续完成所列集数的初稿" if missing_drafts else "")
                        final_started = any((project / "episodes" / f"ep{ep:02d}" / "final.md").is_file()
                                            for ep in planned)
                        if final_started:
                            missing_finals = [ep for ep in planned if not
                                              (project / "episodes" / f"ep{ep:02d}" / "final.md").is_file()]
                            add("final_files", "warning" if missing_finals else "success",
                                f"尚无定稿的集数：{missing_finals}" if missing_finals else "各集均有 final.md",
                                "统一改稿和确认后补齐定稿，不要代填批准" if missing_finals else "")

    status = "error" if errors else "warning" if warnings else "success"
    return {"status": status, "passed": not errors, "summary": f"体检完成：{len(errors)} 项错误，{len(warnings)} 项提醒",
            "checks": checks, "errors": errors, "warnings": warnings, "next_actions": next_actions,
            "artifacts": [str(project / "project.yaml")] if project is not None else []}
