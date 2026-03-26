# Telegram Provider Sessions 设计

## 目标

在不改变 ACP 边界、不破坏单一活跃 Provider + Workspace 运行时模型的前提下，让 Telegram Bot 可以继续接管本地 Codex、Gemini、Claude Code 已经保存过的 ACP session，从而把“电脑端开始、Bot 端继续”的工作流闭环补齐。

## 核心决策

- session 来源仍然只有 ACP `session/list`；bot 不扫描 provider 私有文件，也不维护另一套外部 session 索引。
- provider 原生 session 浏览默认只对管理员开放，因为这些 session 在 provider 侧是全局的，无法安全映射回某个 Telegram 用户。
- 只展示当前活跃 workspace 根目录内的 sessions；即使 provider 返回了其他 cwd，也要在 bot 侧再次过滤。
- 接管 provider session 时，当前 Telegram 用户的 live session 槽位会直接切换到该 session，并把标题回写到本 bot 的本地 history，方便后续继续从 `Session History` 访问。
- 当当前 workspace 下存在可重放的上一轮请求时，provider session 视图会额外提供 `Run+Retry`；它先接管目标 provider session，再立即在该 session 上重放上一轮请求。

## 运行时形状

### Provider session 列表

- `AgentSession.list_sessions()` 继续作为唯一 ACP `session/list` 调用入口。
- `AppServices` 新增临时 catalog session 逻辑：
  - 读取当前活跃 Provider + Workspace
  - 用临时 `AgentSession` 调 `list_sessions`
  - 过滤掉 workspace 根目录之外的 session
  - 把 cwd 转成相对 workspace 的展示标签

### 会话接管

- `SessionStore` 新增“按 session_id 接管 provider 原生 session”的路径。
- 接管流程沿用现有 `load_session(prefer_resume=True)`：
  - 关闭当前 Telegram 用户旧 live session
  - 装载目标 provider session
  - 将该 session 写入当前用户的 live session 槽位
  - 用 provider title 触碰本地 history

## Telegram 交互

### 入口

- `Session History` 视图对管理员新增 `Provider Sessions` 按钮。
- 非管理员仍只看到自己的本地 history，不暴露 provider 全局 session 目录。

### Provider Sessions 视图

- 展示：
  - 当前 provider / workspace
  - provider session 标题或 session_id
  - 相对 cwd
  - `updated_at`
- 操作：
  - `Open N` 打开单个 provider session 的详情页
  - `Run N` 接管该 provider session
  - `Run+Retry N` 接管该 provider session 后立刻重放当前 workspace 的上一轮请求
  - `Prev` / `Next` 按 ACP cursor 翻页
  - `Back to History`

### Provider Session 详情

- 详情页复用当前列表页已经加载出的 provider session 条目，不新增额外 ACP 请求类型。
- 展示：
  - title
  - session_id
  - 相对 cwd 与 provider 原始 cwd
  - `updated_at`
  - 当前 Telegram live session 是否已经附着到该 provider session
- 操作：
  - `Run Session`
  - `Run+Retry Session`
  - `Fork Session`
  - `Fork+Retry Session`
  - `Back to Provider Sessions`
- 详情页只负责检查与决策；真正的接管/分叉成功后，仍沿用原有返回链路：
  - 从状态页进入时，成功后回到 `Bot Status`
  - 从历史页进入时，成功后回到 `Provider Sessions`，再可回 `History`

## 非目标

- 不把 provider 全局 session 目录暴露给所有已授权用户。
- 不展示完整会话 transcript。
- 不做跨 workspace、跨 provider 的 session 聚合。
- 不直接修改 provider 自己的 session 元数据，只回写本 bot 的本地 history。
