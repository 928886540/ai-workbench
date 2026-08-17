# Leon Agent 扩展路线

- 记录日期：2026-08-14
- 状态：Core Agent 基线已完成；当前主线为 **Evaluation → RAG → Trace/Observability**
- 当前基线：独立 `leon` CLI、共享 Agent Runtime、7 个 Leon 生图工具、`speak_text`、SQLite 会话、
  `LeonToolService` 和第一版 MCP Server 已跑通

## 历史需求与候选扩展

1. 做一个适合面试现场展示的 Leon MCP Server。
2. 通过 Telegram Bot 直接聊天、发起生图并接收结果。
3. 研究 Tavo 与 Leon 工具互通，让 Tavo 场景也能使用 Leon 能力。
4. 长期把 CLI 体验增强到接近成熟 Agent 产品，但不让 TUI 阻塞工具与协议主线。

## 可行性结论

| 方向 | 可行性 | 难度 | 当前判断 |
|---|---|---|---|
| 面试 MCP | 高 | 中低 | **第一版已完成**：5 个工具、stdio/Streamable HTTP、fake service 测试、协议 smoke 和演示脚本 |
| Telegram Bot | 高 | 中 | 需要 channel adapter、用户/session 映射、后台任务轮询和图片回传 |
| Leon Agent 连接 Tavo MCP | 高 | 中 | Tavo 已有 MCP Server；Leon Agent 可作为 MCP Client 使用 Tavo 工具 |
| Tavo 聊天直接调用 Leon MCP | 当前受限 | 非代码难度 | Tavo v1.0.0 文档明确：外部 MCP Server 尚未接入聊天工具调用 |
| Tavo 插件贡献 Leon 模型工具 | 当前受限 | 非代码难度 | Tavo v1.0.0 文档明确：插件暂不能贡献模型工具 |
| Codex 风格完整 TUI | 可行 | 高 | 基础 TUI 不难，成熟交互、流式、并发任务和恢复需要持续迭代 |
| Leon 本地 File Search MVP | 高 | 中低 | **第一版已实现**：只读 roots、`list_files` / `file_search` / `read_file`、路径隔离和预算限制 |
| Leon per-turn Planning | 高 | 中 | **第一版已实现**：三个 planning tools、顺序状态机、turn reset 和 metadata-only audit |

## 路线调整（2026-08-17）

Leon Agent 的功能面已经足够作为面试项目：Runtime、Tool Calling、Planning、Memory、File Safety、
Cancellation、MCP、Streaming/SSE 和 Web/API 均有真实代码与回归。下一阶段不再以“增加更多工具”为目标，
而是证明 Agent 是否真的工作得更好：

```text
Evaluation（先建立可重复基线）
        ↓
RAG（把 lexical File Search 扩展为 semantic retrieval）
        ↓
Trace / Observability（解释每一步为什么慢、为什么失败）
```

这条顺序比继续扩展 Planning 或立即拆 Multi-Agent 更重要。Planning 先冻结在 MVP，Multi-Agent 后置到
质量三件套完成，并由 Evaluation 数据决定是否值得做，不用架构复杂度替代证据。

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

CLI、Telegram 和 MCP 不能各复制一套生成逻辑。现有图片 handler 已收口为
`LeonToolService`，入口只负责协议转换和展示；Telegram 后续沿用这一边界。

## Phase A：面试 MCP（已完成）

在 `projects/04-mcp-lab/leon-mcp-server` 已实现独立 MCP Server，第一版开放：

- `list_image_modes`
- `check_image_environment`
- `generate_images`
- `get_image_tasks`
- `get_recent_images`

已支持：

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

可复现脚本：`uv run python projects/04-mcp-lab/leon-mcp-server/scripts/mcp_smoke.py`；默认只做
`initialize/tools/list`，加 `--check-environment` 才调用只读环境检查。

## Search MVP Checkpoint（2026-08-16）

