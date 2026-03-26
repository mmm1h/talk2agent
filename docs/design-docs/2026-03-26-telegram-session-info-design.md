# Telegram Session Info 设计

## 目标

让手机端用户可以在不离开 Bot 的情况下查看当前 live ACP session 的完整状态快照，而不需要把所有信息都挤在 `Bot Status` 主视图里：

- 暴露当前 session id、标题和最近更新时间。
- 展示当前 model / mode 选择与可选项规模。
- 展示 prompt / session capability 矩阵。
- 汇总 usage、命令缓存、plan 缓存和 tool activity 缓存数量。
- 当 usage 已缓存时，允许继续下钻到独立 `Usage` 视图查看更完整的窗口占用细节。

## 核心决策

- `Session Info` 只读取当前 `AgentSession` 已缓存的数据，不新增 ACP request。
- 入口挂在 `Bot Status`，并保持只读；这个视图不承担任何待输入或变更动作。
- 如果当前还没有 live session，也允许打开该视图，但只提示“首个请求时会创建 session”，不能为查看详情而隐式启动 session。
- 该视图可以继续跳到 `Workspace Runtime`、`Usage`、`Last Request`、`Agent Commands`、`Agent Plan`、`Tool Activity`、`Last Turn`，作为手机端的会话 inspection hub；这些子视图要保留 `Back to Session Info`。

## 运行时形状

### ACP 层

- 继续使用 `AgentSession` 已有缓存字段：
  - `session_id`
  - `session_title`
  - `session_updated_at`
  - `capabilities`
  - `plan_entries`
  - `usage`
  - `available_commands`
  - `recent_tool_activities`

### Telegram 层

- `Bot Status` 新增 `Session Info` 按钮。
- `Session Info` 视图展示：
  - session id / title / updated_at
  - model / mode 当前值和 choice 数量
  - prompt capabilities
  - session capabilities
  - usage 快照
  - cached commands / plan / tool activities 数量
- 如果对应缓存非空，还提供直达：
  - `Workspace Runtime`
  - `Usage`
  - `Last Request`
  - `Agent Commands`
  - `Agent Plan`
  - `Tool Activity`
  - `Last Turn`

## 用户交互

1. 用户打开 `Bot Status`。
2. 点击 `Session Info` 查看当前 live session 的完整运行时快照。
3. 若需要进一步追踪，可从这里继续进入 workspace runtime、usage、上一条请求、命令、计划、工具活动或上一轮回放视图，并再返回 `Session Info`。
4. 用户随时可以 `Back to Bot Status` 返回主状态页。

## 非目标

- 不提供 session 编辑能力。
- 不为只读 inspection 视图隐式创建新的 session。
- 不做跨 session 的状态聚合。
