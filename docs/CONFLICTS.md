# 实际仓库与优化需求的对应关系

| 需求文档判断 | 现状 | 处理方式 |
|---|---|---|
| 命令名是建议 | 仓库已有 `ledger`、`status`、`export` 等 | 保留旧命令，新增 `guard`、`approve`、`continuity`、`sentences`、`anchors`、`timing`、`pacing`、`source migrate` |
| 状态由脚本推导 | 旧版 `status.yaml` 含手写历史状态 | `status` 现在生成派生摘要；历史产物不删除 |
| 当前项目没有批准记录 | 实际确实没有有效 `approvals/` | G1/G2/G3/AV1/RELEASE 显示 pending；不把旧文字声明当批准 |
| 第一集已有样片 | 样片和媒体属于历史产物 | 本阶段只补流程与检查文件，第一集音频、字幕、画面留到下一阶段按新流程重做 |
| O12 默认竖屏示例 | 当前样片为 1920×1080 横屏 | 项目级 `visual_pacing.output` 沿用现有横屏，参数可覆盖 |
| O4 要求批准不可由代理执行 | Codex 运行环境无法模拟用户终端确认 | `approve/revoke/override` 在非交互输入下一律拒绝，交互确认留给用户 |
| O10 要求发布合规 | 当前没有用户填写的合规结论 | 创建空模板，正式交付时阻断，代理不代填判断 |
