# Leon Trace / Observability 设计

- 日期：2026-08-17
- 状态：进行中（Core 契约与 AgentRuntime spans 已完成，SQLite/入口接入待实现）
- 前置：Agent Evaluation 与 `03-rag-lab` 最小闭环完成后进入实现
- 范围：Leon CLI、Web Gateway、唯一 `AgentRuntime`、本地 SQLite

## 结论

MVP 采用一套与 SDK 无关的本地域模型：入口创建 `trace_id + turn_id`，共享
`AgentRuntime` 记录 Agent root、iteration、LLM、Tool、Planning span，Leon 使用 SQLite
持久化脱敏 metadata。CLI 和 Web 只消费同一份 trace，不各自推导第二套事实。

第一版不引入 OpenTelemetry SDK、Collector、Jaeger 或云平台。数据模型刻意对齐
OpenTelemetry 的 trace/span 概念和 ID 长度，后续可以增加 exporter，但本地运行不依赖 exporter。

```text
session
  └─ turn_id：一次用户对话轮次
       └─ trace_id：这次轮次的一次 Agent 执行链
            └─ agent.run (root span)
                 ├─ agent.iteration #1
                 │    ├─ llm.request
                 │    ├─ planning.create / planning.update
                 │    └─ tool.call
                 ├─ agent.iteration #2
                 │    ├─ llm.request
                 │    └─ tool.call
                 └─ agent.iteration #3
                      └─ llm.request
```

`AgentEvent.turn` 当前表示 loop iteration，不等于新的 `turn_id`。实现时保留兼容字段，但新 Trace
契约统一称它为 `iteration`，避免把一次用户轮次和多轮 LLM 往返混为一谈。

## 1. 目标与非目标

### 目标

1. 用 `trace_id` 关联一次请求从 CLI/Web 入口到最终回答、失败或取消的完整执行链。
2. 用 `turn_id` 关联同一用户轮次的消息、trace 和 tool audit；重试可以产生新的 trace，而不伪造新轮次。
3. 分别测量 LLM、Tool、Planning 的耗时、状态和安全错误类型，并保留父子关系。
4. 记录模型、provider 返回的 token usage、可选的本地估算 cost、工具名称与调用次数。
5. 默认只落 metadata；prompt、tool args、tool result 和业务正文必须经过 default-deny 脱敏。
6. Trace 写入失败不能改变 Agent 的回答、工具副作用或取消语义。
7. 让一次失败能在本机被查询和解释，同时保持后续接 OpenTelemetry 的可能性。

### 非目标

- 不保存或恢复 Agent 执行状态，不做后台恢复、自动重试和补偿。
- 不扩展 Planning 为 DAG、并行 planner 或 workflow engine。
- 不创建第二套 executor；Trace 只能观察现有 `AgentRuntime`。
- 不引入 OpenTelemetry SDK、Collector、Jaeger、Prometheus 或云观测平台。
- 不做分布式跨服务追踪；Leon/ComfyUI、Tavily 等外部系统第一版只作为 Tool span 的下游边界。
- 不默认采集 prompt、回答、文件正文、Memory value、搜索 query、图片描述、URL 或异常原文。
- 不把 Trace 当作业务审计、任务队列、计费账单或 Evaluation 分数。
- 不先做复杂 Trace 管理后台；Web MVP 复用现有 Agent Timeline，CLI 提供最近一次摘要即可。

## 2. 三类质量证据

| 能力 | 回答的问题 | 第一责任人 | 典型输出 |
|---|---|---|---|
| pytest | 代码是否符合接口、状态机和安全契约 | 单元/集成测试 | pass/fail、回归用例 |
| Evaluation | Agent 在一组任务上的行为质量是否变好 | eval dataset/scorer | Task Success、Tool Selection、Plan Adherence |
| Trace | 某一次真实运行具体发生了什么、慢/错在哪里 | runtime observability | span 树、latency、tokens、tool calls、error type |

Trace 可以解释一个 Evaluation case 为什么失败，但不能用“span 都成功”替代任务质量评分；pytest 全绿也不代表
Agent 的工具选择或回答质量达标。

