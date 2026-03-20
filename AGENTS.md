# AGENTS.md

这个文件是一张地图，不是百科全书。
先读这里，再根据链接进入更窄、更具体的真实信息源。

## 这个仓库是什么

- `talk2agent` 是一个自托管的 Telegram 轮询机器人，会把已授权用户的纯文本消息转发给本地 ACP 兼容智能体运行时。
- 进程同一时刻只维护一个活跃 Provider：`claude`、`codex` 或 `gemini`。
- 每个 Telegram 用户会在当前 Provider 运行时中拥有一个长生命周期 ACP 会话。
- Provider 切换是全局的，仅管理员可用，并且会跨重启持久化。

## 从这里开始

1. 先读 [README.md](README.md)，了解安装、运行命令和运维预期。
2. 再读 [ARCHITECTURE.md](ARCHITECTURE.md)，了解顶层领域和包分层地图。
3. 然后读 [docs/index.md](docs/index.md)，查看结构化文档目录。
4. 最后只打开和当前任务最相关的窄文档或代码文件。

## 仓库地图

- `talk2agent/`：应用主包
- `tests/`：与主包结构对应的回归测试
- `docs/design-docs/`：跨多个模块的设计说明
- `docs/exec-plans/`：实现计划与归档的执行记录
- `README.md`：面向操作者的快速开始
- `ARCHITECTURE.md`：系统和包分层地图
- `AGENTS.md`：当前这份导航文件

## 代码地图

- `talk2agent/cli.py`：`init` 和 `start` 入口
- `talk2agent/config.py`：YAML 配置结构、默认值和校验
- `talk2agent/app.py`：服务装配、运行时快照、Provider 切换
- `talk2agent/provider_runtime.py`：Provider 注册表和持久化 Provider 状态
- `talk2agent/session_store.py`：按用户管理会话、失效、空闲清理和退休机制
- `talk2agent/acp/`：ACP 协议边界
- `talk2agent/bots/`：Telegram 传输边界

## 按任务阅读

- 启动或运维流程：`README.md` -> `talk2agent/config.py` -> `talk2agent/app.py`
- Provider 切换：`ARCHITECTURE.md` -> `talk2agent/provider_runtime.py` -> `talk2agent/app.py` -> `tests/test_app.py`
- Telegram 命令行为：`talk2agent/bots/telegram_bot.py` -> `talk2agent/bots/telegram_stream.py` -> `tests/test_telegram_bot.py`
- 会话生命周期：`talk2agent/session_store.py` -> `talk2agent/acp/agent_session.py` -> `tests/test_session_store.py`
- 历史设计背景：`docs/design-docs/index.md`
- 执行历史：`docs/exec-plans/index.md`

## 常见改动路径

- 新增或修改 Provider：`talk2agent/provider_runtime.py` -> `talk2agent/config.py` -> `README.md` -> 对应测试
- 修改运行时切换行为：`talk2agent/app.py` -> `talk2agent/session_store.py` -> `tests/test_app.py`
- 修改 Telegram 命令或回复：`talk2agent/bots/telegram_bot.py` -> `tests/test_telegram_bot.py`
- 修改流式文本行为：`talk2agent/bots/telegram_stream.py` -> `tests/test_telegram_stream.py`
- 修改 ACP 会话行为：`talk2agent/acp/agent_session.py` -> `tests/test_agent_session.py`
- 修改配置结构：`talk2agent/config.py` -> `README.md` -> `tests/test_config.py`

## 文档树

- [docs/index.md](docs/index.md)：项目文档总入口
- [docs/design-docs/index.md](docs/design-docs/index.md)：做结构性变更前应先读的设计文档
- [docs/exec-plans/index.md](docs/exec-plans/index.md)：活跃计划和已完成计划
- `docs/exec-plans/completed/`：归档的实现计划

## 工作规则

- 保持 `AGENTS.md` 简短。可长期保存的细节应放到 `docs/` 中，而不是这里。
- 当包边界或系统不变量发生变化时，更新 `ARCHITECTURE.md`。
- 当安装方式、命令或配置结构变化时，更新 `README.md`。
- 只有跨多个模块的改动才需要设计文档，小型局部修复不要额外立项。
- 新的执行计划放到 `docs/exec-plans/`，完成后归档到 `completed/`。
- 传输层关注点留在 `talk2agent/bots/`，ACP 协议关注点留在 `talk2agent/acp/`。
- Provider 查找和持久化 Provider 状态必须集中在 `talk2agent/provider_runtime.py`。
- 不要把临时配置、provider-state 文件、缓存或临时笔记长期留在仓库根目录。

## 要避免的漂移

- 不要把 Telegram 格式化或占位消息编辑逻辑混进 `talk2agent/acp/`。
- 不要让 Provider 命令选择逻辑泄漏到 bot handler 中。
- 不要在只读检查路径里创建会话，例如 `/status`。
- 已经有 `docs/` 承载位置时，不要再把设计说明或执行计划扔回仓库根目录。

## 系统不变量

- Telegram 轮询是唯一受支持的传输方式。
- ACP 是唯一受支持的后端协议。
- `permissions.mode` 固定为 `auto_approve`。
- `SessionStore` 为每个 Telegram 用户在当前活跃 Provider 运行时里维护一个活会话。
- `/status` 必须保持只读，不能创建新会话。
- `/provider` 切换的是整个运行时，而不是某个用户的单独会话。
- Provider 命令解析必须来自 `talk2agent/provider_runtime.py`，而不是手写运行命令字符串。
- Provider 切换中的持久化失败必须回滚运行时状态。

## 快速验证

- `python -m pytest -q`
- `python -m talk2agent init --config config.yaml`
- `python -m talk2agent start --config config.yaml`

## 当文档漂移时

- 先修最窄、最接近真实行为的那个信息源。
- 如果代码变了，要在同一轮同时更新测试和相关文档。
- 如果文档已经成为历史资料，应归档，而不是继续放在主路径上。
- 如果一个新文档无法从 `docs/index.md` 链接进去，它大概率就放错了地方。
