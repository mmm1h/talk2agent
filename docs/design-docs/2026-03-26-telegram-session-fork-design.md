# Telegram Session Fork 设计

## 目标

在不改变 Telegram 传输边界、不引入新的 provider 路由模型的前提下，把 ACP `session/fork` 暴露给 Bot 端，让用户在手机上直接从当前 live session、`Session History` 和 `Provider Sessions` 分叉出完整的新工作线，而不是只能依赖“重放上一轮请求”的近似分支。

## 核心决策

- `Fork Session` 与现有 `Fork Last Turn` 明确区分：
  - `Fork Session` 依赖 ACP `session/fork`，复制的是当前 provider 原生 session 状态。
  - `Fork Last Turn` 依赖本地 replay，请求形状保持不变，但不会复制 provider 内部上下文。
- 第一阶段先覆盖“当前 live session -> 新 forked live session”，随后扩展到 `Session History` 与 `Provider Sessions` 里的任意可见 session。
- fork 成功后，当前 Telegram 用户的 live session 槽位切到新 forked session；旧 session 仍留在本地 history 与 provider 原生 session 仓库中。
- fork 成功后，旧 inline buttons、待输入状态、agent command alias 和 media group 缓冲立刻失效；`Context Bundle` 与 bundle chat 保持不变，因为它们仍绑定 Provider + Workspace。
- `Bot Status` 和 provider capability 摘要都要显式展示当前 provider 是否支持 session fork，避免用户点击后才发现能力缺失。

## 运行时形状

### ACP 层

- `AgentSession` 读取并缓存 `session_capabilities.fork`。
- `AgentSession.fork_session(session_id)` 调用 ACP `session/fork`，并把返回的新 `session_id` 与 config/model/mode 状态装配进当前 `AgentSession` 实例。

### SessionStore

- `SessionStore.fork_live_session(user_id)`：
  - 读取当前 live session 的 `session_id`
  - 创建一个新的 `AgentSession`
  - 对源 `session_id` 执行 `session/fork`
  - 安装新的 forked session 为当前 live session
  - 回写本地 history
- `SessionStore.fork_history_session(user_id, session_id)`：
  - 校验 source session 属于当前 Provider + Workspace + Telegram 用户的本地 history
  - 创建一个新的 `AgentSession`
  - 对 source `session_id` 执行 `session/fork`
  - 安装新的 forked session 为当前 live session
  - 用 history title 回写本地 history
- `SessionStore.fork_provider_session(user_id, session_id, title_hint)`：
  - 对 provider 原生列出的 source `session_id` 执行 `session/fork`
  - 安装新的 forked session 为当前 live session
  - 用 provider 列表里的 title hint 回写本地 history

### Telegram 交互

- `Bot Status` 在当前 live session 已存在且 provider 支持 fork 时，显示 `Fork Session` 按钮。
- 点击后直接执行原生 session fork，并回到最新 `Bot Status`。
- 失败时恢复当前状态页，并显示 `Failed to fork session.`。
- `Session History` 在 provider 支持 fork 时，为每条记录显示 `Fork`；如果存在 replay turn，再额外显示 `Fork+Retry`。
- `Provider Sessions` 在 provider 支持 fork 时，为每条记录显示 `Fork`；如果存在 replay turn，再额外显示 `Fork+Retry`。

## 非目标

- 不改变 `Fork Last Turn` 的既有语义。