## 3. 标识与重试语义

### ID

- `trace_id`：32 位小写十六进制，等价于 16-byte OpenTelemetry trace ID；每次执行链新建。
- `span_id`：16 位小写十六进制，等价于 8-byte OpenTelemetry span ID；每个 span 新建。
- `turn_id`：32 位小写十六进制，标识一个逻辑用户轮次。
- ID 由本地安全随机数生成，不编码 session、时间、用户文本或 provider 信息。

### 轮次规则

- 新提交的用户消息：创建新的 `turn_id` 和 `trace_id`。
- Web 的“替换并重试当前轮”：复用原 `turn_id`，创建新的 `trace_id`，旧 trace 保留。
- CLI 当前“追加一轮”的 `/retry`：按现有消息语义创建新 `turn_id` 和新 `trace_id`。
- 相同 `turn_id` 下按开始时间最新的 trace 是当前尝试，但历史 trace 不覆盖、不删除。
- 老 SQLite 消息没有 `turn_id` 时保持 `NULL`，不根据文本或时间猜测回填。

## 4. 数据模型草案

### 运行时契约

共享层新增最小领域对象，不引用 Leon、SQLite 或 OpenTelemetry：

```python
TraceContext(
    trace_id: str,
    turn_id: str,
    session_id: str | None,
    entrypoint: Literal["cli", "web", "eval", "direct"],
)

TraceRecord(
    trace_id: str,
    turn_id: str,
    root_span_id: str,
    status: Literal["running", "ok", "error", "cancelled"],
    outcome: Literal["answered", "direct_answer", "forced_answer", "failed", "cancelled"] | None,
    llm_call_count: int,
    tool_call_count: int,
    planning_call_count: int,
)

SpanRecord(
    span_id: str,
    trace_id: str,
    parent_span_id: str | None,
    sequence_no: int,
    kind: Literal["agent", "iteration", "llm", "tool", "planning"],
    name: str,
    started_at_ms: int,
    ended_at_ms: int | None,
    duration_ms: float | None,
    status: Literal["running", "ok", "error", "cancelled"],
    error_type: str | None,
    attributes: dict[str, bool | int | float | str | None],
)
```

共享 runtime 同时服务没有 Leon session 的 `02-code-agent`，因此 `session_id` 在 Core 中允许为 `None`；
是否必须绑定 session 由 Leon 的持久化/查询适配器校验。

`TraceSink` 是 write-only contract，只包含 `start_trace`、`start_span`、`finish_span`、`finish_trace`。
查询接口不塞进 sink；第二阶段由 Leon 定义 SQLite reader/store 查询能力，避免共享 Core 反向依赖持久化形态。
生产默认使用容错包装后的 `SQLiteTraceStore`；测试使用 `InMemoryTraceSink`；未配置时使用
`NoOpTraceSink`。Trace 写入异常由容错包装吞掉并写一条不含 payload 的本地 warning，不能向 Agent loop
抛出。

### SQLite `traces`

| 字段 | 含义 |
|---|---|
| `trace_id` PK | 一次执行链 |
| `turn_id` | 逻辑用户轮次 |
| `session_id` | 现有 Leon session |
| `entrypoint` | `cli` / `web` / `eval` / `direct` |
| `root_span_id` | 根 span |
| `started_at_ms` / `ended_at_ms` | UTC epoch 毫秒；未结束时 `ended_at_ms=NULL` |
| `duration_ms` | 用单调时钟计算；未结束时 `NULL` |
| `status` | `running` / `ok` / `error` / `cancelled` |
| `outcome` | `answered` / `direct_answer` / `forced_answer` / `failed` / `cancelled` |
| `error_type` | 稳定异常类名或安全错误码，不含 message/stack |
| `model` | 单模型时为 served model；混用或未知时为 `NULL` |
| `llm_call_count` | LLM span 数 |
| `tool_call_count` | domain Tool span 数，不含 Planning |
| `planning_call_count` | Planning span 数 |
| `input_tokens` / `output_tokens` | LLM spans 汇总；provider 不返回时为 `NULL` |
| `estimated_cost_usd` | 可选本地估算；无 usage/价格时为 `NULL`，不能写 `0` |
| `pricing_id` | 使用的本地价格版本；无法估算时为 `NULL` |

