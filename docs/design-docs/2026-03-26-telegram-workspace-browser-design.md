# Telegram Workspace Browser 设计

## 目标

在不引入任意路径访问、不破坏 workspace 白名单约束的前提下，为 Telegram Bot 增加当前 workspace 的只读可见性，让用户离开电脑时仍能查看目录结构和文本文件内容。

## 核心决策

- 浏览范围严格限制在当前活跃 workspace 根目录内。
- 目录浏览和文件预览都通过 inline button + callback query 完成，不引入新的 Telegram 命令。
- 只支持只读访问；不在 Bot 内直接编辑文件。
- 文本文件预览有长度与行数上限，避免超出 Telegram 单消息限制。
- 二进制文件只显示占位提示，不尝试传输原始内容。
- 目录视图支持把“当前页可见文件”批量加入 `Context Bundle`，减少手机端逐个打开文件再收集的成本。
- 目录视图也支持把“当前页可见文件”作为一次性上下文直接发给 agent，减少“先收集到 bundle 再提问”的额外一步。
- 目录视图还支持把“当前页可见文件”直接转成开启状态的 `Bundle Chat`，减少“加入 bundle -> 打开 bundle -> 再启动 chat”的额外步骤。
- 目录视图也会记住当前 workspace 下最近一次普通文本请求；如果存在，用户可直接点击 `Ask With Last Request`，把同一句需求立即重用到当前页可见文件集合，减少手机端重复输入。
- 文件预览页会记住当前 workspace 下最近一次普通文本请求；如果存在，用户可直接点击 `Ask With Last Request`，把同一句需求立即重用到当前文件，减少手机端重复输入。

## 运行时形状

### 安全路径解析

新增 `talk2agent/workspace_files.py`，负责：

- 解析 `workspace root + relative path`
- 拒绝逃逸到 workspace 根目录之外的路径
- 列举目录项
- 读取文本文件预览

这个模块不依赖 Telegram，也不依赖 ACP。

### Telegram 交互

Reply keyboard 新增：

- `Workspace Files`

目录视图提供：

- 当前 provider / workspace
- 当前相对路径
- 当前页文件和目录列表
- `Ask Agent With Visible Files`
- `Ask With Last Request`
- `Start Bundle Chat With Visible Files`
- `Add Visible Files to Context`
- `Open Context Bundle`
- `Up` / `Prev` / `Next`

文件视图提供：

- 当前 provider / workspace
- 文件相对路径
- 文本预览
- `Ask Agent About File`
- `Ask With Last Request`
- `Start Bundle Chat With File`
- `Open Context Bundle`
- `Back to Folder`

### 文件到 agent 的桥接

- 用户在目录视图点击 `Ask Agent With Visible Files` 后，bot 进入短暂待输入状态。
- 如果当前 workspace 下已经有最近一次普通文本请求，用户也可以直接点击目录视图里的 `Ask With Last Request`，跳过待输入步骤，直接把同一句需求应用到当前页可见文件集合。
- 目录视图里的请求会被包装成“当前页可见文件列表 + 用户请求”，再通过现有 `AgentSession.run_turn()` 进入当前 live session。

- 用户在文件预览页点击 `Ask Agent About File` 后，bot 进入短暂待输入状态。
- 如果当前 workspace 下已经有最近一次普通文本请求，用户也可以直接点击 `Ask With Last Request`，跳过待输入步骤，直接把同一句需求应用到当前文件。
- 下一条普通文本消息会被包装成“文件相对路径 + 用户请求”，再通过现有 `AgentSession.run_turn()` 进入当前 live session。
- bot 不会把预览内容直接塞进 prompt；agent 需要在当前 workspace 中重新读取文件，确保使用的是最新磁盘状态。

## 非目标

- 不做任意路径输入。
- 不做文件上传、下载或编辑。
- 不做二进制内容预览。
- 不把工作区浏览逻辑混入 ACP 会话层。
