# talk2agent

`talk2agent` 是一个自托管的 Telegram 轮询机器人。
它会把已授权用户的文本和附件消息转发给本地 ACP 兼容智能体运行时，并把流式结果回传到 Telegram。

## 文档入口

- [AGENTS.md](AGENTS.md)：仓库地图，给人和智能体的默认入口。
- [docs/operator-guide.md](docs/operator-guide.md)：面向操作者的使用说明。
- [docs/manual-checklist.md](docs/manual-checklist.md)：端到端手工验收清单。
- [docs/harness.md](docs/harness.md)：自动化 harness、文档约束和 CI 入口。
- [docs/design-docs/index.md](docs/design-docs/index.md)：结构性设计说明。

## 运行要求

- Python `>=3.10`
- Node.js
- 至少一个可用的 ACP Provider：
  - `npm install -g @zed-industries/claude-agent-acp`
  - `npm install -g @zed-industries/codex-acp`
  - `gemini --acp`
- 一个 Telegram Bot Token，以及至少一个允许访问的 Telegram 用户 ID

## 快速开始

1. 安装项目：

   ```bash
   python -m pip install -e .
   ```

2. 安装你要使用的 Provider。默认模板以 `codex` 启动：

   ```bash
   npm install -g @zed-industries/codex-acp
   ```

3. 生成初始配置：

   ```bash
   talk2agent init --config config.yaml
   ```

4. 编辑 `config.yaml`，至少填写：
   - `telegram.bot_token`
   - `telegram.allowed_user_ids`
   - `telegram.admin_user_id`
   - `agent.provider`
   - `agent.workspaces`
   - `agent.workspace_dir`

5. 启动机器人：

   ```bash
   talk2agent start --config config.yaml
   ```

## 开发验证

统一入口：

```bash
python -m talk2agent harness
```

它会执行文档边界检查、`pytest -q` 和 CLI 烟雾验证。

## 下一步读什么

- 想配置和日常使用 bot：看 [docs/operator-guide.md](docs/operator-guide.md)
- 想做端到端手工验收：看 [docs/manual-checklist.md](docs/manual-checklist.md)
- 想理解系统分层和边界：看 [ARCHITECTURE.md](ARCHITECTURE.md)
- 想理解某个跨模块功能为什么这么设计：看 [docs/design-docs/index.md](docs/design-docs/index.md)