### SQLite `trace_spans`

| 字段 | 含义 |
|---|---|
| `span_id` PK | span 标识 |
| `trace_id` FK | 所属 trace |
| `parent_span_id` | 父 span；root 为 `NULL` |
| `sequence_no` | 同一 trace 内稳定递增，用于还原顺序 |
| `kind` / `name` | span 类型和稳定名称 |
| `started_at_ms` / `ended_at_ms` / `duration_ms` | 生命周期 |
| `status` / `error_type` | 技术状态与安全错误类型 |
| `model` | LLM requested/served model 的最终安全值 |
| `input_tokens` / `output_tokens` | 单次 LLM usage，缺失为 `NULL` |
| `estimated_cost_usd` / `pricing_id` | 单次 LLM 估算，缺失为 `NULL` |
| `tool_name` | registry 认可的工具名；非 Tool/Planning 为 `NULL` |
| `attributes_json` | 经过类型与 key allowlist 的小型 metadata |

两个表都在现有 `LEON_SESSION_DB` 中，由 `SessionStore` 同样的增量迁移策略创建。索引至少覆盖
`traces(session_id, started_at_ms)`、`traces(turn_id, started_at_ms)`、
`trace_spans(trace_id, sequence_no)`。

现有 `messages` 增加 nullable `turn_id`；现有 `tool_calls` 只增加 nullable `trace_id`、`span_id` 做关联。
Trace 不复制 `arguments_json` / `result_json`。`ToolStep`、`AgentEvent` 和 `AgentResult` 已增加尾部 optional
correlation 字段，保持当前位置参数调用兼容；第二阶段只把这些 ID 关联到现有 audit row。

### Cost 规则

- token 只接受 provider 返回的非负整数，不用字符数猜 token。
- cost 仅从 LLM span 的 usage 计算并汇总，避免在 trace 和 span 双重计费。
- 价格来自本地、显式版本化的 model price mapping；不联网查询价格，不硬编码“当前云价格”。
- served model 优先；provider 不返回时可退回 requested model，但要在 attribute 中标明
  `model_source=requested`。
- price 不匹配或 usage 缺失时 cost 为 `NULL`，查询层显示 `n/a`。

## 5. Span 生命周期

### Agent root

1. CLI/Web 接受并校验请求后创建 `TraceContext`。
2. 在调用 direct command 或 `LeonAgent.run()` 前开始 `agent.run` root span。
3. 正常返回时结束为 `ok`；`AgentCancelled` 为 `cancelled`；其他异常为 `error`。
4. root finalizer 汇总已结束 LLM/Tool/Planning spans。工具失败后被模型正确处理时，Tool span 可为
   `error`，但 root 仍可为 `ok`。
5. 进程崩溃时已插入的 trace 保持 `running` 和 `ended_at_ms=NULL`，查询显示 `incomplete`；MVP 不在
   下次启动时伪造结束时间或恢复执行。

### Iteration

每次现有 Agent loop 的 `turn_started` 开始一个 `agent.iteration`，parent 为 root。它包含一次 LLM 请求和
该响应触发的所有 Tool/Planning 调用，直到继续下一 iteration 或返回最终回答。超过 `max_turns` 后的
closing LLM 使用额外 iteration，并标记 `closing=true`，最终 outcome 为 `forced_answer`。

这个结构让 LLM latency 不包含工具时间，又能用 iteration 父节点表达一次“模型决策 -> 执行调用”的完整
因果单元。

### LLM span

- 在 `_chat_turn()` / `_chat()` 调 provider 前开始 `llm.request`，返回或抛错时结束。
- attributes 只允许 `iteration`、`streaming`、`requested_model`、`served_model`、
  `model_source`、`message_count`、`tool_schema_count`。
- 成功记录 provider usage；缺失保持 `NULL`。
- provider/network 异常只记录 Python 异常类名，如 `APIConnectionError`，不记录异常 message、request
  body、URL、headers 或 response body。
