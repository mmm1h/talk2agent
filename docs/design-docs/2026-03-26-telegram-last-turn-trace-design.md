# Telegram Last Turn Trace 设计

## 目标

让手机端用户在执行 `Retry Last Turn` / `Fork Last Turn` 之前，可以先检查 bot 当前缓存的 replay payload：

- 查看上一轮回放标题、来源 provider 和 workspace。
- 查看回放里实际保存的 prompt item 列表。
- 打开单条 prompt item，查看文本内容、资源 URI、MIME type 和 payload 大小。
- 查看因能力降级而伴随保存的 context item 引用。

## 核心决策

- `Last Turn` 只读取 `TelegramUiState` 已缓存的 `_ReplayTurn`，不新增 ACP request。
- 该视图面向“重放前检查”，不是新的编辑器；用户只能查看或直接触发既有 `Retry/Fork Last Turn`。
- 如果用户已切换 provider，但 workspace 未变，`Last Turn` 仍显示原始记录 provider，帮助用户确认这是一次跨 provider 重放。
- `Session Info` 可以把 `Last Turn` 作为 inspection hub 的一个子入口，并保留 `Back to Session Info`。

## 运行时形状

### Telegram 状态缓存

- `_ReplayTurn` 继续保存：
  - `provider`
  - `workspace_id`
  - `prompt_items`
  - `title_hint`
  - `saved_context_items`
- `Last Turn` 列表页展示：
  - recorded provider / workspace
  - prompt item 数量
  - saved context item 数量与预览
  - 分页后的 prompt item 摘要
- 单条 item 详情页展示：
  - kind
  - URI
  - MIME type
  - payload size
  - 文本或文本资源的内容

## 用户交互

1. 用户在 `Bot Status` 看到 `Last Turn`。
2. 打开后先查看回放总览，再按需打开某个 prompt item。
3. 用户可在该视图或 item 详情页直接执行 `Retry Last Turn` / `Fork Last Turn`。
4. 如果是从 `Session Info` 打开，则子视图支持 `Back to Session Info`；否则返回 `Bot Status`。

## 非目标

- 不提供 replay payload 编辑能力。
- 不把旧 turn 做成长久历史仓库。
- 不在这里直接执行新的 workspace file/change 预览跳转。
