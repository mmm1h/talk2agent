# Telegram Turn Cancel 设计

## 目标

让手机端在 ACP 回合运行期间具备接近桌面端的“中断当前工作”能力：

- Bot 在后台持续执行当前 ACP turn，而不是阻塞整个 Telegram handler。
- `Bot Status` 显式展示当前 turn 是否运行中，并提供 `Stop Turn`。
- 点击 `Stop Turn` 后，通过 ACP `session/cancel` 请求当前 live session 停止本轮工作。

## 核心决策

- ACP `session/cancel` 只从 `talk2agent/acp/agent_session.py` 暴露为 `cancel_turn()`；Telegram 层不直接调用 ACP 连接对象。
- 后台执行只覆盖真正的 agent turn；纯只读菜单、状态页和本地视图仍保持原有同步处理。
- 同一 Telegram 用户在任意时刻只允许一个运行中的 turn；当已有后台 turn 时，新的文本、命令或附件 turn 会被拒绝，并提示去 `Bot Status` 停止或等待。
- `Stop Turn` 优先走 ACP `session/cancel`；如果 turn 还未拿到可取消的 session，或当前 session 没有暴露取消入口，再回退为取消本地后台 task。
- 取消后的空输出不再显示通用 `stop_reason=cancelled`，而是直接返回 `Turn cancelled.`。

## 运行时形状

### ACP 层

- `AgentSession.cancel_turn()`：
  - 不隐式创建 session。
  - 仅当连接和 `session_id` 已存在时发送 ACP `session/cancel`。
  - 返回布尔值，表示是否实际发出了 ACP cancel。

### Telegram UI 状态

- `TelegramUiState` 新增当前用户的 `_ActiveTurn`：
  - `provider`
  - `workspace_id`
  - `title_hint`
  - `task`
  - `session`
  - `stop_requested`
- 状态页只展示“当前运行时”对应的 active turn，避免旧运行时残留 turn 污染新状态页。

### Telegram 回合执行

- 当 `python-telegram-bot` `Application` 可用时，agent turn 会作为后台 task 启动。
- 后台 task 启动前，先登记 active turn；拿到 live session 后，再把 session 句柄绑定回 active turn，供 `Stop Turn` 使用。
- 背景 turn 结束后自动清理 active turn。

## 用户交互

- `Bot Status` 新增：
  - `Turn: idle`
  - `Turn: running (...)`
  - `Turn: stop requested (...)`
- 当当前运行时存在 active turn 时，状态页显示 `Stop Turn`。
- 点击 `Stop Turn` 后：
  - 状态页立即刷新为 `Stop requested for the current turn.`
  - 后台 turn 最终以已有输出或 `Turn cancelled.` 收尾

## 非目标

- 不引入多并发 turn 队列。
- 不改变 `SessionStore` 的单 live session 语义。
- 不把 Telegram 变成长期任务调度器；这里只解决当前 live turn 的停止控制。
