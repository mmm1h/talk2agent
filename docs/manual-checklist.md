# Manual Checklist

这份清单面向人类做端到端验收。
它只保留高价值场景，不重复底层设计文档。

## 准备

1. 准备一个允许访问的管理员 Telegram 账号。
2. 准备至少一个可切换的 workspace，其中最好包含一个 Git 仓库。
3. 启动 bot，并确认当前 Provider 能成功创建 ACP session。

## 启动与全局运行时

1. 发送 `/start`，确认会返回欢迎页和主键盘，且不会隐式创建新 session。
   同时确认主键盘只保留四行高频动作：前两行是 `New Session`、`Bot Status`、`Retry Last Turn` 和 `Fork Last Turn`，第三行是 `Workspace Search` / `Context Bundle`，第四行是 `Help` / `Cancel / Stop`；`Session History`、`Model / Mode`、`Agent Commands`、`Workspace Files` / `Workspace Changes`、`Restart Agent` 不再常驻主键盘，而是统一进入 `Bot Status`。另外确认消息顶部会先显示 `Status`、`Recommended next step` 与 `Primary controls right now`，再给出更产品化的“Start here”引导和 `/start`、`/status`、`/help`、`/cancel` 恢复提醒。
2. 发送 `/help`，确认会返回快速使用指南和恢复入口，且不会隐式创建新 session。
   同时确认消息顶部也会先显示当前状态和建议下一步，而不是一上来就铺满运行时细节，并明确说明本地 slash 恢复入口始终可用。
3. 发送 `/status`，确认会直接打开 `Bot Status`，且不会隐式创建新 session；在主键盘被 Telegram 折叠时，这条命令仍然可作为只读恢复入口。
4. 在待输入、运行中 turn 和 Bundle Chat 三种状态下分别发送 `/cancel`，并额外测试一次主键盘 `Cancel / Stop`，确认都会按优先级执行本地取消，而不是误发给 agent。
   额外验证一次 `media_group` 还在收集窗口内时立刻 `/cancel`，确认附件组会被直接丢弃，而不是延迟几百毫秒后仍然发给 agent。
   再额外验证一次 `media_group` 还在收集窗口内时发送普通文本或单独附件，确认 bot 会明确提示这条新消息没有发出去，并继续保留原附件组，而不是把相册悄悄冲掉。
5. 打开 Telegram slash 菜单，确认固定可见 `/start`、`/status`、`/help`、`/cancel`，并且当前 agent 暴露的命令会追加在后面。
6. 人为制造一次关键失败态，例如新 session 创建失败或切换失败，确认提示给出恢复建议，而不是直接暴露内部错误短语。
7. 触发一个已失效的旧 inline button，确认 bot 会提示这是旧菜单上的按钮，并建议重新打开最近视图或使用 `/start`，而不是只弹出无方向的过期提示。
8. 发送一个伪造或跨用户的 callback，确认 bot 会给出“重新打开最近视图”或“从自己的聊天里重新打开菜单 / 使用 `/start`”之类的纠正提示，而不是只显示模糊的系统短语。
9. 点击 `Bot Status`，确认它是只读入口，不会隐式创建新 session。
   同时确认顶部会前置当前状态下的主动作，例如 `Stop Turn`、`Cancel Pending Input`、`Discard Pending Uploads`、`Ask Agent With Context` 或 `Retry Last Turn`。
   如果当前 workspace 已缓存 `Last Request`，再确认状态页会显示它的来源摘要，并提供 `Run Last Request`；这个入口应只重跑请求文本，而不是隐式恢复旧附件或旧上下文。
10. 执行 `Switch Agent`，确认切换前有预检，切换菜单会明确提示旧按钮 / 待输入会被清理、`Context Bundle` 不会跟随切换，而同 workspace 下的 `Last Turn` / `Last Request` 仍可继续复用；切换后旧 UI 动作立即失效。
   同时确认菜单顶部会明确写出这是影响所有 Telegram 用户的全局切换，而不是当前聊天私有动作。
   额外在 `media_group` 仍处于收集窗口内时执行一次，确认 bot 会先明确提示已丢弃待发送上传，且这些旧附件不会在几百毫秒后误发到新 agent。
11. 执行 `Switch Workspace`，确认只显示白名单 workspace，并且切换跨重启持久化；切换菜单和成功回显都要明确提示 workspace 作用域内的 `Context Bundle`、`Last Request`、`Last Turn` 不会跟到新 workspace。
   同时确认菜单顶部会明确写出这是影响所有 Telegram 用户的全局切换，而不是当前聊天私有动作。
   额外在 `media_group` 仍处于收集窗口内时执行一次，确认 bot 会先明确提示已丢弃待发送上传，且这些旧附件不会在几百毫秒后误发到新 workspace。
