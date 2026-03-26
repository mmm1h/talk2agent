# Operator Guide

这份文档面向人类操作者。
它说明如何配置、启动和日常使用 bot，不负责解释底层架构实现。

## 启动前

- 安装 Python `>=3.10`
- 安装 Node.js
- 安装至少一个 ACP Provider
- 准备 Telegram Bot Token 和允许访问的用户 ID

## 初次启动

1. 安装项目：`python -m pip install -e .`
2. 安装 Provider：例如 `npm install -g @zed-industries/codex-acp`
3. 生成配置：`talk2agent init --config config.yaml`
4. 编辑 `config.yaml`
5. 启动：`talk2agent start --config config.yaml`

## 配置重点

- `telegram.bot_token`：机器人 token
- `telegram.allowed_user_ids`：允许访问的 Telegram 用户
- `telegram.admin_user_id`：唯一管理员，必须属于允许用户
- `agent.provider`：启动时默认 Provider
- `agent.workspaces`：允许切换的 workspace 白名单
- `agent.workspace_dir`：启动时默认 workspace

## 日常使用

- `Bot Status`：只读总览当前 Provider、Workspace、会话和最近状态，并作为高频入口。
- `Switch Agent` / `Switch Workspace`：管理员执行的全局切换，会影响所有用户的当前运行时。
- `New Session` / `Restart Agent` / `Session History`：管理当前用户的会话生命周期。
- `Provider Sessions`：管理员浏览并接管 Provider 原生保存的 session。
- `Agent Commands` / `Model / Mode`：使用当前 live session 暴露的能力。
- `Workspace Files` / `Workspace Search` / `Workspace Changes`：围绕当前 workspace 做只读浏览和检查。
- `Context Bundle`：把文件、变更和降级附件累积为持续上下文。
- `Stop Turn`：停止当前正在运行的 agent 回合。

## 附件与降级

- 文本消息直接进入 ACP prompt。
- 图片、音频、视频和文档会尽量映射为 ACP 结构化内容块。
- 如果当前 Provider 不支持某类附件，bot 会优先使用受控降级路径，例如写入当前 workspace 的 `.talk2agent/telegram-inbox/`。

## 例行验证

在提交、升级或大改动后，运行：

```bash
python -m talk2agent harness
```

如果你要做人工验收，再看 [manual-checklist.md](manual-checklist.md)。

## 想继续深入

- 架构边界：看 [../ARCHITECTURE.md](../ARCHITECTURE.md)
- 设计缘由：看 [design-docs/index.md](design-docs/index.md)
- 自动化 harness：看 [harness.md](harness.md)