- **已完成**：CLI 与 Web Gateway 共用可选 `web_search`，后端配置 `TAVILY_API_KEY` 后注册。
- **边界稳定**：`search/provider.py` 适配 Tavily，`search/service.py` 校验并标准化证据，
  `tools.py` 只声明 Agent tool schema；搜索不进入图片 `LeonToolService`。
- **成本默认值**：`basic` depth、5 条结果，允许通过 `TAVILY_MAX_RESULTS` 调整到 `1..10`；
  `advanced` 只用于用户明确要求的深入研究。
- **已验证**：`uv run pytest projects/02-leon-agent/tests/test_search.py -q`，16 个测试通过，
  不消耗真实 Tavily credits。
- **后续缺口**：`extract/crawl/map`、正文读取、缓存、credits 预算、备用 provider 和 MCP 暴露
  均未实现；详细接手契约见 `projects/02-leon-agent/docs/web-search.md`。

## File Search MVP Checkpoint（2026-08-16）

- **已实现**：共享 `workbench_core.files.FileSearchService`，Leon CLI 与 Web Gateway 按配置注入同一套
  `list_files`、`file_search`、`read_file` 工具；`02-code-agent` 的旧 Workspace 工具保留兼容适配。
- **配置边界**：`LEON_FILE_ROOTS` 是 `root_id -> absolute path` 的 JSON allowlist，最多 8 个 root；
  未配置时不注册文件工具，不能因为 File Search 配置错误而静默扩大目录范围。
- **安全边界**：只读、相对路径、每次 resolve 后 containment 检查；跳过 symlink/reparse point、隐藏/系统
  项、`.env`/密钥/凭据/SQLite 和不支持的 binary；单文件 1 MiB、搜索 2,000 文件/20 MiB/50 命中、读取
  200 行/16,000 字符。
- **结果契约**：只返回 root id、相对路径、行号、citation 和 `untrusted_content=true`；文件内容不能修改
  system prompt，也不能授权写入或其他工具调用。详细参数见 `projects/02-leon-agent/docs/file-search.md`。
- **当前缺口**：尚未做 PDF/DOCX 解析、embedding/RAG、增量索引、文件写入或 File Search MCP 暴露；
  临时测试目录的运行态验收作为 MVP 遗留补齐，不改变 Evaluation 基线完成后进入 `03-rag-lab` 的顺序。

## Planning MVP Checkpoint（2026-08-17）

- **执行边界**：继续复用唯一的 `AgentRuntime` tool-calling loop，不另建 planner executor；复杂任务由
  `plan_create` / `plan_update` / `plan_get` 显式记录计划，普通聊天和单工具请求不规划。
- **状态约束**：每 turn 最多一个 2..8 步顺序计划，只允许
  `pending -> in_progress -> completed|failed`，同一时间最多一个活动步骤，下一 turn 自动清空。
- **审计边界**：raw 步骤描述只给当前 LLM；Event、ToolStep、SSE 和 SQLite 只保存 count/index/status。
  Planning 不授权文件/Memory 写入，不注册到 direct `/nsfw` 或 Leon MCP。
- **冻结边界**：不继续做跨 turn 后台恢复、DAG/并行 planner、自动重试、第二套 executor 或管理 UI；
  后续只允许 Evaluation 数据驱动的最小遵循性修正。

## Quality Phase 1：Agent Evaluation（当前优先级）

目标不是再证明“代码能跑”，而是回答“Planning/Tool Calling 加进去以后是否真的更好”。

### 最小数据集

第一版建立 `projects/02-leon-agent/evals/`，先放 20 个可审查 case，再扩到 50 个。每个 case 至少包含：

- `id`、用户原话、隔离 fixture/config 和是否允许真实 provider。
- 必须调用的工具、禁止调用的工具、是否必须创建计划、计划最小/最大步骤数。
- 最终答案断言：必须包含的结论/引用、不能出现的内部 ID 或敏感内容。
- 取消、工具失败、无 roots、无搜索 Key 等负向场景。

默认使用 fake LLM/provider，live eval 必须显式 opt-in，不能让测试消耗 Tavily、ComfyUI 或真实密钥。

### 第一批指标

