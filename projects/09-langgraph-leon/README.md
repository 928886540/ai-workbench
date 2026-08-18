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
当前 checkpointer 是进程内 `InMemorySaver`，尚不宣称支持退出进程后恢复。

### Milestone 1：最小 `leon-graph` CLI

- [ ] 复用 Leon 私有配置构造 LangChain ChatModel。
- [ ] 只接 File Search/Read 和 Web Search。
- [ ] 输出简洁 node 流转，不做 TUI。

验证：`uv run leon-graph` 能完成一次真实模型工具调用；自动测试仍 provider-free。

### Milestone 2：Planning + Memory

- [ ] 用 Graph State 增加 2～4 步简单计划。
- [ ] 复用 `MemoryService` 与 memory tools，不新建存储。
- [ ] 保留显式 consent 和敏感值拒绝策略。

### Milestone 3：Checkpoint / Resume

- [ ] 第一棒用 `InMemorySaver` 学 checkpoint 语义。
- [ ] CLI 需要跨进程恢复时再换 SQLite checkpointer。
- [ ] 用稳定 `thread_id` 实现 `/resume <id>`。
- [ ] 在模拟高风险动作前验证 interrupt/resume，不真的执行系统操作。

### Milestone 4：RAG / Image 与对照报告

- [ ] 先建立可同时注册到两套 Runtime 的共享 RAG Tool，再接 09。
- [ ] 选一个现有 Image Tool 做复用证明，不扩图片产品功能。
- [ ] 用同一组 provider-free case 对照工具选择、状态、恢复与代码复杂度。

## 面试主线

> 我先实现过最小 Agent Runtime，理解 Tool Calling、Observation、多轮执行、Planning、Cancellation
> 和 Trace；之后又基于同一套 Leon Tool 做了 LangGraph Framework Edition，对比 State、Checkpoint、
> Interrupt 和 Workflow 抽象。自研 Runtime 更轻、更可控；LangGraph 在复杂有状态流程和恢复上更成熟。

这句话必须由可运行代码和对照测试支撑，不能只写在简历里。
