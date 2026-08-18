# 09 Leon Agent Framework Edition

Leon Agent Framework Edition：保留原有 Self-built Leon，用 LangChain + LangGraph 替换最核心的
Agent 编排层，并尽量复用同一批业务 Tool、Memory 和检索能力。

## 两套 Runtime，不互相替换

```text
                         Leon business capabilities
                    AgentTool / ToolRegistry / services
                                  |
                 +----------------+----------------+
                 |                                 |
                 v                                 v
        Self-built AgentRuntime            LangGraph StateGraph
                 |                                 |
                 v                                 v
        projects/02-leon-agent            projects/09-langgraph-leon
```

原 Leon 继续是自研 Runtime 版本；本项目是用于学习和对照的 Framework Edition。不得把原 Runtime
改造成 LangGraph，也不得让原 Leon 反向依赖本项目。

## 为什么比另做小 Demo 更值

同一业务能力经过两套编排层，可以真实比较：

- 自研 `while` loop 与 Graph node/edge 的控制流差异
- 自研 Session/Planning 与 Graph State/checkpoint 的职责边界
- Tool schema、异常回注和权限策略能否保持一致
- interrupt/resume 对人工审批和长流程恢复的价值
- 框架减少的代码与增加的升级、调试成本

## 目标架构

```text
User message
    |
    v
LangGraph Messages/State
    |
    +--> plan node -------------------------+
    |                                      |
    v                                      |
agent node -- tools_condition --> ToolNode-+
    |                               |
    |                         existing Leon tools
    |                    File / Web / Memory / Image
    v
   END

Graph checkpoint: thread execution state / resume
Leon MemoryService: cross-session user facts
```

## 复用边界（以当前代码事实为准）

### 可以直接复用

- `workbench_core.agent.AgentTool`：工具名、描述、JSON schema、handler、审计投影。
- `workbench_core.agent.ToolRegistry`：统一执行、未知工具和参数/运行异常边界。
- `leon_agent.file_tools.create_file_tools`：现有 File List/Search/Read 与显式授权写入。
- `leon_agent.search.WebSearchService`：联网搜索业务实现。
- `leon_agent.memory.MemoryService` + `create_memory_tools`：显式长期 Memory 和权限策略。
- `leon_agent.service.LeonToolService`：已有图片任务业务实现。

09 只增加一层 `ToolRegistry -> LangChain BaseTool` 适配，不复制上述 schema 或 handler。

### 属于 Runtime，对照实现

- 原 Leon 的 `AgentRuntime` loop、消息回注、direct answer、取消和 trace event。
- 原 Leon 的 `PlanningService` 顺序状态机。
- LangGraph 的 Graph State、node/edge、`ToolNode`、checkpoint、interrupt/resume。

Planning 在 09 中用 Graph State/node 表达，不把自研 `PlanningService` 套进第二个 executor。

### 当前还不能宣称复用

`03-rag-lab` 已有独立 pipeline，但尚未作为 Leon Tool 接入原 Runtime。后续应先定义一个共享、只读的
`rag_search` 或 `knowledge_search` 业务 Tool，再同时注册到两套 Runtime；在此之前只写“计划接入”，
不写“已复用 Leon RAG”。

## 精简范围

Framework Edition 只保留：

1. Chat
2. Tool Calling
3. Planning
4. Memory
5. RAG / Search
6. Checkpoint / Resume

图片生成只允许复用现有 Tool 做一次集成证明，不重做图库、任务页面或 ComfyUI 业务层。

明确不做：

- Web / FastAPI / SSE / Vue / PWA
- TTS / ASR / 音频队列
- 图片 Gallery 或新图片工作流
- MCP、Coding Agent、Multi-Agent
- 新 Session 产品层或第二套 Memory Store
- 通用 Tool 插件系统

## 稳步推进

### Milestone 0：共享 Tool 契约证明

- [x] 把现有 `ToolRegistry` 薄适配成 LangChain tools。
- [x] 用 `ToolNode + tools_condition` 跑通 provider-free 工具循环。
- [x] 用一个真实的现有 Leon 只读文件 Tool 证明 handler/schema 没有复制。

验证：同一个 `AgentTool` 直接经 `ToolRegistry.execute` 与经 LangGraph 执行，得到一致业务结果。

当前 provider-free 演示：

