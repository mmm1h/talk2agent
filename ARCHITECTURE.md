# ARCHITECTURE.md

这份文档提供系统的顶层地图。
它描述领域边界与包分层，而不是逐行解释实现细节。

## 系统概览

`talk2agent` 是一个位于单一活跃 ACP Provider 运行时之前的 Telegram 轮询机器人。
已授权的 Telegram 用户发送纯文本、图片、语音、音频或文档提示词，机器人把请求路由到 Provider 支撑的 ACP 会话中，再通过 Telegram Draft API 把流式输出回传到 Telegram。

当前进程支持三个 Provider：

- `claude`
- `codex`
- `gemini`

同一时刻只能有一个 Provider 处于活跃状态。
同一时刻也只会有一个全局 Workspace 处于活跃状态。
配置中的管理员可以通过按钮式 `Switch Agent` 和 `Switch Workspace` 对整个进程做全局切换。

## 分层地图

| 分层 | 模块 | 职责 | 不应承担 |
| --- | --- | --- | --- |
| 入口层 | `talk2agent/__main__.py`、`talk2agent/cli.py` | 解析命令并启动应用 | Telegram 逻辑、ACP 协议细节 |
| 应用编排层 | `talk2agent/app.py` | 构建服务、解析启动 Provider / Workspace、切换运行时、优雅关闭 | 消息格式化、YAML 解析细节 |
| 传输适配层 | `talk2agent/bots/telegram_bot.py`、`talk2agent/bots/telegram_stream.py` | Telegram 命令、reply keyboard / inline button、鉴权检查、消息流式输出 | Provider 注册表、ACP 初始化 |
| 运行时策略层 | `talk2agent/config.py`、`talk2agent/provider_runtime.py` | 配置结构、校验、Provider Profile、Workspace 白名单、持久化运行时状态 | 会话归属、Telegram Handler |
| 会话领域层 | `talk2agent/session_store.py`、`talk2agent/session_history.py` | 按用户管理 live session、本地 session history、按 workspace 过滤、重置、失效、空闲清理、退休 | Telegram 专属行为、Provider 持久化 |
| 工作区只读视图层 | `talk2agent/workspace_files.py`、`talk2agent/workspace_git.py` | 在当前白名单 workspace 内做安全路径解析、目录列举、全文搜索、Git 变更读取和文本预览 | Telegram callback、Provider 路由 |
| 工作区附件入口层 | `talk2agent/workspace_inbox.py` | 将 Telegram 附件安全写入当前 workspace 的受控 inbox，供 agent 回退读取 | Telegram handler、Provider 选择逻辑 |
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
3. `app.py` 根据持久化状态或配置默认值解析启动 Provider 与 Workspace。
4. `app.py` 构建一个 `RuntimeState`，其中包含活跃 Provider、Workspace 及其 `SessionStore`。
5. `bots/telegram_bot.py` 把 Telegram Handlers 绑定到应用对象上。

### 用户消息回合

1. `telegram_bot.py` 先检查白名单授权。
2. Handler 对当前运行时状态做一次快照。
3. 活跃 `SessionStore` 会先清理空闲会话，再返回当前用户的会话。
4. `telegram_bot.py` 会把文本、图片、语音、音频、视频和文档输入归一化成 ACP prompt item；图片会进入 `image` block，语音/音频会进入 `audio` block，视频和其他二进制附件会进入 resource block 或受控 inbox 降级，文档则按 MIME 类型映射到 image/resource/audio block。
5. 如果 Telegram 连续投递同一 `media_group_id` 的多附件，`telegram_bot.py` 会先做短暂收敛，再把整组附件合并为一次 ACP 回合。
6. `acp/agent_session.py` 在需要时懒启动 ACP 子进程和 ACP 会话。
7. ACP 更新流被转发到 `TelegramTurnStream`。
8. `telegram_stream.py` 负责驱动 Telegram Draft 流，并在结束时发出最终普通消息。

### 按钮控制流程

1. `telegram_bot.py` 为白名单用户提供常驻 reply keyboard。
2. `Bot Status`、`Switch Agent`、`Switch Workspace`、`Session History`、`Agent Commands`、`Workspace Files`、`Workspace Search`、`Workspace Changes`、`Context Bundle`、`Model / Mode` 等需要选择的动作通过 inline button + callback query 完成。
3. callback token 只在 bot 进程内短期保存，避免把长 `session_id` 直接暴露到 Telegram callback data。
4. `Session History` 只展示当前 Provider + 当前 Workspace 下、当前 Telegram 用户在本 bot 中记录过的本地历史；当前活跃会话会带 `[current]` 标记，并提供 `Run`、`Rename`、`Delete`。
5. 管理员可以从 `Session History` 继续进入 `Provider Sessions`，浏览当前 workspace 下 provider 原生保存的 ACP sessions，并把其中某个 session 接管到自己当前的 Telegram 会话槽位。
6. `Rename` 与带参数的 `Agent Commands` 都采用两段式交互：按钮只负责进入待输入状态，下一条普通文本消息才真正提交标题或命令参数。
7. `Workspace Files`、`Workspace Search` 与 `Workspace Changes` 都只能在当前活跃 workspace 根目录内做只读浏览、搜索、Git 变更查看和文件预览，不能逃逸到根目录之外。
8. `Context Bundle` 只在当前 Provider + 当前 Workspace + 当前 Telegram 用户范围内累积文件和变更项，并通过两段式交互把 bundle 请求提交给当前 live session。
9. Telegram 命令菜单会按允许用户逐个同步为当前 agent 暴露的 slash commands；bot 自己只保留隐藏 `/debug_status`。
10. 一旦全局 `Switch Agent` 或 `Switch Workspace` 成功，旧 callback token、待输入文本动作、agent command alias、media group 缓冲和已开启的 bundle chat 都必须立刻失效，避免旧 Telegram 界面把请求落到新运行时。
11. 一旦当前 live session 被 `New Session`、`Restart Agent`、`Session History -> Run` 或 `Provider Sessions -> Run` 替换，旧 callback token、待输入文本动作、agent command alias 和 media group 缓冲都必须立刻失效；`Context Bundle` 与 bundle chat 保持不变，因为它们绑定的是 Provider + Workspace，而不是单个 session。

