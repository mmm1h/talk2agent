# Manual Checklist

这份清单面向人类做端到端验收。
它只保留高价值场景，不重复底层设计文档。

## 准备

1. 准备一个允许访问的管理员 Telegram 账号。
2. 准备至少一个可切换的 workspace，其中最好包含一个 Git 仓库。
3. 启动 bot，并确认当前 Provider 能成功创建 ACP session。

## 启动与全局运行时

1. 发送 `/start`，确认会返回欢迎页和主键盘，且不会隐式创建新 session。
   同时确认主键盘只保留四行高频动作：前两行是 `New Session`、`Bot Status`、`Retry Last Turn` 和 `Fork Last Turn`，第三行是 `Workspace Search` / `Context Bundle`，第四行是 `Help` / `Cancel / Stop`；`Session History`、`Model / Mode`、`Agent Commands`、`Workspace Files` / `Workspace Changes`、`Restart Agent`，以及管理员的 `Switch Agent` / `Switch Workspace` 都不再常驻主键盘，而是统一进入 `Bot Status`。另外确认消息顶部会先显示 `Status`、`Recommended next step` 与 `Primary controls right now`，再给出更产品化的 `Quick paths` 引导和 `/start`、`/status`、`/help`、`/cancel` 恢复提醒；如果当前 workspace 还留有 `Last Request`、`Last Turn` 或 `Context Bundle`，再确认欢迎页会额外给出 `Resume snapshot`，直接解释哪些内容可以继续复用。若当前还有可恢复内容，或 bot 正卡在运行中 turn / 待输入 / 待发送附件组，再确认 `/start` 会额外补一张 `Quick actions` 卡片，并把对应恢复按钮直接挂出来，同时始终保留 `Open Bot Status` 作为完整控制台回退入口；如果当前还留有 `Context Bundle` 且 `Bundle Chat` 尚未开启，也确认这张卡片会直接给出 `Start Bundle Chat`。
2. 发送 `/help`，确认会返回快速使用指南和恢复入口，且不会隐式创建新 session。
   同时确认消息顶部也会先显示当前状态和建议下一步，而不是一上来就铺满运行时细节，并明确说明本地 slash 恢复入口始终可用；帮助页正文还应补上 `Common tasks` 与 `Core concepts`，把 `Run Last Request`、`Retry / Fork Last Turn`、`Context Bundle`、`Bundle Chat` 这些术语解释清楚；如果当前 workspace 仍有可恢复内容，再确认帮助页也会显示 `Resume snapshot`，并额外补一张 `Quick actions` 卡片，把最相关的恢复按钮直接挂在帮助页下方，同时保留 `Open Bot Status` 这个完整控制台入口；如果当前只差一步就能继续使用已有 `Context Bundle`，也确认这里会直接给出 `Start Bundle Chat`。
3. 发送 `/status`，确认会直接打开 `Bot Status`，且不会隐式创建新 session；在主键盘被 Telegram 折叠时，这条命令仍然可作为只读恢复入口。
4. 在待输入、运行中 turn 和 Bundle Chat 三种状态下分别发送 `/cancel`，并额外测试一次主键盘 `Cancel / Stop`，确认都会按优先级执行本地取消，而不是误发给 agent。
   额外验证一次 `media_group` 还在收集窗口内时立刻 `/cancel`，确认附件组会被直接丢弃，而不是延迟几百毫秒后仍然发给 agent。
   再额外验证一次 `media_group` 还在收集窗口内时发送普通文本或单独附件，确认 bot 会明确提示这条新消息没有发出去，并继续保留原附件组，而不是把相册悄悄冲掉。
