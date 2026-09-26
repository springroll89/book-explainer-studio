---
name: ingest-book
description: 在本书籍精讲项目导入用户指定的原文，或核对原文转换、分章、段落索引与导入版本时使用。
---

# ingest-book

读取 [原文入库与版本](../../../docs/WORKFLOW.md#原文入库与版本)。先确认具体原件，运行 `./run.sh ingest --help` 核对支持格式与参数。

1. 记录书名、作者、版本 / 译者与格式；必要转换只处理副本，保留原件。
2. 通过 `ingest` 建立不可变导入批次；后续记录绑定 `source_generation` 与段落 ID。
3. 抽查章界、开中结尾、编码和 OCR，报告提取质量与需处理的杂项。

只导入明确指定文件，不扫描导入整个图书库。文本不可靠时交付缺陷清单，不能凭模型记忆继续拆书。

原文语言与 `project.yaml` 的 `source.language` 不一致时停止入库。换版本使用 `./run.sh source migrate` 先生成报告，未经用户确认不切换。
