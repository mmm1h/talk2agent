# Telegram Workspace Runtime 设计

## 目标

让手机端用户在不离开 Bot 的情况下确认当前 workspace 这条运行时到底具备哪些本地能力，而不需要回到电脑端检查配置文件：

- 暴露 bot 侧 ACP client filesystem / terminal bridge。
- 暴露当前 workspace 配置的 MCP server 列表。
- 说明这些能力会随当前 workspace 一起切换，并作用于后续 session。

## 核心决策

- `Workspace Runtime` 只读取当前 `AppConfig` 与当前 runtime snapshot，不新增 ACP request。
- 该视图属于 runtime / workspace inspection，不承担任何写操作。
- `Bot Status` 直接提供入口；`Session Info` 也可以把它作为 inspection hub 的一个子入口，并保留 `Back to Session Info`。
- 不展示 MCP server secret value；只展示 transport、command/url 与 env/header 数量。

## 运行时形状

### 配置层

- 继续读取 `agent.workspaces[].mcp_servers[]`。

### Telegram 层

- `Bot Status` 新增 `Workspace Runtime` 按钮。
- `Session Info` 也新增 `Workspace Runtime` 入口。
- `Workspace Runtime` 视图展示：
  - workspace id / path
  - ACP client tools:
    - filesystem bridge
    - terminal bridge
  - MCP server 数量
  - 每个 MCP server 的：
    - name
    - transport
    - command + args 或 url
    - env/header 数量
  - 单个 MCP server 详情：
    - transport
    - command + args 或 url
    - env/header key 名称
    - 不显示 env/header 的 value

## 用户交互

1. 用户在 `Bot Status` 或 `Session Info` 打开 `Workspace Runtime`。
2. 检查当前 workspace 的本地桥接能力与 MCP server 挂载情况。
3. 如果需要，打开单个 MCP server 的详情页，确认 transport、command/url、args 与 key 名称，而不暴露 secret value。
4. 通过 `Back to Workspace Runtime` 返回运行时列表，再通过 `Back to Bot Status` 或 `Back to Session Info` 返回原视图。

## 非目标

- 不提供 MCP server 编辑能力。
- 不在 Telegram 里展示 env/header 的具体 secret value。
- 不为只读 inspection 隐式创建 session。
