# Agent Runtime、LangChain 与 LangGraph 理论和源码导读

> 目标不是背框架 API，而是看懂 Leon 的两套 Runtime，能从一次用户请求讲到模型、工具、状态、恢复和安全边界。

## 1. 学完后必须能回答什么

不要用“看完了多少页”衡量进度。合上文档后，能独立回答下面 8 个问题才算掌握：

1. LLM 返回 Tool Call 后，究竟是谁执行了 Python 函数？
2. Tool Call、Tool Result 和 Observation 分别是什么？
3. Self-built Leon 的 Agent Loop 在哪里，终止条件有哪些？
4. LangGraph 版的 State、Node、Edge、Conditional Edge 分别落在哪段代码？
5. 为什么两套 Runtime 可以复用同一批工具，而不需要重写业务 handler？
6. Session、Graph State、Checkpoint 和 Long-term Memory 为什么不是一回事？
7. Cancellation 和 Interrupt/Resume 分别解决什么问题？
8. 为什么“有 checkpoint”仍然不代表外部副作用只会执行一次？

建议每一节按这个循环学习：

```text
先读概念
  -> 找到对应源码符号
  -> 手画数据流
  -> 跑 provider-free 测试
  -> 不看文档口述一遍
```

真实模型不是本手册的前置条件。先用 fake model 和本地工具理解控制流，可以避免 429 干扰，也更容易复现。

## 2. 一张核心心智模型

Agent 不是“更聪明的模型”，而是一套围绕模型建立的运行时协议：

```text
用户请求
   |
   v
消息上下文 -----> 模型推理
                    |
             +------+------+
             |             |
          最终答案       Tool Call
             |             |
             |             v
             |       Runtime 校验并执行工具
             |             |
             |             v
             |        Tool Result
             |             |
             |             v
             |      作为 Observation 回填
             |             |
             +<------------+
             |
             v
            结束
```

最重要的一句话：

> 模型只生成“调用哪个工具、传什么参数”的结构化意图；Runtime 才真正执行本地函数，并把结果作为新消息交回模型。

因此，一个最小 Agent 可以写成下面的伪代码：

```python
messages = [system_message, user_message]

for _ in range(max_turns):
    response = model(messages, tools=tool_schemas)
    if not response.tool_calls:
        return response.content

    messages.append(response.ai_message)
    for call in response.tool_calls:
        arguments = parse_and_validate(call.arguments)
        result = tool_registry.execute(call.name, arguments)
        messages.append(tool_message(call.id, result))

raise MaxTurnsExceeded()
```

Self-built Leon 是把这个循环及其工程边界显式写出来；LangGraph Leon 是把循环表达成 State、Node 和 Edge，再把调度与 checkpoint 交给框架。

## 3. Agent 基础理论

### 3.1 Message 是协议，不只是聊天记录

典型消息角色：

| 类型 | 作用 | 当前项目中的例子 |
|---|---|---|
| System | 定义行为边界、注入临时上下文 | Leon system prompt、Memory context、plan context |
| Human/User | 用户本轮输入 | `AgentRuntime._run()` 组装的 user message |
| AI/Assistant | 模型回答，或带 Tool Call 的模型消息 | `ChatTurn.raw_message`、LangChain `AIMessage` |
| Tool | 某次 Tool Call 对应的执行结果 | Self-built 的 `role=tool`、LangGraph `ToolMessage` |

Tool Message 必须关联 Tool Call ID，因为一条 AI Message 可能包含多个调用。模型需要知道每个结果对应哪个调用。

### 3.2 Tool Calling 不等于函数已经执行

一次完整工具调用至少包含四步：

1. Runtime 把工具名称、描述和参数 JSON Schema 发给模型。
2. 模型返回 Tool Call，例如 `read_file({"path": "README.md"})`。
3. Runtime 查找工具、校验参数、执行 handler。
4. Runtime 把结果包装成 Tool Message，再调用模型。

所以“模型会调用工具”是一种方便说法。准确说法是：模型会生成符合协议的 Tool Call，执行权始终在应用侧。

### 3.3 Observation 是模型看到的外部证据

Observation 不是一个特定 Python 类，而是 Agent 设计中的语义：工具执行后回填给模型的结果。

在两套 Runtime 中，它分别表现为：

- Self-built：`messages.append({"role": "tool", ...})`
- LangGraph：`ToolNode` 生成 `ToolMessage`，由 `MessagesState` 保存

如果 Observation 没有回填，模型只知道自己“请求过工具”，不知道工具返回了什么，就不能基于证据继续推理。

### 3.4 Agent Loop 的三个常见终止条件

