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

- `/start`：恢复欢迎页和常驻主键盘，不会隐式创建新 session。
- `/start` / `/help` / `/status` / `Bot Status`：消息顶部会先给出当前 `Status` 和 `Recommended next step`，并补上一句 `Primary controls right now`，把“现在处于什么状态、接下来该做什么、该点哪个入口”放在详细运行时信息之前。
- `/start` / `/help`：都会明确提醒本地 `/start`、`/status`、`/help`、`/cancel` 始终可用，即使 Telegram 折叠了主键盘或 slash 菜单正在刷新。
- `/help`：查看当前 Provider / Workspace 下的快速使用指南和恢复入口，不会隐式创建新 session。
- `/cancel`：优先取消待输入动作，其次停止当前 turn，再次关闭 Bundle Chat；只有在没有本地状态可取消时，才会回到 agent 自己的 `/cancel` 命令。
- 待发送附件组：Telegram `media_group` 还在收集窗口内时，bot 会把它视为显式的本地待处理状态；`/cancel` / `Cancel / Stop` 可以直接丢弃，避免“以为取消了，其实附件还是发到了 agent”。
- 相册收集中的并发保护：当 `media_group` 还没收齐时，新的非相册文本/附件不会抢先进入 agent；bot 会明确提示“这条新消息没有发出去”，避免用户误以为它会排队执行，结果把待收集附件组冲掉。
- 状态切换前的上传止损：如果用户在附件组仍处于收集窗口内时执行 `New Session`、`Restart Agent`、会话切换/分叉，或管理员执行 `Switch Agent` / `Switch Workspace`，bot 会先丢弃这些待发送上传，并明确提示“Nothing was sent to the agent”，避免旧附件晚到新 session 或新 workspace。
- 主键盘高频入口：前两行优先放 `New Session`、`Bot Status`、`Retry Last Turn` 和 `Fork Last Turn`；`Workspace Search` 与 `Context Bundle` 紧随其后，`Help` / `Cancel / Stop` 保持为专门的恢复行，减少手机端在高频动作和恢复动作之间来回找按钮。
- Telegram slash 菜单：固定显示本地 `/start`、`/status`、`/help`、`/cancel`，并追加当前 agent 暴露的命令。
  如果 agent 命令发现暂时失败，菜单仍会保留这些本地恢复入口。
- 过期按钮：旧消息上的 inline button 过期后，bot 会明确提示“这是旧菜单上的按钮”，并建议重新打开最近视图或使用 `/start`，避免只留下不可操作的死按钮。
- 无效或跨用户按钮：版本漂移、失效 payload，或点到别人的按钮时，bot 会返回恢复或纠正建议，而不是只显示生硬的系统短语。
- `Bot Status`：只读总览当前 Provider、Workspace、会话和最近状态，并作为高频入口。
- `Bot Status` 顶部会按当前状态前置主动作，例如 `Stop Turn`、`Cancel Pending Input`、`Discard Pending Uploads`、`Ask Agent With Context` 或 `Retry Last Turn`，减少手机端来回扫按钮。
- `Last Request` 不再只是只读缓存：`Bot Status` 会额外显示它来自 plain text / bundle / workspace request 等哪个来源，并提供 `Run Last Request`，用于只重跑请求文本本身；如果你需要原附件或原上下文，则继续使用 `Retry Last Turn`。
  在 `Last Request` 详情页里，如果当前 workspace 还有上一轮可复用 turn，页面也会直接给出 `Retry Last Turn` / `Fork Last Turn`，把“只重跑文本”和“恢复整轮上下文”明确分开。
- `Bot Status` 导航失败：如果某个只读视图临时打开失败，bot 会保留 `Try Again` 和相应的返回按钮，避免把用户丢出当前流程。
- 关键失败态：包括通用请求失败、session 拉起/切换/分叉失败、Provider Session 接管失败，以及 Model / Mode 更新失败，都会优先返回可操作的恢复建议，而不是直接暴露内部错误短语。
- `Switch Agent` / `Switch Workspace`：管理员执行的全局切换，会影响所有用户的当前运行时。
  菜单顶部会先强调这是 shared runtime 的全局动作，避免管理员把它误解成只影响自己当前聊天。
  切换前，bot 会先明确说明旧按钮与待输入会被清理，以及 `Context Bundle`、`Last Request`、`Last Turn` 哪些会继续可复用、哪些会留在旧 runtime / 旧 workspace。
  如果切换前还有待发送附件组，bot 会先直接丢弃并把这件事写进失败/成功回显，避免旧附件误发到新 runtime。
  如果切换失败，bot 会保留当前选择器并带上失败说明，避免管理员重新回到主键盘再打开一次。
  切换成功后，回显也会再次提示 carry-over 规则，避免管理员误以为 context bundle 会自动跟着切走。
- `New Session` / `Restart Agent` / `Session History`：管理当前用户的会话生命周期。
  当这些动作会替换当前 live session 时，如果还有待发送附件组，bot 也会先丢弃并明确告知，而不是让旧上传跨 session 漏过去。
  从 `Session History` 里执行 `Run Session` / `Run+Retry` / `Fork+Retry` 时，成功和失败都会回到历史列表并保留当前上下文；如果上一轮已失效，也会明确提示先发新请求，而不是误报“已经重试成功”。
  如果 `Session History` 还是空的，bot 不会只留一句“没有历史”；而是补上 `New Session`、`Provider Sessions`（管理员）和 `Open Bot Status`，把下一步动作直接放在空状态里。
