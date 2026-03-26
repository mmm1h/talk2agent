# ACP Client Filesystem 设计

## 目标

补齐 `talk2agent` 在 ACP 协议里的 client 侧文件能力，使依赖 `fs/read_text_file` / `fs/write_text_file` 的 Provider 在 Telegram Bot 场景下也能获得与桌面 ACP client 更接近的工作能力。

## 核心决策

- `AgentSession.initialize()` 必须显式广告 client filesystem capabilities；只实现不广告没有意义。
- 当前文档只覆盖文本文件读写；`terminal/*` client methods 由独立文档单独定义。
- 所有 ACP client 文件请求都受当前 `workspace_dir` 约束，不能逃逸到 workspace 根目录之外。
- ACP client 文件能力与 Telegram 里的 `Workspace Files` 只读浏览是两套入口：
  - `Workspace Files` 是给手机用户看的 UI。
  - ACP client 文件接口是给 Provider runtime 走协议用的能力。

## 运行时形状

### AgentSession

- 在启动 ACP 连接时传入：
  - `clientCapabilities.fs.readTextFile = true`
  - `clientCapabilities.fs.writeTextFile = true`
- `terminal/*` 能力见 [2026-03-26-acp-client-terminal-design.md](2026-03-26-acp-client-terminal-design.md)

### BotClient

- `read_text_file(path, session_id, line, limit)`：
  - 只允许访问当前 workspace 内文件
  - 支持从指定行号开始、按行数限制读取
  - UTF-8 `errors="replace"` 解码
- `write_text_file(content, path, session_id)`：
  - 只允许写入当前 workspace 内文件
  - 自动创建父目录
  - 已存在目录路径会报错

### 安全约束

- 请求路径即使是绝对路径，也必须在当前 workspace 根目录内。
- 任何逃逸尝试都会直接报 `path escapes workspace root`。

## 非目标

- 不覆盖 ACP client `terminal/*`
- 不改变 `permissions.mode=auto_approve`
- 不新增 Telegram 终端 UI