1. **正常结束**：模型没有再返回 Tool Call，而是返回最终文本。
2. **直接返回**：某个工具被声明为 `return_direct`，Runtime 直接把工具结果格式化为答案。
3. **强制收口**：超过最大轮数后，Runtime 禁止继续无限调用工具，要求模型依据已有证据回答。

此外还有取消、异常、进程退出等非正常终止。

### 3.5 Structured Output 和 Tool Calling 不要混淆

两者都使用 schema，但目的不同：

| 概念 | 目的 | 是否天然有副作用 |
|---|---|---:|
| Structured Output | 让模型的回答符合指定数据结构 | 否 |
| Tool Calling | 让模型请求应用执行某项能力 | 取决于工具 |

例如，`PlanOutput(steps=[...])` 是结构化输出；`memory_upsert(...)` 是工具调用。前者描述数据，后者可能改变外部状态。

### 3.6 Planning 是显式中间状态，不是魔法

Planning 通常是先让模型输出有限、可校验的步骤，再把计划交给执行节点。它的价值是：

- 让复杂任务的意图更可见；
- 可以在执行前检查计划；
- 可以把计划放进 State，用于恢复和审计。

它的成本也很明确：多一次模型请求、更高延迟、计划可能过时，而且“写了计划”不代表“按计划完成”。

当前 Framework Edition 的 planner 只生成 2 到 4 个有序步骤，不是 DAG 调度器，也不做并行任务分解。

## 4. LangChain：组件抽象层

LangChain 在这个仓库里的学习目标是看懂通用组件，不是再做一个产品。

入口：[08-langchain-lab](../../projects/08-langchain-lab/README.md)

| 抽象 | 本质 | 本地源码 |
|---|---|---|
| ChatModel | 统一模型调用接口 | [`model.py`](../../projects/08-langchain-lab/src/langchain_lab/model.py) |
| Prompt | 可组合、可填参的消息模板 | [`prompt.py`](../../projects/08-langchain-lab/src/langchain_lab/prompt.py) |
| Runnable / LCEL | 把组件连接成统一的 `invoke` 数据流 | [`structured_output.py`](../../projects/08-langchain-lab/src/langchain_lab/structured_output.py) |
| Structured Output | 用 Pydantic 约束模型输出 | [`models.py`](../../projects/08-langchain-lab/src/langchain_lab/models.py) |
| Tool | 名称、描述、输入 schema 和函数的组合 | [`tool.py`](../../projects/08-langchain-lab/src/langchain_lab/tool.py) |
| Retriever | `query -> Document[]` 的检索接口 | [`retriever.py`](../../projects/08-langchain-lab/src/langchain_lab/retriever.py) |
| High-level Agent | 框架提供的预制 Agent 入口 | [`agent.py`](../../projects/08-langchain-lab/src/langchain_lab/agent.py) |

### 4.1 ChatModel

`build_chat_model()` 把 Leon/Workbench 的配置转换成 `ChatOpenAI` 参数。这里的 `ChatOpenAI` 只是 OpenAI-compatible 协议客户端，不代表只能调用 OpenAI 官方模型。

需要看懂的边界：

- `model` 决定请求哪个模型；
- `base_url` 决定请求发往哪个兼容服务；
- `api_key` 用于认证；
- `timeout`、`max_retries` 是调用策略；
- LangChain 统一了调用接口，但不能替应用决定密钥来源和安全策略。

### 4.2 Prompt

`ChatPromptTemplate.from_messages()` 保存的是模板，不会立即请求模型。`.partial(...)` 提前绑定固定字段，剩余字段在 `invoke` 时传入。

本项目把 Prompt 单独放在一个文件，是为了让“提示词”和“解析规则”保持清晰边界，而不是因为每个 Prompt 都必须单独建模块。

### 4.3 Runnable 与 LCEL

下面这行是 LCEL（LangChain Expression Language）：

```python
chain = prompt | model | parser
```

它表达的是数据变换：

```text
输入 dict
  -> PromptValue / messages
  -> 模型响应
  -> Pydantic 对象
```

管道两侧实现统一 Runnable 协议，因此组合后仍然可以 `.invoke()`。这里的 `|` 不是并行，也不是 shell pipe；它构建一个可执行组件图。

### 4.4 Structured Output

`PydanticOutputParser` 做两件事：

1. 从 Pydantic 模型生成 format instructions，告诉模型目标结构；
2. 把模型文本解析并校验为 Pydantic 对象。

注意：只在 Prompt 中要求 JSON 不等于可靠校验。真正的边界是解析失败时应用必须明确处理错误，不能把不合法结果继续当成可信对象。

### 4.5 Tool

