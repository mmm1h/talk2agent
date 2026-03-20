# 多 Provider ACP Telegram 机器人设计

## 目标

在保留现有“每个用户一个长生命周期会话”模型以及 Telegram 流式输出行为的前提下，把现有 Telegram 机器人扩展为可通过 ACP 对接 `claude`、`codex` 和 `gemini`。

## 当前状态

当前代码已经具备与 Provider 无关的 ACP 会话核心：

- `AgentSession` 基于 `command + args` 启动 ACP 智能体子进程、初始化会话、发送提示词，并把更新转发给当前 Telegram 回合的 sink。
- `SessionStore` 为每个 Telegram 用户管理一个长生命周期会话，支持重置、失效、空闲清理和关闭时清理。
- Telegram Handler 以传输层为中心，并不依赖 Claude 私有实现。

当前仍然“只支持 Claude”的部分在产品层：

- `config.py` 只接受 `agent.provider == "claude"`。
- `write_default_config()` 仍然把 Claude ACP 可执行文件写成默认命令。
- `/status` 无条件返回 `provider=claude`。
- README 也只记录了 Claude ACP 的安装前提和运行路径。

因此，正确的实现方向不是重写 ACP 会话核心，而是在其上方扩展 Provider 选择层。

## 已确认的产品决策

设计讨论中已经确认以下行为：

1. ACP 仍然是唯一支持的后端协议，不会去包装普通交互式 CLI。
2. 机器人必须支持 `claude`、`codex` 和 `gemini`。
3. Provider 选择通过运行时命令 `/provider` 完成。
4. `/provider` 对整个机器人进程做全局切换。
5. Provider 切换后，后续流量会立刻进入新的 Provider 专属 `SessionStore`，然后再关闭旧 Store 中的会话。
6. 只有配置中的 Telegram 管理员用户才能执行 `/provider`。
7. 其他白名单用户仍可正常聊天，但不能切换 Provider。
8. 机器人重启后，应恢复最近一次成功切换到的 Provider，而不是总是退回配置默认值。

## Provider 运行时模型

实现应保证整个进程同一时刻只有一个活跃 Provider。

进程启动时，按以下顺序解析“实际启动 Provider”：

1. 最近一次成功持久化的 Provider，如果存在且合法
2. 否则使用 `config.agent.provider`

启动完成后，`/provider <name>` 会以原子方式把进程切换到一个新的运行时状态对象；该对象同时包含当前 Provider 和该 Provider 对应的 `SessionStore`。切换完成后，旧 Store 中的会话会被关闭。

这个设计保留了现有会话模型的稳定性：

- 每个 Telegram 用户在当前 Provider 下仍然只有一个长生命周期 ACP 会话
- 机器人不会把旧 Provider 的上下文历史混入新 Provider 的回合
- 不引入按用户维度路由不同 Provider 的模型

为了在并发下真正保证这一点，Provider 切换必须使用一把进程级状态锁，同时保护“运行时状态快照”和“切换 + 持久化事务”：

1. 解析目标 Provider
2. 创建一个新的 `SessionStore`，其 factory 使用该 Provider 对应的 ACP 命令映射
3. 构造一个新的运行时状态对象，同时包含 `provider` 和 `session_store`
4. 在状态锁内，把旧运行时状态引用替换为新引用
5. 在同一把锁内，把旧 Store 标记为 retired，阻止它继续创建或重置会话
6. 在仍持有锁时，把新的 Provider 值持久化
7. 如果持久化失败，则在释放锁之前恢复旧运行时状态引用，并取消旧 Store 的 retired 状态
8. 释放锁
9. 当“切换 + 持久化事务”成功后，再对退休的旧 Store 调用 `close_all()`

读路径也必须一致地基于运行时快照：

