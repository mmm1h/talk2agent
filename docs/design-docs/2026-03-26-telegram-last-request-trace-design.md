# Telegram Last Request Trace 设计

## 目标

让手机端用户在不离开 Bot 的情况下检查“最近一次可复用请求文本”的完整内容，而不必只看 `Bot Status` 中的一行摘要：

- 暴露完整请求文本，而不是截断后的 snippet。
- 标明该请求记录时所在的 provider / workspace。
- 标明该请求来自普通文本、Bundle Chat、Context Bundle，还是文件 / 变更跟进请求。

## 核心决策

- `Last Request` 只读取 `TelegramUiState` 已缓存的最近请求快照，不新增 ACP request。
- 请求快照继续按 workspace 作用域缓存；workspace 改变后旧记录自动失效。
- 该视图是只读 inspection，不直接发送请求；真正的复用动作仍由现有 `Ask With Last Request` 流程负责。
- `Session Info` 可以把 `Last Request` 作为 inspection hub 的一个子入口，并保留 `Back to Session Info`。

## 运行时形状

### Telegram Ui State

- `_LastRequestText` 从仅保存文本升级为同时保存：
  - `provider`
  - `workspace_id`
  - `text`
  - `source_summary`

### 请求来源摘要

- 普通文本：`plain text`
- Bundle Chat：`bundle chat (N items)`
- Context Bundle 请求：`context bundle request (N items)`
- 选中的 context items：`selected context request (...)`
- Workspace file / change 跟进请求：对应的文件或变更摘要

## Telegram 视图

- `Bot Status` 在最近请求存在时新增 `Last Request` 按钮。
- `Session Info` 在最近请求存在时也提供 `Last Request` 入口。
- `Last Request` 视图展示：
  - 记录时 provider
  - workspace id
  - source summary
  - 文本长度
  - 请求全文（必要时截断）

## 用户交互

1. 用户在 `Bot Status` 看到 `Last Request`。
2. 点击后查看最近一次可复用请求文本的完整内容与来源。
3. 若是从 `Session Info` 打开，则可通过 `Back to Session Info` 回到 inspection hub；否则回到 `Bot Status`。

## 非目标

- 不提供请求编辑器。
- 不为只读 inspection 隐式创建 session。
- 不改变现有 `Ask With Last Request` 的执行语义。