- streaming delta 不创建 span，也不落 Trace；首 token 指标不在 MVP 范围。

### Tool span

- 在 `ToolRegistry.execute()` 外层开始，在 handler 返回/抛出/取消时结束。
- parent 为当前 `agent.iteration`；`tool_name` 只能使用 registry 的 `audit_name()` 安全视图。
- `result.ok is True` 为 `ok`；安全投影中的稳定 `error_code` 或失败结果为 `error`；取消为
  `cancelled`。
- `tool_call_count` 统计实际进入执行边界的 domain tools。参数 JSON 解析失败没有执行 handler，但仍记录
  一个 `tool` span，`error_type=invalid_arguments`，便于解释模型失败。
- side effect 已完成后再收到取消时，先结束 Tool span、保留现有 projected audit，再把 root 标为
  `cancelled`；Trace 不宣称回滚。

### Planning span

Planning 仍通过 `plan_create` / `plan_update` / `plan_get` 走唯一 ToolRegistry，但注册时声明
`span_kind=planning`，因此不会和 domain tool 重复计数。每个 planning 调用是一个短 span：

- `planning.create`：只记录 `step_count`。
- `planning.update`：只记录 `step_index` 和目标 `status`。
- `planning.get`：不记录步骤描述，只记录安全汇总计数。

MVP 不跨多个 LLM iteration 保持一个“步骤执行 span”。步骤耗时可用 `in_progress` 与终态 transition 的时间
差在查询层派生；Evaluation 继续负责 Plan Adherence。这样不会把 Planning 状态机变成第二套 executor，也不会
因取消留下需要恢复的长生命周期对象。

## 6. 接入点

### 共享 Agent loop

- `AgentRuntime.run(..., trace_context=None, trace_sink=None)` 保持可选参数；现有调用方不传时行为不变。
- Runtime 自己负责 root、iteration、LLM、Tool/Planning span，不能让 CLI/Web 手工复刻计时。
- `AgentTool` 增加默认 `span_kind="tool"`；Planning tools 显式设为 `planning`。
- `AgentResult` 返回 `trace_id`、`turn_id` 和安全汇总，取消的 `partial_result` 继续只含已完成 audit steps，
  不携带 raw transcript。
- Trace instrumentation 不能改变 LLM messages、tool schema、调用顺序、异常包装或 cancellation check 的位置。

### Leon CLI

- `process()` 在读取 history 后、进入 Agent 前创建 turn/trace；成功、失败、取消都 finalize。
- direct `/nsfw` 同样建立 root + Tool span，entrypoint 为 `direct`，但不注册 Planning。
- 正常聊天输出保持安静；`/trace` 显示当前 session 最近一次 trace 摘要与 span 树，失败提示附短
  `trace_id` 方便定位。
- `/trace` 只查本地 SQLite，不调用 LLM、Leon/ComfyUI、搜索或任何网络。

### Web Gateway

- `send_message()` 是 turn/trace 的 owner；worker thread 只接收已创建的显式 `TraceContext`。
- `assistant.started/completed/cancelled`、`agent.error`、`tool.started/finished` SSE 增加
  `trace_id`、`turn_id`，span 事件再增加 `span_id`、`parent_span_id`。这些字段用于实时关联，不替代
  SQLite Trace。
- `assistant.delta` 只带现有正文和关联 ID，不写入 Trace history，避免每 token 事件放大存储。
- 新增 session-scoped metadata-only 查询：最近 trace 列表与单个 trace/span 树；必须复用现有 token
  鉴权，并验证 trace 属于 URL 中的 session。
- 现有 Agent Timeline 可按 `trace_id` 分组展示 duration/status/model/usage；MVP 不新增独立管理后台。
- Web retry 复用被替换消息的 `turn_id`，创建新 `trace_id`；普通发送创建两者。

### Evaluation

Evaluation runner 可以注入 `InMemoryTraceSink` 帮助失败分析，但 scorer 不读取 Trace 决定 pass/fail。
fake provider 继续是默认值，Trace 测试不得触发真实 provider。trace overhead、token/cost 汇总与
Evaluation 的 `latency_ms`/usage 可以交叉校验，但保持两个输出契约独立。