- 每个 Handler 只在需要时持有状态锁，用于抓取当前运行时快照
- 凡是同时需要 Provider 和 Store 的路径，比如 `/status`，都必须使用同一个快照中的值
- Handler 不应再分别读取 `services.active_provider` 和 `services.session_store` 这种彼此独立的可变字段
- 实现应暴露类似 `snapshot_runtime_state()` 的 helper，使“快照式读取”成为默认做法
- 只读检查路径应使用不会创建会话的查询方法，例如 `SessionStore.peek(user_id)`，确保 `/status` 不会附带创建 ACP 会话
- 如果 Handler 与 Provider 切换竞态，碰到退休 Store，应丢弃旧快照，重新抓一个新快照，并针对当前活跃 Store 重试一次

在“切换 + 持久化事务”成功之后，即便旧 Store 的关闭仍在进行，所有后续成功的 `get_or_create()` 也必须进入新 Store。

## Provider 命令映射

程序应自己维护“Provider -> 命令”的映射，而不是要求操作者在每次切换时手工维护 `command` 和 `args`。

初始映射如下：

- `claude` -> command `claude-agent-acp`，args `[]`
- `codex` -> command `codex-acp`，args `[]`
- `gemini` -> command `gemini`，args `["--acp"]`

原因：

- Claude 和 Codex 应直接使用它们的 ACP 可执行文件。
- 本地安装的 Gemini CLI 通过 `--acp` 开启 ACP 模式。
- 运行时切换应变成一次简单的 Provider 状态切换，而不是一套可变 `command/args` 的重写逻辑。

由 Provider 驱动的命令解析必须是运行时执行的唯一事实来源。

这意味着：

- 真正执行的 ACP 命令和参数由活跃 Provider 决定
- 运行时切换不应去改写原始命令字符串
- `agent.command` 和 `agent.args` 不再具有权威性

作为兼容迁移，YAML 输入仍可接受旧版 `agent.command` 和 `agent.args`，但运行时 Provider 选择必须忽略它们。默认配置输出也应停止生成这些字段。

## 架构改动

### 1. 配置层

`config.py` 不应再强制只允许 Claude，而应接受：

- `claude`
- `codex`
- `gemini`

配置仍然提供：

- Telegram bot token
- 允许访问的用户 ID
- 管理员用户 ID
- 工作目录
- 初始 Provider
- 运行时参数

默认配置模板仍应面向自用场景，但不能再把系统描述成“只支持 Claude”。

`telegram.admin_user_id` 必须是一个常规必填配置字段，就像 bot token 和白名单一样，而不是从开发测试用户 ID 中硬编码出来。

为支持“重启后恢复最近一次 Provider”，同时又不改写操作者主 YAML 文件，配置里还需要一个小型 Provider 状态文件路径，例如 `runtime.provider_state_path`。该文件只负责保存最近一次成功切换的 Provider。

### 2. App Services

`AppServices` 应通过一个可替换的运行时状态对象承载可变的进程级状态：

- `runtime_state`：其中包含 `provider` 和 `session_store`
- `admin_user_id`
- 一把进程级状态锁：用于运行时快照、切换事务、Provider 持久化提交，以及持久化失败时的回滚
- 不可变的配置 / 运行时值

`build_services()` 应根据配置初始化 `runtime_state`，并通过一个接收 Provider 名称的 helper 创建 `SessionStore`。该 Store 的 session factory 必须解析出对应 Provider 的 ACP 命令映射。

由于 Provider 切换是整包替换整个 Store，session factory 不需要去热迁移已有 `AgentSession`；它只需要为当前 Store 捕获的那个 Provider 创建新会话即可。

`AppServices` 应暴露一个同步化 helper，让每个请求都能只抓取一次运行时快照。`SessionStore` 还应提供：

- 一个只读查询现有会话的方法，让 `/status` 可以读取当前 `session_id`，而不会创建新的会话项
- 一个退休机制，在 Provider 切换后阻止该 Store 继续执行 `get_or_create()` 或 `reset()`

### 3. Provider 状态持久化

