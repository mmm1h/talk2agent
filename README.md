# talk2agent

这是一个 Telegram 轮询机器人 MVP，用于将已授权用户的纯文本、图片、语音、音频、视频和文档消息转发到基于 ACP 的智能体会话中，并通过 Telegram Draft API 持续回传流式响应。

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

2. 安装你要使用的 Provider。默认模板以 `codex` 启动，所以本地最容易跑通的基线是：

   ```bash
   npm install -g @zed-industries/codex-acp
   ```

   如果希望在同一个机器人进程里切换到 Gemini：

   ```bash
   npm install -g @google/gemini-cli
   ```

   如果 Provider 依赖环境变量鉴权或代理，例如 `OPENAI_API_KEY`、`CODEX_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY`、`HTTP_PROXY`，要在启动 `talk2agent` 之前先设置好；机器人会把自己的进程环境传给 Provider 子进程。

3. 生成一个初始配置：

   ```bash
   talk2agent init --config config.yaml
   ```

4. 编辑 `config.yaml`：

   - 设置 `telegram.bot_token`
   - 替换 `telegram.allowed_user_ids`
   - 将 `telegram.admin_user_id` 设为其中一个允许的用户 ID
   - 选择启动时的 `agent.provider`：`claude`、`codex` 或 `gemini`
   - 配置 `agent.workspaces` 白名单；`agent.workspace_dir` 必须对应其中一个默认 workspace

5. 启动机器人：

   ```bash
   talk2agent start --config config.yaml
   ```

   模块入口与上面等价：

   ```bash
   python -m talk2agent start --config config.yaml
   ```

## 主要交互

- Telegram 私聊输入支持：
  - 纯文本消息
  - 图片消息（caption 会和图片一起进入同一轮 ACP prompt）
  - 语音和音频消息（会转成 ACP audio block）
  - 视频消息（会优先转成 ACP binary resource；当前 provider 不支持 embedded context 时会自动降级到 workspace inbox）
  - 文档消息（文本类文档会以内嵌文本资源提交；如果当前 provider 不支持文档资源块，会自动降级为内联纯文本；其他文档会以内嵌二进制资源提交）
  - 同一 `media_group_id` 下的多附件会自动合并为一次 ACP 回合
  - 如果当前用户已开启 `Bundle Chat`，附件和 `media_group` 回合同样会自动附着当前 `Context Bundle`
  - 如果当前 provider 不支持图片、音频、视频或其他二进制附件对应的 ACP prompt 能力，bot 会把附件保存到当前 workspace 的 `.talk2agent/telegram-inbox/`，再要求 agent 从本地磁盘读取；成功提交后，这些落盘文件也会自动加入当前用户的 `Context Bundle`
  - 如果当前 provider 不支持对应 ACP prompt 能力且没有安全降级路径，bot 会返回明确提示，而不是通用失败
  - 单个附件大小上限为 `8 MiB`

- Reply keyboard 常驻按钮：
  - `Bot Status`
  - `New Session`
  - `Retry Last Turn`
  - `Fork Last Turn`
  - `Session History`
  - `Agent Commands`
  - `Model / Mode`
  - `Workspace Files`
  - `Workspace Search`
  - `Workspace Changes`
  - `Context Bundle`
  - `Restart Agent`
  - `Switch Agent`（仅管理员可见）
  - `Switch Workspace`（仅管理员可见）
