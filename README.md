# 书籍精讲工作室

一个在 Codex / Claude Code 会话中使用的本地工作流：模型负责精读、策划、写稿和独立审稿；Python 负责证据、硬指标、版本依赖和导出。默认没有模型 API、配音或发布调用。

## 它由什么组成

这是一个文件驱动的混合工作流，不是单一程序，也不是单一 Skill：`.agents/skills/` 和 `docs/` 保存工作方法，`bookflow/` 与 `run.sh` 执行确定性操作，本机的 `projects/<book>/` 保存每本书的长期资料和版本，聊天窗口负责当前一轮的理解与协调。`projects/`、`图书库/` 和个人风格库不上传到公开仓库。

从 `./run.sh next <项目>` 获取文件推导的阶段与待办；当前阶段的最小读取范围见 `.agents/skills/` 对应 Skill。

## 先看效果

`demos/source/` 提供原创测试材料：中文《迟到的钟》用于音画全流程，另有一篇 [CC0 英文短篇](demos/source/FOREIGN_FIXTURE.md) 用于外文原文导入与分章自检。书目项目和生成产物只在本机保存；运行示例后可在本机 `projects/` 查看结果。

## 人工改稿

新建书目默认全季初稿后统一人工改稿（`drafting.mode: full_season_review`），现有书目按用户要求启用。以 `next` 推进四项确认，不要求先改第一集才写后续；计划、来源和实际前情仍检查，文字授权不包含媒体及正式交付。
新建剧情项目默认 `profile: story`：核心收获与 `[钩子]` 不再是硬性要求，文字审校仍须有独立事实报告；新建非虚构项目默认 `profile: explainer`，保留精讲检查。旧项目不自动改档位。场景对白还原用中文双引号，所在段须标原文依据；逐字原文引用仍按 `〔引 p…〕「…」` 核对。

人工修改默认使用 Markdown：直接修改 `.md` 后发回即可。系统保留原稿，逐处对比改前改后，再由助手提炼有证据的风格规则；Word / WPS 仍兼容，但不再作为默认格式。详见[改稿导入与反馈流程](.agents/skills/import-feedback/SKILL.md)。

用户在聊天里明确给出改前、改后文字时，助手完成修改后登记原话和精确替换：`./run.sh edit instruction projects/my-book 3 --quote '把“旧地名”改为“新地名”' --replace '旧地名' '新地名'`。可重复 `--replace` 登记多处。当前定稿必须恰好等于这些替换结果，改后文字须出现在原话中；普通 `next` 验证通过后才沿用确认。会话记录可读取时要求原话与最近用户消息一致；不可读取时会留下提示并继续。多余改动、整集替换、撤回或证据失效都不会沿用。

助手主动小改先登记：`./run.sh edit assistant-change projects/my-book 3 --replace '旧句' '新句' --risk-review '仅调整语序，不改变人物、事实或结尾' --no-identity-change --no-plot-fact-change --no-ending-change`。默认阈值 `approvals.minor_change_ratio: 0.03`；证据和风险检查均通过、没有其他变化时保留文案确认。`next` 列出待过目清单，下次人工确认一起看，不为小改单独卡住制作。超限、整集替换、未登记变动或证据失效仍要求重新确认；前情和媒体须按新内容重检。

主体口播和声音设计分开：旁白、音效分别列入正式清单，绑定稿件版本、句子锚点和费用证据，再由本地阶段合成。当前豆包 2.0 适配器尚未完成真实服务端/账单验收；豆包 1.0 付费音效适配器尚未接入。声音流程与安全边界见 [produce-audio](.agents/skills/produce-audio/SKILL.md)。

本机共享音效库默认放在 `音效库/`，已从公开仓库排除。`./run.sh sfx search 雨 窗户` 只返回已验收且文件哈希有效的素材；`./run.sh sfx stats` 查看索引。`./run.sh sfx add projects/my-book ep01 /绝对路径/音效.wav --desc '雨打窗户' --tags '雨,窗户' --class ambience` 默认仅作为 `pending` 登记；标记 `--status accepted` 必须有该集有效样片或成片确认，以及音效制作清单的同哈希记录。`sound estimate` 会给 cue 推荐候选，但只有人工试听后把 `asset_id` 明确填为已验收的 `SFX-编号` 才减少新生成数量。它只估算音效；口播和音效的合计上限由 `produce check` 的 `budget` 报告给出。两项单价默认为 `null`，须按实际计费规则核对后填入 `project.yaml` 的 `sound_design.pricing`；价格未知时正式制作预检会阻止付费。上述命令都不启动付费生成。