运行时应把最近一次选中的 Provider 持久化到一个小型状态文件，而不是去改写主 YAML 配置。

必需行为：

- 启动时，如果 `runtime.provider_state_path` 文件存在且包含受支持的 Provider 名称，则从中恢复
- 如果文件缺失、不可读或包含未知 Provider，则回退到 `config.agent.provider`
- `/provider` 返回成功必须同时满足：
  - 新的运行时快照已经安装
  - 新 Provider 名称已经可靠写入 Provider 状态文件
- 如果 `/provider` 的持久化阶段失败，机器人必须恢复先前运行时，取消旧 Store 的退休状态，关闭新预备好的 Store，并向用户返回失败，而不是假装切换已经可靠完成

持久化格式可以非常简单，例如一个只包含一个字段的 JSON 文档，只要读写确定、测试容易验证即可。

### 4. Telegram 机器人

新增一个命令：

- `/provider <claude|codex|gemini>`

预期行为：

- 非管理员调用：拒绝
- 参数缺失或非法：回复 usage/help
- 管理员传入合法参数：
  - 创建并原子安装一个新的运行时快照
  - 关闭旧 Store 中的全部会话
  - 回复新的 Provider 名称

现有命令也会有轻微变化：

- `/status` 必须报告当前活跃 Provider，而不是硬编码 `claude`
- `/status` 不能调用 `get_or_create()`，也不能有“顺手创建 ACP 会话”的副作用
- 普通文本消息必须路由到当前活跃 Provider 创建出的会话

### 5. 会话生命周期

`AgentSession` 的 ACP 协议机制本身不需要改动。

唯一变化是每个新会话如何选择 `command/args`：

- 之前：总是从 Claude 形态的配置中取
- 之后：从当前活跃 Provider 的映射中取

对某一个 Store 而言，会话失效规则保持不变：

- `/new` 重置调用者当前会话
- 空闲超时会清掉过期会话
- 单轮对话失败会让失败会话失效
- `/status` 只检查当前 Store 和当前用户已有的会话项
- `/provider` 会让旧 Store 退休，并关闭其中全部会话

退休 Store 的行为必须显式：

- 对退休 Store 调用 `get_or_create()` 和 `reset()` 时，必须快速失败，而不是继续创建或替换会话
- Handler 收到退休 Store 错误后，应重新抓取运行时快照并重试一次
- `close_all()` 仍以 best-effort 方式关闭该退休 Store 中已有的会话

## 错误处理

### Provider 切换

Provider 切换失败场景包括：

- 非法 Provider 名称
- 非管理员调用

机器人应始终给用户一个可见结果。就 MVP 安全性而言：

- 校验错误返回简单的 usage 或 unauthorized 文案
- 切换成功的定义是：新的运行时快照安装成功，且所选 Provider 已持久化成功
- 旧 Store 清理保持 best-effort，沿用现有 `close_all()` 行为
- 如果代码选择记录清理失败，那只是运维遥测，不应把它上升为用户可见的切换失败

### 状态查询

`/status` 应呈现：

- `provider=<用于获得 session 的同一个运行时快照中的 active_provider>`
- `session_id=<当前 session id、pending 或 none>`

`pending` 表示会话对象已经存在，但 ACP session id 尚未分配。
`none` 表示当前活跃 Store 中不存在该用户的会话项。

如果只读状态查询失败，继续沿用当前行为，回复 `Request failed.`。

## 测试策略

### 配置测试

需要覆盖：

- 允许 `claude`、`codex`、`gemini`
- 拒绝未知 Provider
- 默认配置反映新的默认 Provider 文案

### App / Service 测试

需要覆盖：

