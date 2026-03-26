# Telegram Plan Trace 设计

## 目标

让手机端用户不只看到 ACP plan preview，而是能完整查看当前 live session 缓存的计划：

- `Bot Status` 提供 `Agent Plan` 入口。
- 用户可以分页浏览完整计划列表，而不是只看前几条 preview。
- 用户可以打开单条计划详情，查看完整内容、状态和优先级。

## 核心决策

- 计划缓存仍以 `AgentSession.plan_entries` 为唯一事实来源；Telegram 层不额外维护计划副本。
- 计划下钻入口只挂在 `Bot Status`，作用域只针对当前 live session。
- 列表页只负责分页和快速浏览；单条详情页负责承载完整计划内容，避免把状态页或列表页撑得过长。
- 交互保持和 `Tool Activity` 一致的返回路径：`Bot Status -> Agent Plan -> Plan Detail -> Agent Plan -> Bot Status`。

## 运行时形状

### ACP 层

- `AgentSession` 继续在收到 ACP `session_update = "plan"` 时更新 `plan_entries`。
- Telegram 侧不需要新的 ACP request；只消费已有缓存。

### Telegram 层

- `Bot Status` 在存在 `plan_entries` 时显示 `Agent Plan` 按钮。
- `Agent Plan` 列表页展示：
  - 当前 session id
  - 计划总项数
  - 分页后的计划项摘要
- 单条计划详情页展示：
  - 当前条目序号
  - status
  - priority
  - 完整 content

## 用户交互

1. 用户打开 `Bot Status`。
2. 如果当前 live session 已缓存 ACP plan，状态页显示 `Agent Plan`。
3. 用户进入后可分页查看计划列表。
4. 用户打开某条计划后，可查看完整内容，再返回列表或状态页。

## 非目标

- 不引入计划编辑、重排或确认机制。
- 不做跨 session 的计划历史归档。
- 不把计划视图变成新的待输入交互入口。
