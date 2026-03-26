# Telegram 附件输入设计

## 目标

在不突破 ACP 边界、不引入新的传输层的前提下，让 Telegram Bot 能把图片、语音、音频、视频和文档作为 ACP 内容块提交给当前会话，使 Bot 端输入能力更接近桌面端。

## 核心决策

- 当前支持 Telegram `photo`、`voice`、`audio`、`video` 和 `document` 消息；`media group` 会在 bot 侧做短暂收敛并合并为一次 ACP 回合。
- caption 会和附件一起进入同一轮 ACP prompt；caption 缺失时，bot 会补一个最小文本提示，避免出现“只有二进制块没有用户意图”的空白回合。
- 对于同一 `media_group_id` 的多附件，bot 会以整组附件共享一条 lead text，并把组内所有附件 block 一次性提交。
- 如果当前用户已开启 `Bundle Chat`，附件和 `media group` 回合会在原始附件 lead text 之前自动附着当前 `Context Bundle` 引用。
- Telegram 图片消息以及 `image/*` 类型的文档会转成 ACP `image` block。
- Telegram `voice`、`audio` 以及 `audio/*` 类型的文档会转成 ACP `audio` block。
- Telegram `video` 会转成 ACP embedded blob resource。
- 可判定为 UTF-8 文本的文档会转成 ACP embedded text resource；其他文档转成 embedded blob resource。
- bot 会读取 provider 在 ACP `initialize` 中声明的 `promptCapabilities`。
- 若当前 provider 不支持 embedded context，文本类文档会自动降级为内联纯文本，尽量保留文档工作流。
- 若当前 provider 不支持图片、音频、视频，或二进制文档所需的 embedded context，bot 会先把附件写入当前 workspace 的 `.talk2agent/telegram-inbox/`，再把本地相对路径作为文本提示交给 agent。
- 上述 inbox 降级在 turn 成功后，会把对应相对路径自动加入当前用户、当前 Provider、当前 Workspace 作用域下的 `Context Bundle`，方便后续多步复用。
- 只有当某类输入既不支持 ACP block、也没有安全降级路径时，bot 才会在 Telegram 中返回明确提示。
- 原生支持的附件只在内存中暂存；只有能力降级路径才允许写入受控的 workspace inbox。`Context Bundle` 状态本身仍不跨重启持久化。
- 为了控制 Telegram 下载和 ACP payload 体积，单个附件大小上限固定为 8 MiB。
- 所有现有两段式待输入状态仍然只接受纯文本；如果用户在待输入状态发附件，bot 会提示先发送文本或取消当前动作。

## 运行时形状

### Telegram 侧

- `telegram_bot.py` 新增附件 handler，处理 `filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO | filters.VIDEO`。
- handler 会从 Telegram 下载附件字节流，推断 MIME 类型和资源 URI，并构造结构化 prompt item。
- 对于 media group，handler 不会逐条立即发起 agent 回合，而是先缓冲同组消息，在短暂静默后统一提交。
- 如果 `Bundle Chat` 处于开启状态，handler 会在提交附件 prompt 前注入当前 bundle 的文件/变更引用前缀，但仍保留原始 caption / fallback text 和附件 block。
- 当当前 provider 不支持某些 block 时，handler 会把可安全降级的附件写入 workspace inbox，并把“本地路径 + 读取要求”改写成普通 text block。
- 当上述降级 turn 成功完成后，handler 会把这些 inbox 路径自动加入当前作用域的 context bundle。
- prompt item 仍然进入当前 Provider + 当前 Workspace 下的 live ACP session，不改变现有 session 路由。

### ACP 侧

- `AgentSession` 新增结构化 prompt item 类型，并提供 `run_prompt()`。
- `AgentSession` 会在 `run_prompt()` 前根据已缓存的 `promptCapabilities` 拒绝不受支持的 block 类型。
- Telegram 传输层会在调用 `run_prompt()` 前，先把可保真降级的文本类文档按能力内联成普通 text block。
- Telegram 传输层也会在调用 `run_prompt()` 前，把不受支持的图片、音频、视频和二进制文档写入 workspace inbox，并改写为要求 agent 读取本地文件的 text block。
- 现有 `run_turn()` 保持文本快捷路径，只是退化为单个 text item 的特例。
- ACP block 的构造细节继续留在 `talk2agent/acp/` 内部，不把 `image_block` / `audio_block` / `resource_block` 直接泄漏给 Telegram 传输层。

## 非目标

- 不做 Telegram 内的图片编辑、OCR、音频转写后处理、视频解析后处理或 PDF 预览。
- 不做任意自定义落盘路径；附件落盘只允许进入受控 inbox。
- 不支持超大附件。
