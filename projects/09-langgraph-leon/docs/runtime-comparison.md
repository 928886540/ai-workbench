# Self-built Leon 与 LangGraph Framework Edition 对照

## 结论

这不是“哪个 Runtime 全面更好”的框架评测，而是一次受控替换实验：

- 业务层固定为同一个 `AgentTool + ToolRegistry`、Memory service 和 RAG service。
- Self-built Leon 使用自研 `AgentRuntime` 负责工具循环、取消、Trace 和审计投影。
- Framework Edition 使用 LangGraph `StateGraph`、`ToolNode` 和 checkpointer 负责状态编排与恢复。
- 两边只在编排层分叉，不复制 Tool schema、handler、权限策略或检索实现。

因此可以得到一个可验证的工程结论：自研 Runtime 更轻、更容易按产品约束定制；LangGraph 对显式
State、Checkpoint、Interrupt 和跨进程 Resume 的抽象更成熟。框架减少了通用编排代码，但不会替应用
自动解决权限、敏感数据、幂等性、外部副作用和恢复策略。

## 对照边界

```text
                         Canonical business layer
                 AgentTool + ToolRegistry + services
                              |
                 +------------+------------+
                 |                         |
                 v                         v
       Self-built AgentRuntime      LangGraph StateGraph
       messages + for loop          MessagesState + node/edge
       cancel + trace               checkpoint + interrupt
                 |                         |
                 +------------+------------+
                              |
                    File / Web / Memory / RAG
```

这次对照覆盖 Chat、Tool Calling、Planning、Memory、RAG/Search 和 Checkpoint/Resume。Web、TTS/ASR、
Gallery、SSE、MCP、Coding Agent 和 Multi-Agent 不进入 Framework Edition。

Image Tool 也不再追加。共享 `rag_search` 已经用同一个 Tool 实例、同一个 Registry 和相同参数跑过两套
Runtime，并证明 raw observation 完全一致；再接一个 Image Tool 只会重复证明适配器可复用，同时引入
外部副作用、异步任务和环境依赖，不能增加本次 Runtime 对照的有效信息。

## 核心差异

| 维度 | Self-built Leon | LangGraph Framework Edition | 工程判断 |
|---|---|---|---|
| 工具循环 | `AgentRuntime` 显式维护 LLM -> Tool -> Observation 循环 | `agent` node、`ToolNode` 与 conditional edge | 简单循环自研更直观；分支增多后 Graph 更清晰 |
| 状态 | 当前 turn 的 message、step、usage 由 Runtime 管理，会话持久化在产品层 | `MessagesState` 和 `plan` 是 Graph State | 需要显式工作流状态时 LangGraph 更自然 |
| Tool 契约 | 直接调用 `ToolRegistry.execute()` | 薄适配成 `StructuredTool` 后仍调用同一 Registry | 业务 Tool 不应绑定 Runtime |
| Planning | Leon 的 per-turn Planning service/tools | 可选 `plan` node，2 到 4 步保存在 State | Framework 版更适合展示 node/state；两边都不做 DAG executor |
| Memory | `MemoryService` + 三个 canonical tools | 复用相同 service/tools | Memory 是业务能力，不应重建存储 |
| RAG | canonical `rag_search` Tool | 同一个 Tool 经 `ToolNode` 执行 | raw observation parity 已验证 |
| 取消/暂停 | 协作式 cancellation，关注立刻终止当前 turn | durable interrupt，关注在节点边界暂停并恢复 | 两者解决的问题不同，不能混为同一个能力 |
| 恢复 | 产品层保存 session/message；Runtime 本身不保存执行游标 | checkpointer 保存 channel/state/pending writes | 长流程和人工审批更适合 LangGraph |
| 可观测 | Runtime 原生发 AgentEvent，并记录 Trace span | 当前实验只显示 node 流转 | Framework 不会自动继承 Leon 的 Trace 语义 |
| 安全 | audit projection、取消后的安全 partial result | 完整 checkpoint 加密、opaque thread id、恢复工具白名单 | 安全策略仍必须由应用实现 |
| 失败语义 | max turns 后强制收口；工具错误进入 observation | 按 node/checkpoint 位置恢复 | Graph 恢复前必须分析节点副作用和幂等性 |

## Provider-free 证据矩阵

