# ACP Client Terminal 设计

## 目标

补齐 `talk2agent` 在 ACP 协议里的 client 侧终端能力，使依赖 `terminal/*` 的 Provider 在 Telegram Bot 场景下也能获得与桌面 ACP client 更接近的工作能力。

## 核心决策

- `AgentSession.initialize()` 必须显式广告 `clientCapabilities.terminal = true`，否则 Provider 不会使用这条协议路径。
- 所有 ACP client 终端请求都受当前 `workspace_dir` 约束；`cwd` 不能逃逸到 workspace 根目录之外。
- 终端生命周期归当前 Telegram 用户的 ACP session 所有；不同 session 之间不能复用同一个 terminal id。
- 终端输出只保留最近一段文本尾部，默认上限 `64 KiB`，用于匹配 ACP `terminal/output` 的拉取语义，而不是在 Bot 侧做完整日志归档。
- `AgentSession.close()` 必须回收所有 client terminal，避免 Provider 或 session 切换后遗留后台子进程。

## 运行时形状

### AgentSession

- 在启动 ACP 连接时传入：
  - `clientCapabilities.fs.readTextFile = true`
  - `clientCapabilities.fs.writeTextFile = true`
  - `clientCapabilities.terminal = true`

### BotClient

- `create_terminal(command, args, cwd, env, output_byte_limit, session_id)`：
  - 在当前 workspace 内启动受控子进程
  - 默认继承 bot 进程环境，并叠加 ACP 请求里的环境变量
  - `stdout` 与 `stderr` 合流，`stdin` 关闭
- `terminal_output(session_id, terminal_id)`：
  - 返回当前累计输出
  - 输出超过上限时仅保留尾部，并标记 `truncated = true`
  - 如果进程已退出，也会附带 exit status
- `wait_for_terminal_exit(session_id, terminal_id)`：
  - 等待进程退出
  - 确保后台输出读取完成后再返回 exit status
- `kill_terminal(session_id, terminal_id)`：
  - 终止当前终端对应进程
- `release_terminal(session_id, terminal_id)`：
  - 释放 terminal 句柄
  - 若进程仍在运行，先终止再回收

### 实现选择

- 当前 Windows 环境下，`asyncio.create_subprocess_exec(...)` 结合异步 pipe 会触发权限错误。
- 因此终端实现采用 `subprocess.Popen(...)` + 后台线程读取输出，再通过 `asyncio.to_thread(...)` 等待退出。
- 这是平台兼容性选择，不改变对 ACP 协议面的语义。

## 安全约束

- `cwd` 即使传入绝对路径，也必须位于当前 workspace 根目录内。
- 终端启动使用 `shell=False`，避免额外 shell 注入面。
- `output_byte_limit` 不能为负数。
- `terminal_id` 只在 bot 进程内保存，并绑定 `session_id` 校验。

## 非目标

- 不提供 Telegram 内联交互式终端 UI
- 不支持 stdin / TTY / shell 会话复用
- 不改变 `permissions.mode = auto_approve`