12. 使用一个未授权 Telegram 账号访问 bot，确认提示会明确说明需要联系操作者开通访问。

## 会话生命周期

1. 发送普通文本，确认会创建或复用当前运行时中的 live session。
2. 点击 `New Session` 和 `Restart Agent`，确认会话被替换，旧 session 不再接收新请求。
   额外在 `media_group` 仍处于收集窗口内时分别执行一次，确认 bot 会先提示已丢弃待发送上传，而不是让旧附件继续流入新 session。
3. 打开 `Session History`，确认可以浏览、切换、分叉、重命名和删除本地历史会话。
   额外在本地历史为空时打开一次，确认空状态会直接给出 `New Session`、`Provider Sessions`（管理员）和 `Open Bot Status`，而不是只显示一句“没有历史”。
   再确认列表和详情都会明确解释 `Run`、`Fork`、`Run+Retry`、`Fork+Retry` 的差别，而不是只堆按钮缩写。
4. 如果 Provider 支持原生 session 浏览，打开 `Provider Sessions`，确认可以接管或分叉 provider 侧 session。
   再分别制造“当前 agent 不支持 provider session browsing”和“当前页暂时没有任何 provider session”两种空状态，确认视图会解释原因，并保留 `Refresh` 或 `Open Bot Status` 恢复入口，而不是只留一句空文案。
   同时确认列表和详情都会明确解释 `Run` 是接管 provider session、`Fork` 是基于它开新分支，`Run+Retry` / `Fork+Retry` 会在切换后立刻重放上一轮。
5. 验证 `Retry Last Turn` 和 `Fork Last Turn` 会复用上一轮保存的 replay payload。
6. 对一次普通成功回合，确认最终结果消息本身附带 `Retry Last Turn`、`Fork Last Turn`、`Open Bot Status` 和 `New Session`。
   再点击一次结果消息上的 `Open Bot Status`，确认 bot 会新发一条状态消息，而不是把原答案直接改写掉。
7. 人为制造一次会话切换、分叉或接管失败，确认提示会给出重试或重新打开对应视图的建议；如果失败发生在 `Session History` 或 `Provider Sessions` 内，bot 应恢复原列表而不是只显示通用失败短语。
   再制造一次“当前没有可复用 `Last Turn`，但仍有 `Last Request`”的失败态，确认恢复面板会改成 `Run Last Request` / `New Session` / `Open Bot Status`，而不是继续保留 `Retry Last Turn` / `Fork Last Turn` 死入口。
8. 在 `Session History` 或 `Provider Sessions` 里执行 `Run+Retry` 或 `Fork+Retry` 后，再让上一轮在点击前失效，确认 bot 会在原列表里提示“先发送一条新请求”，而不是误报已经重试成功。
9. 在 `Retry Last Turn`、`Fork Last Turn`、以及带 `Switch+Retry` / `Fork+Retry` 的状态页快捷入口上，人为让上一轮在点击前失效，确认 bot 会原地恢复当前视图并提示先发送一条新请求，而不是误报“已经重试成功”。

## Workspace 与上下文

1. 打开 `Workspace Files`，确认可以浏览和预览当前 workspace 内的文本文件。
   再进入一个空目录，确认视图会保留 `Workspace Search` 和状态页恢复入口，而不是只显示 `[empty directory]`。
2. 打开 `Workspace Search`，确认可以用下一条文本作为搜索词并查看结果；取消待输入后仍保留 `Search Again` 和 `Open Bot Status` 恢复入口。
   在等待搜索词时误发一个附件或 sticker，确认提示会点名当前等待的是 `Workspace Search`，而不是只显示笼统的“等待纯文本”。
   再搜一个明确不存在的关键词，确认空结果视图会直接给出 `Search Again`、`Workspace Files` 和状态页恢复入口，而不是只留“没有匹配”。
3. 打开 `Workspace Changes`，确认可以查看当前 Git 变更和 diff 预览。
   再分别在“当前 workspace 不是 Git 仓库”和“Git 仓库但工作树干净”两种空状态下打开，确认视图会直接给出 `Workspace Files`、`Workspace Search` 和状态页恢复入口。
4. 从文件、搜索结果或变更中加入 `Context Bundle`，确认 bundle 可浏览、移除、清空和持续附着。
5. 在 `Context Bundle` 为空、或点击 `Ask With Last Request` 但当前 workspace 没有上一条请求时，确认提示会明确指向“先加上下文”或“先发送一条新请求”，而不是只显示空泛短语。
   其中 `Context Bundle` 为空时，确认视图本身也会提供 `Workspace Files`、`Workspace Search` 和 `Workspace Changes` 的直接入口。

