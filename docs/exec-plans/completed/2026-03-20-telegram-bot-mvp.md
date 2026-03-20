# Talk2Agent MVP（Telegram + Claude ACP）实施计划

**目标：** 构建一个自托管 Telegram 机器人，让小范围白名单用户可以通过 Telegram 驱动本地 Claude ACP 智能体；每个 Telegram 用户保留一个长生命周期 ACP 会话，并支持以源码方式本地安装。

**架构：** CLI 启动一个轮询模式的 `python-telegram-bot` 应用，并为每个允许的 Telegram 用户按需创建 `AgentSession`。每个 `AgentSession` 持有一个长生命周期 ACP 子进程、一个 ACP 会话、一个 prompt 锁，以及一个按回合存在的临时输出 sink。这样既能让 ACP client 只绑定一次，又能让 Telegram 流式输出保持消息级隔离。MVP 有意把权限、智能体选择和打包范围收窄，好让后续增加 Codex/Gemini、Telegram 审批按钮和 PyPI 发布时无需推翻会话核心。

**技术栈：** Python 3.12+、`agent-client-protocol>=0.8.1,<0.9`、`python-telegram-bot>=22.7,<23`、`PyYAML>=6,<7`、`pytest`、`pytest-asyncio`

---

## 锁定范围

- MVP 只支持 Telegram 轮询模式和 Claude ACP 适配器。
- 每个 Telegram 用户只有一个长生命周期 ACP 会话；`/new` 和空闲超时都会关闭并重建它。
- 部署模型限定为自用或严格白名单；`auto_approve` 只允许出现在这个边界内。
- 验证目标是本地源码安装 `pip install -e .`，不是 PyPI 发布。
- Telegram 流式输出在 MVP 中只支持纯文本；编辑流式消息时不要使用 `parse_mode`。

## 明确的非目标

- MVP 阶段不支持 Codex 或 Gemini 的运行时切换。
- 不支持飞书或 Discord 适配器。
- 不提供 Telegram 权限确认按钮。
- 不支持 webhook 模式，也不面向公网多用户部署。
- 不提供 `pip install talk2agent` 的发行故事。

## 文件地图

- 创建 `pyproject.toml`：项目元数据、运行时和测试依赖、控制台脚本、pytest 配置
- 创建 `README.md`：本地安装、Claude ACP 前置条件、配置示例、冒烟测试步骤、MVP 限制
- 创建 `talk2agent/__init__.py`：包标记和版本导出
- 创建 `talk2agent/__main__.py`：`python -m talk2agent` 入口
- 创建 `talk2agent/cli.py`：`init` / `start` 子命令
- 创建 `talk2agent/config.py`：配置 dataclass、YAML 读写辅助、启动校验
- 创建 `talk2agent/app.py`：启动装配、优雅关闭、会话清理
- 创建 `talk2agent/session_store.py`：`telegram_user_id -> AgentSession` 映射与空闲清理
- 创建 `talk2agent/acp/permission.py`：MVP 自动批准权限策略
- 创建 `talk2agent/acp/bot_client.py`：绑定到单个 `AgentSession` 的 ACP Client 实现
- 创建 `talk2agent/acp/agent_session.py`：ACP 进程 / 会话生命周期、prompt 锁、按回合 sink 分发
- 创建 `talk2agent/bots/telegram_stream.py`：ACP 更新渲染为文本、节流编辑、切块输出
- 创建 `talk2agent/bots/telegram_bot.py`：轮询 Handler、白名单检查、`/new`、`/status` 与文本转发
- 创建 `tests/` 下对应测试文件，覆盖 CLI、配置、权限、ACP 会话、SessionStore、流式输出和 Telegram Handler

## 配置形状

```yaml
telegram:
  bot_token: "REPLACE_ME"
  allowed_user_ids:
    - 123456789
agent:
  provider: "claude"
  workspace_dir: "."
  command: "node"
  args:
    - "claude-agent-acp"
permissions:
  mode: "auto_approve"
runtime:
  idle_timeout_minutes: 30
  stream_edit_interval_ms: 700
```

## 必须保持成立的设计说明

- `spawn_agent_process()` 必须按 `spawn_agent_process(client, command, *args, cwd=...)` 调用，而不是传 `(command, args)` 元组。
- `BotClient` 必须对每个 `AgentSession` 只创建一次，不能按 Telegram 消息替换。
- `request_permission()` 必须接受 `tool_call: ToolCallUpdate`，而不是 `ToolCall`。
- Telegram 流式输出必须使用绑定到当前回合的 sink 对象，而不是替换 ACP client。
- `/status` 和启动期校验必须明确提示 MVP 只支持 Claude。

