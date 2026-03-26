# Manual Checklist

这份清单面向人类做端到端验收。
它只保留高价值场景，不重复底层设计文档。

## 准备

1. 准备一个允许访问的管理员 Telegram 账号。
2. 准备至少一个可切换的 workspace，其中最好包含一个 Git 仓库。
3. 启动 bot，并确认当前 Provider 能成功创建 ACP session。

## 启动与全局运行时

1. 点击 `Bot Status`，确认它是只读入口，不会隐式创建新 session。
2. 执行 `Switch Agent`，确认切换前有预检，切换后旧 UI 动作立即失效。
3. 执行 `Switch Workspace`，确认只显示白名单 workspace，并且切换跨重启持久化。

## 会话生命周期

1. 发送普通文本，确认会创建或复用当前运行时中的 live session。
2. 点击 `New Session` 和 `Restart Agent`，确认会话被替换，旧 session 不再接收新请求。
3. 打开 `Session History`，确认可以浏览、切换、分叉、重命名和删除本地历史会话。
4. 如果 Provider 支持原生 session 浏览，打开 `Provider Sessions`，确认可以接管或分叉 provider 侧 session。
5. 验证 `Retry Last Turn` 和 `Fork Last Turn` 会复用上一轮保存的 replay payload。

## Workspace 与上下文

1. 打开 `Workspace Files`，确认可以浏览和预览当前 workspace 内的文本文件。
2. 打开 `Workspace Search`，确认可以用下一条文本作为搜索词并查看结果。
3. 打开 `Workspace Changes`，确认可以查看当前 Git 变更和 diff 预览。
4. 从文件、搜索结果或变更中加入 `Context Bundle`，确认 bundle 可浏览、移除、清空和持续附着。

## Agent 能力与检查视图

1. 打开 `Agent Commands`，确认显示的是当前 agent 暴露的命令，而不是 bot 自己的管理命令。
2. 打开 `Model / Mode`，确认可以查看并切换当前 live session 暴露的选项。
3. 从 `Bot Status` 进入 `Session Info`、`Workspace Runtime`、`Usage`、`Last Request`、`Last Turn`、`Agent Plan`、`Tool Activity`，确认这些只读检查视图都能打开并返回。

## 附件与长回合

1. 发送图片、音频、视频和文档，确认支持的类型直接进入 ACP，不支持的类型走明确降级。
2. 发送同一 `media_group_id` 的多附件，确认它们被合并为一次 ACP 回合。
3. 发起一个持续几秒的请求，确认 `Bot Status` 能显示运行中状态并允许 `Stop Turn`。

## 回归出口

1. 运行 `python -m talk2agent harness`。
2. 如果本轮修改覆盖多个交互面，再补做与改动直接相关的人工场景。
3. 验证通过后再同步到 GitHub。