5. 打开 Telegram slash 菜单，确认固定可见 `/start`、`/status`、`/help`、`/cancel`，并且当前 agent 暴露的命令会追加在后面。
6. 人为制造一次关键失败态，例如新 session 创建失败或切换失败，确认提示给出恢复建议，而不是直接暴露内部错误短语。
7. 触发一个已失效的旧 inline button，确认 bot 会提示这是旧菜单上的按钮，并建议重新打开最近视图或使用 `/start`，而不是只弹出无方向的过期提示。
8. 发送一个伪造或跨用户的 callback，确认 bot 会给出“重新打开最近视图”或“从自己的聊天里重新打开菜单 / 使用 `/start`”之类的纠正提示，而不是只显示模糊的系统短语。
9. 点击 `Bot Status`，确认它是只读入口，不会隐式创建新 session。
   同时确认顶部会前置当前状态下的主动作，例如 `Stop Turn`、`Cancel Pending Input`、`Discard Pending Uploads`、`Ask Agent With Context`、`Run Last Request` 或 `Retry Last Turn`。
   再确认正文被分成 `Current runtime`、`Resume and memory`、`Workspace context`、`Agent capabilities` 和 `Controls` 这类可扫读分段，而不是一整块无层次长文本。
   再确认 `Controls` 段会补一组 `Action guide`，把 `Refresh / Session History / Provider Sessions`、`New Session / Restart Agent / Fork Session`、`Model / Mode / Agent Commands`、`Workspace Files / Search / Changes / Context Bundle` 这些成组入口先解释清楚，而不是只留按钮名。
   同时确认状态页里会动态出现的密集 inline controls 也按双列拆开；像 `Refresh` / `Session History` / `Provider Sessions` 或一组恢复动作都不应再被挤进单行。
   如果当前 turn 仍在运行，再确认状态页会显示 `Turn elapsed`；如果当前正在等待纯文本输入，再确认状态页会显示 `Next plain text`，让用户知道下一条该发什么。
   如果当前 workspace 已缓存 `Last Request`，再确认状态页会显示它的来源摘要，并提供 `Run Last Request`；这个入口应只重跑请求文本，而不是隐式恢复旧附件或旧上下文。
   如果该 `Last Request` 或 `Last Turn` 最初记录在另一个 Provider 上，再确认状态页会明确提示这次重放将落到当前 Provider，而不是让用户自己猜。
10. 执行 `Switch Agent`，确认切换前有预检，切换菜单会明确提示旧按钮 / 待输入会被清理、`Context Bundle` 不会跟随切换，而同 workspace 下的 `Last Turn` / `Last Request` 仍可继续复用；切换后旧 UI 动作立即失效。
   同时确认菜单顶部会明确写出这是影响所有 Telegram 用户的全局切换，而不是当前聊天私有动作。
   再确认菜单会显示 `Available agents`，但第一层只负责列出目标；点进单个 provider 后，详情页才会集中展示能力摘要、`Switch to ...`、`Retry on ...`、`Fork on ...`，避免管理员在列表页误触就立刻切换。
   从 `Bot Status` 进入这条流时，再确认切换成功或失败后仍会停留在当前目标详情页，并保留 `Switch to ...` 或 `Back to Switch Agent` 这类下一步按钮；如果入口来自状态页内的其他恢复动作，则应回到状态页并保留同样的 notice。
   额外在 `media_group` 仍处于收集窗口内时执行一次，确认 bot 会先明确提示已丢弃待发送上传，且这些旧附件不会在几百毫秒后误发到新 agent。
11. 执行 `Switch Workspace`，确认只显示白名单 workspace，并且切换跨重启持久化；切换菜单和成功回显都要明确提示 workspace 作用域内的 `Context Bundle`、`Last Request`、`Last Turn` 不会跟到新 workspace。
   同时确认菜单顶部会明确写出这是影响所有 Telegram 用户的全局切换，而不是当前聊天私有动作。
   再确认菜单会显示 `Configured workspaces`，并且点进单个 workspace 后要先看到目标详情与影响说明，再通过 `Switch to ...` 真正确认切换，避免管理员在多 workspace 环境下误切。
   从 `Bot Status` 进入这条流时，再确认切换成功或失败后仍会停留在当前目标详情页，并保留 `Switch to ...` 或 `Back to Switch Workspace` 这类下一步按钮；如果入口来自状态页内的其他恢复动作，则应回到状态页并保留同样的 notice。
   额外在 `media_group` 仍处于收集窗口内时执行一次，确认 bot 会先明确提示已丢弃待发送上传，且这些旧附件不会在几百毫秒后误发到新 workspace。
12. 使用一个未授权 Telegram 账号访问 bot，确认提示会明确说明需要联系操作者开通访问。

## 会话生命周期

1. 发送普通文本，确认会创建或复用当前运行时中的 live session。
2. 点击 `New Session` 和 `Restart Agent`，确认会话被替换，旧 session 不再接收新请求。
   额外在 `media_group` 仍处于收集窗口内时分别执行一次，确认 bot 会先提示已丢弃待发送上传，而不是让旧附件继续流入新 session。
   如果当前 workspace 还留有 `Last Request`、`Last Turn` 或 `Context Bundle`，再确认成功回显会明确说明哪些内容仍可复用；若 `Bundle Chat` 仍开启，也要明确提醒下一条纯文本仍会自动带上当前 bundle，避免把“新 session”误解成“所有上下文已清空”。