### Provider / Workspace 切换

1. 管理员执行 `Switch Agent` 或 `Switch Workspace`。
2. 在展示 `Switch Agent` 菜单时，`app.py` 会通过短生命周期 discovery session 读取各 provider 在当前 workspace 下的 prompt/session 能力摘要。
3. `app.py` 解析目标 Provider Profile 或 Workspace 配置。
4. `app.py` 先做 Provider 可执行文件和 ACP `new_session` 预检。
5. 预检通过后，`app.py` 构建一个绑定到目标 Provider + Workspace 的 `SessionStore`。
6. 在同一把锁下，旧 Store 被标记退休，新运行时被安装，新的 Provider / Workspace 会被持久化。
7. 如果持久化失败，`app.py` 会恢复旧运行时并重新激活旧 Store。
8. 切换成功后，退休 Store 里的旧会话会被尽力关闭。

## 领域边界

### Telegram 传输层

`talk2agent/bots/telegram_bot.py` 负责：

- 命令处理
- Agent slash command 菜单同步与别名映射
- Reply keyboard 与 inline button 菜单
- 当前运行时 `Bot Status` 总览、快捷入口，以及待输入取消、bundle chat 启停、新建/重试/分叉 session、model/mode、provider sessions 与运行时切换入口等高频状态控制
- `Switch Agent` 菜单中的 provider capability 摘要展示
- Telegram 图片/语音/音频/文档下载、media group 收敛、能力感知降级，以及到 ACP 结构化 prompt item 的桥接
- ACP command center 列表与按钮执行
- 当前 workspace 的只读目录浏览、全文搜索、Git 变更查看与文件预览
- 从文件预览页发起“下一条文本作为文件请求”的 agent 回合
- 从 Git diff 预览页发起“下一条文本作为变更请求”的 agent 回合
- 按当前 Provider + Workspace + 用户聚合的 context bundle 收集、分页、移除、清空与提交
- 历史会话重命名的短期待输入状态
- 带参数 agent command 的短期待输入状态
- workspace search 查询词的短期待输入状态
- workspace file request 查询词的短期待输入状态
- workspace change request 查询词的短期待输入状态
- context bundle request 查询词的短期待输入状态
- 白名单与管理员校验
- 当旧快照撞上退休 Store 时的单次重试逻辑
- 构建 `python-telegram-bot` 应用

`talk2agent/bots/telegram_stream.py` 负责：

- 将 ACP 更新渲染为纯文本
- 节流 Draft 更新频率
- Telegram Draft 流与最终文本切块投递

### Provider 运行时

`talk2agent/provider_runtime.py` 负责：

- 支持的 Provider 名称集合
- Provider 到命令的映射
- Provider / Workspace 状态文件的读写
- 启动 Provider 的解析逻辑

它必须保持为运行时可执行命令选择的唯一事实来源。

### 会话生命周期

`talk2agent/session_store.py` 负责：

- 每个 Telegram 用户唯一的会话实例
- 当前 Provider + Workspace 下、当前 Telegram 用户的本地 history 索引接入
- 把 provider 原生 session 接管为当前 Telegram 用户的 live session，并回写到本地 history
- 重置和失效语义
- 空闲会话清理
- Provider / Workspace 切换时的 Store 退休机制

它不应该了解 Telegram 命令名或 Provider 持久化细节。

### ACP 集成

`talk2agent/acp/agent_session.py` 负责：

- 启动 ACP 子进程
- 创建或复用单一 ACP 会话
- 读取并缓存 agent `promptCapabilities`，作为 Bot 多模态输入的能力边界
- `session/load` / `session/resume` / `session/set_config_option` 等 ACP 会话控制
- 把文本、图片、音频和资源类 prompt item 转成 ACP content block，并对可降级的文本类文档输入做能力感知降级
- 捕获 ACP `available_commands_update` 并缓存当前 agent 命令集合
- 通过生命周期锁串行化每一轮对话
- 只把更新转发给当前回合对应的输出 sink

`talk2agent/acp/bot_client.py` 和 `talk2agent/acp/permission.py` 为这层提供支撑，并应保持传输层无关。

### 工作区附件入口

`talk2agent/workspace_inbox.py` 负责：

- 将 Telegram 附件写入当前 workspace 下的 `.talk2agent/telegram-inbox/`
- 生成安全、不可逃逸的 inbox 路径
- 在 provider 不支持某类 ACP prompt block 时，为 bot 提供“落盘后让 agent 从本地读取”的降级基础

## 架构不变量

- Telegram 轮询是唯一传输方式。
- ACP 是唯一后端协议。
- 在当前 MVP 中，`permissions.mode` 保持为 `auto_approve`。
- 每个进程恰好只有一个活跃 Provider + Workspace 运行时。
- 在任意一个 Store 中，每个 Telegram 用户至多只有一个活会话。
- Session history 是当前 bot 的本地索引，不等于 Provider 侧全局 session 仓库，并按 Provider + Workspace 过滤。
- `/debug_status` 是观测命令，绝不能隐式创建新会话。
- `Switch Agent` / `Switch Workspace` 切换的是整个进程运行时，并会清空旧会话。
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