| 问题 | 可观察结果 | 自动化证据 |
|---|---|---|
| 是否复用了同一个 Tool handler/schema | 适配前后 schema 一致，handler 没有复制 | `tests/test_tool_adapter.py` |
| 两边是否给模型相同 RAG observation | 相同参数下 raw JSON 字符串和解析结果完全相等，handler 各执行一次 | `tests/test_rag_runtime_parity.py` |
| 多 case 下是否保持相同行为 | 10 个 direct/File/RAG/error/multi-step case 均为 `10/10` success，observation parity `10/10` | `tests/test_runtime_comparison.py`、`docs/provider-free-comparison.md` |
| Graph State 是否保存计划 | `plan` 进入 State，但只在启用 planner 时注入模型 | `tests/test_graph.py`、`tests/test_framework_planning.py` |
| Memory 是否复用且避免自动上下文落 checkpoint | 使用 Leon `MemoryService`；自动上下文只临时注入 | `tests/test_composition.py`、`tests/test_graph.py` |
| interrupt 是否真的停在工具执行前 | 暂停前 handler 调用数为 0，恢复后为 1 | `tests/test_graph.py` |
| 进程关闭后能否恢复 | 关闭并重开 SQLite 后从 pending checkpoint 继续，不重投 HumanMessage | `tests/test_checkpointing.py`、`tests/test_framework_cli.py` |
| checkpoint 是否泄漏原文 | DB/WAL/SHM 不出现 User、AI、File、Memory 原文，重开后仍可认证解密 | `tests/test_checkpointing.py` |
| 错 key、旧明文库和危险恢复是否 fail closed | 拒绝错 key/明文 mixed row；pending 写工具拒绝跨进程自动恢复 | `tests/test_checkpointing.py`、`tests/test_framework_cli.py` |

只有 Tool/RAG case 是严格的 A/B parity：输入、Tool 和 observation 都相同。State、取消与恢复并不是两套
Runtime 的同名实现，因此用相同观察维度比较职责和语义，不伪造一个没有意义的统一分数。

### 受控任务集

`leon-runtime-compare` 把严格 A/B 范围扩展为 10 个确定性 case：直接回答、两种文件读取、缺失文件错误
回注、三种 RAG 查询、两种双工具顺序和一条三步工具链。两边共享同一个 `ToolRegistry`，scripted model
只负责发出相同工具调用，不访问 provider。每个 case 交替执行两套 Runtime 并取 7 次中位数，避免固定
先后顺序造成单边热缓存偏差。

当前快照：

- Self-built task success：`10/10`
- LangGraph task success：`10/10`
- raw observation parity：`10/10`
- 每个 case 的模型调用轮数相同

本机耗时只用于观察纯编排开销，不能代表真实 LLM 延迟或框架的普遍性能结论。逐 case 数据与代码物理行数
快照见 [Provider-free Runtime Comparison](provider-free-comparison.md)。

## State 与恢复

Self-built `AgentRuntime` 的核心状态属于一次调用：message transcript、tool steps、usage 和 Trace context。
SQLite session 由 Leon 产品层负责，所以 Runtime 可以保持小而直接，但它没有“恢复到某个执行节点”的
一等语义。

Framework Edition 将 `messages` 与可选 `plan` 放进 Graph State。checkpointer 在 superstep 边界保存
checkpoint 和 pending writes，因此可以：

1. 在 `ToolNode` 前暂停。
2. 关闭进程。
3. 用同一个 opaque `thread_id` 重开数据库。
4. 以 `graph.stream(None, config)` 继续，而不是重投用户消息。

代价是应用必须理解恢复点。`web_search` 虽然只读，但会消耗外部配额；如果外部请求完成后、pending write
落盘前进程崩溃，恢复可能按 at-least-once 语义再次搜索。Memory 写工具涉及用户 consent，所以跨进程
pending 恢复直接拒绝，不尝试猜测原始授权。

## Interrupt 与 Cancellation

这两个概念很容易在面试中说混：

- Cancellation 是“当前 turn 不要再跑了”。Self-built Runtime 用 cancel event 协作式通知 LLM 和长工具，
  已完成副作用会先保留脱敏审计，再抛出 `AgentCancelled`。
- Interrupt 是“在一个可恢复边界暂停”。LangGraph 把暂停位置写入 checkpoint，之后可以由另一个进程继续。

当前 Framework Edition 使用 `interrupt_before=["tools"]` 验证机制，不注册高风险写工具。真实审批流还需要
审批身份、授权有效期、状态变更检测和幂等键；仅仅能 `resume` 不等于审批安全已经完成。

## Checkpoint 安全边界

完整 `ToolMessage` 对恢复很重要：删除 observation 可能导致状态失真或重复工具调用。但 File/Memory/RAG
结果也可能包含敏感正文，因此 Framework Edition 选择完整状态的 authenticated encryption at rest：

- 使用 LangGraph 官方 `EncryptedSerializer` 和 AES-EAX 加密 checkpoint/pending writes。
- SQLite metadata 不经过 serializer，所以调用方 metadata 被丢弃，thread id 只允许稳定 opaque 值。
- 启动时拒绝旧明文/mixed row、错 key 和缺 key，不静默生成新 key 覆盖旧状态。
- key 存在相邻 sidecar，只证明 SQLite/WAL 中没有可读原文，不宣称达到系统级密钥托管。
- 自动 Memory context 根本不进入 `MessagesState`；显式工具 observation 则保留并加密，以维持恢复语义。

