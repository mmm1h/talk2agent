# 设计文档索引

这个目录用于保存跨多个模块、会改变系统形状、不变量或运维行为的设计说明。
局部小修复不应使用这里。

## 当前文档

- [2026-03-20-multi-provider-acp-design.md](2026-03-20-multi-provider-acp-design.md)：多 Provider ACP 支持、运行时快照、全局 Provider 切换与重启恢复的已实现设计。
- [2026-03-20-telegram-agent-controls-design.md](2026-03-20-telegram-agent-controls-design.md)：Telegram 按钮化 agent 控制、session history、本地 history 索引与 model/mode 切换设计。
- [2026-03-26-telegram-command-center-design.md](2026-03-26-telegram-command-center-design.md)：基于 ACP `available_commands` 的 Telegram Command Center 设计。
- [2026-03-26-telegram-provider-sessions-design.md](2026-03-26-telegram-provider-sessions-design.md)：基于 ACP `session/list` 的 Telegram provider 原生 session 浏览与接管设计。
- [2026-03-26-telegram-workspace-browser-design.md](2026-03-26-telegram-workspace-browser-design.md)：基于 workspace 白名单的 Telegram 只读文件浏览设计。
- [2026-03-26-telegram-workspace-search-design.md](2026-03-26-telegram-workspace-search-design.md)：基于 workspace 白名单的 Telegram 只读全文搜索设计。
- [2026-03-26-telegram-workspace-changes-design.md](2026-03-26-telegram-workspace-changes-design.md)：基于 workspace Git 状态的 Telegram 只读变更浏览设计。
- [2026-03-26-telegram-context-bundle-design.md](2026-03-26-telegram-context-bundle-design.md)：基于 workspace 文件与 Git 变更的 Telegram Context Bundle 设计。
- [2026-03-26-telegram-attachment-input-design.md](2026-03-26-telegram-attachment-input-design.md)：Telegram 图片/文档输入与 ACP 结构化内容块桥接设计。

## 什么时候需要新增设计文档

- 新增 Telegram 以外的传输通道
- 变更会话归属或生命周期语义
- 调整 Provider 路由模型
- 调整安全或授权模型
- 任何会同时影响多个包边界的改动