成片确认且有正式导出包后，`./run.sh archive projects/my-book ep01` 按媒体分类复制到 OneDrive，逐文件校验哈希并保存 `archive/ep01.yaml`。本机 `~/.config/bookflow/config.yaml` 须设置 `onedrive_archive_root: /绝对路径/OneDrive挂载目录`；这个路径不提交到 Git。上传完成后会再次核对云端副本哈希；只有逐文件确认“仅在线”后才移除本地原件。当前 macOS 的 `fileproviderctl` 没有直接“释放空间”命令，需在访达对云端归档目录执行“释放空间”，然后重跑 `archive`。完成后的 `next` 与 `check` 会核对完整清单、重复条目、成片记录、目标路径和文件大小，不读取仅在线文件以免重新下载；实际取回时再验哈希。`./run.sh archive restore projects/my-book ep01 --only audio` 不覆盖本地不同内容。清单的 `bytes_freed` 是已移除源文件的逻辑大小；实际磁盘回收量仍需用系统磁盘用量核实。未完成真实 OneDrive 上传与释放空间验收前，不把归档视为完成。

```bash
./run.sh edit copy projects/my-book/episodes/ep01/draft_v3.md --output projects/my-book/episodes/ep01/human_edit/v3
./run.sh edit import '/你的路径/改稿.md' --baseline projects/my-book/episodes/ep01/human_edit/v3/baseline.json
./run.sh lessons context projects/my-book
```

已确认的 `final.md` 若由用户本人修改：以该文件执行 `edit copy`，用 `edit import` 导入返回的稿件，并完成该轮语义复核（`learning.yaml` 为 `reviewed`）。当原确认、导入差异和当前 `final.md` 的纯口播逐一吻合时，普通 `next` 会在确认日志追加一条带证据的“沿用”；`next --read-only` 不写日志。导入记录不能单独证明编辑者身份，助手须先核对稿件确由用户交回；未复核、撤回、证据损坏或文字不匹配时不会沿用。教训分诊、前情快照及下游音画检查仍须分别完成。

## 经验收件箱

明确的用户纠正可用 `./run.sh lessons observe projects/my-book --quote '用户原话' --evidence '会话消息定位'` 登记；导入人工改稿和可定位到书目项目的命令失败会自动登记。`./run.sh lessons triage projects/my-book` 查看待分诊条目，助手准备唯一归宿、改前改后和配套检查后交用户确认。`next` 在阶段交界遇到未分诊条目会暂停。收件箱保留在本机，不上传公开仓库。用户独立确认后，`lessons apply <项目> <编号>` 可验证并单独提交共享规则；`lessons report <项目>` 列出已应用但标为 `unverified` 的规则。撤回需用户独立回复“撤回 <编号>”，然后运行 `lessons revert <项目> <编号> --quote '撤回 <编号>'`；目标文件有后续改动时会停止自动撤回。本书专属教训暂不提交到公开仓库，需先建立本地版本机制。

## 前情表迁移预览

旧书目的工作前情和正式账本可先运行 `./run.sh recap migrate projects/my-book` 预览；成功时不改写书目文件。核对集数与失效候选后，才显式运行 `./run.sh recap migrate projects/my-book --write`，新建本机 `episodes/recap.yaml`。迁移文件保留两套旧记录并标为待逐集语义复核，不覆盖已有 recap、不删除旧文件，也不自动认可草稿或定稿。命令失败会按教训收件箱规则登记错误；目前 `next`、草稿审校、初稿任务卡和正式导出已逐步改读 recap，其余旧命令尚未全部迁移。

已有 recap 的书目可用 `./run.sh recap inspect projects/my-book` 只读获取当前候选的文件和条目指纹；读完实际稿件后再填写每集 `selected_basis` 与 `semantic_review`（复核者、具体核对说明、结尾摘要、空的未决列表、`reviewed_file_sha256`、`reviewed_entry_sha256`）。迁移命令不会代填。随后用 `./run.sh recap check projects/my-book` 核对版本与记录，`./run.sh recap context projects/my-book 3` 读取已复核的前两集实际内容。`next` 遇到已存在但未完成复核或已失效的 recap 会停在相应文字或声音阶段；草稿审校也会拒绝绕回旧工作前情。正式导出要求本集及前序的定稿快照有效，发现无效 recap 不会退回旧账本；尚无 recap 的旧书目暂沿用旧账本。旧 `ledger` 命令仍保留兼容，不能据此视为已完成全量迁移。

