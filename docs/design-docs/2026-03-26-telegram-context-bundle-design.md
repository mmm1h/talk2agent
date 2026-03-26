# Telegram Context Bundle 设计

## 目标

在不突破 ACP 边界、不放宽 workspace 白名单约束的前提下，为 Telegram Bot 增加一个可以跨多个文件和 Git 变更累积上下文的轻量工作台，让用户离开电脑时仍能用一次 Bot 请求复用接近桌面端的多上下文工作流。

## 核心决策

- `Context Bundle` 只保存引用，不缓存文件正文或 diff 正文。
- bundle 的作用域固定为当前 Provider + 当前 Workspace + 当前 Telegram 用户；跨 Provider 或跨 Workspace 不共享。
- bundle 项来源包括现有只读入口中的文件预览页、Workspace Search 结果页、Git diff 预览页、Git 变更列表页的整批加入动作，以及 provider 能力不足时自动降级写入 workspace inbox 的 Telegram 附件。
- 提交 bundle 请求时，bot 仍只把“路径 / status code / 用户请求”发给 agent；agent 必须回到本地 workspace 读取最新文件和 Git 状态。
- bundle 交互保持两段式：先点 `Ask Agent With Context`，再把下一条普通文本作为用户请求提交。
- bundle 视图也会记住当前 workspace 下最近一次普通文本请求；如果存在，用户可直接点击 `Ask With Last Request`，把同一句需求立即重用到当前 bundle，减少手机端重复输入。
- 同一套“路径 / status code + 用户请求”桥接逻辑也允许只读视图直接发起一次性请求；这种快捷入口不会写入 bundle，只是复用相同的 agent prompt 形状。
- bundle 还支持显式开启持续附着模式；开启后，后续普通文本都会自动附着当前 bundle，直到用户手动关闭或 bundle 被清空。
- 当 `Bundle Chat` 打开时，Telegram 附件和 `media_group` 回合同样会自动附着当前 bundle，而不是退回到“仅附件自身上下文”。

## 运行时形状

### UI 状态

- `telegram_bot.py` 内部新增按用户保存的 context bundle 状态。
- bundle 中的每个 item 只记录：
  - `kind`
  - `relative_path`
  - `status_code`（仅 change item 使用）
- 当当前用户切换到不同 Provider 或 Workspace 时，旧 bundle 会在下一次访问时失效并被清理。

### Telegram 交互

Reply keyboard 新增：

- `Context Bundle`

文件预览页新增：

- `Add File to Context`
- `Start Bundle Chat With File`
- `Open Context Bundle`

Workspace Files 目录页新增：

- `Add Visible Files to Context`
- `Start Bundle Chat With Visible Files`
- `Open Context Bundle`

Workspace Search 结果页新增：

- `Add Matching Files to Context`
- `Start Bundle Chat With Matching Files`
- `Open Context Bundle`

Git diff 预览页新增：

- `Add Change to Context`
- `Start Bundle Chat With Change`
- `Open Context Bundle`

Git 变更列表页新增：

- `Add All Changes to Context`
- `Open Context Bundle`

附件能力降级路径新增：

- 不支持原生 ACP prompt 的图片、音频或二进制文档，在成功写入 `.talk2agent/telegram-inbox/` 并完成 turn 后，会自动加入 bundle
- 如果某个成功回合让 Git 变更集合发生变化，bot 也会主动给出“批量加入当前变更到 bundle”的快捷入口

bundle 视图提供：

- 当前 provider / workspace
- 当前 bundle 项列表
- `Bundle chat: on/off`
- `Open N`
- `Remove N`
- `Ask Agent With Context`
- `Ask With Last Request`
- `Start Bundle Chat` / `Stop Bundle Chat`
- `Clear Bundle`
- `Prev` / `Next`

### bundle 到 agent 的桥接

- 用户点击 `Ask Agent With Context` 后，bot 进入短暂待输入状态。
- 如果当前 workspace 下已经有最近一次普通文本请求，用户也可以直接点击 bundle 视图里的 `Ask With Last Request`，跳过待输入步骤，直接把同一句需求应用到当前 bundle。
- 下一条普通文本会被包装成“文件列表 + 变更列表 + 用户请求”，再通过现有 `AgentSession.run_turn()` 进入当前 live session。
- bot 不直接转发预览缓存，避免 Telegram 侧上下文与本地 workspace 状态脱节。
- 当 `Bundle Chat` 打开时，用户后续普通文本会直接复用同样的 prompt 形状，但不会冻结 bundle 快照；agent 总是基于当前 bundle 引用解析最新 workspace 状态。
- 当 `Bundle Chat` 打开时，附件回合会先注入同样的 bundle 前缀，再继续提交原始 caption / fallback text 和结构化附件 block。
- 用户也可以从 bundle 视图直接重新打开某个文件预览或 diff 预览，在预览页里直接把当前项移出 bundle，并返回 bundle 继续工作，不需要重新经过原始来源视图。

## 非目标

- 不做文件内容快照缓存或离线镜像。
- 不做 bundle 跨重启持久化。
- 不做 Bot 内直接编辑、提交、stage 或 patch 应用。
- 不做任意自定义路径输入；bundle 入口仍然只能来自现有只读视图。