`@tool(args_schema=RunbookLookupInput)` 把普通函数包装成 LangChain Tool。一个工具至少需要：

- 稳定、唯一的名称；
- 让模型知道何时使用它的描述；
- 有界的输入 schema；
- 真正执行业务逻辑的 handler。

Schema 只能约束“参数长什么样”，不能替代授权。例如 `path: str` 合法，不代表这个路径允许读取。Leon 的文件工具仍要在 handler/service 层检查 workspace root。

### 4.6 Retriever 与 RAG

Retriever 只是接口：

```text
query -> relevant Documents
```

RAG 是完整流程：

```text
文档加载 -> chunk -> embedding/index -> retrieve -> context assembly -> generation -> citation
```

`VectorRetrieverAdapter` 只把现有 RAG Lab 的 `RetrievalHit` 转换成 LangChain `Document`。它不重新拥有 chunk、embedding 和搜索算法，这正是 adapter 应有的边界。

### 4.7 `create_agent`

`build_runbook_agent()` 用 `create_agent()` 体验高层 Agent API。它适合快速得到标准 Tool Calling 循环，但学习时不能停在“会调用这个函数”。至少还要知道：

- 它接收的 model、tools、system prompt 分别来自哪里；
- Tool Call 和 Tool Result 如何进入消息；
- 达到停止条件时谁结束循环；
- 需要 checkpoint、审批或自定义状态时应该在哪一层扩展。

## 5. LangGraph：有状态工作流编排层

LangGraph 关心的核心问题不是“怎样包装一个 Prompt”，而是“有状态任务按什么节点和路径执行，以及怎样暂停和恢复”。

核心入口：[graph.py](../../projects/09-langgraph-leon/src/leon_framework/graph.py)

### 5.1 State

State 是节点之间共享、由 Graph 管理的数据。当前定义：

```python
class LeonGraphState(MessagesState):
    plan: list[str]
```

它包含：

- `messages`：继承自 `MessagesState` 的对话与工具消息；
- `plan`：开启 planner 时生成的 2 到 4 个步骤。

State 应保存“恢复执行所需的数据”，不是把所有应用数据都塞进去。

### 5.2 Node

Node 是接收当前 State、返回 State update 的可执行单元。当前有三个节点：

| Node | 输入 | 输出/副作用 |
|---|---|---|
| `plan` | 当前 messages | 返回 `{"plan": [...]}` |
| `agent` | messages、可选 plan、临时 Memory context | 调用模型，返回一条新 AI Message |
| `tools` | 带 Tool Call 的 AI Message | 执行工具，返回 Tool Message |

Node 返回的是更新量，不需要原地修改完整 State。这使状态合并、checkpoint 和重放更容易由框架统一处理。

### 5.3 Reducer

当节点返回：

```python
{"messages": [new_message]}
```

框架需要知道这是替换旧 messages，还是追加到旧 messages。`MessagesState` 已为 `messages` 配置消息 reducer，因此这里表示按消息语义合并/追加。

这是学习 LangGraph 时很容易漏掉的一层：State 字段不只需要类型，还可能需要“如何合并更新”的规则。

### 5.4 Edge 与 Conditional Edge

当前未开启 Planning 时：

```text
START -> agent
           |
           +-- 有 Tool Call --> tools -> agent
           |
           +-- 无 Tool Call -------------> END
```

开启 `--plan` 时：

```text
START -> plan -> agent -> tools -> agent -> ... -> END
```

普通 Edge 表示固定流向。`tools_condition` 是 Conditional Edge：检查最新 AI Message 是否有 Tool Call，有则去 `tools`，否则结束。

### 5.5 Compile

`builder.compile(...)` 把声明式 Graph 变成可 `invoke()` / `stream()` 的运行对象，并接入：

- checkpointer；
- `interrupt_before` 等中断配置。

Builder 描述拓扑，compiled graph 才是实际运行时。

### 5.6 Checkpoint

Checkpoint 是 Graph 在执行边界保存的状态快照和执行元数据，目的是让同一个 thread 可以继续运行。

它能保存：

- 当前 messages / plan；
- 当前执行到哪个节点；
- channel versions、pending writes 等框架恢复信息。

它不是 Long-term Memory。Checkpoint 面向“这个工作流怎么继续”，Memory 面向“以后会话需要记住什么业务事实”。

### 5.7 Interrupt 与 Resume

`interrupt_before=["tools"]` 表示在 ToolNode 真正执行前暂停。恢复时使用相同 `thread_id`，从 checkpoint 继续，而不是重新投递 Human Message。

这适合构建人工审批：

```text
模型建议调用写工具
  -> 工具执行前 interrupt
  -> 人工检查参数
  -> 批准后 resume
```

