# 执行计划索引

执行计划是实现过程的记录。
应让进行中的工作易于定位，并把完成后的计划归档，而不是让大体量计划文件长期滞留在仓库根目录。

## 进行中

- `docs/exec-plans/active/` 当前为空。
- 当某个改动需要分阶段执行或显式检查点时，把计划放在这里。

## 已完成

- [completed/2026-03-20-telegram-bot-mvp.md](completed/2026-03-20-telegram-bot-mvp.md)：Telegram + ACP 机器人最初的 MVP 构建计划。
- [completed/2026-03-20-multi-provider-acp-telegram-bot.md](completed/2026-03-20-multi-provider-acp-telegram-bot.md)：后续引入多 Provider 切换和运行时 Provider 持久化的实现计划。

## 约定

- 尽量一项重要改动对应一份计划
- 文件名保持日期前缀
- 完成后移动到 `completed/`
- 如果计划已经过时，要么更新替代，要么归档，不要悄悄让历史失真
