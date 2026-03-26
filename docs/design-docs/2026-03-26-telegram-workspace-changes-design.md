# Telegram Workspace Changes 设计

## 目标

在不改变 ACP 边界、不放宽 workspace 白名单约束的前提下，为 Telegram Bot 增加当前 workspace 的只读 Git 变更视图，让用户离开电脑时仍能查看工作区改动、预览 diff，并把某个 change 直接交给 agent 处理。

## 核心决策

- 只有当前 workspace 是 Git 仓库时才显示变更内容；否则明确返回“不是 Git 仓库”。
- 变更来源使用本地 `git status --short --branch` 和 `git diff`，不依赖 provider 自己暴露的命令。
- 变更列表只展示当前 workspace 视角下的相对路径，而不是绝对路径。
- diff 预览是只读的，并带有长度上限；untracked 文件用“文件内容预览 + untracked 标记”替代标准 diff。
- 从 diff 预览页可以发起 `Ask Agent About Change`，下一条普通文本会作为用户请求进入当前 live session。
- 变更列表页也会记住当前 workspace 下最近一次普通文本请求；如果存在，用户可直接点击 `Ask With Last Request`，把同一句需求立即重用到当前整批变更，减少手机端重复输入。
- diff 预览页会记住当前 workspace 下最近一次普通文本请求；如果存在，用户可直接点击 `Ask With Last Request`，把同一句需求立即重用到当前 change，减少手机端重复输入。
- 变更列表页支持把“当前整批 Git 变更”一次性加入 `Context Bundle`，避免手机端逐条收集。
- 变更列表页也支持把“当前整批 Git 变更”作为一次性上下文直接交给 agent，减少 bundle 往返。
- 变更列表页和 Git 变更 follow-up 还支持把“当前整批 Git 变更”直接转成开启状态的 `Bundle Chat`，减少“加到 bundle -> 打开 bundle -> 再启动 chat”的额外步骤。
- 当某个成功回合让当前 workspace 的 Git 变更集合发生变化时，bot 会补一条轻量 follow-up，直接给出“查看变更 / 直接基于当前变更追问 agent / 直接启动基于当前变更的 Bundle Chat / 批量加入 bundle / 打开 bundle”的快捷入口。

## 运行时形状

### Git helper

新增 `talk2agent/workspace_git.py`，负责：

- 判断当前 workspace 是否位于 Git 仓库内
- 读取当前 workspace 范围内的 `git status`
- 读取指定路径的 diff 预览
- 对 untracked 文件回退到文件预览

### Telegram 交互

Reply keyboard 新增：

- `Workspace Changes`

变更列表视图提供：

- 当前 provider / workspace
- 当前 branch 信息
- 当前页变更项
- `Open N`
- `Ask Agent With Current Changes`
- `Ask With Last Request`
- `Start Bundle Chat With Changes`
- `Add All Changes to Context`
- `Open Context Bundle`
- `Prev` / `Next`

diff 预览视图提供：

- 当前 provider / workspace
- 文件相对路径
- status code
- diff 预览
- `Ask Agent About Change`
- `Ask With Last Request`
- `Start Bundle Chat With Change`
- `Open Context Bundle`
- `Back to Changes`

## change 到 agent 的桥接

- 用户在变更列表页点击 `Ask Agent With Current Changes` 后，bot 进入短暂待输入状态。
- 如果当前 workspace 下已经有最近一次普通文本请求，用户也可以直接点击列表页里的 `Ask With Last Request`，跳过待输入步骤，直接把同一句需求应用到当前整批变更。
- 变更列表页里的请求会被包装成“当前 Git 变更列表 + 用户请求”，再通过现有 `AgentSession.run_turn()` 进入当前 live session。

- 用户在 diff 预览页点击 `Ask Agent About Change` 后，bot 进入短暂待输入状态。
- 如果当前 workspace 下已经有最近一次普通文本请求，用户也可以直接点击 `Ask With Last Request`，跳过待输入步骤，直接把同一句需求应用到当前 change。
- 下一条普通文本会被包装成“相对路径 + git status code + 用户请求”，再通过现有 `AgentSession.run_turn()` 进入当前 live session。
- bot 不直接把缓存 diff 塞进 prompt；agent 需要在本地 workspace 中重新读取最新 Git 状态和 diff。
- 用户在变更列表页点击 `Add All Changes to Context` 后，bot 只把当前 change 引用写入 bundle，不缓存 diff 正文。

## 非目标

- 不做 Git commit、stage、checkout 或其他写操作。
- 不做跨 workspace 或跨仓库聚合。
- 不尝试复刻桌面 Git GUI，只提供远程场景下最关键的只读闭环。
