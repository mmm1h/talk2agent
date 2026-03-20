# 多 Provider ACP Telegram 机器人实施计划

**目标：** 将现有 Telegram 轮询机器人扩展为可通过 ACP 运行 `claude`、`codex` 和 `gemini`，支持通过 `/provider` 做全局切换、让 `/status` 保持只读，并在重启后恢复最近一次选中的 Provider。

**架构：** 保持 `AgentSession` 和 ACP 传输边界不变；新增一层 Provider 运行时逻辑，负责 Provider Profile、持久化 Provider 状态和启动期解析；扩展 `SessionStore`，加入只读查询与退休语义；让 app / bot 通过同步化运行时快照读取 Provider 状态，以保证并发下的切换一致性。

**技术栈：** Python 3.10+、`agent-client-protocol`、`python-telegram-bot`、`PyYAML`、`pytest`

---

## 执行说明

- 默认模板 Provider 为 `gemini`，因为本地环境中可以直接通过 `gemini --acp` 跑通。
- 运行时仍然支持三种 Provider：`claude`、`codex`、`gemini`。
- 旧版 YAML 键 `agent.command` 和 `agent.args` 保留兼容输入能力，但运行时必须忽略它们。
- 当时工作区还不是 Git 仓库，所以原计划里的每次 commit 只是阶段检查点；现在该限制已解除。
- 真实 Telegram 密钥只能放在临时配置文件中，不能写进被跟踪的文档或模板。

## 文件地图

- 新建 `talk2agent/provider_runtime.py`：Provider Profile 映射、Provider 状态文件读写、启动 Provider 解析、`RuntimeState`
- 修改 `talk2agent/config.py`：解析 `telegram.admin_user_id`、解析 `runtime.provider_state_path`、扩展 Provider 校验、停止输出 `command/args`
- 修改 `talk2agent/session_store.py`：增加只读 `peek()`、退休状态、退休错误以及回滚可恢复的重新激活路径
- 修改 `talk2agent/app.py`：构建绑定 Provider 的 Store、维护运行时快照、实现 Provider 切换事务、启动时恢复持久化 Provider
- 修改 `talk2agent/bots/telegram_bot.py`：新增 `/provider`、把 `/status` 做成只读、为文本和 `/new` 增加退休 Store 单次重试
- 修改 `README.md`：记录支持的 Provider、各自安装方式、`/provider` 语义、重启恢复和真实机器人冒烟流程
- 新建和修改相关测试文件，覆盖 Provider 运行时、配置、Store、App 和 Telegram Handler 变化

## 任务拆分

### 任务 1：加入 Provider 运行时基础设施

涉及文件：

- `talk2agent/provider_runtime.py`
- `tests/test_provider_runtime.py`

执行步骤：

1. 先写失败测试，覆盖默认 Provider、Provider 到命令的映射、Provider 状态文件读写和启动 Provider 解析。
2. 运行测试，确认缺失运行时辅助逻辑。
3. 实现 `ProviderProfile`、`RuntimeState` 和 Provider 状态文件读写 helper。
4. 重新运行测试。
5. 形成第一个可提交检查点。

### 任务 2：扩展配置解析与 `init` 模板

涉及文件：

- `talk2agent/config.py`
- `tests/test_config.py`

执行步骤：

1. 先写失败测试，覆盖三种 Provider、`admin_user_id`、`provider_state_path` 以及忽略旧版 `command/args`。
2. 更新配置 dataclass、校验逻辑和默认模板输出。
3. 重新运行配置测试。
4. 确认 README 与模板叙述已不再是 Claude-only。

### 任务 3：为 `SessionStore` 增加只读查询和退休语义

涉及文件：

- `talk2agent/session_store.py`
- `tests/test_session_store.py`

执行步骤：

1. 加入 `peek()`，让 `/status` 可以只读查询。
2. 加入 `retire()` / `activate()`，使切换过程中的旧 Store 无法继续创建或重置会话。
3. 加入 `RetiredSessionStoreError`，让上层可以识别并重试。
4. 补足相关并发和回滚测试。

### 任务 4：构建运行时快照与 Provider 切换事务

涉及文件：

- `talk2agent/app.py`
- `tests/test_app.py`

执行步骤：

1. 用 `RuntimeState` 持有当前活跃 Provider 和对应 `SessionStore`。
2. 提供 `snapshot_runtime_state()`，让读路径基于一致快照工作。
3. 在 `switch_provider()` 中实现“新 Store 准备 -> 旧 Store 退休 -> 新状态安装 -> Provider 持久化 -> 成功后关闭旧 Store”的事务。
4. 如果持久化失败，则回滚旧运行时并关闭新建 Store。
5. 用测试覆盖原子切换、回滚和并发可见性。

### 任务 5：接上 `/provider`、只读 `/status` 与退休 Store 重试逻辑

涉及文件：

- `talk2agent/bots/telegram_bot.py`
- `tests/test_telegram_bot.py`

执行步骤：

1. 新增 `/provider <claude|codex|gemini>`。
2. 让 `/status` 使用 `peek()`，保证不创建新会话。
3. 让文本消息、`/new` 和 `/status` 都通过 `_with_active_store()` 在退休 Store 错误上最多重试一次。
4. 更新测试，覆盖管理员约束、usage 文案、只读行为和切换后的文本消息路径。

### 任务 6：更新文档并跑完整验证

涉及文件：

- `README.md`

执行步骤：

1. 记录三种 Provider 的安装方式。
2. 记录 `/provider` 和重启恢复语义。
3. 运行全部自动化测试。
4. 跑一次可编辑安装和 CLI 冒烟检查。
5. 准备临时 Telegram 配置，完成真实机器人冒烟测试。

## 关键测试覆盖点

### 配置层

- 允许 `claude`、`codex`、`gemini`
- 拒绝未知 Provider
- 默认模板含 `admin_user_id`
- 默认模板含 `provider_state_path`
- 旧版 `command/args` 不影响运行时 Provider

### 应用层

- 持久化 Provider 优先于配置默认值
- 无效持久化状态会回退到配置值
- Provider 切换只有在持久化成功后才算成功
- 切换失败时不会把瞬时新状态泄漏给其他 Handler
- 新流量会进入新 Store，即便旧 Store 正在关闭

### Telegram 层

- `/status` 动态显示当前 Provider
- `/status` 不创建会话
- 非管理员无法执行 `/provider`
- 非法 Provider 参数返回 usage
- Handler 遇到退休 Store 时会重试一次

## 手工验证清单

1. 使用 `talk2agent init --config config.yaml` 生成配置。
2. 填入真实 `telegram.bot_token`。
3. 配置白名单和 `telegram.admin_user_id`。
4. 确认 `gemini`、`codex-acp`、`claude-agent-acp` 在 PATH 中可见。
5. 启动机器人。
6. 发送 `/status`，确认在无会话时返回 `provider=<当前 provider> session_id=none`。
7. 发送普通文本消息，确认 `Thinking...` 会被编辑成流式输出。
8. 以管理员身份执行 `/provider codex`，确认成功切换。
9. 重新发送 `/status`，确认显示 `provider=codex`。
10. 重启机器人，确认仍恢复到最近一次切换后的 Provider。

## 推荐落地顺序

1. 先落 Provider 运行时基础设施
2. 再改配置层
3. 再扩展 `SessionStore`
4. 再实现 `AppServices` 的切换事务
5. 再把 Telegram 命令接上线
6. 最后更新 README 和验证流程

这样做的风险最低，因为它尽量保留了原有 ACP 会话核心不变，只是在其上增加一层运行时管理。
