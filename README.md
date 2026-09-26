# 书籍精讲工作室

一个在 Codex / Claude Code 会话中使用的本地工作流：模型负责精读、策划、写稿和独立审稿；Python 负责证据、硬指标、版本依赖和导出。默认没有模型 API、配音或发布调用。

## 它由什么组成

这是一个文件驱动的混合工作流，不是单一程序，也不是单一 Skill：`.agents/skills/` 和 `docs/` 保存工作方法，`bookflow/` 与 `run.sh` 执行确定性操作，本机的 `projects/<book>/` 保存每本书的长期资料和版本，聊天窗口负责当前一轮的理解与协调。`projects/`、`图书库/` 和个人风格库不上传到公开仓库。

上下文边界、最小读取范围和跨聊天续接方法见 [上下文管理与跨聊天续接](docs/CONTEXT_MANAGEMENT.md)。

## 先看效果

`demos/source/` 提供原创测试材料。书目项目和生成产物只在本机保存；运行示例后可在本机 `projects/` 查看结果。

## 人工改稿

新建书目默认先输出全部集数文案，再统一人工改稿（`drafting.mode: full_season_review`）。现有书目按用户要求启用；文字阶段不再被 G2/G3/G4 或两集预览上限中断。机器计划、原文依据和全季工作连续性仍检查，媒体及正式交付仍保留人工闸门。

人工修改默认使用 Markdown：直接修改 `.md` 后发回即可。系统保留原稿，逐处对比改前改后，再由助手提炼有证据的风格规则；Word / WPS 仍兼容，但不再作为默认格式。详见 [人工改稿与风格积累](docs/HUMAN_EDITING.md)。

主体口播和声音设计分开：2.0 只生成纯口播，1.0 的音效、环境声、回声和片头由独立声音清单规划，绑定小节、句子和估算时间，剪辑阶段再合成。默认每条素材只生成一个版本；均匀环境声用短循环，零散事件一次生成后切片，安静底层可用本地 FFmpeg 合成。音效必须有可辨认的声源和叙事作用，关键转折优先；不能用泛泛白噪凑数量。敲门、枪响等事件音会让口播短暂停顿；雨声、风声等环境音可以压低后铺在口播下面，但压低后仍应保留辨识度。完整规则见 [口播视频声音制作规范](docs/SOUND_PRODUCTION.md)。

```bash
./run.sh edit-copy projects/my-book/episodes/ep01/draft_v3.md --output projects/my-book/episodes/ep01/human_edit/v3
./run.sh import-edits '/你的路径/改稿.md' --baseline projects/my-book/episodes/ep01/human_edit/v3/baseline.json
./run.sh feedback-context projects/my-book
```

## 本地运行

Codex 桌面版可自行配置本地环境 Actions 作为常用命令入口；默认使用终端运行 `run.sh`。本机 Actions 配置不上传到公开仓库。

已准备好项目内 `.venv`。换电脑或重建环境时执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

要求 Python 3.11+；原文原子提交锁使用 macOS/Linux 的 `fcntl`。Windows 需适配锁实现，尚未验证。

```bash
./run.sh --help
./run.sh new my-book --title '书名' --author '作者' --genre suspense
./run.sh ingest projects/my-book '/绝对路径/原文.epub'
./run.sh status projects/my-book
./run.sh guard projects/my-book draft --ep 1
```

TXT、Markdown、EPUB、DOCX 可直接导入。PDF、MOBI 在首版中需先转换为 TXT/EPUB；扫描 PDF 需先 OCR。脚本不会处理 DRM。已有原文 generation 不同的导入会被拒绝，请另建项目用于新版本，避免旧段落编号指向新内容。

## 让代理开始工作

项目技能已放在 `.agents/skills/`，角色正文在 `roles/`。可以直接说：

> 给 my-book 做精读与查漏，产出逐章笔记、全书简报、线索表，做完给我看。

> 给 my-book 做分集策划。每集先说观众带走什么，再决定讲哪些情节。

> 写 my-book 第 1 集，并用三个独立角色审稿，出效果预览。

具体步骤、各角色输入和审校格式见 [工作流](docs/WORKFLOW.md)。原方案备份见 [设计参考](docs/DESIGN_REFERENCE_v1.1.md)。

如果聊天变长，可以开启新聊天，只需说：

> 继续这个项目，接着做：……

如果有多本书，再加上书名即可。项目文件的读取和状态核对由代理完成，不需要你记住文件名。

## 常用命令

译名更正与资料同步见 [统一译名流程](docs/NAME_CONSISTENCY.md)。`names-check` 查当前用名一致性，`name-change` 先列影响清单；真实改稿中的名称变化会单独提示核对人物身份。

```bash
./run.sh coverage projects/my-book
./run.sh check-plan projects/my-book
./run.sh lint projects/my-book/episodes/ep01/draft_v1.md
./run.sh quotes projects/my-book/episodes/ep01/draft_v1.md
./run.sh listener-input projects/my-book/episodes/ep01/draft_v1.md --output /tmp/listener.txt
./run.sh review-check projects/my-book/episodes/ep01/draft_v1.md --report projects/my-book/episodes/ep01/review/summary_v1.yaml
./run.sh export projects/my-book/episodes/ep01/draft_v1.md --preview --review projects/my-book/episodes/ep01/review/summary_v1.yaml
./run.sh ledger context projects/my-book 2
./run.sh sentences generate projects/my-book/episodes/ep01/draft_v1.md
./run.sh anchors check projects/my-book/episodes/ep01
./run.sh timing import projects/my-book/episodes/ep01 --audio /path/to/voice.mp3
./run.sh pacing budget projects/my-book/episodes/ep01
./run.sh pacing check projects/my-book/episodes/ep01
./run.sh source migrate projects/my-book --to <generation>
```

`lint/quotes/check-plan/review-check` 有错误时退出码为 1。预览允许显示尚未通过的稿件，但会在页面明确显示缺项；它不代表正式验收。

## 正式定稿

1. 先生成 `final.md` 候选，元数据设置 `status: final`。
2. 对这份确切文件跑检查、独立审稿，保存对应的 hash。元数据变化也会改变 hash，不能挪用旧报告。
3. 用户确实完成终审确认后，在交互终端运行 `./run.sh approve <project> G4 --ep N`；`ledger stamp` 只认哈希一致的批准记录。旧 `--approved-by` 仅作兼容。
4. 使用 `export final.md --deliver --review <本版审校汇总>` 生成正式交付包。

## 首版重点保护的行为

- 原文完整导入后才切换指针；失败时旧资料继续可用。
- `coverage_machine.json` 与 `coverage_review.yaml` 分开，重扫不清掉人工意见。
- 核心收获必须讲到、有据；个人评论占比只作诊断。
- 书外事实采用 `ext:E01`，要求来源标题、链接、核实日期与 `verified` 状态。
- listener 看句子 ID 和纯口播；钩子按句子对齐，避免段首时间误差。
- 正式入账绑定定稿、原文、前序定稿、原始审校报告的指纹；前集重盖章不会自动解除后集复核。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

单元测试覆盖文件保留、导入顺序、范围证据、独立审稿缺项、核心深度、前后集依赖等。实际配音时长、真人听感、真实观众留存须在样稿确认后另行验证。

公开仓库只保留工作室通用代码、规则、文档和原创测试材料；本机书目制作成果不作为公开示例上传。