但“可以暂停”不等于“审批系统已经安全”。生产级审批仍需要身份、授权有效期、参数变更检查、审计和幂等键。

## 6. 六组必须分清的概念

### 6.1 State、Session、Checkpoint、Memory

| 概念 | 回答的问题 | 当前项目中的所有者 |
|---|---|---|
| Graph State | 当前 Graph 正在处理哪些数据？ | `LeonGraphState` |
| Session | 用户的一段产品会话有哪些消息和属性？ | Leon 产品层 |
| Checkpoint | 工作流中断后从哪里、带什么状态继续？ | LangGraph checkpointer |
| Long-term Memory | 跨会话要保留哪些业务事实或偏好？ | Leon `MemoryService` / `MemoryStore` |

它们可以使用同一个 ID 建立关联，但职责不能因此混为一体。

### 6.2 Checkpoint 与 Memory

当前 Framework Edition 有两种 Memory 进入模型的路径：

1. `MemoryService.build_context()` 生成自动上下文，临时插入本次模型输入，不写进 `MessagesState`；
2. 模型显式调用 `memory_get`，返回的 Tool Message 属于执行历史，会进入 checkpoint。

这个区别非常关键：自动上下文可以避免在 Graph State 中复制长期记忆；显式 Observation 若丢失，会破坏恢复语义，因此必须完整保存并加密。

### 6.3 Retriever 与 RAG

Retriever 是 RAG 的一个阶段，不负责回答问题。只有检索结果被组装进模型上下文并生成回答，才完成一次 RAG 调用链。

### 6.4 Cancellation 与 Interrupt

| 能力 | 目的 | 是否预期恢复 |
|---|---|---:|
| Cancellation | 尽快停止当前正在运行的 turn | 通常否 |
| Interrupt | 在可持久化边界暂停工作流 | 是 |

Self-built Runtime 的 cancellation 会在模型调用、工具边界和事件发布附近反复检查取消信号。LangGraph interrupt 则把暂停位置写入 checkpoint，允许另一个进程继续。

### 6.5 Tool Schema 与权限校验

Tool schema 是给模型和参数校验器看的；权限规则必须在可信应用边界执行。

例如：

```text
schema: path 必须是字符串
policy: resolve 后的绝对路径必须位于允许的 workspace root
```

不能因为模型输出通过 Pydantic/JSON Schema，就信任它访问任意路径或执行任意写操作。

### 6.6 At-least-once 与 Exactly-once

Checkpoint 能记录节点进度，但不能自动让外部副作用变成 exactly-once。

典型风险：工具已经成功写入外部系统，但进程在“写入成功”和“checkpoint 提交完成”之间崩溃。恢复后框架可能无法证明该工具是否完成，重试就可能重复写入。

生产方案通常需要一种或多种机制：

- 幂等键；
- 外部系统去重；
- 数据库事务；
- outbox/inbox；
- 将审批、执行和结果提交设计为明确状态机。

当前 CLI 对 pending checkpoint 的恢复使用工具白名单，禁止自动恢复 `memory_upsert/delete` 这类写工具。这是 fail closed，但不是通用 exactly-once 方案。

## 7. Self-built Leon 源码调用链

先看两个核心文件：

- [`runtime.py`](../../packages/workbench_core/src/workbench_core/agent/runtime.py)：Agent Loop、取消、事件、Trace、终止策略
- [`tools.py`](../../packages/workbench_core/src/workbench_core/agent/tools.py)：工具定义、schema、执行边界、audit projection

### 7.1 从 `AgentRuntime.run()` 开始

```text
AgentRuntime.run(user_message)
  -> 解析 cancel_event
  -> 创建 TraceRecorder
  -> 进入 cancellation_scope
  -> AgentRuntime._run(...)
  -> 根据成功 / 取消 / 异常结束 trace
  -> 返回 AgentResult
```

`run()` 负责一轮执行的外层生命周期；真正的循环在 `_run()`。

### 7.2 `_run()` 先组装消息

消息顺序是：

```text
固定 system prompt
  -> 可选 system_context
  -> history
  -> 当前 user message
```

这个顺序体现了不同上下文的来源。`system_context` 是本轮临时注入，不需要伪装成用户消息。

### 7.3 每轮把 Tool Schema 发给模型

`ToolRegistry.schemas` 从 `AgentTool.schema` 生成 OpenAI-compatible function schema。随后 `_chat_turn()` 同时发送 messages 和 schemas。

这里有两个独立对象：

- `AgentTool.schema`：模型可见的能力说明；
- `AgentTool.handler`：应用内部真正的 Python 函数。

