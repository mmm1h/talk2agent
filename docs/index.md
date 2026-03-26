# 文档索引

这个目录是项目的记录系统。
请保持文档精炼、可链接、按主题收口，让读者可以先从窄入口开始，再按需深入。

## 推荐阅读顺序

1. [../AGENTS.md](../AGENTS.md)
2. [../ARCHITECTURE.md](../ARCHITECTURE.md)
3. 再进入下面最相关的子区域

## 文档分区

- [design-docs/index.md](design-docs/index.md)：跨多个模块的设计说明，以及已接受的解决方案形状
- [exec-plans/index.md](exec-plans/index.md)：实现计划、进行中的工作以及归档执行记录

## 约定

- 可长期保存的系统指引应放在 `AGENTS.md`、`ARCHITECTURE.md` 或 `docs/` 下的专题文档里。
- `AGENTS.md` 保持在 100 行以内；如果超过上限，应拆到更窄的专题文档，并从入口文档回链。
- 设计缘由放在 `docs/design-docs/`。
- 执行计划放在 `docs/exec-plans/`。
- 已完成计划归档到 `docs/exec-plans/completed/`。
- 每份文档都应收敛在自己的主题边界内；超出边界的内容应迁移到更合适的文档，而不是继续横向膨胀。
- 尽量链接代码和测试，而不是把大段代码直接复制进文档。
- 如果文档已经不再代表当前状态，但仍有历史价值，应归档，而不是继续留在主路径中。
