# 0004 - LangChain Lab 与 LangGraph Leon 边界

## 决策

在保留 `02-leon-agent` 自研 Runtime 的前提下，新增两个不同目的的阶段项目：

- `08-langchain-lab`：只学习 LangChain 基础组件，保持很小。
- `09-langgraph-leon`：Leon Framework Edition，复用业务能力并替换 Agent 编排层。

原路线中的 `05-workflow` 保持占位，不把框架学习、通用工作流和 Leon 对照实验混成一个项目。

## 核心复用契约

`AgentTool + ToolRegistry` 继续是业务工具的唯一事实来源。LangGraph 版只增加薄适配器，把已有 JSON
schema 和 `ToolRegistry.execute` 暴露给 LangChain `BaseTool/ToolNode`。

不复制 File、Web、Memory、Image 的 schema、handler、权限和错误处理。

## Runtime 分界

- Self-built Leon：保留 `AgentRuntime`、现有 Planning、取消、Trace 和 Session 产品层。
- Framework Edition：使用 Graph State、node/edge、`ToolNode`、checkpoint 与 interrupt/resume。
- 原 Leon 不依赖 09；09 可以依赖 `leon-agent` 来复用业务 service 和 tool factory。

Planning 属于编排能力，在两套 Runtime 中分别实现。Memory 属于跨会话业务事实，复用现有
`MemoryService`。Graph checkpoint 只保存 thread 执行状态，不能冒充长期 Memory。

## 已知缺口

`03-rag-lab` 目前没有作为 Tool 接入 Leon，因此还不存在“同一 Leon RAG Tool”可复用。先建立一个
共享只读 RAG Tool，再让两套 Runtime 同时注册；未完成前不在简历中声称已共享。

## 范围

09 只做 Chat、Tool Calling、Planning、Memory、RAG/Search 和 Checkpoint/Resume。允许选一个现有
Image Tool 证明复用，不做 Web、TTS/ASR、Gallery、SSE、MCP、Coding Agent 或 Multi-Agent。

## 后果

- 会同时维护两套编排实现，但这是有意的对照实验，不抽象成“万能 Runtime”。
- 业务 Tool 修复只发生在原 service/tool factory，两个 Runtime 同时受益。
- Framework Edition 可以快速演示框架价值，同时不会抹掉自研 Runtime 的面试差异化。
