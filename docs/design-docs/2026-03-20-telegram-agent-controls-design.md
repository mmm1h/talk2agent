# Telegram 按钮化 Agent 控制设计

## 目标

在不打破“单一活跃 Provider + Workspace、每用户一个 live session、隐藏 `/debug_status` 只读”这些现有不变量的前提下，把 Telegram 交互主入口从命令扩展为按钮，并补齐：

- `Switch Agent`
- `Switch Workspace`
- `New Session`
- `Session History`
- `Restart Agent`
- `Model / Mode`

## 核心决策

- Provider 切换仍然是全局行为，只允许管理员触发。
- Workspace 切换也是全局行为，只允许管理员触发，并且只能从配置白名单中选择。
- `Restart Agent` 语义是“关闭当前用户的 agent 进程并创建一个新的 session”。
- `Retry Last Turn` 语义是“在当前 Workspace 作用域内，重放上一条真正发给 ACP 的请求”；如果当前 live session 已失效，bot 会先自动拉起新的 session 再重放；如果用户刚切到另一个 agent，只要 workspace 没变，也可以直接把上一回合交给新 agent 再跑一遍。
- `Fork Last Turn` 语义是“在当前 Workspace 作用域内，先创建一个新的 session，再把上一条真正发给 ACP 的请求重放到新 session 中”；旧 session 仍通过本地 history 保留，用户在手机端即可直接分叉工作线；如果用户刚切到另一个 agent，只要 workspace 没变，也可以直接把上一回合分叉到新 agent 上。
- 当当前 Workspace 下存在可重放的上一轮请求时，`Switch Agent` 菜单会为每个目标 provider 暴露一键 `Retry on ...` / `Fork on ...` 入口；它们先执行全局 Provider 切换，再立即在目标 provider 上重试或分叉上一轮。
- 当当前 Workspace 下存在可重放的上一轮请求时，`Session History` 和管理员可见的 `Provider Sessions` 视图也会暴露一键 `Run+Retry` 入口；它们先接管目标 session，再立即把上一轮请求重放进这个 session。
- `Session History` 不只保留列表切换；手机端应能先打开单个 history entry 的详情页，检查 session id、cwd、创建/更新时间与当前附着状态，再决定是否切换或分叉。
- Session history 删除只删 `talk2agent` 本地记录，不尝试硬删除 Provider 侧原始 session。
- `Session History` 只显示“当前 Provider + 当前 Workspace + 当前 Telegram 用户 + 当前 bot 自己记录过”的会话。
- `Session History` 里的 `Run+Retry` 只在当前 Workspace 下存在可重放上一轮时显示；它不改变 replay 的作用域，只是压缩“切 session -> 再点 Retry Last Turn”的手机端操作。
- `Model / Mode` 选项全部来自 ACP 会话真实返回；不在 bot 里硬编码模型名或模式名。
- 如果当前用户还没有 live session，点击 `Model / Mode` 时允许 bot 先为该用户拉起一个 session，再展示真实选项，避免手机端必须先发一条占位消息。
- 由 `New Session`、`Restart Agent` 或 `Model / Mode` 首次拉起的本地 live session，也会立即写入本地 history，避免用户刚创建会话却在 `Session History` 里看不到它。
- `Model / Mode` 切换成功后，也应立刻刷新当前 session 的本地 history 时间戳，并同步刷新当前用户的 Telegram slash command 菜单，避免命令集随配置变化后仍停留在旧状态。
- 当当前 Workspace 下存在可重放的上一轮请求时，`Model / Mode` 视图也会为非当前选项暴露一键 `...+Retry` 入口；它先更新当前 session 的 model 或 mode，再立即重放上一轮请求。
- `Model / Mode` 视图里的每个选项还应支持单独打开详情页，查看 label、value、description、当前是否生效以及对应 config option，再决定是否切换；如果入口来自 `Bot Status`，详情页返回链也要保留到状态页。
- 如果用户在 `Session History` 里删除的是当前 `[current]` live session，bot 也要同步清理该 session 绑定的旧 callback token、待输入文本动作、agent command alias 和 media group 缓冲，并把该用户的 Telegram slash command 菜单刷新回当前 Provider 的默认发现结果；`Context Bundle` 与 `Bundle Chat` 保持不变。
- 如果某次普通文本、命令或附件回合因为 provider / session 异常而导致 bot 主动失效当前 live session，bot 也要同步清理该 session 绑定的旧 callback token、待输入文本动作、agent command alias 和 media group 缓冲，并把该用户的 Telegram slash command 菜单刷新回当前 Provider 的默认发现结果；`Context Bundle` 与 `Bundle Chat` 保持不变。
- 上述 session 异常失效后的失败消息要直接给出恢复入口：普通用户至少能一跳进入 `Retry Last Turn`、`Fork Last Turn`、`New Session`、`Session History`、`Model / Mode`；管理员还应额外得到 `Switch Agent` 和 `Switch Workspace`。
- Telegram 公开命令菜单只显示当前 agent 暴露的 slash commands；本地 bot 只保留隐藏 `/debug_status`。
- 流式输出从“编辑占位消息”迁移到 Telegram Draft API。