- `Switch Agent` 通过 inline button 在 `Claude Code`、`Codex`、`Gemini CLI` 之间切换；切换前会先做可执行文件与 ACP session 创建预检，失败时返回 `session creation failed`
- `Switch Agent` 菜单会在当前 workspace 下预先展示每个 provider 的 prompt/session 能力摘要，例如图片、音频、文档输入支持，以及 provider session list/resume 支持面
- 如果当前 workspace 下存在可重放的上一轮请求，`Switch Agent` 菜单还会为每个目标 provider 额外提供一键 `Retry on ...` / `Fork on ...` 入口：先执行全局 agent 切换，再立刻在目标 provider 上重试或分叉上一轮
- `Switch Workspace` 只展示配置白名单中的路径；切换同样是全局行为，并且会跨重启持久化
- 当 `Switch Agent` 或 `Switch Workspace` 成功后，旧 inline buttons、待输入文本动作、agent command alias、media group 缓冲和已开启的 `Bundle Chat` 会立即失效，避免把旧界面的操作送进新运行时
- `Bot Status` 会用只读方式汇总当前 Provider、Workspace、当前 workspace 的 Git 变更摘要、当前 live session、当前 live session 在本地 history 中的标题（如果已有）、model/mode、待输入状态摘要、本地 session history 数量、可重放上一轮及其短摘要、最近一次普通文本请求摘要、`Context Bundle` 项数与 `Bundle Chat` 状态；当当前 workspace 是 Git 仓库且存在工作区变更时，状态页还会直接展示前几项 change 预览和剩余数量，方便手机端快速确认本地工作区脏状态，并可直接在状态页里对“当前变更集”执行 `Ask Agent...`、`Ask With Last Request`、`Add All Changes to Context`、`Start Bundle Chat With Changes`；当 bundle 非空时，状态页还会直接展示前几项 bundle 预览和剩余数量，方便手机端在不跳转子页面的情况下确认当前工作上下文，并可直接在状态页里执行 `Ask Agent With Context`、`Bundle + Last Request`、`Clear Bundle`；当 live session 已经缓存 agent commands 时，状态页也会直接展示前几项命令预览和剩余数量，方便快速判断当前 agent 能做什么，并为前几条已缓存命令直接提供 `Run /...` 或 `Args /...` 快捷按钮，减少进入完整命令页的往返；如果当前 live session 已经暴露真实 model / mode 选项，状态页也会直接给出前几个可切换的 `Model: ...` / `Mode: ...` 按钮，切换后原状态消息会立即刷新为最新运行时快照；当当前 workspace 还存在可重放的上一轮请求时，这些常用选项还会额外提供 `Model+Retry: ...` / `Mode+Retry: ...` 快捷按钮，允许用户在手机端直接完成“切配置并立即重跑上一轮”；而当当前 Provider + Workspace 下已有其他本地历史会话时，状态页也会直接展示最近几条 session 预览，并提供 `Switch ...` / `Switch+Retry ...` 快捷按钮，让用户无需先进入完整 `Session History` 即可切换工作线。它本身不会为查看状态而主动拉起新 session，但会提供通往 `Session History`、`Agent Commands`、`Workspace Files`、`Workspace Search`、`Workspace Changes` 和 `Context Bundle` 的快捷入口，并允许直接 `Cancel Pending Input`、对当前 bundle 执行 `Start/Stop Bundle Chat`、在当前消息里直接打开 `Model / Mode` 菜单，以及直接触发 `New Session`、`Retry/Fork Last Turn`、`Restart Agent`；管理员还可直接从这里进入 `Provider Sessions`、`Switch Agent` / `Switch Workspace`。这些直接控制、子视图和内联菜单都会尽量在当前状态消息内刷新，并支持 `Back to Bot Status` 返回当前运行时总览；从状态页直接执行 `Retry/Fork Last Turn`，或进入 `Session History`、`Provider Sessions`、`Model / Mode`、`Switch Agent` 后执行带重放的快捷操作会回到状态页；从状态页打开 `Workspace Search` 后输入搜索词，也会继续在原状态消息里展开结果；而从状态页打开的 `Agent Commands` 参数输入、`Workspace Files` / `Workspace Changes` / `Context Bundle` 里的两段式 `Ask Agent...` 请求，以及这些视图里的 `Ask With Last Request`，在成功后都会回到状态页；如果这些从状态页发起的回合在真正执行 turn 时失败，原状态消息也会恢复为最新 `Bot Status`，避免旧 inline 视图悬空
- `Session History` 只显示当前 Provider + 当前 Workspace 下、当前 Telegram 用户自己在本 bot 中使用过的会话；当前活跃会话会被标记为 `[current]`，每条记录支持 `Run`、`Rename` 和 `Delete`；如果当前 workspace 下存在可重放的上一轮请求，还会额外提供 `Run+Retry`
- 通过 `New Session`、`Restart Agent` 或 `Model / Mode` 首次拉起的本地 live session，会立即写入本地 `Session History`，不需要先跑一轮 prompt 才出现
- 管理员可在 `Session History` 视图内继续打开 `Provider Sessions`，浏览当前 workspace 下 provider 原生保存的 ACP sessions，并把其中某个 session 接管到自己当前的 Telegram 会话槽位；接管成功后会同步写回本 bot 的本地 history；如果当前 workspace 下存在可重放的上一轮请求，provider session 列表里同样会额外提供 `Run+Retry`
- 当 `New Session`、`Restart Agent`、`Session History -> Run` 或 `Provider Sessions -> Run` 成功替换当前 live session 后，旧 inline buttons、待输入文本动作、agent command alias 和 media group 缓冲会立即失效；`Context Bundle` 与已开启的 `Bundle Chat` 会保留，因为它们的作用域仍然是当前 Provider + Workspace
- 当在 `Session History` 里删除当前 `[current]` live session 时，也会立即清理与该 session 绑定的旧 inline buttons、待输入文本动作、agent command alias 和 media group 缓冲，并把当前用户的 Telegram slash command 菜单刷新回当前 Provider 的默认发现结果；`Context Bundle` 与已开启的 `Bundle Chat` 仍保留
- 当某次普通文本、命令或附件回合因为 provider / session 异常而导致 bot 主动失效当前 live session 时，也会立即清理与该 session 绑定的旧 inline buttons、待输入文本动作、agent command alias 和 media group 缓冲，并把当前用户的 Telegram slash command 菜单刷新回当前 Provider 的默认发现结果；`Context Bundle` 与已开启的 `Bundle Chat` 仍保留
- `Retry Last Turn` 会在当前 workspace 作用域内重放上一条真正发给 ACP 的请求，覆盖普通文本、命令包装、上下文文件/变更请求、`Bundle Chat` 包装后的文本，以及附件 / media group 回合；如果当前 live session 已失效，bot 会先自动拉起新的 session 再重放；如果用户刚切换到另一个 agent，只要 workspace 没变，也可以直接把上一回合交给新 agent 再跑一遍
- `Fork Last Turn` 会在当前 workspace 下先创建一个新的 live session，再把上一条真正发给 ACP 的请求重放到这个新 session 中；旧 session 仍可通过 `Session History` 找回，相当于在手机端直接从上一回合分叉一条新工作线；如果用户刚切换到另一个 agent，只要 workspace 没变，也可以直接把上一回合分叉到新 agent 上
- 上述 session 异常失效后的失败消息会直接附带恢复入口：`Retry Last Turn`、`Fork Last Turn`、`New Session`、`Session History`、`Model / Mode`；管理员还会额外看到 `Switch Agent` 和 `Switch Workspace`
- 点击 `Rename` 后，用户下一条普通文本消息只会用于重命名该历史会话，不会被转发给当前 agent；也可以直接点击 `Cancel Rename` 放弃
- `Agent Commands` 会直接展示当前 ACP agent 暴露的真实 slash commands；无参命令可一键执行，带 hint 的命令会进入“下一条普通文本作为参数”的待输入状态，并支持 `Cancel Command`
- `Workspace Files` 会在当前白名单 workspace 内提供只读目录浏览和文本文件预览；目录导航和文件预览都不会离开当前 workspace 根目录，目录页既支持把当前页可见文件批量加入 `Context Bundle`，也支持直接用当前可见文件发起下一条 agent 请求，或一键用当前可见文件启动 `Bundle Chat`；如果当前 workspace 下已经有最近一次普通文本请求，目录页还会额外提供 `Ask With Last Request`，可直接把同一句需求重用到当前页可见文件集合
- 在文件预览页可以直接点击 `Ask Agent About File`；随后下一条普通文本会作为用户请求，与该相对路径一起转发给当前 agent，让 agent 在本地 workspace 中读取最新文件内容后继续工作。如果当前 workspace 下已经有最近一次普通文本请求，预览页还会额外提供 `Ask With Last Request`，可直接用同一句需求重问当前文件。预览页也支持直接用当前文件启动 `Bundle Chat`，或直接打开 `Context Bundle`；如果 bundle 是从当前目录页或文件预览页打开的，bundle 里也会保留返回该目录页或预览页的入口
- `Workspace Search` 会在当前白名单 workspace 内做只读全文搜索；点击按钮后，下一条普通文本会被当作搜索词，结果支持分页、打开命中文件、把当前命中的文件批量加入 `Context Bundle`、直接基于当前命中文件集合发起 agent 请求、直接用当前命中文件集合启动 `Bundle Chat`，以及 `Cancel Search`；如果当前 workspace 下已经有最近一次普通文本请求，结果页还会额外提供 `Ask With Last Request`，可直接把同一句需求重用到当前命中文件集合；如果 bundle 是从搜索结果页或命中文件预览页打开的，bundle 里也会保留返回搜索结果或预览页的入口
- `Workspace Changes` 会在当前 workspace 是 Git 仓库时展示工作区变更列表、diff 预览，并支持 `Ask Agent About Change`、`Ask Agent With Current Changes`、`Start Bundle Chat With Changes`、`Add All Changes to Context`；如果当前 workspace 下已经有最近一次普通文本请求，变更列表页和 diff 预览页都会额外提供 `Ask With Last Request`，可分别直接把同一句需求重用到“当前整批变更”或“当前 change”。diff 预览页也支持直接用当前 change 启动 `Bundle Chat`，或直接打开 `Context Bundle`；如果当前 workspace 不是 Git 仓库，会明确提示；如果 bundle 是从变更列表、diff 预览或变更 follow-up 打开的，bundle 里也会保留返回当前来源页的入口
- 文件预览页和 diff 预览页都支持把当前条目加入 `Context Bundle`；bundle 作用域固定为“当前 Provider + 当前 Workspace + 当前 Telegram 用户”
- `Context Bundle` 会把多个文件和 Git 变更聚合成一次 agent 请求；bundle 视图本身也支持重新打开其中某个文件或变更进行检查。用户既可以点击 `Ask Agent With Context` 发起一次性请求，也可以在当前 workspace 下已有最近一次普通文本请求时直接点击 `Ask With Last Request` 复用同一句需求，还可以开启 `Bundle Chat`，让后续普通文本以及附件回合持续自动附着当前 bundle；agent 会在本地 workspace 中读取最新文件和 diff 状态；能力降级后落到 `.talk2agent/telegram-inbox/` 的附件也会自动出现在这里；如果 bundle 是从某个具体只读视图打开的，bundle 内会保留返回该来源视图的入口，而从 bundle 里重新打开的文件/变更预览也会同时保留返回 bundle 和返回原来源页的入口
- 当成功回合让当前 workspace 的 Git 变更集合发生变化时，bot 会补一条轻量提示，允许用户直接打开 `Workspace Changes`、直接基于当前变更继续追问 agent、直接用当前变更启动 `Bundle Chat`、批量把当前变更加入 `Context Bundle`，或直接打开 bundle 继续追问；如果当前 workspace 下存在最近一次普通文本请求，这条 follow-up 里也会额外提供 `Ask With Last Request`，并且从这条 follow-up 发起的 `Ask Agent...` / `Ask With Last Request` / `Cancel Ask` 都会尽量在同一条 follow-up 消息内刷新；如果从这条 follow-up 继续打开 `Workspace Changes` 或 `Context Bundle`，子页面也会保留 `Back to Change Update`
- `Model / Mode` 会直接展示当前 live session 暴露的实际选项，不再经过额外子菜单；如果当前用户还没有 live session，bot 会先为该用户拉起一个 session，再显示真实 model/mode 选项。切换成功后，当前 session 的本地 history 时间戳和 Telegram slash command 菜单也会立刻刷新；如果当前 workspace 下已有可重放的上一轮请求，还会额外提供 `Model+Retry ...` / `Mode+Retry ...`，用于一键切配置并立即重跑上一轮
- Telegram 命令菜单会动态显示当前 agent 暴露的 slash commands；本地仅保留隐藏调试命令 `/debug_status`

