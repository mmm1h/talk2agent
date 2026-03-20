# ARCHITECTURE.md

这份文档提供系统的顶层地图。
它描述领域边界与包分层，而不是逐行解释实现细节。

## 系统概览

`talk2agent` 是一个位于单一活跃 ACP Provider 运行时之前的 Telegram 轮询机器人。
已授权的 Telegram 用户发送纯文本提示词，机器人把请求路由到 Provider 支撑的 ACP 会话中，再通过编辑占位消息把流式输出回传到 Telegram。

当前进程支持三个 Provider：

- `claude`
- `codex`
- `gemini`

同一时刻只能有一个 Provider 处于活跃状态。
配置中的管理员可以通过 `/provider` 对整个进程做全局切换。

## 分层地图

| 分层 | 模块 | 职责 | 不应承担 |
| --- | --- | --- | --- |
| 入口层 | `talk2agent/__main__.py`、`talk2agent/cli.py` | 解析命令并启动应用 | Telegram 逻辑、ACP 协议细节 |
| 应用编排层 | `talk2agent/app.py` | 构建服务、解析启动 Provider、切换运行时、优雅关闭 | 消息格式化、YAML 解析细节 |
| 传输适配层 | `talk2agent/bots/telegram_bot.py`、`talk2agent/bots/telegram_stream.py` | Telegram 命令、鉴权检查、消息流式输出 | Provider 注册表、ACP 初始化 |
| 运行时策略层 | `talk2agent/config.py`、`talk2agent/provider_runtime.py` | 配置结构、校验、Provider Profile、持久化 Provider 状态 | 会话归属、Telegram Handler |
| 会话领域层 | `talk2agent/session_store.py` | 按用户管理会话、重置、失效、空闲清理、退休 | Telegram 专属行为、Provider 持久化 |
| ACP 边界层 | `talk2agent/acp/agent_session.py`、`talk2agent/acp/bot_client.py`、`talk2agent/acp/permission.py` | ACP 子进程生命周期、会话创建、Prompt 分发、权限策略 | Telegram API 细节、Provider 切换策略 |

## 包依赖方向

期望的依赖方向是：

`cli/config` -> `app` -> `bots` 与 `session/domain` -> `acp`

额外的辅助依赖路径：

- `app` 依赖 `provider_runtime.py` 来解析当前活跃 Provider。
- `bots` 依赖 `AppServices`、`SessionStore` 和 `AgentSession` 的公开行为。
- `tests/` 应该镜像这些模块边界，并与之保持一致。

`talk2agent/bots/` 应保持为薄适配层。
`talk2agent/acp/` 应继续作为唯一了解 ACP 协议细节的区域。

## 主要运行流程

### 启动流程

1. `cli.py` 解析 `init` 或 `start`。
2. `config.py` 加载并校验 YAML 配置。
3. `app.py` 根据持久化状态或配置默认值解析启动 Provider。
4. `app.py` 构建一个 `RuntimeState`，其中包含活跃 Provider 及其 `SessionStore`。
5. `bots/telegram_bot.py` 把 Telegram Handlers 绑定到应用对象上。

### 用户消息回合

1. `telegram_bot.py` 先检查白名单授权。
2. Handler 对当前运行时状态做一次快照。
3. 活跃 `SessionStore` 会先清理空闲会话，再返回当前用户的会话。
4. `acp/agent_session.py` 在需要时懒启动 ACP 子进程和 ACP 会话。
5. ACP 更新流被转发到 `TelegramTurnStream`。
6. `telegram_stream.py` 负责编辑 Telegram 占位消息，并在超长时发出后续分块消息。

### Provider 切换

1. 管理员执行 `/provider <name>`。
2. `app.py` 解析目标 Provider 的 Profile。
3. `app.py` 构建一个绑定到新 Provider 的 `SessionStore`。
4. 在同一把锁下，旧 Store 被标记退休，新运行时被安装，新的 Provider 会被持久化。
5. 如果持久化失败，`app.py` 会恢复旧运行时并重新激活旧 Store。
6. 切换成功后，退休 Store 里的旧会话会被尽力关闭。

## 领域边界

### Telegram 传输层

`talk2agent/bots/telegram_bot.py` 负责：

- 命令处理
- 白名单与管理员校验
- 当旧快照撞上退休 Store 时的单次重试逻辑
- 构建 `python-telegram-bot` 应用

`talk2agent/bots/telegram_stream.py` 负责：

- 将 ACP 更新渲染为纯文本
- 节流消息编辑频率
- Telegram 文本切块与超长续发

### Provider 运行时

`talk2agent/provider_runtime.py` 负责：

- 支持的 Provider 名称集合
- Provider 到命令的映射
- Provider 状态文件的读写
- 启动 Provider 的解析逻辑

它必须保持为运行时可执行命令选择的唯一事实来源。

### 会话生命周期

`talk2agent/session_store.py` 负责：

- 每个 Telegram 用户唯一的会话实例
- 重置和失效语义
- 空闲会话清理
- Provider 切换时的 Store 退休机制

它不应该了解 Telegram 命令名或 Provider 持久化细节。

### ACP 集成

`talk2agent/acp/agent_session.py` 负责：

- 启动 ACP 子进程
- 创建或复用单一 ACP 会话
- 通过生命周期锁串行化每一轮对话
- 只把更新转发给当前回合对应的输出 sink

`talk2agent/acp/bot_client.py` 和 `talk2agent/acp/permission.py` 为这层提供支撑，并应保持传输层无关。

## 架构不变量

- Telegram 轮询是唯一传输方式。
- ACP 是唯一后端协议。
- 在当前 MVP 中，`permissions.mode` 保持为 `auto_approve`。
- 每个进程恰好只有一个活跃 Provider 运行时。
- 在任意一个 Store 中，每个 Telegram 用户至多只有一个活会话。
- `/status` 是观测命令，绝不能隐式创建新会话。
- `/provider` 切换的是整个进程运行时，并会清空旧 Provider 会话。
- Provider 持久化是对配置的补充，而不是改写主 YAML 文件。

## 变更入口图

当你新增 Provider 时：

- 更新 `talk2agent/provider_runtime.py`
- 如有必要，更新配置校验和默认值
- 更新 `README.md`
- 更新 Provider 相关测试

当你调整 Telegram 交互时：

- 从 `talk2agent/bots/` 开始
- 确认没有把 ACP 或 Provider 策略泄漏进传输层

当你调整会话语义时：

- 从 `talk2agent/session_store.py` 开始
- 确认 `talk2agent/app.py` 和 `talk2agent/bots/telegram_bot.py` 仍然正确处理退休 Store 行为

## 相关文档

- [AGENTS.md](AGENTS.md)：仓库导航地图
- [docs/index.md](docs/index.md)：项目文档总入口
- [docs/design-docs/index.md](docs/design-docs/index.md)：设计背景
- [docs/exec-plans/index.md](docs/exec-plans/index.md)：实现历史