3. 打开 `Session History`，确认可以浏览、切换、分叉、重命名和删除本地历史会话。
   额外在本地历史为空时打开一次，确认空状态会直接给出 `New Session`、`Provider Sessions`（管理员）和 `Open Bot Status`，并补一行 `Recommended next step`，而不是只显示一句“没有历史”。
   如果当前 workspace 还留有 `Last Request`、`Last Turn` 或 `Context Bundle`，再确认这个空状态会继续先补一行更贴近当前可复用内容的 `Recommended next step`，再补上 `Recovery options`，并直接给出 `Run Last Request`、`Retry / Fork Last Turn`、`Ask Agent With Context`、`Bundle + Last Request` 或 `Open Context Bundle`。
   再确认列表和详情都会先补一行 `Recommended next step`，并明确解释 `Run`、`Fork`、`Run+Retry`、`Fork+Retry` 的差别，而不是只堆按钮缩写。
   同时确认 `Run` / `Open` / `Rename` / `Delete` 以及 `...+Retry` / `Fork` 这些密集动作不会再被挤进单行，而是按双列拆开，保证手机端仍然可点可读。
   如果历史记录超过一页，再确认页首会显示 `Local sessions`、`Showing` 和 `Page`，让用户知道总共有多少条、当前页覆盖哪一段，而不是只剩 `Prev` / `Next`。
4. 如果 Provider 支持原生 session 浏览，打开 `Provider Sessions`，确认可以接管或分叉 provider 侧 session。
   再分别制造“当前 agent 不支持 provider session browsing”和“当前页暂时没有任何 provider session”两种空状态，确认视图会解释原因，并保留 `Refresh` 或 `Open Bot Status` 恢复入口，同时补一行 `Recommended next step`，而不是只留一句空文案。
   如果当前 workspace 还留有 `Last Request`、`Last Turn` 或 `Context Bundle`，再确认这些 provider-session 空状态也会继续先补一行更贴近当前可复用内容的 `Recommended next step`，再补上 `Recovery options`，让管理员能直接继续工作，而不用先退回 `Bot Status`。
   同时确认列表和详情都会先补一行 `Recommended next step`，并明确解释 `Run` 是接管 provider session、`Fork` 是基于它开新分支，`Run+Retry` / `Fork+Retry` 会在切换后立刻重放上一轮。
   再确认这些高密度动作同样按双列拆开，而不是把 `Run+Retry`、`Fork`、`Fork+Retry` 全塞进一排。
   如果 provider session 列表存在多页，再确认页首会显示当前页加载数量和 `Cursor page`，避免管理员翻页后失去方向感。
5. 验证 `Retry Last Turn` 和 `Fork Last Turn` 会复用上一轮保存的 replay payload。
6. 对一次普通成功回合，确认最终结果消息本身附带 `Retry Last Turn`、`Fork Last Turn`、`Open Bot Status` 和 `New Session`。
   如果当前 workspace 还保留 `Context Bundle`，再确认结果消息会额外附上 `Start / Stop Bundle Chat` 与 `Open Context Bundle`，把“继续沿用上下文”与“退出持续上下文模式”直接放在答案旁边。
   再点击一次结果消息上的 `Open Bot Status`，确认 bot 会新发一条状态消息，而不是把原答案直接改写掉。
   同时分别点击一次结果消息上的 `Open Context Bundle` 与 `Start / Stop Bundle Chat`，确认它们也会新发恢复消息，而不是把原答案直接改写掉。
7. 人为制造一次会话切换、分叉或接管失败，确认提示会给出重试或重新打开对应视图的建议；如果失败发生在 `Session History` 或 `Provider Sessions` 内，bot 应恢复原列表而不是只显示通用失败短语。
   再制造一次“当前没有可复用 `Last Turn`，但仍有 `Last Request`”的失败态，确认恢复面板会改成 `Run Last Request` / `New Session` / `Open Bot Status`，而不是继续保留 `Retry Last Turn` / `Fork Last Turn` 死入口。