新书目可不建旧工作前情文件：先阅读当前草稿，按 [固定示例](demos/fixtures/ep01_recap.yaml) 填写实际讲出的揭示、线索、身份和未决问题，再运行 `./run.sh recap update-working projects/my-book 1 --draft projects/my-book/episodes/ep01/draft_v1.md --actual projects/my-book/episodes/ep01/actual.yaml`。命令只登记候选，不自动判定情节，也不代填语义复核；相同输入重跑保留复核，新版草稿另存历史并重新待审。并行初稿缺前集时会标出待补依赖，收齐后须按实际前情重新登记并逐集复核。

用户完成文案确认后，`next` 会先要求定稿前情快照，未保存有效快照不能进入声音阶段。运行 `./run.sh recap snapshot-final projects/my-book 1`：若 `final.md` 纯口播与已复核工作稿一致，只沿用那份复核并记录来源；已复核定稿只有标记或元数据变化时也可沿用。若正文不同，须先读定稿并用 `--actual <定稿实际内容.yaml>` 显式提交，快照保持待语义复核。确认缺失、撤回或交付物变化时命令拒绝写入，旧候选及复核历史保留。该命令不执行人工确认。

初稿任务卡只记录本集之前的 recap 范围指纹；前集尚未复核时，任务卡明确要求按分集计划暂写，全季汇总后再核对实际前情。串行初稿队列每完成一集便重排后续卡，已完成且仍有效的卡保持完成状态。

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
# 历史叙事等题材可显式加 --profile story；旧项目不会自动改档位。
./run.sh ingest projects/my-book '/绝对路径/原文.epub'
./run.sh status projects/my-book
./run.sh next projects/my-book
```

TXT、Markdown、EPUB、DOCX 可直接导入。PDF、MOBI 在首版中需先转换为 TXT/EPUB；扫描 PDF 需先 OCR。脚本不会处理 DRM。已有原文 generation 不同的导入会被拒绝，请另建项目用于新版本，避免旧段落编号指向新内容。

## 让代理开始工作

项目技能已放在 `.agents/skills/`，角色正文在 `roles/`。可以直接说：

> 给 my-book 做精读与查漏，产出逐章笔记、全书简报、线索表，做完给我看。

> 给 my-book 做分集策划。每集先说观众带走什么，再决定讲哪些情节。

> 写 my-book 第 1 集，按书目档位审稿，出效果预览。

具体步骤、各角色输入和审校格式见 [工作流](docs/WORKFLOW.md)。其中阶段总览由 `bookflow/flow.py` 生成；修改阶段定义后运行 `./.venv/bin/python tools/generate_workflow_doc.py` 同步，`--check` 可检查是否漂移。当前架构边界见[设计取舍](docs/DESIGN.md)，早期方案仅供[历史追溯](docs/archive/DESIGN_REFERENCE_v1.1.md)。

如果聊天变长，可以开启新聊天，只需说：

> 继续这个项目，接着做：……

如果有多本书，再加上书名即可。项目文件的读取和状态核对由代理完成，不需要你记住文件名。

## 常用命令

译名更正与资料同步见[名称同步参考](.agents/skills/import-feedback/references/name-consistency.md)。`names-check` 查当前用名一致性，`name-change` 先列影响清单；真实改稿中的名称变化会单独提示核对人物身份。

```bash
./run.sh coverage projects/my-book
./run.sh check-plan projects/my-book
./run.sh check projects/my-book
./run.sh lint projects/my-book/episodes/ep01/draft_v1.md
./run.sh quotes projects/my-book/episodes/ep01/draft_v1.md
./run.sh story-check projects/my-book
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

