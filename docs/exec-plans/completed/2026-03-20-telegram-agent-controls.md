# Telegram Agent Controls 实现计划

## Summary

把 Telegram 机器人的主交互从命令扩展为按钮，并补齐：

- 全局 `Switch Agent`
- `New Session`
- `Session History`
- `Restart Agent`
- `Model / Mode`

同时保持原有不变量：

- 仍然只有一个活跃 Provider runtime
- Provider 切换仍然只有管理员可触发
- `/status` 保持只读

## 实现要点

1. 扩展 `AgentSession`，支持 ACP `load` / `resume` / `list` / `set_config_option`。
2. 引入 `session_history.py`，把本地 history 索引从 live session 生命周期里拆出来。
3. 扩展 `SessionStore`，接入 history 列表、删除和切换。
4. 在 `app.py` 给 Provider 切换增加 session 创建 preflight。
5. 在 `telegram_bot.py` 新增 reply keyboard、inline callback 和菜单 token 管理。
6. 保留 `/new`、`/provider` 作为兼容入口，但不再是主 UX。

## 验证

- Provider 切换预检失败时返回 `session creation failed`
- `New Session` / `Restart Agent` 能创建新 session
- `Session History` 可列出、运行、删除本地记录
- `Model / Mode` 使用当前会话真实返回的选项
- `python -m pytest -q`