8. 在 `Session History` 或 `Provider Sessions` 里执行 `Run+Retry` 或 `Fork+Retry` 后，再让上一轮在点击前失效，确认 bot 会在原列表里提示“先发送一条新请求”，而不是误报已经重试成功。
9. 在 `Retry Last Turn`、`Fork Last Turn`、以及带 `Switch+Retry` / `Fork+Retry` 的状态页快捷入口上，人为让上一轮在点击前失效，确认 bot 会原地恢复当前视图并提示先发送一条新请求，而不是误报“已经重试成功”。
   再额外从主键盘直接点击一次 `Retry Last Turn` 或 `Fork Last Turn`，在当前没有可复用 `Last Turn` 的情况下，确认 bot 会打开带 notice 的 `Bot Status` 并给出 `Run Last Request`、`Session History`、`New Session` 等恢复入口，而不是只回复一条死提示。

## Workspace 与上下文

1. 打开 `Workspace Files`，确认可以浏览和预览当前 workspace 内的文本文件。
   再进入一个空目录，确认视图会保留 `Workspace Search` 和状态页恢复入口，而不是只显示 `[empty directory]`。
2. 打开 `Workspace Search`，确认可以用下一条文本作为搜索词并查看结果；取消待输入后仍保留 `Search Again` 和 `Open Bot Status` 恢复入口。
   在等待搜索词时误发一个附件或 sticker，确认提示会点名当前等待的是 `Workspace Search`，而不是只显示笼统的“等待纯文本”。
   再搜一个明确不存在的关键词，确认空结果视图会直接给出 `Search Again`、`Workspace Files` 和状态页恢复入口，而不是只留“没有匹配”。
3. 打开 `Workspace Changes`，确认可以查看当前 Git 变更和 diff 预览。
   再分别在“当前 workspace 不是 Git 仓库”和“Git 仓库但工作树干净”两种空状态下打开，确认视图会直接给出 `Workspace Files`、`Workspace Search` 和状态页恢复入口。
   再分别检查 `Workspace Files`、`Workspace Search`、`Workspace Changes` 以及单文件 / 单变更预览，确认页面正文会直接解释 `Ask Agent ...`、`Ask With Last Request`、`Start Bundle Chat ...`、`Add ... to Context` 或 `Remove From Context` 的差别，而不是只堆动作按钮。
   当当前 workspace 已缓存 `Last Request` 时，再确认单文件 / 单变更预览页会先补一行 `Recommended next step`，并把 `Ask With Last Request` 放到首行按钮，而不是埋在第二排。
4. 从文件、搜索结果或变更中加入 `Context Bundle`，确认 bundle 可浏览、移除、清空和持续附着。
   再分别打开非空的 `Workspace Files`、`Workspace Search`、`Workspace Changes` 和 `Context Bundle`，确认页首都会补一行 `Recommended next step`，把“先打开单项检查”和“已经可以直接问 agent / 加入上下文”明确区分开，而不是让用户自己从动作说明里猜第一步。
   当 bundle 非空时，再确认页面会直接解释 `Ask Agent With Context`、`Ask With Last Request` 和 `Start / Stop Bundle Chat` 各自会做什么，而不是只显示动作按钮。
   当 bundle 非空且当前 workspace 还留有 `Last Request` 时，再确认首行按钮会优先放 `Ask With Last Request`，把“直接复用已保存文本”放在需要继续输入新文本的动作前面。
   对 `Workspace Files`、`Workspace Search`、`Workspace Changes` 和 `Context Bundle` 这些列表页，再分别制造一次超过一页的场景，确认页首会显示 `Entries` / `Matches` / `Changes` / `Items`，以及 `Showing` 和 `Page`，而不是只留翻页按钮。
5. 在 `Context Bundle` 为空、或点击 `Ask With Last Request` 但当前 workspace 没有上一条请求时，确认提示会明确指向“先加上下文”或“先发送一条新请求”，而不是只显示空泛短语。
   其中 `Context Bundle` 为空时，确认视图本身也会提供 `Workspace Files`、`Workspace Search` 和 `Workspace Changes` 的直接入口。

## Agent 能力与检查视图

1. 打开 `Agent Commands`，确认显示的是当前 agent 暴露的命令，而不是 bot 自己的管理命令。
   再制造一个“当前没有任何可发现命令”的场景，确认空状态会解释这是“仍在发现中”或“agent 不暴露命令”，并保留 `Refresh` 与状态页恢复入口。
   如果命令列表超过一页，再确认页首会显示 `Commands`、`Showing` 和 `Page`，让用户知道当前只看到哪一段命令。
