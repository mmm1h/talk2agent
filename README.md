# talk2agent

这是一个 Telegram 轮询机器人 MVP，用于将已授权用户的纯文本消息转发到基于 ACP 的智能体会话中，并通过编辑占位消息的方式持续回传流式响应。

## 文档地图

- [AGENTS.md](AGENTS.md) 是给人和智能体看的简短仓库地图。
- [ARCHITECTURE.md](ARCHITECTURE.md) 是顶层系统结构与包分层地图。
- [docs/index.md](docs/index.md) 用于索引更深一层的设计文档和执行记录。

## 运行要求

- `PATH` 中可用的 Python `>=3.10`
- `PATH` 中可用的 Node.js
- `PATH` 中至少有一个受支持的 ACP Provider：
  - Claude ACP 适配器：`npm install -g @zed-industries/claude-agent-acp`
  - Codex ACP 适配器：`npm install -g @zed-industries/codex-acp`
  - 带 ACP 模式的 Gemini CLI：`gemini --acp`
- 一个 Telegram Bot Token，以及至少一个允许访问的 Telegram 用户 ID

## 快速开始

1. 以可编辑模式安装本项目：

   ```bash
   python -m pip install -e .
   ```

2. 安装你要使用的 Provider。默认模板以 `gemini` 启动，所以本地最容易跑通的基线是：

   ```bash
   npm install -g @google/gemini-cli
   ```

   如果希望在同一个机器人进程里切换到 Codex：

   ```bash
   npm install -g @zed-industries/codex-acp
   ```

3. 生成一个初始配置：

   ```bash
   talk2agent init --config config.yaml
   ```

4. 编辑 `config.yaml`：

   - 设置 `telegram.bot_token`
   - 替换 `telegram.allowed_user_ids`
   - 将 `telegram.admin_user_id` 设为其中一个允许的用户 ID
   - 选择启动时的 `agent.provider`：`claude`、`codex` 或 `gemini`

5. 启动机器人：

   ```bash
   talk2agent start --config config.yaml
   ```

   模块入口与上面等价：

   ```bash
   python -m talk2agent start --config config.yaml
   ```

## 命令

- `/status` 返回 `provider=<name> session_id=<none|pending|value>`，且不会额外创建新的 ACP 会话
- `/new` 重置当前调用者的会话
- `/provider <claude|codex|gemini>` 仅管理员可用；切换后会先清空旧 Provider 的所有会话，再让新流量进入新 Provider

## Provider 持久化

- `runtime.provider_state_path` 用于保存最近一次选中的 Provider
- 进程重启后，会优先恢复该 Provider；若不存在，再回退到 `agent.provider`
- 如果你想让 YAML 中的 `agent.provider` 重新生效，可以手工切回去，或删除/重置 provider-state 文件
- 旧版 YAML 键 `agent.command` 和 `agent.args` 在运行时会被忽略

## 手工冒烟流程

1. 用 `talk2agent init --config config.yaml` 生成配置。
2. 将真实 Telegram Token 填入 `telegram.bot_token`。
3. 把你自己的 Telegram 用户 ID 加入 `telegram.allowed_user_ids`。
4. 将 `telegram.admin_user_id` 设置为同一个测试用户 ID。
5. 在同一个 shell 中确认所选 Provider 的可执行文件可见：
   - `Get-Command gemini`
   - `Get-Command codex-acp`
   - `Get-Command claude-agent-acp`
6. 使用 `talk2agent start --config config.yaml` 启动机器人。
7. 先发送 `/status`，确认在任何提示词之前返回的是当前 Provider 和 `session_id=none`。
8. 从允许的 Telegram 账号发送一条普通文本消息，确认机器人先回复 `Thinking...`，随后通过编辑该消息输出流式文本。
9. 以管理员身份发送 `/provider codex`，确认回复 `provider=codex`。
10. 再次发送 `/status`，确认它报告 `provider=codex`。
11. 发送 `/new`，确认该用户的会话被重置。
12. 重启机器人，确认它恢复到最近一次选中的 Provider，而不是更早的配置默认值。

这个 MVP 只支持 Telegram 轮询模式，不支持 webhook。

## MVP 范围与限制

- 仅适用于白名单 / 自用场景：非允许用户的消息会被拒绝
- 在当前活跃 Provider 运行时中，每个 Telegram 用户只保留一个长生命周期 ACP 会话
- `permissions.mode` 固定为 `auto_approve`
- 流式输出仅支持纯文本，并通过消息编辑投递
- Provider 切换是全局性的，并且只允许管理员触发

## 验证清单

这些命令可用于验证多 Provider 改动后的仓库状态：

```bash
python -m pytest -q
python -m pip install -e .
python -m talk2agent init --config .tmp-multi-provider.yaml
talk2agent init --config .tmp-multi-provider-script.yaml
```

如果要做真实 Telegram 冒烟测试，可以创建一个临时配置文件，比如 `.tmp-real-telegram.yaml`，填入真实 token 和用户 ID 后运行：

```bash
python -m talk2agent start --config .tmp-real-telegram.yaml
```