模型永远拿不到 handler，只看到 schema。

### 7.4 有 Tool Call 时怎样执行

核心路径：

```text
ChatTurn.tool_calls
  -> parse_tool_arguments
  -> ToolRegistry.audit_arguments
  -> 发出 tool_started / 开始 tool span
  -> ToolRegistry.execute(name, arguments)
  -> ToolRegistry.audit_result
  -> 记录 ToolStep / 发出 tool_finished
  -> raw result 压缩后追加为 tool message
  -> 进入下一轮模型调用
```

`ToolRegistry.execute()` 是故障隔离边界：

- unknown tool 返回结构化错误；
- 参数不匹配的 `TypeError` 转成工具错误；
- 普通 handler 异常转成工具错误，允许模型观察并决定下一步；
- `CancelledError` 继续抛出，因为取消是 Runtime 控制流，不能伪装成普通工具失败。

### 7.5 为什么同时有 raw result 和 audited result

模型需要较完整的 raw result 才能继续工作，但日志、事件和持久化审计不一定允许保存敏感原文。

所以代码把两条数据流分开：

```text
raw result ------> in-memory LLM transcript
     |
     +-- audit projection --> ToolStep / event / trace
```

audit projection 先 `deepcopy`，失败时返回 `{"audit_error": "projection_failed"}`，不会因为投影函数出错而退回泄漏原始 payload。

### 7.6 Self-built Runtime 的终止语义

- 无 Tool Call：返回 `answered`；
- `return_direct` 工具产生答案：返回 `direct_answer`；
- 达到 `max_turns`：增加 closing prompt，再做一次不带工具的收口调用，返回 `forced_answer`；
- 取消：抛出 `AgentCancelled`，若已有工具步骤则携带安全的 partial audit result。

面试时不要只说“我写了 while loop”。更准确的说法是：你实现了模型调用、工具协议、错误 Observation、最大轮数、直接返回、协作式取消、流式事件、Trace 和审计投影的完整运行边界。

## 8. LangGraph Leon 源码调用链

按下面顺序读，而不是一上来钻进框架内部：

1. [`cli.py`](../../projects/09-langgraph-leon/src/leon_framework/cli.py)：进程入口、thread、stream/resume
2. [`composition.py`](../../projects/09-langgraph-leon/src/leon_framework/composition.py)：组装配置、模型和共享工具
3. [`tool_adapter.py`](../../projects/09-langgraph-leon/src/leon_framework/tool_adapter.py)：把 Leon Tool 适配为 LangChain Tool
4. [`graph.py`](../../projects/09-langgraph-leon/src/leon_framework/graph.py)：State、Node、Edge、compile
5. [`planning.py`](../../projects/09-langgraph-leon/src/leon_framework/planning.py)：可选结构化 planner
6. [`checkpointing.py`](../../projects/09-langgraph-leon/src/leon_framework/checkpointing.py)：加密 SQLite checkpoint

完整调用链：

```text
leon-graph
  -> cli.main()
  -> _run_live()
  -> open_encrypted_sqlite_checkpointer()
  -> _build_live_runtime()
  -> build_framework_components()
       -> load_framework_settings()
       -> build_chat_model()
       -> build_tool_registry()
  -> build_leon_graph()
       -> adapt_leon_tools()
       -> model.bind_tools()
       -> StateGraph.add_node/add_edge()
       -> compile(checkpointer=...)
  -> graph.stream() / graph.invoke()
  -> agent node
  -> tools_condition
  -> ToolNode
  -> StructuredTool
  -> ToolRegistry.execute()
  -> 原 Leon handler
  -> ToolMessage 写回 State
  -> agent node
  -> END
```

### 8.1 `composition.py` 为什么重要

Composition Root 的职责是“在程序边界把具体依赖组装起来”，不是承载 Agent Loop。

它复用：

- Leon 私有配置加载；
- 文件搜索 service 和 file tools；
- Web Search service/tool；
- `MemoryService`、`MemoryStore` 和 memory tools；
- 可选的 RAG search tool；
- `workbench_core.agent.ToolRegistry`。

默认 live CLI 的实际工具取决于本地配置：文件 roots 未配置就没有文件工具，搜索 key 未配置就没有 Web Search；Memory 默认开启，可用 `--no-memory` 关闭。`rag_search` 的组合入口已经存在，但默认 live CLI 当前没有注入 `RAGSearchService`，主要在受控对照中验证。

### 8.2 `graph.py` 为什么只有几十行

因为节点调度、消息 reducer、ToolNode 执行和 checkpoint 协议由 LangGraph 库实现了。应用代码仍然必须负责：