## 运行时形状

### Provider / Workspace 切换

`app.py` 在真正安装新 runtime 前先做 preflight：

1. 解析 Provider Profile
2. 检查命令是否可执行
3. 启动一个临时 `AgentSession`
4. 执行 ACP `initialize`
5. 执行 ACP `new_session`
6. 关闭临时会话

只有 preflight 成功后，才进入现有的“退休旧 Store -> 安装新 Store -> 持久化 Provider / Workspace -> 回滚或提交”事务。

### Session History

本地新增 `session_history.py`，把 history 作为独立索引文件管理：

- 键维度：`provider + telegram_user_id + session_id`
- 展示维度：`provider + cwd + telegram_user_id`
- 元数据：`title`、`cwd`、`created_at`、`updated_at`
- 只记录本 bot 看到并使用过的 session

`SessionStore` 继续只维护 live session，同时对外提供：

- `list_history()`
- `activate_history_session()`
- `delete_history()`
- `record_session_usage()`

### AgentSession

`acp/agent_session.py` 从“只会 new + prompt”扩展为会话控制边界：

- 懒启动 ACP 连接，但把 `initialize` 和 `new_session` 拆开
- 支持 `session/resume`、`session/load`、`session/list`
- 捕获 `initialize` 返回的 capability
- 捕获 `new/load/resume` 返回的 `configOptions` / `models` / `modes`
- 捕获 ACP `available_commands_update`
- 优先用 `session/set_config_option` 做 model / mode 切换
- 只有 agent 没有 `configOptions` 时，才回退到 legacy `set_session_model` / `set_session_mode`

## Telegram 交互

### 主入口

使用常驻 reply keyboard 承载一跳动作：

- `New Session`
- `Retry Last Turn`
- `Fork Last Turn`
- `Session History`
- `Model / Mode`
- `Restart Agent`
- `Switch Agent`（仅管理员）
- `Switch Workspace`（仅管理员）

### 选择流程

使用 inline button + callback query 承载多步选择：

- Provider 切换
- Provider 切换并立即 `Retry Last Turn` / `Fork Last Turn`
- Model / Mode 切换并立即 `Retry Last Turn`
- Workspace 切换
- Session history 分页 / 单项详情 / 运行 / 删除
- Model / Mode 选项列表 / 单项详情

callback data 不直接携带长 `session_id`；bot 进程内部用短 token 做临时映射。
Telegram 命令菜单通过 `setMyCommands` 按允许用户逐个同步，并在 bot 内维护 alias -> 原始 agent command 的映射。

## 边界与非目标

- 不做跨 Provider 的 history 聚合。
- 不做 Telegram 内任意路径浏览；workspace 只能来自配置。
- 不做 transcript replay 到 Telegram；history `Run` / `Run+Retry` 只切到对应 remote session 继续对话或在其上重放当前 Workspace 的上一轮请求。
- 不做 Provider 侧 session hard delete。
- 不把 Provider 命令映射泄漏回 Telegram handler。