- `Retry Last Turn` / `Fork Last Turn`：如果当前 workspace 还没有上一轮可复用，bot 会明确提示先发送一条新请求；从 `Bot Status` 里触发这类回放时，也会原地恢复状态页，而不是跳出当前流程。
- 回合完成快捷操作：当一次 turn 正常结束且没有更具体的 workspace change follow-up 时，最终结果消息本身会附上 `Retry Last Turn`、`Fork Last Turn`、`Open Bot Status` 和 `New Session`。
  这些按钮会从结果继续发起下一步，而不会把刚收到的答案编辑掉。
- `Provider Sessions`：管理员浏览并接管 Provider 原生保存的 session。
  其中 `Run+Retry` / `Fork+Retry` 也会在原列表内完成成功/失败回显，避免管理员在 provider 会话列表和结果页之间来回跳转。
  如果当前 agent 不支持 provider-side session browsing，或当前页暂时没有任何 provider session，bot 会解释这是 provider 能力或当前状态所致，并补上 `Refresh` / `Open Bot Status` 恢复入口，而不是只留一句空文案。
- `Agent Commands` / `Model / Mode`：使用当前 live session 暴露的能力。
  `Model / Mode` 不再只是按钮列表；页面会先展示当前 setup、说明这是对当前 live session 的原地更新，再列出可选项和“先看详情还是直接切换”的路径，减少手机端误切。
  如果当前 session 只暴露 `Model` 或只暴露 `Mode`，页面会直接说明另一半当前不可用，而不是让用户误以为按钮加载失败。
  进入单个 choice 详情后，bot 会明确说明这次切换的作用范围；如果存在上一轮请求，还会直接解释 `Use ... + Retry` 会在切换后立刻重跑上一轮。
  如果 live session 已失效，bot 会提示直接发送文本或附件来重新开始，并保留 `Reopen Model / Mode` 或返回状态页的恢复入口。
  如果入口来自 `Bot Status`，其中 `...+Retry` 在重放准备失败或执行失败时也会回到状态页并明确提示失败原因，而不是误报已经重放成功。
  如果暂时没有发现任何 agent commands，bot 会把这件事解释成“可能还在发现中，或当前 agent 根本不暴露命令”，并保留 `Refresh` 与状态页恢复入口。
- `Workspace Files` / `Workspace Search` / `Workspace Changes`：围绕当前 workspace 做只读浏览和检查。
  如果进入的是空目录，`Workspace Files` 不会把用户留在死路里；视图会直接保留 `Workspace Search` 和状态页恢复入口。
  取消 `Workspace Search` 的待输入后，消息会保留 `Search Again` 和 `Open Bot Status` 恢复入口。
  对 `Workspace Search` 无结果、`Workspace Changes` 无 Git 仓库或工作树干净这类空结果，bot 也会直接给出 `Search Again`、`Workspace Files`、`Workspace Search` 或状态页入口，而不是只留一个终点文案。
  当 bot 正在等待纯文本时，如果用户误发了附件、sticker 等非文本消息，提示会点名当前等待的动作，例如 `Workspace Search` 或 `Rename session title`，并明确说明这条误发消息没有被转给 agent，避免只看到笼统的“等待输入”。
- 空上下文与缺失 last request：`Context Bundle` 为空时，bot 会明确提示先从 Files/Search/Changes 加内容；`Ask With Last Request` 这类快捷动作在缺少上一条请求时，也会提示先发送一条新请求。
- `Context Bundle`：把文件、变更和降级附件累积为持续上下文。
  当 `Context Bundle` 还是空的，视图内会直接给出 `Workspace Files`、`Workspace Search` 和 `Workspace Changes`，把“先去哪里补上下文”变成一跳动作。
- `Stop Turn`：停止当前正在运行的 agent 回合。
  如果用户在回合仍运行时又发来一条新消息，bot 会明确说明这条新消息没有发给 agent，避免误以为系统会排队执行。
- `Cancel / Stop`：主键盘上的常驻快捷入口，对应 `/cancel` 的本地优先语义，便于手机端在长回合中快速止损。
- 不支持的 Telegram 富消息：例如 sticker、location、contact、poll、GIF、video note 或 dice 不会再静默丢弃；bot 会明确提示改发文本、图片、文档、音频或视频，并补上 `/help` / `/start` 的恢复路径。若当前正在等待纯文本，仍会优先提醒继续发送纯文本。
- 未授权访问：未被允许的 Telegram 用户会收到明确的访问拒绝说明，提示联系操作者开通访问。

## 附件与降级

- 文本消息直接进入 ACP prompt。
- 图片、音频、视频和文档会尽量映射为 ACP 结构化内容块。
- 超过 `8 MiB` 的附件会在送出前直接被拒绝，并明确提示压缩或改发更小文件；不会把任何内容半路发给 agent。
- 如果当前 Provider 不支持某类附件，bot 会优先使用受控降级路径，例如写入当前 workspace 的 `.talk2agent/telegram-inbox/`。
- 当附件被这样降级时，bot 会额外发一条说明消息，明确告知附件已加入 `Context Bundle`，并附上 `Open Context Bundle` 与 `Open Bot Status` 恢复入口。
- 即使这类附件所在的 turn 后续失败，bot 也会保留已落盘的文件并继续挂在 `Context Bundle` 里，避免用户被迫重新上传。
- 流式 Draft 预览在文本超长时会明确用省略号表示“当前只展示尾部进度”，尽量避免用户误以为前面的回复被 bot 吞掉。
- 如果 Telegram Draft 预览暂时不可用，bot 也会先发一条“正在处理”的普通消息，再在完成后补发最终回复，避免长回合时出现无反馈的静默等待。
- 当单次回复超过 Telegram 文本上限时，bot 会优先按段落、换行或空格分段，尽量避免把一句完整话硬切成难读的碎片消息。

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