```powershell
uv run leon-graph --demo
```

输出节点流转 `START -> agent -> tools -> agent -> END`，实际执行原 Leon 的 `read_file` handler；
这个一次性 `--demo` 刻意使用进程内 `InMemorySaver`，不创建用户文件；真实 live CLI 与
`--interrupt-demo` 使用后文的加密 SQLite。

### Milestone 1：最小 `leon-graph` CLI

- [x] 复用 Leon 私有配置构造 LangChain `ChatOpenAI`。
- [x] 只接 File Search/Read 和 Web Search。
- [x] 输出简洁 node 流转，不做 TUI。

组合层调用原 Leon 的 `create_file_tools` 和 `create_web_search_tool`，只筛选
`file_search`、`read_file`、`web_search` 三个工具；没有复制 schema、handler 或搜索鉴权。
原 Leon 的 `create_leon_tools` 也改为调用同一个 `create_web_search_tool`，两套 Runtime 的业务契约
仍只有一个事实来源。

真实入口（需要先完成一次 `leon-config init`）是：

```powershell
# 交互模式
uv run leon-graph

# 单轮模式，便于面试演示
uv run leon-graph --once "搜索 LangGraph 当前稳定版本，并给出官方链接"
```

入口只读取 `%USERPROFILE%\.leon\config.toml`；其中没有配置 `LEON_FILE_ROOTS` 或 Tavily key 时，
对应工具不会注册，仍可进行纯聊天。自动测试和 `--demo` 不访问真实模型、文件外部目录或 Tavily。
live checkpoint 默认保存在 `%USERPROFILE%\.leon\langgraph-checkpoints.db`，key 使用相邻 sidecar；
`--thread-id` 用于新 thread，`--resume` 和交互 `/resume` 用于已有 thread。

### Milestone 2：Planning + Memory

- [x] 用 Graph State 增加 2～4 步简单计划。
- [x] 复用 `MemoryService` 与 memory tools，不新建存储。
- [x] 保留显式 consent 和敏感值拒绝策略。

Planning 是显式可选能力：

```powershell
uv run leon-graph --plan
uv run leon-graph --plan --once "分析当前仓库的 Agent 架构"
```

开启后节点流变为 `START -> plan -> agent -> tools -> agent -> END`。结构化 planner 只返回
2～4 个有序步骤，计划保存在 Graph State/checkpoint 中，并作为临时 system context 提供给 agent，
不会复制成 Leon `PlanningService` 或第二套业务执行器。

计划只会在当前 Graph 明确启用 planner 时注入模型。恢复已有 thread 但不传 `--plan` 时，checkpoint
中上一轮的计划不会污染新问题；CLI 显示的 Planning 状态与实际 Prompt 保持一致。

默认不开启 Planning，因为 `--plan` 每个用户 turn 会额外产生一次模型请求。`--demo` 使用 fake planner
展示相同节点与 state 变化，不访问真实 provider。当前只保存计划，不做 DAG、并行步骤、自动重试或
跨进程任务恢复。

Memory 默认开启，使用 Leon 同一个 `LEON_SESSION_DB`、固定 local owner、`MemoryService` 和
`memory_get / memory_upsert / memory_delete` 工具；可用 `--no-memory` 显式关闭：

```powershell
uv run leon-graph --no-memory
```

CLI 把当前用户原话绑定到原 Leon 的 `memory_turn`，所以模型不能伪造 consent 参数；没有明确保存/删除
指令时写操作 fail closed，每轮最多一次写尝试，敏感 key/value 继续由原策略拒绝。`build_context()`
返回的长期记忆只作为临时 system context 传给 model，不写入 `MessagesState`。

`MessagesState` 会保存正常对话和 ToolMessage；显式 `memory_get`、文件读取等原始工具结果也会进入
这些消息。Milestone 3 没有用 audit projection 覆盖这些原始值，因为裁掉
ToolMessage 会让恢复后的 Graph 缺少 observation，甚至被迫重复执行工具。这里选择完整 checkpoint 的
authenticated encryption at rest，同时继续让自动 Memory context 根本不进入 State。

### Milestone 3：Checkpoint / Resume