2. 打开 `Model / Mode`，确认可以查看并切换当前 live session 暴露的选项。
   同时确认页面会先显示当前 setup、补一行 `Recommended next step`，并明确说明这次切换作用于当前 live session；主列表还应告诉用户可以直接切换，或先打开某个 choice 查看详情再决定。
   再确认主列表会用 `Action guide` 解释 `Model: ...` / `Mode: ...`、`...+Retry` 和 `Open Model N` / `Open Mode N` 的差别，而不是只把按钮名称堆出来。
   如果当前 session 只暴露 `Model` 或只暴露 `Mode`，再确认页面会直接说明另一半当前不可用，而不是静默少一半按钮。
   如果当前 session 两者都不暴露，再确认 bot 会把这件事解释成能力空状态，并保留 `Open Bot Status` 或返回按钮，而不是留下一张近乎空白的页面。
   再打开一个 choice 详情，确认页面会明确说明 `Use ...` 与 `Use ... + Retry` 的差别，而不是只给出字段列表。
3. 在打开 `Model / Mode` 后让当前 live session 失效，再执行一次切换或 `...+Retry`，确认 bot 会提示直接发送文本或附件来重新开始，并保留 `Reopen Model / Mode` 或状态页恢复入口，而不是只剩死路文案。
4. 从 `Bot Status -> Model / Mode` 进入后执行一次 `...+Retry`，再人为制造 replay 准备失败或 turn 失败，确认 bot 会回到状态页并提示失败，而不是误报“已经重试成功”。
5. 人为制造一次 `Bot Status` 里的只读视图打开失败，确认消息会保留 `Try Again` 和对应的返回按钮，而不是只剩失败文本。
6. 从 `Bot Status` 进入 `Session Info`、`Workspace Runtime`、`Usage`、`Last Request`、`Last Turn`、`Agent Plan`、`Tool Activity`，确认这些只读检查视图都能打开并返回。
   再从 `Last Request` 详情点击一次 `Run Last Request`，确认 bot 会执行该文本并回到状态页，而不是把用户困在只读详情页里。
   如果当前 workspace 还有上一轮可复用 turn，再确认 `Last Request` 详情会同时提供 `Retry Last Turn` / `Fork Last Turn`，把“重跑文本”和“恢复整轮上下文”区分清楚。
   如果 `Last Request` 或 `Last Turn` 记录自另一个 Provider，再确认详情页会直接写明“Recorded provider` 与 `Current provider` 的差异，以及当前重放到底会发往哪里”，避免跨 runtime 误会。
   同时确认 `Last Request` / `Last Turn` 详情页都会先补一行 `Recommended next step`，并且 `Last Turn` 详情会额外解释 `Retry Last Turn` 是在当前 live session 里重放整轮 payload，而 `Fork Last Turn` 会先开新 session 再重放。
   在“当前仍有 live session，且 workspace 还留有 `Last Request`、`Last Turn` 或 `Context Bundle`”的场景下，再打开 `Session Info` 和 `Usage`，确认它们同样会补上 `Recommended next step` 与 `Recovery options`，把 `Run Last Request`、`Retry / Fork Last Turn`、`Ask Agent With Context`、`Bundle + Last Request` 或 `Open Context Bundle` 直接留在页内，而不是逼用户先退回 `Bot Status`。
   同时确认 `Session Info` 的辅助入口会拆成多行，而不是把 `Last Request`、`Last Turn`、`Agent Commands`、`Agent Plan`、`Tool Activity` 挤在单行按钮里。
   再打开 `Workspace Runtime` 和单个 MCP server 详情，确认这两页同样会先补一行 `Recommended next step`，明确当前更适合继续打开 server 看 transport / key 名，还是直接返回状态页。
   再打开非空的 `Agent Commands`、`Agent Plan`、`Tool Activity`，确认页首也会补一行 `Recommended next step`，把“直接执行 / 打开详情 / 刷新等待更新”的首选路径写清楚，而不是让用户自己从按钮名里猜。
   同时确认 `Agent Commands` 会直接解释 `Run N`、`Args N`、`Open N` 的差别，`Agent Plan` / `Tool Activity` 详情页也会明确提示当前更适合 `Refresh`、回到列表继续比对，还是先打开相关文件 / diff。
   如果 `Last Turn`、`Agent Plan` 或 `Tool Activity` 超过一页，再确认页首会显示总数、`Showing` 和 `Page`，避免只读排查长列表时失去位置感。
   额外在“当前没有 live session，但 workspace 还留有 `Last Request`、`Last Turn` 或 `Context Bundle`”的场景下，再打开 `Session Info`、`Usage` 或无命令的 `Agent Commands`，确认页面会补上 `Recovery options`，并直接给出 `Run Last Request`、`Retry / Fork Last Turn`、`Ask Agent With Context` 或 `Bundle + Last Request`，而不是只剩返回按钮。
   再额外制造一次“刚刚还能点开 `Last Request` / `Last Turn` / `Agent Plan` / `Tool Activity`，但实际打开时数据已经失效或暂时为空”的场景，确认页面不会只剩一句 `No ...`；它仍会给出 `Run Last Request`、`Retry / Fork Last Turn`、`Ask Agent With Context`、`Bundle + Last Request` 或 `Refresh` 这类下一步动作，而不是把用户困在空页里。

## 附件与长回合

1. 发送图片、音频、视频和文档，确认支持的类型直接进入 ACP；不支持的类型会明确提示已保存到当前 workspace，并给出 `Open Context Bundle` / `Open Bot Status` 恢复入口。
   再发送一个超过 `8 MiB` 的文件，确认 bot 会明确说明超过上传上限并提示压缩或改发更小文件，且不会把任何内容发给 agent。
2. 发送同一 `media_group_id` 的多附件，确认它们被合并为一次 ACP 回合。
   再打开一次 `Bot Status`，确认在附件组尚未真正送出前，状态顶部会明确显示这是待发送上传，并提供 `Discard Pending Uploads`。
3. 发起一个持续几秒的请求，确认 `Bot Status` 能显示运行中状态并允许 `Stop Turn`。
   再在 turn 运行中分别发送一条普通文本、一个单附件和一个 Telegram 相册，确认 bot 都会立即提示“这条新消息没有发出去”，而不是悄悄排队；其中被挡住的文本也不应覆盖已有 `Last Request`。
   再执行一次 `/cancel` 或 `Stop Turn`，确认最终的取消回显本身会带上 `Retry Last Turn`、`Fork Last Turn`、`Open Bot Status` 和 `New Session`，而不是只剩一条终点文案。
   再分别取消一次 `Workspace Search`、`Bundle Chat` 和“其实没有任何东西可取消”的空状态，确认 bot 都会继续补一张 `Quick actions` 卡片，把 `Search Again`、`Ask Agent With Context`、`Run Last Request`、`Open Bot Status` 或 `New Session` 这类下一步动作直接放出来，而不是让用户自己回忆接下来该点哪里。
4. 发起一个足够长的流式回合，确认 Telegram Draft 预览在超长时会明确用省略号表示当前只显示尾部进度，而不是无提示截断。
   再人为模拟一次 Draft 预览不可用，确认 bot 仍会立即发出“正在处理”的普通消息，而不是在最终回复前完全静默。
5. 发送一条足够长、会超过 Telegram 文本上限的请求结果，确认 bot 会优先按段落、换行或空格分段，而不是把一句完整的话硬切成难读碎片。
6. 人为制造一次附件降级后的 turn 失败，确认已落盘的文件仍保留在 `Context Bundle`，并提示用户无需重新上传即可恢复或重试。
7. 在 bot 正在等待纯文本动作时误发附件或 sticker，确认提示不仅会点名当前待完成的动作，还会明确说明这条误发消息没有转给 agent。
   同时确认这类阻断提示会直接附上 `Stop Turn`、`Cancel Pending Input`、`Discard Pending Uploads` 或 `Open Bot Status` 之类的恢复按钮，而不是只给文字说明。
8. 发送 sticker、location、contact、poll、GIF、video note 或 dice，确认 bot 不会无响应，而是明确提示改发文本、图片、文档、音频或视频，并给出 `/help` 或 `/start` 的恢复路径；如果当前正在等待纯文本，仍应优先提示继续发送纯文本，并点名当前待完成的动作。
   对附件过大、附件降级失败等本地校验错误，也确认提示会保留 `Open Bot Status` 恢复入口。
   再发送一条只包含空格或换行的纯文本，确认 bot 会明确提示这条消息在去掉空白后为空，且不会启动新 session 或新 turn。

## 回归出口

1. 运行 `python -m talk2agent harness`。
2. 如果本轮修改覆盖多个交互面，再补做与改动直接相关的人工场景。
3. 验证通过后再同步到 GitHub。