## Telegram 命令

- Telegram 可见命令菜单只显示当前 agent 暴露的 slash commands，并按允许用户逐个同步
- 用户从命令菜单点击的 slash command 会原样转发给当前 agent 会话
- 本地 bot 控制命令已经退回按钮；仅保留隐藏调试命令 `/debug_status`
- `/debug_status` 返回当前 `provider`、`workspace_id`、`cwd` 与 `session_id`；如果当前 live session 已经启动过，还会附带当前 agent 的 prompt/session 能力摘要，且不会额外创建新的 ACP 会话

## Provider 持久化

- `runtime.provider_state_path` 用于保存最近一次选中的 Provider 与 Workspace
- `runtime.session_history_path` 用于保存本 bot 的本地 session history 索引
- 进程重启后，会优先恢复该 Provider 与 Workspace；若不存在，再回退到 YAML 默认值
- 如果你想让 YAML 中的 `agent.provider` / `agent.workspace_dir` 重新生效，可以手工切回去，或删除/重置 provider-state 文件
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
7. 先发送 `/debug_status`，确认在任何提示词之前返回的是当前 Provider、Workspace 和 `session_id=none`。
8. 从允许的 Telegram 账号发送一条普通文本消息，确认机器人先通过 Telegram Draft API 流式展示增量文本，随后再落一条最终普通消息。
9. 发送一张图片（可带 caption），确认 bot 会把图片和 caption 一起提交给当前 ACP session，并返回正常响应。
10. 发送一个语音或音频消息，确认 bot 会把音频作为 ACP audio block 提交给当前 session，并返回正常响应。
11. 发送一个小型视频，确认 bot 会把视频作为 ACP binary resource 提交给当前 session；如果当前 provider 不支持 embedded context，也要确认它会自动降级到 `.talk2agent/telegram-inbox/` 并要求 agent 从本地文件读取。
12. 发送一个小型文本或 PDF 文档，确认 bot 会把文档作为 ACP 资源块提交给当前 session，并返回正常响应。
13. 连续发送一个 Telegram 相册或同一组多文档，确认 bot 会在短暂收敛后把整组附件合并为一次 ACP 回合，而不是拆成多轮。
14. 以管理员身份点击 `Switch Agent`，选择 `Codex` 或 `Gemini CLI`，确认切换成功；如果当前 workspace 下已经有上一轮请求，也可以直接点击 `Retry on ...` / `Fork on ...`，确认 bot 会先切换 agent，再在目标 provider 上立即重试或分叉上一轮；如果目标 Provider 缺少可执行文件或无法创建 session，应返回 `session creation failed`。
15. 以管理员身份点击 `Switch Workspace`，确认只显示配置白名单中的路径，并且切换成功后 `/debug_status` 中的 `workspace_id` / `cwd` 发生变化。
16. 点击 `New Session`，确认该用户的会话被重置。
17. 点击 `Session History`，确认可以看到当前用户在当前 Provider + 当前 Workspace 下的本地历史；当前活跃会话会显示 `[current]`，并可执行 `Run` / `Rename` / `Delete`；如果当前 workspace 下已有上一轮请求，也可以直接点击 `Run+Retry`，确认 bot 会先切到所选历史 session，再立刻重放上一轮。
18. 以管理员身份在 `Session History` 里点击 `Provider Sessions`，确认可以看到当前 workspace 下 provider 原生保存的 ACP sessions；点击 `Run` 后，当前 Telegram 用户应接管该 session，并且该 session 会回写到本地 history；如果当前 workspace 下已有上一轮请求，也可以直接点击 `Run+Retry`，确认 bot 会先接管该 provider session，再立刻重放上一轮。
19. 在 `Session History` 里点击 `Rename`，发送下一条普通文本，确认该文本只会更新历史标题，不会被转发给 agent；如果点击 `Cancel Rename`，则应返回历史列表。
20. 点击 `Agent Commands`，确认可以看到当前 agent 暴露的 slash commands 列表；无参命令可直接运行，带 `args:` hint 的命令会要求下一条普通文本作为参数。
21. 在 `Agent Commands` 里点击一个带参数的命令，发送下一条普通文本，确认 bot 实际转发的是 `/command args`；如果点击 `Cancel Command`，则应返回命令列表。
22. 点击 `Workspace Files`，确认可以浏览当前 workspace 的目录，并预览文本文件；点击目录可进入，点击 `Back to Folder` / `Up` 可返回上级目录。也要确认目录页的 `Ask Agent With Visible Files` 会把当前页可见文件作为一次性上下文交给 agent，`Start Bundle Chat With Visible Files` 会把当前页可见文件加入 bundle 并开启持续上下文，以及 `Add Visible Files to Context` 能把当前页可见文件批量加入 bundle。如果当前 workspace 下已经有最近一次普通文本请求，也要确认目录页的 `Ask With Last Request` 会直接复用同一句需求重新请求当前可见文件集合。
23. 在文件预览页点击 `Ask Agent About File`，发送下一条普通文本作为请求，确认 agent 实际收到的是“文件路径 + 用户请求”，并基于当前 workspace 的最新文件内容作答。如果当前 workspace 下已经有最近一次普通文本请求，也要确认 `Ask With Last Request` 会直接复用同一句需求重新请求当前文件。也要确认预览页的 `Start Bundle Chat With File` 会把当前文件加入 bundle 并直接开启持续上下文，而 `Open Context Bundle` 会直接跳到 bundle 视图。
24. 点击 `Workspace Search`，发送下一条普通文本作为搜索词，确认能返回 workspace 内的匹配项；点击结果后应能打开对应文件预览，并可返回搜索结果。也要确认结果页的 `Ask Agent With Matching Files` 会把当前搜索命中的文件作为一次性上下文交给 agent，`Start Bundle Chat With Matching Files` 会把当前搜索命中的文件加入 bundle 并开启持续上下文，以及 `Add Matching Files to Context` 能把当前搜索命中的文件批量加入 bundle。如果当前 workspace 下已经有最近一次普通文本请求，也要确认结果页的 `Ask With Last Request` 会直接复用同一句需求重新请求当前命中的文件集合。
25. 点击 `Workspace Changes`，确认可以看到当前 Git 工作区变更；打开某条变更后应显示 diff 预览，并可点击 `Ask Agent About Change` 发起下一条文本请求。如果当前 workspace 下已经有最近一次普通文本请求，也要确认变更列表页和 diff 预览页的 `Ask With Last Request` 都会直接复用同一句需求，分别重新请求当前整批变更和当前 change。也要确认 diff 预览页的 `Start Bundle Chat With Change` 会把当前 change 加入 bundle 并直接开启持续上下文，`Open Context Bundle` 会直接跳到 bundle；列表页的 `Ask Agent With Current Changes` 会把当前整批变更作为一次性上下文交给 agent，`Start Bundle Chat With Changes` 会把当前整批变更加入 bundle 并开启持续上下文，而 `Add All Changes to Context` 能把整批当前变更加入 bundle。
26. 在文件预览页或 diff 预览页点击 `Add ... to Context`，再点击 `Context Bundle`，确认可以看到当前用户在当前 Provider + 当前 Workspace 下收集的 bundle 项，并可从 bundle 内重新打开文件预览或 diff 预览、直接移除当前预览项并返回 bundle；如果某个附件因 provider 能力不足被降级保存到 `.talk2agent/telegram-inbox/`，也应自动出现在 bundle 中。
27. 在 `Context Bundle` 中点击 `Ask Agent With Context`，发送下一条普通文本，确认 agent 实际收到的是“多文件/多变更上下文 + 用户请求”，并基于当前 workspace 的最新本地状态作答。如果当前 workspace 下已经有最近一次普通文本请求，也要确认 bundle 视图的 `Ask With Last Request` 会直接复用同一句需求重新请求当前 bundle。也要确认 `Start Bundle Chat` 打开后，后续普通文本以及附件回合会持续自动附着当前 bundle，直到点击 `Stop Bundle Chat` 或清空 bundle。
28. 让 agent 执行一次实际会修改工作区的任务，确认如果当前 Git 变更集合发生变化，bot 会额外发出一条快捷提示，可直接打开 `Workspace Changes`、点击 `Ask Agent With Current Changes` 继续追问、点击 `Start Bundle Chat With Changes` 进入持续上下文、批量加入 bundle 或打开 `Context Bundle`。也要确认从这条 follow-up 打开的 `Workspace Changes` / `Context Bundle` 子页面会带上 `Back to Change Update`，可以回到原始快捷提示。
29. 点击 `Model / Mode`，确认可以直接看到当前 agent 暴露的模型/模式选项；如果当前用户还没有 live session，bot 应先拉起一个 session 再展示选项，并在切换后给出成功提示；如果当前 workspace 下已有上一轮请求，也可以直接点击 `Model+Retry ...` / `Mode+Retry ...`，确认 bot 会先更新配置，再立即重跑上一轮。
30. 打开 Telegram 命令菜单，确认显示的是当前 agent 暴露的 slash commands，而不是 bot 自己的管理命令。
31. 点击 `Restart Agent`，确认会话被重新拉起并分配新的 session。
32. 重启机器人，确认它恢复到最近一次选中的 Provider 与 Workspace，而不是更早的配置默认值。
33. 点击 `Bot Status`，确认它不会为查看状态而隐式创建新 session，但会展示当前 Provider、Workspace、路径、session、model/mode、待输入状态、session history 数量、last turn replay、last request text、`Context Bundle` 和 `Bundle Chat` 状态；也要确认其中的 `Session History`、`Agent Commands`、`Workspace Files`、`Workspace Search`、`Workspace Changes`、`Context Bundle`、`Model / Mode`、`Switch Agent`、`Switch Workspace` 快捷按钮都能直接打开对应视图或内联菜单，并且这些从状态页打开的子视图/菜单都支持 `Back to Bot Status` 返回；同时确认可以直接 `Cancel Pending Input`、对当前 bundle 执行 `Start/Stop Bundle Chat`、以及直接触发 `New Session`、`Retry/Fork Last Turn`、`Restart Agent`；管理员还应能直接进入 `Provider Sessions`。

这个 MVP 只支持 Telegram 轮询模式，不支持 webhook。

## MVP 范围与限制

- 仅适用于白名单 / 自用场景：非允许用户的消息会被拒绝
- 在当前活跃 Provider 运行时中，每个 Telegram 用户只保留一个长生命周期 ACP 会话
- Session history 删除只影响 `talk2agent` 的本地记录，不会硬删除 Provider 侧原始 session
- Session history 作用域是当前 Provider + 当前 Workspace + 当前用户
- `permissions.mode` 固定为 `auto_approve`
- 输入侧支持纯文本、图片、语音、音频、视频和文档；流式输出仍仅支持纯文本，并通过 Telegram Draft API 投递；当前实现按私聊场景设计
- Provider 与 Workspace 切换都是全局性的，并且只允许管理员触发

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