```text
Task Success            任务是否满足最终断言
Tool Selection          必须/禁止工具是否符合约束
Plan Adherence          计划创建、顺序推进、终态完成率
Safety                  越权路径、敏感值、取消和审计是否合规
Latency                 总耗时与 LLM 各轮耗时
Tool Calls              工具调用数与无效调用数
Tokens / Cost           input/output token 与估算成本（provider 有值才统计）
Answer/Citation Quality 最终答案与证据是否足够
```

测试通过率仍然保留，但它和 eval score 分开报告：前者证明实现没有回归，后者证明 Agent 行为满足任务。
每次修改 Planning、Memory、Tool schema 或 system prompt，都必须重跑 eval 并保存 baseline/diff。

## Quality Phase 2：RAG（Evaluation 基线后）

把现有 File Search 的 exact/lexical retrieval 演进为独立 `03-rag-lab` 能力，不把向量库直接塞进 Leon
生产工具：

```text
Document → Chunker → Embedding → Vector Store → Retriever → Citation Context → Leon
```

落地顺序固定为 chunk → embedding → retrieval → citation。

第一版先支持 Markdown/TXT/JSON，复用当前 roots 的安全边界与 citation 规则；再评估 Qdrant/本地向量库、
增量索引、rerank 和 PDF/DOCX。RAG 必须有独立 retrieval eval：`Recall@K`、`MRR`、citation precision、
answer faithfulness，不能只用“问起来像能答”验收。

## Quality Phase 3：Trace / Observability（RAG 闭环后）

在现有 `AgentEvent`、SSE 和 SQLite audit 之上统一：

```text
trace_id / turn_id
  ├─ LLM span：model、latency、tokens、error
  ├─ Tool span：tool_name、latency、success/error
  ├─ Planning span：step_index、status
  └─ Final：total_latency、total_tokens、tool_calls、outcome
```

Trace 只记录脱敏 metadata；文件正文、Memory raw value、搜索 query 和 provider secret 仍不能进入持久
观测。先解决可查询、可聚合、可关联，再决定是否接 OpenTelemetry，不为接 SDK 而接 SDK。

## 暂停清单与面试表达

- **Planning**：冻结在顺序状态机 MVP；暂不做 DAG、并行 planner、自动重试、后台恢复、第二套 executor
  或十层 workflow。
- **Multi-Agent**：质量三件套完成前不做。等 Evaluation 数据证明 single-agent 的明确瓶颈，再用对照实验
  说明拆分收益。
- **更多业务工具 / Telegram / Tavo 互通**：作为渠道与集成候选，排在质量三件套之后，不阻塞当前主线。
- **LangGraph**：不作为目标；先能解释自研 Runtime 与框架的边界和取舍。

面试时要明确区分：`430 tests passed` 说明代码行为符合契约；Evaluation 才说明 Agent 在任务层面变好了。
推荐用一句话概括当前架构：

> 以唯一 AgentRuntime 驱动 LLM↔Tools，多轮 Planning 只是受约束的 state capability；现在用 Evaluation
> 量化工具选择和任务完成，再用 RAG 和 Trace 把检索质量与运行原因补齐。

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

1. Agent Evaluation：20 → 50 个 case，建立 baseline、指标和回归报告。
2. RAG：在 `03-rag-lab` 做 chunk/embedding/retrieval/citation，并用独立指标验收。
3. Trace / Observability：统一 trace/turn/span 与脱敏 metrics，解释延迟、失败和成本。
4. Telegram Bot / Leon Agent → Tavo MCP：作为渠道和集成层接入，不复制 Agent/Tool schema。
5. Multi-Agent：质量三件套完成，且 Evaluation 证明单 Agent 有明确瓶颈后再做对照实验。

## 不做的捷径

- 不让 Telegram 通过模拟终端进程调用 `leon`。
- 不给 CLI、Bot、MCP 各写一份 Tool schema。
- 不把 Telegram token、Tavo bearer token 或 Leon provider key写进仓库。
- 不因为面试演示而默认触发多张真实图片。
