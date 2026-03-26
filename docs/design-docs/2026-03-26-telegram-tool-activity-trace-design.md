# Telegram Tool Activity Trace 设计

## 目标

让手机端用户在不接触桌面终端的前提下，也能继续跟随 ACP tool call 的执行细节：

- `Bot Status` 不只显示最近工具摘要，还能继续下钻。
- 用户可以查看单条 tool activity 的状态、输入摘要、关联路径和内容类型。
- 如果工具开过 client terminal，用户可以直接看到最近终端输出尾部。
- 如果工具涉及 workspace 文件或当前 Git change，用户可以直接跳到既有只读预览。

## 核心决策

- ACP tool update 的归一化继续留在 `talk2agent/acp/tool_activity.py`；Telegram 层只消费稳定的摘要字段，不解析原始 ACP payload。
- 工具活动下钻入口只挂在 `Bot Status`，并且只针对当前 live session 最近缓存的 activity，不额外引入独立历史仓库。
- 终端能力保持只读；工具详情页只展示 terminal output 尾部和退出状态，不提供交互式 terminal UI。
- 文件和变更预览复用现有 `Workspace Files` / `Workspace Changes` 视图构建逻辑，不再单独实现一套工具产物预览系统。
- 从工具详情跳出的文件/变更预览必须保留 `Back to Tool Activity`，避免手机端在多层 inline 视图里迷路。

## 运行时形状

### ACP 层

- `ToolActivitySummary` 除原有标题、状态、kind 外，新增：
  - `input_summary`
  - `path_refs`
  - `paths`
  - `terminal_ids`
  - `content_types`
- `summarize_tool_update(...)` 负责从 ACP tool update 中提取：
  - 执行命令、搜索词、URL、目标路径等输入摘要
  - location / diff 里的路径引用
  - terminal 内容块里的 `terminalId`
  - content block 类型
- `AgentSession.read_terminal_output(terminal_id)` 通过 `BotClient.terminal_output(...)` 读取当前 session 绑定 terminal 的最新输出，用于 Telegram 详情页展示。

### Telegram 层

- `Bot Status` 保留最近工具摘要预览；当存在 recent activities 时，额外展示 `Tool Activity` 按钮。
- `Tool Activity` 列表页展示最近几条 activity 的状态、标题与关键信息摘要。
- 单条详情页展示：
  - title / status / kind
  - 输入摘要与结构化 detail
  - 关联路径与内容类型
  - terminal 输出尾部与 exit status
- 工具详情页会把 activity 里提取到的路径解析到当前 workspace：
  - 若该路径在 workspace 内且是文本文件，可打开文件预览
  - 若该路径正好出现在当前 Git status 中，可打开 change 预览

## 用户交互

1. 用户打开 `Bot Status`。
2. 如果 live session 存在 recent tool activities，状态页显示 `Tool Activity`。
3. 用户进入列表页后，可以逐条打开某个 activity。
4. 在详情页里，用户可以：
   - 查看工具输入和输出摘要
   - 查看 terminal 输出尾部
   - 打开关联文件
   - 打开当前 Git change
5. 从文件或变更预览返回时，界面先回到 tool activity 详情；再由详情返回列表或 `Bot Status`。

## 非目标

- 不提供完整的 terminal 交互、stdin 或长期日志归档。
- 不把 tool activity 做成跨 session、跨 provider 的长期审计记录。
- 不暴露未归一化的 ACP 原始 tool payload 给 Telegram 用户。