`check <项目>` 读取 `next` 的当前阶段，汇总该阶段的机器检查、错误和待人工事项；拆书阶段会重生成 `analysis/coverage_machine.json`，但保留独立人工 `coverage_review.yaml`，其余阶段只读。归档检查核对完整清单、目标路径和文件大小，不会下载仅在线文件重算哈希；检查通过不等于用户确认或云端现场验收。旧单项检查命令仍保留兼容，尚未全部移入 `dev`。`lint/quotes/check-plan/story-check/review-check` 有错误时退出码为 1。`story-check` 只读选取每集定稿或最新工作稿，核对全季年份写法、线索 `spoken_key` 在埋点和回收集的实际出现，以及集长分布；缺集不推算分布。它也可诊断尚未迁入 `story` 档位的旧项目，并在结果中标出原档位，不改配置。长描述性称呼重复属于单集 `lint` 的启发式提醒，不是人物身份结论。全季字数阈值仍是未获用户确认的候选基线。预览允许显示尚未通过的稿件，但会在页面明确显示缺项；它不代表正式验收。

本机 `approvals/log.yaml` 保存文案确认时的纯口播快照，属于不上传的书目资料。`next` 显示集号、文件和首处差异，按用户改稿依据、助手小改证据或实质变化分别处理；旧记录无快照时不猜测改前文字。保留确认与媒体内容仍有效是两回事，不可互相替代。

## 正式定稿

1. 先生成 `final.md` 候选，元数据设置 `status: final`。
2. 对这份确切文件运行 `check <项目>` 和独立审稿，保存对应的 hash。元数据变化也会改变 hash，不能挪用旧报告。
3. 用户独立回复“拍板文案”并将该集写入 `approvals/log.yaml` 后，运行 `recap snapshot-final <项目> <集号>` 保存并复核定稿前情；有 recap 的书目不再单独运行 `ledger stamp`。旧书目未迁 recap 时才保留旧账本兼容路径。后续成片与合规文件还须单独“拍板成片”。
4. 使用 `export final.md --deliver --review <本版审校汇总>` 生成正式交付包。

## 首版重点保护的行为

- 原文完整导入后才切换指针；失败时旧资料继续可用。
- `coverage_machine.json` 与 `coverage_review.yaml` 分开，重扫不清掉人工意见。
- `explainer` 的核心收获必须讲到、有据；`story` 的收获只作策划参考，两档的事实和原文依据都须核对。
- 书外事实采用 `ext:E01`，要求来源标题、链接、核实日期与 `verified` 状态。
- listener 看句子 ID 和纯口播；钩子按句子对齐，避免段首时间误差。
- 正式入账绑定定稿、原文、前序定稿、原始审校报告的指纹；前集重盖章不会自动解除后集复核。

## 验证

只改技能与说明时先运行快速集（目标一分钟内，不跑媒体编码）；修改行为时还需相关回归、完整集和自检。

```bash
./run.sh test --fast
./run.sh test
./run.sh selftest
```

无本地虚拟环境但 `python3` 已有依赖时，等价入口是 `python3 -m bookflow test --fast`、`python3 -m bookflow test` 和 `python3 -m bookflow selftest`。快速集不能代替行为修改的完整回归。

共享剧情写法见 [story_craft.md](style/story_craft.md)。私有 `style/personal.yaml` 不上传；迁机时与 `projects/` 一起备份、恢复到私人存储，`doctor` 检查其存在性，但不据此声称云端上传或仅在线状态已验证。

`selftest` 在临时目录运行原创示范书：从新建、导入、稿件预览到静音口播/音效、占位图、字幕、真实 MP4、测试事实审校与正式格式交付；再用独立的假云目录模拟上传阻断、归档和哈希取回。ffprobe 核对音视频流与时长，抽帧检查字幕。所有测试确认和测试审校均标为 `test_fixture_only`，不会进入真实项目；返回 `scope: text_media_fixture`，仍不代表真实付费制作、人工审校/试听、OneDrive 或发布验收。无 ffmpeg/ffprobe 时自检明确失败。单元测试覆盖文件保留、导入顺序、范围证据、独立审稿缺项、核心深度、前后集依赖等。