- State 里保存什么；
- 节点业务逻辑；
- 哪些工具允许暴露；
- Memory 如何注入；
- checkpoint 的加密、key 和恢复策略；
- 副作用和幂等性。

因此不能用“应用源码行数”直接判断两套 Runtime 谁更简单。Framework Edition 的大量通用能力位于依赖库内部。

### 8.3 临时上下文为什么不直接写 State

`call_model()` 会复制当前 messages，再把 Memory context 和 plan context 作为临时 `SystemMessage` 插入模型输入，只返回模型生成的新消息。

这样做的结果：

- 自动 Memory context 不会每轮重复固化进对话；
- plan 仍以结构化 `state["plan"]` 保存，而不是只剩一段不可解析文本；
- checkpoint 保留恢复所需状态，临时 prompt 组装保持在节点内部。

## 9. 同一批工具如何被两套 Runtime 复用

真正共享的是 `AgentTool` 和 `ToolRegistry`，而不是复制 handler：

```text
业务 handler
   |
AgentTool(name, description, parameters, handler)
   |
ToolRegistry
   |
   +--> Self-built AgentRuntime 直接读取 schemas / execute()
   |
   +--> adapt_leon_tools()
           -> LangChain StructuredTool
           -> LangGraph ToolNode
           -> 最终仍调用 ToolRegistry.execute()
```

[`tool_adapter.py`](../../projects/09-langgraph-leon/src/leon_framework/tool_adapter.py) 只做三件事：

1. 从 canonical registry 读取名称、描述、参数 schema；
2. 用 `StructuredTool.from_function()` 包装调用入口；
3. 执行时把参数原样交回 `registry.execute()`。

它没有复制文件读取、Web Search、Memory 或 RAG 业务逻辑。所以两套 Runtime 的核心对比变量是“编排方式”，不是两套不同的工具实现。

注意一个边界：当前 adapter 复用了 handler 执行和 schema，但 Self-built Runtime 的 `return_direct`、audit event、trace span 等 Runtime 语义不会因为包装成 `StructuredTool` 自动等价迁移。复用业务能力不等于两个 Runtime 的所有外围语义完全相同。

## 10. Checkpoint 安全设计

核心文件：[`checkpointing.py`](../../projects/09-langgraph-leon/src/leon_framework/checkpointing.py)

当前实现：

- 默认数据库：`%USERPROFILE%\.leon\langgraph-checkpoints.db`；
- `thread_id` 使用 opaque UUID 形式，不把用户名或任务标题写进 ID；
- 使用 LangGraph `EncryptedSerializer` 和 AES-EAX 加密 checkpoint/pending writes payload；
- 32-byte key 放在相邻 sidecar 文件，不写入 SQLite；
- metadata 只保留受控的 `source` 和整数 `step`；
- 旧明文 row、错误 key、现有 DB 丢 key 时 fail closed。

为什么不能只挑 messages 加密？

因为恢复不仅依赖消息，还依赖 plan、channel versions、pending writes 等完整状态。裁剪后可能出现“看起来能打开数据库，但无法正确恢复”的假安全。

当前方案的边界也必须讲清楚：sidecar key 与数据库在同一用户目录，只能防止 SQLite/WAL 中直接出现明文，不等于 Windows DPAPI、TPM 或云 KMS。备份时 DB 和 key 必须一起保留，key 丢失就不能恢复旧 checkpoint。

## 11. 怎样理解 10-case Runtime 对照

报告：[provider-free-comparison.md](../../projects/09-langgraph-leon/docs/provider-free-comparison.md)

对照使用 deterministic fake models 和本地只读工具，覆盖：

- 纯聊天；
- 单次文件读取及错误 Observation；
- 单次 RAG；
- 文件与 RAG 的不同顺序组合；
- 三步连续工具调用。

现有结果：

```text
Self-built task success: 10/10
LangGraph task success:   10/10
Raw observation parity:   10/10
每个 case 的模型调用轮数一致
```

这个实验能证明：

- 两套 Runtime 在这 10 条受控路径上完成了相同任务；
- adapter 最终调用了同一业务 handler；
- 工具原始 Observation 没有因框架切换而改变；
- 多步 Agent Loop 的调用轮数可对齐。

它不能证明：

- LangGraph 在真实 provider 下必然更快或更慢；
- 所有并发、流式和异常路径已经等价；
- 写工具恢复天然 exactly-once；
- Framework Edition 已经覆盖 Self-built Leon 的全部产品能力。

报告中的本地毫秒数主要是编排开销，真实请求里通常会被网络和模型延迟淹没。不要拿这组数字做普遍性能结论。

## 12. 面试口述答案

下面不是逐字背诵稿。先理解因果，再换成自己的语言。

