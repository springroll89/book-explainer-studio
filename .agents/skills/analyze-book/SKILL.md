---
name: analyze-book
description: 在本书籍精讲项目原文已入库后，逐章拆书、汇总作者论证与叙事手法，并进行查漏审计时使用。
---

# analyze-book

读取 [拆书与覆盖审计](../../../docs/WORKFLOW.md#拆书与覆盖审计) 和当前原文索引。本阶段不写口播。

剧情类先读 [剧情文字总纲](../../../style/narrative_charter.md)。记录场景、信息揭示、冲突与可行选择如何变化，以及有依据的环境压力；全剧透因果梳理供内部核对，讲述方向应保留读者逐步发现的过程，不预设道德结论再挑情节证明。

1. 按 [chapter-reader](../../../roles/chapter-reader.md) 分章读取，给事实及第 2、3 层素材附原文依据。
2. 汇总简报、线索、人物 / 概念与误读风险。依据不足的作者意图写成待核解释。`analysis/characters.yaml` 里有对白的人物须写 `gender`、`age_group`（儿童／青年／中年／老年，按出声时）、`personality` 和一句 `summary`，供选音色单使用；依据不足写“未知”并注明。
3. 运行 `./run.sh coverage <项目路径>`；按 [coverage-auditor](../../../roles/coverage-auditor.md) 逐项复核候选，补分析并重跑。

机器报告 `coverage_machine.json` 与语义判断 `coverage_review.yaml` 分开保存。按工作模式交付可审简报与未决项；不把机器清零当完整阅读。

分析完成后保留原文批次指纹；外文项目同步维护 `analysis/quote_bank.yaml` 和译名表。运行 `./run.sh next <project>` 和阶段 `check`，再准备方案确认材料。
