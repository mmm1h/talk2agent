# 文档索引

`docs/` 是项目的记录系统。
这里承接从顶层地图下沉出来的细节，并通过渐进式披露把读者引向更窄的真实信息源。

## 推荐阅读顺序

1. [../AGENTS.md](../AGENTS.md)
2. [../ARCHITECTURE.md](../ARCHITECTURE.md)
3. 再进入下面最相关的专题文档

## 常驻文档

- [operator-guide.md](operator-guide.md)：面向操作者的日常使用说明
- [manual-checklist.md](manual-checklist.md)：端到端手工验收清单
- [harness.md](harness.md)：自动化 harness、文档预算和 CI 入口

## 结构化目录

- [design-docs/index.md](design-docs/index.md)：跨多个模块的设计缘由和已接受的方案形状
- [exec-plans/index.md](exec-plans/index.md)：实现计划、进行中的工作和归档执行记录

## 文档边界约定

- `AGENTS.md` 是地图，不是手册。
- `ARCHITECTURE.md` 是系统地图，不是 UI 规格。
- `README.md` 是给人的快速开始，不是默认工程上下文。
- 设计缘由放在 `docs/design-docs/`。
- 执行计划放在 `docs/exec-plans/`，完成后归档到 `completed/`。
- 每份文档都应收敛在自己的主题边界内；超出边界的内容应迁移到更窄的文档。
