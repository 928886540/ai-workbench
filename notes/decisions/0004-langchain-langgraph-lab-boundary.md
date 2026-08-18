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

## Checkpoint 安全边界

Framework Edition 的 SQLite checkpoint 选择“完整状态的 authenticated encryption at rest + 明文
metadata 最小化”，不使用 Tool audit projection 代替 Graph State。原因是 LangGraph 会在每个
superstep 保存 checkpoint 和 pending writes；如果先保存 raw ToolMessage、再用 scrub node 清理，历史
row 仍已泄漏。如果直接裁掉 observation，跨进程恢复又会失真或重复执行工具。

具体边界：

- `EncryptedSerializer` 使用 AES-EAX 加密 checkpoint/pending writes，保留完整恢复语义。
- 自动 Memory context 只在调用 model 前临时注入，本来就不进入 `MessagesState`。
- SQLite metadata、thread/node/channel 等结构仍是明文；只允许随机 opaque thread id，调用方 metadata
  不进入 saver。
- 旧明文或 mixed SQLite 一律拒绝，不做原地迁移；key 错误或缺失时 fail closed。
- 相邻 key sidecar 解决的是“SQLite/WAL 不出现可读原文”，不是系统级密钥托管。若要求连密文都不能
  保存原始内容，需要另做 `UntrackedValue`/受限恢复设计，并明确牺牲哪些恢复点。

## RAG 复用状态

`03-rag-lab` 的 `rag_search` 是唯一业务 Tool 事实来源。09 通过显式 workspace 依赖和可选
`RAGSearchService` 注入复用它；provider-free 测试已证明同一个 `AgentTool + ToolRegistry` 经
Self-built `AgentRuntime` 与 LangGraph `ToolNode` 得到相同 raw observation。

这项证明不等于 live 接入：原 Leon CLI/Gateway 与 09 live CLI 都尚未获得预建索引来源，简历和面试
只能表述为“双 Runtime 共享 Tool 契约已验证”，不能表述为“Leon 线上 RAG 已启用”。

## 范围

09 只做 Chat、Tool Calling、Planning、Memory、RAG/Search 和 Checkpoint/Resume。允许选一个现有
Image Tool 证明复用，不做 Web、TTS/ASR、Gallery、SSE、MCP、Coding Agent 或 Multi-Agent。

## 后果

- 会同时维护两套编排实现，但这是有意的对照实验，不抽象成“万能 Runtime”。
- 业务 Tool 修复只发生在原 service/tool factory，两个 Runtime 同时受益。
- Framework Edition 可以快速演示框架价值，同时不会抹掉自研 Runtime 的面试差异化。
