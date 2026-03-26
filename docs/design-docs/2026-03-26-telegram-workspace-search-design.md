# Telegram Workspace Search 设计

## 目标

在不引入任意路径输入、不破坏 workspace 白名单约束的前提下，为 Telegram Bot 增加当前 workspace 的只读全文搜索能力，让用户离开电脑时仍能快速定位代码、配置和产物。

## 核心决策

- 搜索范围严格限制在当前活跃 workspace 根目录内。
- 搜索入口使用 reply keyboard 的 `Workspace Search`，真正的搜索词通过下一条普通文本提交。
- 搜索结果只返回相对路径、行号和截断后的文本片段，不返回绝对路径。
- 搜索结果可分页，并支持打开命中文件预览后返回结果页。
- 搜索结果页支持把“当前搜索命中的文件集合”批量加入 `Context Bundle`，避免手机端逐个打开再收集。
- 搜索结果页也支持把“当前搜索命中的文件集合”作为一次性上下文直接交给 agent，减少手机端多一步 bundle 跳转。
- 搜索结果页还支持把“当前搜索命中的文件集合”直接转成开启状态的 `Bundle Chat`，减少“加入 bundle -> 打开 bundle -> 再启动 chat”的额外步骤。
- 搜索结果页也会记住当前 workspace 下最近一次普通文本请求；如果存在，用户可直接点击 `Ask With Last Request`，把同一句需求立即重用到当前命中的文件集合，减少手机端重复输入。
- 二进制文件不参与搜索。

## 运行时形状

### 搜索 helper

`talk2agent/workspace_files.py` 新增全文搜索 helper，负责：

- 遍历当前 workspace 内的文件
- 跳过二进制文件
- 在文本文件中按大小写不敏感方式搜索
- 限制最大结果数和扫描文件数
- 返回安全的相对路径、行号和简短片段

### Telegram 交互

Reply keyboard 新增：

- `Workspace Search`

交互为两段式：

1. 点击按钮进入待输入状态
2. 下一条普通文本作为搜索词提交

搜索结果视图提供：

- 当前 provider / workspace
- 当前搜索词
- 当前页匹配项
- `Open N`
- `Ask Agent With Matching Files`
- `Ask With Last Request`
- `Start Bundle Chat With Matching Files`
- `Add Matching Files to Context`
- `Open Context Bundle`
- `Prev` / `Next`

文件预览视图提供：

- 当前 provider / workspace
- 文件相对路径
- 文本预览
- `Ask Agent About File`
- `Back to Search`

### 搜索结果到 agent 的桥接

- 用户在搜索结果页点击 `Ask Agent With Matching Files` 后，bot 进入短暂待输入状态。
- 如果当前 workspace 下已经有最近一次普通文本请求，用户也可以直接点击结果页里的 `Ask With Last Request`，跳过待输入步骤，直接把同一句需求应用到当前命中的文件集合。
- 搜索结果页里的请求会被包装成“当前命中的文件列表 + 用户请求”，再通过现有 `AgentSession.run_turn()` 进入当前 live session。

## 非目标

- 不做正则搜索语法或布尔查询。
- 不做跨 workspace 搜索。
- 不做文件编辑。
- 不尝试复刻桌面 IDE 的完整搜索 UI，只提供远程使用场景下最有用的最小闭环。
