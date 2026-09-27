# 历史文档归档

本目录用于保留已被当前代码、README、工作流或设计说明取代的历史材料。归档内容可用于追溯，不作为当前操作指令或配置依据。

| 原文件 | 当前去向 / 处理原因 |
|---|---|
| [`CONFLICTS.md`](CONFLICTS.md) | 当时需求与仓库差异记录；现状以代码、README 和工作流为准。 |
| [`DESIGN_REFERENCE_v1.1.md`](DESIGN_REFERENCE_v1.1.md) | 早期完整设计稿；当前架构取舍见 [`../DESIGN.md`](../DESIGN.md)，阶段状态见 [`../WORKFLOW.md`](../WORKFLOW.md)。 |
| [`IMPLEMENTATION.md`](IMPLEMENTATION.md) | 首版实现摘要；现行取舍已归并到 [`../DESIGN.md`](../DESIGN.md)。 |
| `VALIDATION.md`、`validation-tests.txt` | 已移除：记录含过期测试计数和旧示范项目路径；当前验证命令与边界见 [README 验证章节](../../README.md#验证)，运行结果以当次输出为准。 |
| `STATES.md` | 已移除：旧阶段与旧批准模型由 [`../WORKFLOW.md`](../WORKFLOW.md)、[`../../bookflow/flow.py`](../../bookflow/flow.py) 和 [`../../bookflow/approvals.py`](../../bookflow/approvals.py) 取代。 |
| `DELIVERY_CONDITIONS.md` | 已移除：交付与归档检查以 [`../../bookflow/exporting.py`](../../bookflow/exporting.py) 和 [`../../bookflow/archive.py`](../../bookflow/archive.py) 为准。 |
| `HUMAN_EDITING.md`、`NAME_CONSISTENCY.md` | 合并到 [改稿导入与反馈 Skill](../../.agents/skills/import-feedback/SKILL.md) 及其[名称同步参考](../../.agents/skills/import-feedback/references/name-consistency.md)，避免重复维护操作说明。 |
| `SOUND_PRODUCTION.md` | 迁入 [produce-audio](../../.agents/skills/produce-audio/SKILL.md) 与其[声音制作参考](../../.agents/skills/produce-audio/references/production-guide.md)，并按当前未验收的服务状态更新。 |
| `MEDIA_MANIFEST.md` | 移至 [媒体清单参考](../../.agents/skills/produce-audio/references/media-manifest.md)，保留正式清单字段与核验约定。 |
| `CONTEXT_MANAGEMENT.md` | 删除重复续接说明：状态由 `next` 从项目文件推导，最小读取范围由根目录 [`../../AGENTS.md`](../../AGENTS.md) 与阶段 Skills 规定。 |
| `GATE_CHECKLISTS.md` | 迁入[确认材料 Skill](../../.agents/skills/prepare-confirmation/SKILL.md)及其[分闸门审阅清单](../../.agents/skills/prepare-confirmation/references/review-checklists.md)。 |

用户要求独立交付的 [书籍精讲工作室说明书](../书籍精讲工作室说明书.md) 保留在原位置，不作为历史归档处理。
