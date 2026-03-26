# Telegram Command Center 设计

## 目标

在不改变 ACP 边界、不引入新的传输层、不破坏现有 session / provider / workspace 不变量的前提下，把 Telegram 从“只能手打 slash command”提升为“可浏览、可点击、可带参数执行 agent 命令”的使用入口。

## 核心决策

- 命令来源仍然只有 ACP `available_commands`；bot 不硬编码 provider 专属命令。
- Telegram slash command 菜单固定保留本地 `/start`、`/status`、`/help` 与 `/cancel`；agent 命令继续由当前 ACP `available_commands` 增量追加。
- 当 agent 命令 discovery 暂时失败时，Telegram slash command 菜单仍至少保留这些本地恢复入口，避免用户失去恢复路径。
- 有 live session 时，优先读取该 session 当前暴露的命令集合；没有 live session 时，退回到临时 discovery。
- 无参数命令可以直接通过 inline button 触发，并像普通文本回合一样进入当前用户的 live session。
- 带 `hint` 的命令使用两段式交互：先点击命令按钮，再把下一条普通文本当作参数，最终转发为 `/command args`。
- 命令中心是对 Telegram slash command 菜单的补充，而不是替代；两种入口应保持等价。

## 运行时形状

### 命令发现

- `AgentSession` 继续负责缓存 `available_commands_update`。
- `telegram_bot.py` 新增 command center 视图逻辑：
  - 如果当前用户已有 live session，则优先 `ensure_started()` 并读取该 session 的 `available_commands`
  - 如果当前用户还没有 live session，则调用现有 `services.discover_agent_commands()`

### 命令执行

- 所有命令最终仍通过 `AgentSession.run_turn()` 进入 ACP 会话。
- 无参命令直接转发 `/command`。
- 带参数命令在 Telegram 内部短暂保存 `command_name + page`，下一条普通文本到来时再转发 `/command args`。
- 待输入状态被主菜单按钮、显式取消按钮或真正提交后的成功路径清除。

## Telegram 交互

### 主入口

Reply keyboard 新增：

- `Agent Commands`

### 命令中心视图

- 文本区域展示：
  - 当前 provider / workspace
  - 当前 session 是否存在
  - 当前页可用命令、描述与 `args:` hint
- inline button 提供：
  - `Open N`：打开单个命令详情页
  - `Run N`：用于无参命令
  - `Args N`：用于带 hint 的命令
  - `Prev` / `Next`：用于分页

### 命令详情

- 详情页复用当前列表页已经加载出的 `available_commands`，不新增 ACP request。
- 展示：
  - 命令名
  - 描述
  - args hint
  - 当前 session id
  - 可直接复制心智模型的示例形式，例如 `/command` 或 `/command <args>`
- inline button 提供：
  - `Run Command` 或 `Enter Args`
  - `Back to Agent Commands`

### 待输入状态

- 点击 `Args N` 后，bot 会提示用户发送下一条普通文本作为参数。
- `Cancel Command` 会恢复命令列表，不会把任何文本转发给 agent。

## 非目标

- 不做 Telegram 内的任意 JSON 表单编辑。
- 不尝试解释 structured input schema；当前只消费 unstructured hint。
- 不改变 ACP 权限模型；`permissions.mode` 仍固定为 `auto_approve`。
- 不在 bot 内复制桌面端完整 UI，只提供与 ACP 命令集一致的可操作入口。