- [x] 第一棒用 `InMemorySaver` 学 checkpoint 语义。
- [x] 建立 AES-EAX 加密的 SQLite checkpointer 安全边界。
- [x] 把 live CLI 切到加密 SQLite，并完成关闭进程后的恢复。
- [x] 用稳定 opaque `thread_id` 实现 `--resume <id>` 与交互 `/resume <id>`。
- [ ] 在模拟高风险动作前验证 interrupt/resume，不真的执行系统操作。

安全 SQLite 基座和最小 CLI 恢复闭环已经完成：

- LangGraph `checkpoint` 与 `pending writes` 经官方 `EncryptedSerializer` 完整加密，恢复语义不裁剪。
- SQLite metadata 不经过 serializer，因此 wrapper 会丢弃调用方自带 metadata；后续 thread id 只能使用
  随机 opaque ID，不能放用户名、标题、路径或 Prompt。
- 启动时拒绝任何没有 `+aes` 类型的旧明文/mixed row，也会用 AES MAC 检查 key 是否匹配，不能静默
  兼容旧库或生成新 key 覆盖现有库。
- 32-byte key 使用独立 sidecar 文件，只保证 checkpoint payload 不以明文出现在 SQLite/WAL；这不是
  Windows 系统级密钥托管。备份和恢复必须同时保留 DB 与 key，换 key 等于放弃旧 checkpoint。

provider-free 测试会扫描 DB/WAL/SHM，确认 Human、AI、File Tool、调用方 metadata 和临时 Memory
context sentinel 均不出现明文；随后关闭连接、重开 SQLite，并验证完整 ToolMessage 仍可认证解密。

跨进程演示：

```powershell
# 进程 A：在 ToolNode 前暂停并退出，输出一个可复制的 thread id
uv run leon-graph --interrupt-demo --thread-id demo-thread

# 进程 B：不重投 HumanMessage，直接从同一 checkpoint 恢复
uv run leon-graph --interrupt-demo --resume demo-thread
```

日常 live CLI 默认使用同一加密 SQLite。`--once` 结束后可用 `--resume <id> --once "下一问"`
继续已有对话；交互模式输入 `/resume <id>` 会切换到已有 thread。恢复 pending checkpoint 时只允许
`file_search`、`read_file`、`web_search`、`memory_get` 等只读工具；`memory_upsert/delete` 不会在
缺失原始 consent 的跨进程上下文中自动执行。`--thread-id` 只创建新 thread，已存在时 fail closed。
如果 planner 请求（包括上游 429）让 checkpoint 停在 `plan` 节点，使用同一 thread 并带 `--plan`
即可安全重试这个无副作用节点；省略 `--plan` 会明确拒绝，避免用不同 Graph 拓扑误恢复。
`web_search` 虽然不修改业务数据，但会消耗外部配额；若进程恰好在请求完成而 pending write 尚未落盘时
崩溃，恢复遵循 at-least-once 语义，可能重复搜索一次。

当前 provider-free 暂停/恢复演示：

```powershell
uv run leon-graph --interrupt-demo
```

Graph 使用静态 `interrupt_before=["tools"]`，第一次运行在 ToolNode 前停住，此时 handler 尚未执行；
随后用同一 `thread_id` 和 `graph.stream(None, config)` 从 checkpoint 恢复，工具只执行一次，再回到
agent 形成最终回答。测试同时断言暂停前调用数为 0、恢复后为 1。

这条命令和 live CLI 都已经覆盖“进程 A 暂停并关闭 -> 进程 B 用同一 thread 恢复”；后续只补
受限高风险动作的 interrupt 说明，不扩展成第二套 Session 产品。

### Milestone 4：RAG / Image 与对照报告

- [ ] 先建立可同时注册到两套 Runtime 的共享 RAG Tool，再接 09。
- [ ] 选一个现有 Image Tool 做复用证明，不扩图片产品功能。
- [ ] 用同一组 provider-free case 对照工具选择、状态、恢复与代码复杂度。

## 面试主线

> 我先实现过最小 Agent Runtime，理解 Tool Calling、Observation、多轮执行、Planning、Cancellation
> 和 Trace；之后又基于同一套 Leon Tool 做了 LangGraph Framework Edition，对比 State、Checkpoint、
> Interrupt 和 Workflow 抽象。自研 Runtime 更轻、更可控；LangGraph 在复杂有状态流程和恢复上更成熟。

这句话必须由可运行代码和对照测试支撑，不能只写在简历里。
