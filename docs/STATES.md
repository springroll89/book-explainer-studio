# 状态模型

本文件定义工作流状态。`status.yaml` 由 `./run.sh status` 派生生成，顶部标记“自动生成，请勿手改”。

## 闸门状态

- `pending`：没有有效批准记录。
- `passed`：批准记录存在，且记录中的对象哈希仍与当前文件一致。
- `invalidated`：批准记录存在，但对象或依赖哈希已改变。
- `revoked`：用户撤回批准。

闸门包括 G1 拆书、G2 分集、G3 第一集风格、G4 每集定稿、AV1 第一集音画样片、RELEASE 发布合规。批准只能通过交互式 `./run.sh approve` 由用户完成。

## 稿件状态

`planned` → `outlined` → `drafted` → `reviewed` → `final_candidate` → `approved` → `ledgered`。

附加标记：`preview`、`preview_override`、`review_partial`、`needs_recheck`、`on_hold`、`awaiting_human`、`awaiting_season_review`。

- `awaiting_season_review`：全季文案先行模式的草稿，待整季交齐后统一人工修改。G2/G3/G4 真实状态显示在 `gates` / `deferred_gates`，不因此给文案加 `on_hold`。`season_drafts` 显示计划集数、已写集数、缺集与工作连续性是否齐全；未齐继续写稿，齐全后下一步为用户统一改稿。

- `preview`：依赖工作连续性或处于预览上限内。
- `preview_override`：用户明确越界授权后产生。
- `needs_recheck`：上游稿件、原文批次或账本发生变化。
- `on_hold`：超出当前闸门上限，暂停继续制作。

## 制作产物状态

声音条目使用 O13 状态链：`planned` → `asset_ready` → `placed` → `mixed` → `accepted`；锚点失效时标记 `stale`，尚未批准生成或等待第一集试听时标记 `on_hold`。第 2–8 集在第一集 AV1 通过前保持 `on_hold`。

`planned`（规划文件存在）→ `generated`（媒体存在）→ `anchors_ok`（锚点通过）→ `accepted`（对应 AV1 或项目批准存在）。`stale` 表示稿件或锚点已变化。

## 推导规则

状态命令扫描文件、哈希、审校报告和 `approvals/`，不把手写的闸门状态当作事实。用户备注和优先级应写入 `notes.yaml`。