## Agent 能力与检查视图

1. 打开 `Agent Commands`，确认显示的是当前 agent 暴露的命令，而不是 bot 自己的管理命令。
   再制造一个“当前没有任何可发现命令”的场景，确认空状态会解释这是“仍在发现中”或“agent 不暴露命令”，并保留 `Refresh` 与状态页恢复入口。
2. 打开 `Model / Mode`，确认可以查看并切换当前 live session 暴露的选项。
   同时确认页面会先显示当前 setup，并明确说明这次切换作用于当前 live session；主列表还应告诉用户可以直接切换，或先打开某个 choice 查看详情再决定。
   如果当前 session 只暴露 `Model` 或只暴露 `Mode`，再确认页面会直接说明另一半当前不可用，而不是静默少一半按钮。
   如果当前 session 两者都不暴露，再确认 bot 会把这件事解释成能力空状态，并保留 `Open Bot Status` 或返回按钮，而不是留下一张近乎空白的页面。
   再打开一个 choice 详情，确认页面会明确说明 `Use ...` 与 `Use ... + Retry` 的差别，而不是只给出字段列表。
3. 在打开 `Model / Mode` 后让当前 live session 失效，再执行一次切换或 `...+Retry`，确认 bot 会提示直接发送文本或附件来重新开始，并保留 `Reopen Model / Mode` 或状态页恢复入口，而不是只剩死路文案。
4. 从 `Bot Status -> Model / Mode` 进入后执行一次 `...+Retry`，再人为制造 replay 准备失败或 turn 失败，确认 bot 会回到状态页并提示失败，而不是误报“已经重试成功”。
5. 人为制造一次 `Bot Status` 里的只读视图打开失败，确认消息会保留 `Try Again` 和对应的返回按钮，而不是只剩失败文本。
6. 从 `Bot Status` 进入 `Session Info`、`Workspace Runtime`、`Usage`、`Last Request`、`Last Turn`、`Agent Plan`、`Tool Activity`，确认这些只读检查视图都能打开并返回。
   再从 `Last Request` 详情点击一次 `Run Last Request`，确认 bot 会执行该文本并回到状态页，而不是把用户困在只读详情页里。
   如果当前 workspace 还有上一轮可复用 turn，再确认 `Last Request` 详情会同时提供 `Retry Last Turn` / `Fork Last Turn`，把“重跑文本”和“恢复整轮上下文”区分清楚。

## 附件与长回合

1. 发送图片、音频、视频和文档，确认支持的类型直接进入 ACP；不支持的类型会明确提示已保存到当前 workspace，并给出 `Open Context Bundle` / `Open Bot Status` 恢复入口。
   再发送一个超过 `8 MiB` 的文件，确认 bot 会明确说明超过上传上限并提示压缩或改发更小文件，且不会把任何内容发给 agent。
2. 发送同一 `media_group_id` 的多附件，确认它们被合并为一次 ACP 回合。
   再打开一次 `Bot Status`，确认在附件组尚未真正送出前，状态顶部会明确显示这是待发送上传，并提供 `Discard Pending Uploads`。
3. 发起一个持续几秒的请求，确认 `Bot Status` 能显示运行中状态并允许 `Stop Turn`。
4. 发起一个足够长的流式回合，确认 Telegram Draft 预览在超长时会明确用省略号表示当前只显示尾部进度，而不是无提示截断。
   再人为模拟一次 Draft 预览不可用，确认 bot 仍会立即发出“正在处理”的普通消息，而不是在最终回复前完全静默。
5. 发送一条足够长、会超过 Telegram 文本上限的请求结果，确认 bot 会优先按段落、换行或空格分段，而不是把一句完整的话硬切成难读碎片。
6. 人为制造一次附件降级后的 turn 失败，确认已落盘的文件仍保留在 `Context Bundle`，并提示用户无需重新上传即可恢复或重试。
7. 在 bot 正在等待纯文本动作时误发附件或 sticker，确认提示不仅会点名当前待完成的动作，还会明确说明这条误发消息没有转给 agent。
8. 发送 sticker、location、contact、poll、GIF、video note 或 dice，确认 bot 不会无响应，而是明确提示改发文本、图片、文档、音频或视频，并给出 `/help` 或 `/start` 的恢复路径；如果当前正在等待纯文本，仍应优先提示继续发送纯文本，并点名当前待完成的动作。

## 回归出口

1. 运行 `python -m talk2agent harness`。
2. 如果本轮修改覆盖多个交互面，再补做与改动直接相关的人工场景。
3. 验证通过后再同步到 GitHub。
