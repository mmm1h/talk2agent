# AGENTS.md

这个文件是仓库地图，不是操作手册，也不是设计文档。
默认入口给人和智能体都一样：先看边界，再看窄文档，再看代码。

## 这个仓库是什么

- `talk2agent` 是一个自托管的 Telegram 轮询机器人，把已授权用户的消息转发给本地 ACP 兼容智能体运行时。
- 进程同一时刻只维护一个活跃 Provider 和一个全局 Workspace。
- 每个 Telegram 用户会在当前 Provider + Workspace 运行时里拥有一个长生命周期会话。
- Provider / Workspace 切换是全局行为，仅管理员可用，并会跨重启持久化。

## 默认阅读顺序

1. 先读 [ARCHITECTURE.md](ARCHITECTURE.md)。
2. 再读 [docs/index.md](docs/index.md)。
3. 然后只打开和当前任务直接相关的窄文档或代码文件。
4. 只有在本地安装、启动或值守机器人时，才读 [README.md](README.md)。

## 文档边界

- `AGENTS.md`：仓库地图，控制在 100 行以内。
- `ARCHITECTURE.md`：顶层领域边界、依赖方向和系统不变量；不放按钮矩阵或验收清单。
- `README.md`：给人的快速开始；不作为智能体默认上下文。
- `docs/operator-guide.md`：面向操作者的日常使用说明。
- `docs/manual-checklist.md`：面向人的端到端手工验收清单。
- `docs/harness.md`：仓库 harness、文档约束和自动验证入口。
- `docs/design-docs/`：跨模块设计缘由与已接受方案。
- `docs/exec-plans/`：执行计划与归档记录。

## 仓库地图

- `talk2agent/`：应用主包。
- `talk2agent/bots/`：Telegram 传输边界。
- `talk2agent/acp/`：ACP 协议边界。
- `tests/`：与主包结构对应的回归测试。
- `docs/`：结构化文档与历史记录。

## 代码地图

- `talk2agent/cli.py`：`init`、`harness`、`start` 入口。
- `talk2agent/config.py`：YAML 配置结构、默认值和校验。
- `talk2agent/app.py`：服务装配、运行时快照、Provider / Workspace 切换。
- `talk2agent/provider_runtime.py`：Provider 注册表和持久化运行时状态。
- `talk2agent/session_store.py`：按用户管理会话、失效、空闲清理和退休机制。
- `talk2agent/workspace_inbox.py`：将 Telegram 附件安全落到当前 workspace 的受控 inbox。
- `talk2agent/harness.py`：仓库 harness 与文档约束检查。

## 按任务阅读

- 启动或运维：`docs/operator-guide.md` -> `talk2agent/config.py` -> `talk2agent/app.py`
- Harness / CI / 文档治理：`docs/harness.md` -> `talk2agent/harness.py` -> `tests/test_harness.py`
- Provider / Workspace 切换：`ARCHITECTURE.md` -> `talk2agent/provider_runtime.py` -> `talk2agent/app.py` -> `tests/test_app.py`
- Telegram 命令行为：`talk2agent/bots/telegram_bot.py` -> `talk2agent/bots/telegram_stream.py` -> `tests/test_telegram_bot.py`
- 会话生命周期：`talk2agent/session_store.py` -> `talk2agent/acp/agent_session.py` -> `tests/test_session_store.py`
- 历史设计背景：`docs/design-docs/index.md`
- 执行历史：`docs/exec-plans/index.md`

## 工作规则

- 先修最窄、最接近真实行为的信息源。
- 代码变更要在同一轮同步更新测试和相关文档。
- 每份文档都应守住自己的主题边界；超出边界的内容应迁回更合适的文档。
- 默认用 `python -m talk2agent harness` 做相关验证。
- 每次相关验证通过后，应把本轮变更以不夹带无关改动的方式同步到 GitHub。
- 不要把临时配置、provider-state、session history、缓存或临时笔记长期留在仓库根目录。

## 系统不变量

- Telegram 轮询是唯一受支持的传输方式。
- ACP 是唯一受支持的后端协议。
- `permissions.mode` 固定为 `auto_approve`。
- `SessionStore` 为每个 Telegram 用户在当前活跃 Provider + Workspace 运行时里维护一个活会话。
- `/debug_status` 必须保持只读，不能创建新会话。
- `Switch Agent` / `Switch Workspace` 切换的是整个运行时，而不是某个用户的单独会话。
- Provider 命令解析必须来自 `talk2agent/provider_runtime.py`。
- Provider / Workspace 切换中的持久化失败必须回滚运行时状态。