## 7. 默认脱敏规则

采用 default-deny：没有显式注册的 attribute key 一律丢弃；不是 `bool/int/finite float/bounded str/None`
的值一律拒绝。单个字符串和整个 `attributes_json` 都设置小型上限，超限只记 `truncated=true`，不截取原文
落库。

### 永不进入 Trace 的内容

- system/user/assistant prompt 与 streaming delta 正文。
- tool arguments、tool result、异常 message、stack、HTTP request/response body。
- API key、Authorization、cookie、token、provider base URL、真实配置文件内容。
- File Search/FileWrite 的正文、query、匹配文本、写入内容、绝对路径。
- Memory 的 key/value/context。
- web search query、snippet、正文和 URL。
- 图片 `source_text`、任务 ID、图片 URL、工作流/LoRA 细节。
- TTS/ASR 原始文本或音频内容。

### 默认允许的 metadata

- IDs、entrypoint、iteration、span kind/name、父子关系、时间、duration、status。
- 注册过的安全 tool name、调用次数、稳定 error code/type。
- requested/served model、input/output tokens、版本化估算 cost。
- Planning 的 step count/index/status 聚合，不含 description。
- 明确由 tool adapter 注册的计数型字段，例如 `result_count`、`truncated`；第一版不默认开放 path、query、
  citation 或任意 JSON passthrough。

Trace projector 与现有 audit projector 分开：audit 允许为追责保留的安全 path/citation，不代表 Trace 也可以
保存。任何 projector 异常都 fail closed 为固定 `redaction_failed`，不能回退到 raw payload。

## 8. 与现有 audit logging 的边界

| Audit logging | Trace |
|---|---|
| 证明调用过哪个工具以及安全投影后的参数/结果 | 解释一次执行的顺序、耗时、状态和资源使用 |
| `ToolStep` -> SQLite `tool_calls` | `TraceContext` / spans -> `traces` / `trace_spans` |
| 对已完成副作用，取消后仍应保留 audit | root 可以 cancelled，但已完成 Tool span 仍是 ok/error |
| 可包含 adapter 明确允许的 path/citation metadata | 默认更严格，不复制 arguments/result |
| 是工具行为审计，但不是完整聊天记录 | 是运行诊断，但不是副作用事实源 |

关联仅靠 `tool_calls.trace_id + span_id`，不在 Trace 中复制 audit JSON。Trace 写入失败不能阻止 audit；audit
写入失败也不能通过 Trace 冒充已持久化的业务证据。SSE 是实时交付协议，不是 durable audit，也不是 Trace
事实源。

## 9. 最小测试矩阵

| 层级 | 最小验证 |
|---|---|
| ID/模型 | ID 长度与字符集、同 trace 父子合法、sequence 稳定、非法状态/非有限 duration 拒绝 |
| Runtime happy path | fake LLM：root -> iteration -> LLM -> Tool/Planning；父子关系、顺序、模型、usage 正确 |
| Runtime 多轮 | 多次 LLM/tool 往返与 closing LLM；汇总不重复计数，`forced_answer` 正确 |
| 失败/取消 | LLM 异常、tool error、planning error、取消前后副作用；status/error_type 正确且不改变原异常语义 |
| Redaction | prompt、API key marker、query、file/memory/image/TTS 正文、异常原文不出现在 event、对象 repr、SQLite、API |
| Projector fail closed | projector 抛错、返回非法类型/超大值时只出现固定 `redaction_failed` |
| SQLite migration | 旧 DB 增量建表/加 nullable 列；旧消息可读；trace/session 越界查询失败 |
| 时间 | wall clock 可查询、duration 使用 monotonic 且非负；运行中记录保留 NULL end/duration |
| Token/cost | provider usage 精确汇总；缺 usage/price 为 NULL；多 LLM span 不双计费 |
| Audit 边界 | tool_call 与 span 可关联；Trace 不复制 audit JSON；取消已完成副作用仍有安全 audit |
| CLI | `/trace` 只读显示最近 trace；成功/错误/取消均 finalize；fake clients 保证零网络 |
| Web | SSE 关联 ID、metadata-only 查询、session ownership、retry turn 复用、direct `/nsfw` fake tool trace |
| Evaluation | 开启/关闭 Trace 得到相同 AgentResult 和 eval 分数；Trace 只帮助诊断 |

