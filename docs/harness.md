# Harness

这份文档定义仓库的自动化 harness。
目标是把验证、文档边界和反馈回路编码进仓库，而不是只靠口头约定。

实现参考 OpenAI 官方文章：
[Harness engineering](https://openai.com/zh-Hans-CN/index/harness-engineering/)

## 统一入口

```bash
python -m talk2agent harness
```

它会执行三类检查：

1. 文档约束检查
2. `pytest -q`
3. CLI 烟雾验证：`python -m talk2agent --help` 与 `python -m talk2agent init --config ...`

## 文档约束

当前 harness 会强制以下约束：

- `AGENTS.md` 不超过 100 行
- `ARCHITECTURE.md` 不超过 140 行
- `README.md` 不超过 120 行
- `docs/index.md` 和 `docs/design-docs/index.md` 保持为窄入口
- `AGENTS.md` 必须指向 `ARCHITECTURE.md` 和 `docs/index.md`
- `README.md` 和 `docs/index.md` 必须链接到更窄的专题文档
- `AGENTS.md` 不能把 `README.md` 当默认阅读入口
- `ARCHITECTURE.md` 不能承载 UI 级按钮细节

这些规则由 [talk2agent/harness.py](../talk2agent/harness.py) 执行。

## CI

GitHub Actions 会在 push 和 pull request 上运行相同入口：

- [.github/workflows/harness.yml](../.github/workflows/harness.yml)

## 什么时候更新 harness

- 顶层文档边界变化时
- 默认验证链变化时
- CI 入口变化时
- 需要把新的工程约束从“约定”升级为“自动检查”时
