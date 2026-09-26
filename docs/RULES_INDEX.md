# 规则索引

| 主题 | 唯一定义 | 强制方式 |
|---|---|---|
| 阶段上限与守卫 | `config/defaults.yaml`、`bookflow/guard.py` | `./run.sh guard`；写稿技能要求先运行 |
| 状态模型 | `docs/STATES.md`、`bookflow/states.py` | `./run.sh status` 派生 |
| 人工批准 | `docs/GATE_CHECKLISTS.md`、`bookflow/approvals.py` | 交互式 `approve`，代理不能调用 |
| 工作连续性 | `bookflow/continuity.py`、`working_continuity.yaml` | `continuity context/check` |
| 正式账本 | `bookflow/ledger.py`、`series_ledger.yaml` | 哈希、审校和批准检查 |
| 句子 ID、锚点、时间 | `bookflow/sentences.py`、`bookflow/anchors.py` | `anchors check/migrate`、`timing import` |
| 外文译名与引文 | `analysis/quote_bank.yaml`、`analysis/names.yaml`、`production/pronunciation.yaml` | lint/quotes 和项目核查 |
| 画面节奏 | `config/defaults.yaml`、`bookflow/pacing.py` | `pacing budget/check/baseline` |
| 声音设计与混音 | `config/defaults.yaml`、`bookflow/sound.py`、`production/sound_cues.yaml`、[口播视频声音制作规范](SOUND_PRODUCTION.md) | `sound cues/estimate/check/assemble/mix/baseline`；默认每条素材一个版本；用户确认费用后才调用豆包；最终按真实“口播＋音效”时间轴 |
| 字幕生成与媒体验收 | `package-episode`、`review-episode` Skill、`timing import` | 最终稿简体文本和标点；ASR 只作词级时间/差异审计；音频或稿件变化后重建 SRT，并抽查渲染可见性 |
| 正式交付 | `docs/DELIVERY_CONDITIONS.md`、`release/compliance.yaml` | `export --deliver` |

说明书和各 Skill 只解释使用方式，发现冲突时以本索引指向的实现为准。

项目级读音表位于 `projects/<slug>/production/pronunciation.yaml`；集级目录可放覆盖项。

静态图节奏的唯一标准见 `visual-development` Skill 与 `docs/WORKFLOW.md`：5–7 秒为目标，15 秒为上限；13 分钟集数按 110–130 个镜头和至少 90–120 张独立图片规划。