### 12.1 什么是 Agent Runtime？

> 模型本身只会生成文本或结构化 Tool Call。Agent Runtime 负责维护消息上下文，把工具 schema 提供给模型，解析并执行 Tool Call，把结果作为 Observation 回填，再循环到模型输出最终答案。同时它还要处理最大轮数、异常、取消、Trace 和权限边界。

### 12.2 为什么先自研 Runtime，再做 LangGraph 版？

> 我先实现最小 Runtime，是为了真正理解 Tool Calling、Observation 和多轮循环，而不是只会调用高层 API。之后我让 LangGraph 版复用同一套 Tool、Memory 和 RAG，主要替换编排层，这样能直接比较手写循环和 StateGraph 在状态、checkpoint、interrupt/resume 上的取舍。

### 12.3 两套方案怎样复用工具？

> 业务工具统一定义为 AgentTool，注册到 ToolRegistry。自研 Runtime 直接读取 registry 的 schema 并调用 execute；LangGraph 版通过一个很薄的 adapter 转成 StructuredTool，ToolNode 最终仍回到同一个 registry.execute，所以没有复制文件、搜索、Memory 和 RAG handler。

### 12.4 Self-built 和 LangGraph 各自适合什么场景？

> 流程短、状态简单、强调低依赖和细粒度控制时，自研 Runtime 更直接。需要显式多节点状态、持久化 checkpoint、跨进程恢复或人工审批时，LangGraph 的抽象更成熟。但使用框架后仍要自己负责数据治理、权限、幂等和版本迁移。

### 12.5 State 和 Memory 有什么区别？

> State 是当前工作流执行所需的数据，checkpoint 是它的持久化恢复机制；Memory 是跨会话保存的业务事实或偏好。把所有 Memory 都复制到 State 会造成泄漏和状态膨胀，所以 Leon 的自动 Memory context 只临时注入模型，显式 memory tool 的 Observation 才进入执行历史。

### 12.6 Cancellation 和 Interrupt 有什么区别？

> Cancellation 的目标是尽快停止当前 turn，通常不计划继续；Interrupt 是在可恢复节点边界暂停，把位置和状态写入 checkpoint，之后用同一个 thread resume。前者是协作式终止，后者是 durable pause。

### 12.7 有 checkpoint 为什么还要考虑幂等？

> checkpoint 和外部系统写入通常不在同一个原子事务里。工具可能已经写成功，但进程在提交新 checkpoint 前崩溃，恢复后就可能重复执行。所以写工具还需要幂等键、去重或事务设计，不能把 resume 直接等同于 exactly-once。

### 12.8 为什么不直接只用 `create_agent()`？

> `create_agent()` 很适合标准 Tool Calling 快速开发，我也用小实验理解了它。但 Leon 需要比较取消、Trace、审计投影、显式 State 和恢复语义，所以保留自研 Runtime，再用 LangGraph 构建 Framework Edition，更容易看清抽象究竟替我解决了什么。

### 12.9 Retriever 就是 RAG 吗？

> 不是。Retriever 只负责 query 到 Documents；RAG 还包括文档切分、索引、上下文组装、生成和引用。我的 LangChain adapter 只把已有 RAG Lab 的结果转成 Document，没有复制检索实现。

### 12.10 这版 Planning 做到了什么？

> 开启 `--plan` 后增加一个 plan node，用结构化输出生成 2 到 4 个步骤并存入 Graph State，然后临时注入 agent 的模型上下文。它展示了显式中间状态和 checkpoint，不是 DAG executor，也没有并行调度；每个用户 turn 会额外增加一次模型请求。

## 13. 源码阅读练习

所有练习都可以先用 fake model 完成，不需要真实 provider。

### 练习 1：追踪一次文件读取

目标：从模型返回 `read_file` Tool Call 开始，分别在两套 Runtime 中写出经过的函数。

完成标准：能解释下面两个序列的每一跳：

```text
Self-built:
ChatTurn -> parse_tool_arguments -> ToolRegistry.execute -> tool message -> next ChatTurn

LangGraph:
AIMessage -> tools_condition -> ToolNode -> StructuredTool -> ToolRegistry.execute
-> ToolMessage -> agent node
```

### 练习 2：解释 unknown tool

阅读 `ToolRegistry.execute()`，回答：

1. unknown tool 为什么返回 Observation，而不是直接让进程崩溃？
2. `audit_name()` 为什么把未知的模型输入统一映射成 `unknown_tool`？
3. 如果错误也回填给模型，模型下一轮可能做什么？

### 练习 3：解释 `MessagesState` 更新

阅读 `call_model()`，回答为什么返回：