## 任务拆分

### 任务 1：搭建包和 CLI 外壳

涉及文件：

- `pyproject.toml`
- `talk2agent/__init__.py`
- `talk2agent/__main__.py`
- `talk2agent/cli.py`
- `tests/test_cli_smoke.py`

执行步骤：

1. 先写失败的 CLI 冒烟测试，验证 `python -m talk2agent --help` 还不可用。
2. 运行测试，确认当前包尚不存在。
3. 写最小可运行的包结构和 CLI 解析逻辑。
4. 重新运行测试，确认通过。
5. 提交 bootstrap 结果。

### 任务 2：加入配置加载、校验和 `init`

涉及文件：

- `pyproject.toml`
- `talk2agent/cli.py`
- `talk2agent/config.py`
- `tests/test_config.py`

执行步骤：

1. 先写失败的配置测试，覆盖模板生成、白名单校验和基础加载行为。
2. 运行测试，确认配置模块尚未存在。
3. 实现 dataclass、模板写入和 CLI 对 `init` / `start` 的接线。
4. 重新运行配置测试，确认通过。
5. 提交配置层。

### 任务 3：实现 ACP 权限策略和 Client 桥接

涉及文件：

- `talk2agent/acp/permission.py`
- `talk2agent/acp/bot_client.py`
- `tests/test_permission.py`
- `tests/test_bot_client.py`

执行步骤：

1. 写失败测试，固定 `auto_approve` 的行为。
2. 实现权限策略和 Client 桥接。
3. 重新运行测试。
4. 确认 ACP 回调契约与库要求一致。
5. 提交 ACP 边界的基础设施。

### 任务 4：实现长生命周期 `AgentSession`

涉及文件：

- `talk2agent/acp/agent_session.py`
- `tests/test_agent_session.py`

执行步骤：

1. 先写失败测试，覆盖启动、单次初始化、按回合 sink 路由、重置和关闭行为。
2. 实现 `AgentSession`：启动 ACP 进程、创建会话、串行化 prompt、转发更新。
3. 覆盖异常时的清理行为。
4. 重新运行测试并提交。

### 任务 5：加入 `SessionStore` 与空闲清理

涉及文件：

- `talk2agent/session_store.py`
- `tests/test_session_store.py`

执行步骤：

1. 写失败测试，固定“每用户一个会话”的约束。
2. 实现按用户复用、重置、失效和关闭全部会话。
3. 加入空闲清理逻辑。
4. 确保单个用户的慢操作不会阻塞其他用户。
5. 提交该层改动。

### 任务 6：实现 Telegram 流式渲染器

涉及文件：

- `talk2agent/bots/telegram_stream.py`
- `tests/test_telegram_stream.py`

执行步骤：

1. 写失败测试，覆盖普通文本、工具事件、编辑节流与长文本切块。
2. 实现 ACP 更新到 Telegram 文本的渲染。
3. 实现超长文本分块与补发。
4. 重新运行测试并提交。

### 任务 7：打通 Telegram 机器人与应用生命周期

涉及文件：

- `talk2agent/bots/telegram_bot.py`
- `talk2agent/app.py`
- `tests/test_telegram_bot.py`

执行步骤：

1. 写失败测试，覆盖白名单、`/new`、`/status`、普通文本消息和错误路径。
2. 接上 `SessionStore`、`AgentSession` 和 `TelegramTurnStream`。
3. 实现应用启动、关闭和会话回收。
4. 重新运行测试并提交。

### 任务 8：补文档并完成验证

涉及文件：

- `README.md`

执行步骤：

1. 补充安装说明、配置示例、Claude ACP 前置条件与手工验证流程。
2. 跑完整自动化测试。
3. 执行 `pip install -e .`。
4. 生成配置并完成一次真实 Telegram 冒烟验证。
5. 提交文档与最终验证记录。

## 验证命令

```bash
pytest -q
python -m pip install -e .
python -m talk2agent init --config .tmp-real-telegram.yaml
python -m talk2agent start --config .tmp-real-telegram.yaml
```

## 文档验证时应明确的 README 结构

### 运行要求

- Python `>=3.12`
- Node.js 在 PATH 中
- 已安装 Claude ACP 适配器
- Telegram bot token 与允许访问的用户 ID

### 快速开始

1. `pip install -e .`
2. `talk2agent init --config config.yaml`
3. 填写 `telegram.bot_token`
4. 填写 `telegram.allowed_user_ids`
5. 运行 `talk2agent start --config config.yaml`

### 延后到 MVP 之后的事项

- 多 Provider 运行时切换
- 更丰富的消息格式与按钮交互
- webhook 模式
- 面向公开分发的安装方式
