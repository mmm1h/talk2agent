# Workspace MCP Servers 设计

## 目标

让 `talk2agent` 在 Telegram Bot 场景下也能像桌面 ACP client 一样，把 workspace 绑定的 MCP servers 传给本地 Codex、Gemini、Claude Code。

## 核心决策

- MCP server 配置作用域是 `workspace`，而不是全局 provider 或单个 Telegram 用户。
- 所有通过该 workspace 创建、恢复、分叉或预检的 ACP session，都必须携带同一组 MCP servers。
- 配置入口放在 `agent.workspaces[].mcp_servers[]`，这样 workspace 切换时会连同 MCP 工具面一起切换。
- 首轮同时支持三种 ACP MCP transport：
  - `stdio`
  - `http`
  - `sse`
- `env` 与 `headers` 同时接受两种 YAML 形状：
  - 映射：更适合手写配置
  - `name/value` 列表：更贴近 ACP schema

## 运行时形状

### 配置层

- `WorkspaceConfig.mcp_servers` 保存当前 workspace 的 MCP server 列表。
- 每个 MCP server 至少包含：
  - `name`
  - `transport`
- `stdio` 额外要求：
  - `command`
  - 可选 `args`
  - 可选 `env`
- `http` / `sse` 额外要求：
  - `url`
  - 可选 `headers`

### 应用编排层

- `app._build_agent_session(...)` 会根据 `workspace_dir` 找回对应 `WorkspaceConfig`。
- 当前 workspace 的 MCP server 配置会被转换成 ACP schema 对象，并传给 `AgentSession(..., mcp_servers=...)`。
- 下列路径都共享同一转换结果：
  - 启动 live session
  - runtime preflight
  - provider capability discovery
  - provider session list / resume / fork
  - workspace / provider 切换后的新 `SessionStore`

### ACP 边界层

- `AgentSession` 已经支持把 `mcp_servers` 传给：
  - `new_session`
  - `load_session`
  - `resume_session`
  - `fork_session`
- 这次改动不改变 ACP 协议面，只是把原本悬空的配置链路接通。

## 安全与运维约束

- MCP server 切换跟随 workspace 切换，是全局运行时行为。
- 当前实现不提供 Telegram 侧动态增删 MCP server 的入口；运维入口仍然是 YAML 配置。
- MCP server 的密钥如果放在 `env` 或 `headers` 里，会进入本地配置文件；这属于操作者自管范畴。

## 非目标

- 不做 Telegram 内联 MCP 管理 UI
- 不做按用户差异化的 MCP server 视图
- 不做运行中热更新单个 MCP server 配置
