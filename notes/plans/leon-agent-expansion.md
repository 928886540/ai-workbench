# Leon Agent 扩展路线

- 记录日期：2026-08-14
- 状态：Planned
- 当前基线：独立 `leon` CLI、共享 Agent Runtime、5 个 Leon 生图工具、SQLite 会话已跑通

## 新需求

1. 做一个适合面试现场展示的 Leon MCP Server。
2. 通过 Telegram Bot 直接聊天、发起生图并接收结果。
3. 研究 Tavo 与 Leon 工具互通，让 Tavo 场景也能使用 Leon 能力。
4. 长期把 CLI 体验增强到接近成熟 Agent 产品，但不让 TUI 阻塞工具与协议主线。

## 可行性结论

| 方向 | 可行性 | 难度 | 当前判断 |
|---|---|---|---|
| 面试 MCP | 高 | 中低 | 现有 5 个工具已有 schema 和 handler，主要工作是增加 MCP transport 与演示脚本 |
| Telegram Bot | 高 | 中 | 需要 channel adapter、用户/session 映射、后台任务轮询和图片回传 |
| Leon Agent 连接 Tavo MCP | 高 | 中 | Tavo 已有 MCP Server；Leon Agent 可作为 MCP Client 使用 Tavo 工具 |
| Tavo 聊天直接调用 Leon MCP | 当前受限 | 非代码难度 | Tavo v1.0.0 文档明确：外部 MCP Server 尚未接入聊天工具调用 |
| Tavo 插件贡献 Leon 模型工具 | 当前受限 | 非代码难度 | Tavo v1.0.0 文档明确：插件暂不能贡献模型工具 |
| Codex 风格完整 TUI | 可行 | 高 | 基础 TUI 不难，成熟交互、流式、并发任务和恢复需要持续迭代 |

## 目标边界

```text
CLI -----------┐
Telegram Bot --+--> LeonAgentService --> AgentRuntime --> ToolRegistry
未来其他入口 --┘                             |
                                              +--> Leon / ComfyUI

MCP Client --> Leon MCP Server --> LeonToolService --> Leon / ComfyUI

Leon Agent --MCP Client--> Tavo MCP Server --> Tavo 角色/聊天/消息/记忆工具

Tavo Chat --外部 MCP--> Leon MCP Server
  当前 v1.0.0 尚不可用，等 Tavo 接入后复用同一个 Leon MCP Server
```

CLI、Telegram 和 MCP 不能各复制一套生成逻辑。下一步应先把现有工具 handler 收口为
`LeonToolService` / `LeonAgentService`，入口只负责协议转换和展示。

## Phase A：面试 MCP（最高优先级）

在 `projects/04-mcp-lab/leon-mcp-server` 实现独立 MCP Server，第一版开放：

- `list_image_modes`
- `check_image_environment`
- `generate_images`
- `get_image_tasks`
- `get_recent_images`

同时支持：

- stdio：方便 Codex、Claude Code 和本机 MCP Inspector 演示
- Streamable HTTP：方便后续 Telegram 服务、局域网和未来 Tavo 接入
- `generate_images` 明确标成有副作用操作，演示时只生成 1 张
- 单元测试 mock Leon 后端；集成测试默认只读，不误触真实生图

### 面试演示脚本

1. 用 MCP Inspector / Agent 连接 Server。
2. `tools/list` 展示强类型工具 schema。
3. 调 `check_image_environment`，展示 19 模式、节点和 LoRA 自检。
4. 调 `generate_images`，返回 `generationPlanId / jobId`。
5. 调任务工具查询状态，最终展示 ComfyUI 图片。
6. 解释同一业务工具同时服务 CLI、MCP 和未来 Telegram/Tavo，没有重复实现。

这个演示能覆盖：tool schema、MCP transport、异步任务、幂等身份、真实外部系统集成和测试边界。

## Phase B：Telegram Bot

推荐第一版使用 long polling，不需要公网 webhook：

```text
Telegram update
  -> user/chat allowlist
  -> TelegramAdapter
  -> LeonAgentService
  -> 普通回答或工具调用
  -> 立即返回任务 ID
  -> 后台轮询任务
  -> 完成后主动发送图片
```

必须补齐：

- `telegram_user_id + chat_id -> leon_session_id` 映射
- 只允许配置中的个人账号使用
- Bot token 只进环境变量，不进 SQLite、日志或 git
- 每个聊天独立上下文，避免串 session
- 生图任务后台轮询、超时和失败消息
- 图片 URL 下载/回传失败时仍保留 jobId 供查询

## Phase C：Tavo 互通

### 当前可做

1. **Leon Agent 连接 Tavo**：把 Tavo 内置 MCP Server 接入 Leon Agent，让 Leon 能读取/修改
   当前角色、聊天、消息、世界书和记忆，再结合 Leon 生图工具完成跨系统任务。
2. **Tavo 插件直接调用 HTTP**：现有 `leon-image` 已这样工作，但这是插件业务调用，不是模型
   动态选择的 Tool Calling。
3. **使用 Tavo 内置工具**：Tavo v1.0.0 的模型可以调用它自带的图片、文件、消息等工具。

### 当前不能宣称已支持

- Tavo Chat 直接连接外部 Leon MCP Server。
- `leon-image` 插件向 Tavo 模型动态注册 `generate_images`。

虽然 `extension_tool_search` 已进入工具发现设计，但同一版文档明确外部 MCP 和插件工具尚未接入
聊天执行链。以运行版本能力为准；功能发布后只新增 Tavo 连接配置，不重写 Leon MCP 工具。

## 推荐顺序

1. MCP Server：面试价值最高，也为未来 Tavo 接入准备标准协议。
2. 抽取 Channel-independent Service：防止 CLI / MCP / Telegram 分叉。
3. Telegram Bot：形成手机端真实使用闭环。
4. Leon Agent 接入 Tavo MCP Server：展示 Agent 同时编排 Tavo 与 ComfyUI。
5. 等 Tavo 外部 MCP Client 能力正式可用，再做 Tavo -> Leon MCP。
6. 流式输出、完成通知和基础 TUI 持续增强；完整 Codex 风格界面后置。

## 不做的捷径

- 不让 Telegram 通过模拟终端进程调用 `leon`。
- 不给 CLI、Bot、MCP 各写一份 Tool schema。
- 不把 Telegram token、Tavo bearer token 或 Leon provider key写进仓库。
- 不因为面试演示而默认触发多张真实图片。
