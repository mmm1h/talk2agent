# ARCHITECTURE.md

这份文档只描述系统地图、领域边界和关键不变量。
它不承担操作者说明、按钮清单、手工验收步骤或逐功能设计细节。

## 系统概览

`talk2agent` 是一个位于单一活跃 ACP Provider 运行时之前的 Telegram 轮询机器人。
已授权用户通过 Telegram 发送文本或附件，bot 将其路由到当前 Provider + Workspace 上下文中的 ACP 会话，再把流式结果回传到 Telegram。

当前进程支持三个 Provider：

- `claude`
- `codex`
- `gemini`

同一时刻只有一个活跃 Provider。
同一时刻也只有一个活跃 Workspace。
管理员可以执行全局 `Switch Agent` 和 `Switch Workspace`，并把结果跨重启持久化。

## 分层地图

| 分层 | 模块 | 职责 | 不应承担 |
| --- | --- | --- | --- |
| 入口层 | `talk2agent/__main__.py`、`talk2agent/cli.py` | 解析命令并启动应用 | Telegram 逻辑、ACP 协议细节 |
| 应用编排层 | `talk2agent/app.py` | 构建服务、切换运行时、优雅关闭 | Telegram 格式化、配置解析细节 |
| 传输适配层 | `talk2agent/bots/telegram_bot.py`、`talk2agent/bots/telegram_stream.py` | Telegram Handler、鉴权、流式消息输出 | Provider 注册表、ACP 初始化细节 |
| 运行时策略层 | `talk2agent/config.py`、`talk2agent/provider_runtime.py` | 配置结构、Provider Profile、Workspace 白名单、运行时持久化 | Telegram 回调逻辑、会话归属 |
| 会话领域层 | `talk2agent/session_store.py`、`talk2agent/session_history.py` | live session、本地 history、退休与失效语义 | Telegram 专属行为、Provider 持久化 |
| 工作区视图层 | `talk2agent/workspace_files.py`、`talk2agent/workspace_git.py`、`talk2agent/workspace_inbox.py` | 只读浏览、搜索、Git 变更读取、附件安全落盘 | Telegram callback、Provider 选择逻辑 |
| ACP 边界层 | `talk2agent/acp/` | ACP 子进程生命周期、会话控制、prompt 转换、client bridge | Telegram API 细节、全局切换策略 |

## 依赖方向

期望依赖方向：

`cli/config` -> `app` -> `bots` 与 `session/domain` -> `acp`

补充约束：

- `talk2agent/bots/` 保持为薄适配层。
- `talk2agent/acp/` 是唯一了解 ACP 协议细节的区域。
- Provider 命令解析和持久化 Provider 状态必须集中在 `talk2agent/provider_runtime.py`。
- `tests/` 应镜像这些包边界。

## 核心流程

### 启动

1. `cli.py` 解析 `init`、`harness` 或 `start`。
2. `config.py` 加载并校验 YAML 配置。
3. `app.py` 解析持久化运行时状态，构建当前 Provider + Workspace 的 `RuntimeState`。
4. `telegram_bot.py` 将 Telegram Handlers 绑定到应用对象上。

### 用户回合

1. `telegram_bot.py` 做鉴权并抓取一次运行时快照。
2. `SessionStore` 返回当前用户在当前运行时中的 live session。
3. 传输层把文本和附件归一化为 ACP prompt item，必要时写入 workspace inbox。
4. `acp/agent_session.py` 负责会话控制、prompt 分发和更新流消费。
5. `telegram_stream.py` 把更新折叠为适合 Telegram 的流式输出。

### 全局切换

1. 管理员选择目标 Provider 或 Workspace。
2. `app.py` 执行 discovery / 预检，验证目标运行时可启动。
3. 在同一把锁下安装新运行时并持久化状态。
4. 如果持久化失败，则回滚到旧运行时。
5. 切换成功后，旧 `SessionStore` 进入退休并尽力关闭旧会话。

## 架构不变量

- Telegram 轮询是唯一传输方式。
- ACP 是唯一后端协议。
- 在当前 MVP 中，`permissions.mode` 保持为 `auto_approve`。
- 每个进程恰好只有一个活跃 Provider + Workspace 运行时。
- 每个 `SessionStore` 中，每个 Telegram 用户至多只有一个活会话。
- Session history 是本 bot 的本地索引，不等于 Provider 侧全局 session 仓库。
- `/debug_status` 是只读观测入口，绝不能隐式创建新会话。
- `Switch Agent` / `Switch Workspace` 切换的是整个进程运行时。
- Provider / Workspace 切换中的持久化失败必须回滚运行时状态。

## 什么不应放在这里

- Reply keyboard 或 inline button 的枚举。
- 手机端视图层的逐页文案和返回链。
- 手工验收 checklist。
- 逐功能设计缘由与边界讨论。

## 相关文档

- [AGENTS.md](AGENTS.md)：仓库导航地图
- [README.md](README.md)：给人的快速开始
- [docs/index.md](docs/index.md)：文档总入口
- [docs/operator-guide.md](docs/operator-guide.md)：操作者说明
- [docs/manual-checklist.md](docs/manual-checklist.md)：手工验收清单
- [docs/design-docs/index.md](docs/design-docs/index.md)：设计缘由
- [docs/exec-plans/index.md](docs/exec-plans/index.md)：执行记录