`produce check <项目> ep03` 可只读查看阶段输入/输出和费用预检。新项目在 `project.yaml` 中默认设置每集 20 元上限；预检将口播字符、待生成音效、已明确选用的库素材、已发生的逐笔费用合并计算。真实媒体清单须保留 `charges` 逐笔记录，旧清单只有可覆盖的阶段费用时必须先核对历史账单，不能直接重试付费阶段。`produce <项目> ep03 --until mix`、`--from voice` 的断点续跑与哈希清单已在隔离示范项目的 `--test-mode` 验证。正式配音已有独立适配器，但必须显式允许付费且通过媒体守卫；真实服务端接口和账单尚未验收。正式音效新增不调用服务商的请求预留／结清日志；费用预检会阻断未结清请求，正式绑定也不会接受未结清的日志化素材。豆包 1.0 音效提交适配器尚未接入，`produce` 目前仍只绑定已选素材，不会自动付费生成。测试模式需同时具备临时目录标记和测试项目配置，不能仅靠修改真实项目配置开启。
正式清单的路径、哈希、逐笔费用和阶段依赖格式见 [媒体清单参考](.agents/skills/produce-audio/references/media-manifest.md)。`next` 对正式媒体也会按清单重新核验，不能仅凭阶段的 `done` 状态推进。
本地 FFmpeg 成片阶段已可通过 `produce <项目> ep03 --from render` 单独执行，但前序真实媒体阶段必须已有有效清单；标题卡默认需要项目内基线和透明 PNG。它只生成并校验本地视频，`production/_reports/` 的字幕抽帧仍待人工观看，完整真实长片和付费音画制作尚未验收。
测试模式的口播按段缓存静音 WAV：同一段内改两句仅重建该段，其余段复用；整集拼接、时间表、混音、字幕和分镜按依赖重检。这验证的是断点续跑逻辑，不是实际配音质量或费用。

任务队列可用 `./run.sh jobs plan <项目> --stage draft|audio|visual` 生成每集任务卡，
`./run.sh jobs claim <项目> --stage audio --session <会话ID>` 领取，
`./run.sh jobs done <项目> audio-ep03 --session <会话ID>` 验收交回。
`next` 汇总任务卡里的 `needs_you`。音频队列的 `jobs run <项目> --stage audio --test-mode`
目前只可在隔离示范项目串行运行；三集夹具测试覆盖中断续跑及输入未变时跳过。
`jobs run <项目> --stage draft` 已有全季初稿的串行 Codex 单卡入口：每集先核对方案确认、来源、计划和当前工作连续性，再由独立会话只新增本集 `draft_vN.md`；父进程保护已有版本及共享输入，检查集号、lint、quotes 后生成 Markdown 编辑包并交回卡片。输入不变时跳过已验收卡，处理中断时优先验收已写出的新版稿，不重复调用模型。任务卡全部完成**不代表**全季前情、揭示和人物时序已完成语义复核：须按实际稿件补录工作连续性后统一交付编辑包。该入口仅用模拟 Codex 子进程和隔离示范稿测试，尚未实跑真实写稿会话；共享文件哈希检查只能发现越界改动，不能自动恢复被覆盖的文件。
普通 `jobs run --stage audio` 已有串行的真实执行入口：每张卡以独立、短期的 Codex 会话规划 cue，再由父进程按当前确认、媒体守卫、费用上限及逐笔请求日志推进豆包配音；不付费的素材绑定、混音和字幕才交给第二个独立会话。Codex 子会话的环境变量不传豆包密钥，超时时清理整个子进程组；父进程在每一步后重新核对文件哈希与任务验收，遇到待决定、费用阻断、失败或未结清请求会停下或保留同一任务等待查询。共享音色表、人物表及任务卡按提示词要求只读，并在会话前后核对哈希；**这只能发现越界改动，不能恢复被改过的文件，也不构成真实 Codex 执行路径的操作系统级只读验收**。该入口目前只用模拟子进程和假豆包状态测试，**没有真实调用 Codex 或豆包做完整三集验收**；豆包 1.0 付费音效生成仍未接入，未选定音效的任务不会被伪报完成。正式书目使用前须先在隔离副本完成权限及端到端验收。

公开仓库只保留工作室通用代码、规则、文档和原创测试材料；本机书目制作成果不作为公开示例上传。

当前工作室版本为 `0.2.0-dev.1`。新书目在 `project.yaml` 记录 `studio_created_version` 与 `studio_version`；已有书目运行 `next` 时仅在版本变化后更新后者，保留原 YAML 其他内容，不推测旧书目的创建版本。`jobs plan` 也会先更新版本再生成输入哈希；单卡子会话改用 `next <项目> --read-only --json` 预检，不写项目根目录的进度文件。普通 `next` 仍照常写入进度。版本变动不代替稿件、媒体和人工确认的重新核验。