```python
{"messages": [bound_model.invoke(messages)]}
```

而不是返回完整旧消息列表。答案必须提到 reducer。

### 练习 4：画出 Planning 的数据流

必须包含：

```text
HumanMessage -> latest user text -> prompt | structured model -> PlanOutput
-> state.plan -> format_plan_context -> temporary SystemMessage -> agent
```

再回答：为什么关闭 `--plan` 后不能随意恢复一个停在 plan node 的 thread？

### 练习 5：区分两种 Memory 注入

在 `graph.py` 和 `composition.py` 中找到：

- 自动 context provider；
- `memory_get/upsert/delete` 的工具注册路径。

然后解释为什么前者不进入 checkpoint，后者的 Tool Message 会进入。

### 练习 6：分析一次崩溃窗口

假设 `memory_upsert` 已成功写数据库，但进程随后崩溃。写出：

- checkpoint 可能处于什么位置；
- 盲目 resume 的重复执行风险；
- 当前只读恢复白名单怎样降低风险；
- 真正开放写工具审批还缺什么。

### 练习 7：读对照实验而不是只看 PASS

打开 `runtime_comparison.py` 和 provider-free 报告，找出：

- fake model 怎样决定下一次 Tool Call；
- raw Observation 怎样被比较；
- 为什么模型调用次数应一致；
- 为什么本地 median ms 不能代表线上端到端性能。

### 推荐验证命令

```powershell
uv run pytest projects/08-langchain-lab/tests -q
uv run pytest projects/09-langgraph-leon/tests/test_graph.py -q
uv run pytest projects/09-langgraph-leon/tests/test_checkpointing.py -q
uv run leon-runtime-compare --repeats 7
```

## 14. 7 天学习安排

每天 60 到 90 分钟。每天必须有一个“说出来或画出来”的输出，不能只读。

| 天 | 主题 | 阅读 | 当天输出 |
|---|---|---|---|
| Day 1 | Agent 基础协议 | 第 2、3 节；Self-built `_run()` | 手画 Tool Calling 循环，口述 3 分钟 |
| Day 2 | LangChain 组件 | 第 4 节；08 的 6 个源码文件 | 解释 `prompt | model | parser` 每一段类型变化 |
| Day 3 | Self-built Runtime | 第 7 节；`runtime.py`、`tools.py` | 从 user message 追到 final answer，标出取消和 trace |
| Day 4 | LangGraph 编排 | 第 5、8 节；`graph.py`、adapter | 不看源码画出 State/Node/Edge 和条件分支 |
| Day 5 | 状态与恢复 | 第 6、10 节；checkpoint tests | 讲清 State/Session/Checkpoint/Memory 和崩溃窗口 |
| Day 6 | 双 Runtime 对照 | 第 9、11 节；comparison 报告 | 用 5 分钟解释复用边界、证据和实验局限 |
| Day 7 | 面试演练 | 第 12、13 节 | 录一遍 10 问口述，卡住的地方回源码找证据 |

不要一天追完七天。间隔回忆比连续阅读更能暴露“以为自己懂了”的部分。

## 15. 最终自测清单

每一项只填“能”或“不能”，不要填“差不多”。

- [ ] 能在白板上画出最小 Agent Loop。
- [ ] 能解释模型为什么没有直接执行 Python 工具。
- [ ] 能从 `AgentRuntime.run()` 追到 `ToolRegistry.execute()` 再回到下一轮模型。
- [ ] 能指出 Self-built Runtime 的 3 个正常终止路径。
- [ ] 能解释 raw result 与 audit projection 的数据分流。
- [ ] 能从 CLI 入口追到 compiled graph。
- [ ] 能指出 LeonGraphState 的字段和 reducer 语义。
- [ ] 能解释 `tools_condition` 如何决定去 ToolNode 还是 END。
- [ ] 能解释 adapter 复用了什么、没有复用什么。
- [ ] 能分清 Structured Output 和 Tool Calling。
- [ ] 能分清 Retriever 和完整 RAG。
- [ ] 能分清 State、Session、Checkpoint 和 Memory。
- [ ] 能分清 Cancellation 和 Interrupt。
- [ ] 能解释 checkpoint 为什么不能保证 exactly-once。
- [ ] 能说明当前 checkpoint 加密方案解决了什么、没有解决什么。
- [ ] 能解释 10-case 对照证明了什么、不能证明什么。
- [ ] 能用 5 分钟完整讲出“先自研、再框架化”的项目故事。

最后记住：面试官真正想确认的不是你记住多少类名，而是你能不能把**数据怎样流动、状态由谁拥有、失败怎样处理、边界为什么这样划分**讲清楚。