验证顺序仍按仓库约定：shared core 定向测试 -> Leon Trace/CLI/Gateway 定向测试 -> Leon 项目测试 -> Ruff ->
`git diff --check`。真实运行验收只在实现完成并重启 `leon-server` 后进行。

## 10. 三个小提交的实现顺序

### 提交 1：`feat(core): add sdk-neutral agent trace contracts`

- 在 `workbench_core.agent` 增加 `TraceContext`、`TraceRecord`、`SpanRecord`、write-only `TraceSink`、
  No-op/In-memory 实现和严格 metadata projector。
- 在唯一 `AgentRuntime` 接入 root、iteration、LLM、Tool/Planning span。
- `AgentTool` 只增加默认兼容的 `span_kind`；audit/event/result 预留 optional correlation ID；不接 SQLite、
  不改 CLI/Web。
- 用 fake LLM/tool 覆盖顺序、父子、usage、异常、取消和“开关 Trace 不改变 AgentResult”。

### 提交 2：`feat(leon): persist redacted traces and audit correlation`

- 增加 SQLite `traces` / `trace_spans`、messages/tool_calls nullable correlation 列与查询接口。
- 增加 tool-specific attribute allowlist、本地版本化 cost estimator、tool audit correlation。
- Planning tools 标记为 planning span；补迁移、脱敏、cost、并发/失败隔离测试。
- 暂不增加 UI；可通过 store 测试查询完整 span tree。

### 提交 3：`feat(leon): wire trace context into cli and gateway`

- CLI/Web/direct 请求入口统一创建 turn/trace，明确 retry 语义。
- CLI 增加 `/trace`；Gateway SSE 增加 correlation IDs 与 metadata-only trace 查询。
- 复用 Agent Timeline 展示，不新增第二套前端状态模型。
- 补 CLI/Gateway provider-free 集成测试；重启 `leon-server` 后做本地真实 smoke。

每个提交都应独立通过定向 pytest、Ruff 和 `git diff --check`，且不依赖真实 API key 或网络。

## 11. 风险与取舍

### 同步 SQLite 写入增加延迟

MVP 使用本地小记录同步写入，换取简单、一致和进程退出前可见。Trace sink 必须故障隔离；先在 fake 与真实
smoke 中测 overhead，再决定是否引入有界批量 writer。第一版不为性能预判增加后台队列。

### 进程崩溃留下 running trace

保留 `running + NULL ended_at` 比启动时猜测失败原因更诚实。查询层显示 incomplete；不自动恢复、不自动
重试，也不修改 Agent 状态。

### audit 与 Trace 重复或泄露

通过“Trace 不复制 args/result，只保存 stricter metadata，并用 span_id 关联 audit”控制。任何新增 tool
attribute 必须有 marker-based 泄露测试，不能把 audit projection 无条件整包复用。

### provider usage 与价格不可靠

usage 缺失就显示 `n/a`；cost 标注 `estimated` 和 `pricing_id`，不作为账单。模型混用时在 span 层保留，
trace summary 不伪造单一 model。

### Trace 改变运行语义

instrumentation 只能包围现有边界，不能调整消息、tool 顺序、cancel check 或异常包装。用开启/关闭 Trace
得到相同 `AgentResult` 和 Evaluation 分数作为硬回归。

### 本地数据增长

metadata 比正文小，但仍会增长。MVP 先提供按 session/时间查询和显式清理接口设计，不做后台清理任务；
观察真实规模后再确定保留天数/条数，避免未经确认删除诊断证据。

### OpenTelemetry 兼容与当前简洁性的冲突

第一版只对齐 trace/span ID、parent、时间、status 和 attributes 语义，不照搬 SDK。后续 exporter 把
`SpanRecord` 映射到 OpenTelemetry；本地 SQLite 仍是默认 sink，是否接 collector 由部署场景决定。