## 代码复杂度快照

在提交 `74f7282` 上，选取核心编排边界做一次可复核快照：

| 边界 | 文件 | 物理行数 |
|---|---|---:|
| Self-built | `workbench_core/agent/runtime.py` | 680 |
| Self-built | `workbench_core/agent/tools.py` | 112 |
| Framework | `leon_framework/graph.py` | 62 |
| Framework | `leon_framework/tool_adapter.py` | 35 |
| Framework | `leon_framework/planning.py` | 73 |
| Framework | `leon_framework/checkpointing.py` | 179 |

这个数字不能直接推出 LangGraph “少写了多少代码”：Self-built Runtime 同时包含 streaming、协作取消、
AgentEvent、Trace、usage 和 audit projection，而 Framework 统计没有包含 LangGraph/LangChain 库内部代码，
也尚未复刻这些产品能力。它只能说明两点：

1. `StateGraph + ToolNode` 确实让基础 node/edge/tool loop 很薄。
2. 一旦进入安全 checkpoint 和恢复策略，应用侧仍然会出现不可省略的代码。

## 什么时候选哪一种

选择 Self-built Runtime：

- 工作流主要是线性的 LLM/tool loop。
- 产品已经有自己的 Session、Trace、取消和审计系统。
- 需要极细的流式事件、错误映射或工具执行控制。
- 团队愿意自己维护循环语义和边界测试。

选择 LangGraph：

- 流程包含多个显式节点、条件分支或人工审批。
- 需要 durable checkpoint、跨进程 resume 或可视化工作流状态。
- 希望使用统一的 State/reducer/node/edge 心智模型组织复杂流程。
- 团队能承担 checkpoint 数据治理、版本迁移、幂等性和框架升级成本。

Leon 当前不迁移到 LangGraph。Self-built Leon 是完整产品和原理证明；Framework Edition 是独立对照实验。
只有未来业务出现复杂、长时间、可恢复的有状态流程，才基于数据重新评估编排层，而不是为了使用框架而迁移。

## 面试表达

30 秒版本：

> 我先自研了最小 AgentRuntime，亲自处理 Tool Calling、Observation、多轮循环、Cancellation、Trace 和
> 审计；然后保留同一套 Tool、Memory、RAG，另外做了 LangGraph Framework Edition。对照后我认为，
> 自研 Runtime 在线性流程里更轻、更可控，LangGraph 在显式 State、Checkpoint、Interrupt 和跨进程恢复上
> 抽象更成熟。框架能减少编排代码，但权限、敏感数据和副作用恢复仍然必须由业务负责。

追问“为什么一开始不用 LangGraph”：

> 因为先实现最小 Runtime 能让我真正理解模型工具选择、observation 回注、循环终止和取消边界，而不是只会
> 调 `create_agent()`。学完原理后我用相同 Tool 契约做框架版，RAG parity test 证明两边拿到完全相同的
> raw observation，所以比较的是 Runtime，不是两套业务实现。

追问“LangGraph 最大价值是什么”：

> 不是让 Tool Calling 突然变强，而是把有状态工作流变成一等模型：State、Node、Edge、Checkpoint、
> Interrupt 和 Resume 可以组合。尤其是进程退出后的执行位置恢复，自研 Runtime 如果自己补，会很快进入
> checkpoint schema、pending write、幂等和迁移问题。

## 可声明与不可声明

可以声明：

- 已完成 LangChain 基础组件体验和 LangGraph Framework Edition。
- 两套 Runtime 复用同一个 `AgentTool + ToolRegistry` 业务契约。
- 共享 `rag_search` 的 raw observation parity 已通过 provider-free 自动化测试。
- Framework Edition 支持加密 SQLite checkpoint 和跨进程 resume。

不可声明：

- 不宣称原 Leon 已改造成 LangGraph。
- 不宣称原 Leon 或 Framework CLI 已启用 live RAG 索引。
- 不宣称 LangGraph 版拥有原 Leon 全部 Web、语音、图片、Trace 和取消能力。
- 不宣称 sidecar key 等同于 Windows DPAPI、TPM 或企业 KMS。
- 不宣称所有外部 Tool 都具备 exactly-once 恢复语义。

## 验证命令

```powershell
uv run --package leon-agent-framework leon-graph --demo
uv run --package leon-agent-framework leon-graph --interrupt-demo
uv run leon-runtime-compare --repeats 7
uv run pytest -q projects/09-langgraph-leon/tests
uv run pytest -q projects/09-langgraph-leon/tests/test_rag_runtime_parity.py
```

这些命令默认不请求真实模型、Tavily 或外部文件目录。live CLI 只有在显式执行
`uv run --package leon-agent-framework leon-graph` 时才读取
`%USERPROFILE%\.leon\config.toml` 并访问所配置的 provider。