- Provider 映射解析
- 旧版 `agent.command/args` 输入不会覆盖 Provider 驱动的运行时解析
- Store factory 使用的是该 Store 捕获的 Provider
- 启动时优先选择持久化 Provider，而不是配置默认值
- 当持久化 Provider 状态缺失或非法时，启动会回退到配置默认值
- `/provider` 只有在 Provider 状态持久化成功后才报告成功
- Provider 切换会原子替换整个运行时状态对象
- `/provider` 持久化失败时，不会把短暂的新运行时状态泄漏给其他 Handler
- 切换期间旧 Store 会被退休，因此之后不允许再在旧 Store 上创建新会话
- 即便旧 Store 关闭仍在进行，新流量也会进入新 Store
- `/status` 不会把一个运行时快照中的 Provider 和另一个快照中的 Session 混用

### Telegram 机器人测试

需要覆盖：

- `/status` 动态报告当前 Provider
- `/status` 不会创建新会话
- `/status` 在无会话时返回 `session_id=none`
- 非管理员不能切换 Provider
- 管理员可以切换 Provider
- 非法 `/provider` 参数返回 usage
- 成功的 `/provider` 会触发 `close_all()`
- Handler 在旧快照撞上退休 Store 时会重试一次
- Provider 切换后的文本消息会在新 Provider 下创建会话

### 回归覆盖

现有长会话行为必须保持不变：

- 每个用户始终只有一个会话，直到 `/new`、空闲过期、失败失效或 Provider 切换发生
- 流式输出仍然只支持纯文本
- 白名单限制保持不变

## 文档更新

README 需要更新，说明：

- 支持的 ACP Provider：Claude、Codex、Gemini
- 每个 Provider 的安装前提
- 配置中的初始 Provider
- `/provider` 命令以及“仅管理员可用”的约束
- 全局切换语义：Provider 改变后，所有旧会话都会被清掉

Provider 安装说明应包含：

- Claude：安装 `@zed-industries/claude-agent-acp`
- Codex：安装 `@zed-industries/codex-acp`
- Gemini：安装 `@google/gemini-cli` 并通过 `gemini --acp` 启用 ACP 模式

## 手工验证

手工 Telegram 验证应覆盖：

1. 当持久化状态存在时，启动在最近一次选中的 Provider 上；否则启动在配置默认 Provider 上。
2. 确认 `/status` 正确报告该 Provider，且不会自行创建会话。
3. 发送普通提示词，确认可以看到流式编辑输出。
4. 以管理员身份执行 `/provider codex`，确认切换成功。
5. 确认 `/status` 现在报告 `provider=codex`。
6. 再发送普通提示词，确认新会话是在 Codex 下创建的。
7. 执行 `/provider gemini`，重复同样检查。
8. 重启机器人，确认它恢复到 `gemini`，而不是更早的配置默认值。
9. 验证非管理员用户无法修改 Provider。
10. 验证切换 Provider 后，`/new` 和空闲超时仍然正常工作。

## 本次变更的非目标

- 不做按用户维度的 Provider 选择
- 不做 Provider 专属的 Telegram UX 差异
- 不偏离 ACP 作为 bot 与 agent 之间传输协议的定位

## 推荐实现形状

最低风险实现路径是：

1. 先把配置校验从“只支持 Claude”扩展到三种 Provider
2. 新增 `telegram.admin_user_id`
3. 新增 `runtime.provider_state_path`
4. 让 Provider 映射成为运行时 `command/args` 的唯一来源
5. 增加 Provider 解析、Store 构造和持久化 Provider 的一层运行时逻辑
6. 在 `AppServices` 中维护单个可替换运行时状态引用和一把切换锁
7. 为 `/status` 增加只读会话检查路径
8. 新增 `/provider`
9. 更新 `/status`
10. 更新文档和真实机器人验证步骤

这样可以保留现有 ACP 会话核心，而不是把它整体重写掉。

## 环境说明

这个工作区当时不是一个 Git 仓库，因此常规的“写完设计文档后提交到 Git”步骤在当时无法完成。文档仍然可以在本地写入并评审，只是不能以 commit 形式记录。
