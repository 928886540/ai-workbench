# 08 LangChain Lab

一个刻意做小的 LangChain 组件实验，用来理解行业框架抽象，不做第二个 Agent 产品。

## 定位

Leon 已经证明了自研 Agent Runtime、Tool Calling、Planning、Memory、Evaluation 和 Trace。
这个 Lab 只回答：LangChain 如何封装模型、Prompt、结构化输出、Tool、Retriever 和基础 Agent。

真正的有状态 Agent 编排放在 `09-langgraph-leon`。

## 学习顺序

```text
01 Model
  -> 02 Prompt
  -> 03 Structured Output
  -> 04 Tool
  -> 05 Retriever
  -> 06 Agent
```

每一步只保留一个可运行例子和一个 provider-free 测试：

- [x] Model：用 `ChatOpenAI` 接共享实验的 OpenAI-compatible provider
- [x] Prompt：用 `ChatPromptTemplate` 组织 system/human 消息
- [x] Structured Output：用 Pydantic 固定输出契约
- [x] Tool：用 `@tool` 生成 `StructuredTool` 和 Pydantic 参数 schema
- [x] Retriever：把 `03-rag-lab` 的 `RetrievalHit` 薄适配成 LangChain `Document`
- [x] Agent：用当前 `create_agent()` 体验一次 tool call → observation → final answer

## 完成后的最小数据流

```text
@tool + Pydantic input
        -> StructuredTool

03 VectorRetriever
        -> RetrievalHit
        -> VectorRetrieverAdapter
        -> LangChain Document

create_agent(fake model, lookup_runbook)
        -> AIMessage(tool_call)
        -> ToolMessage(observation)
        -> AIMessage(final answer)
```

- `tool.py`：静态本地 runbook，只展示 schema 与 handler 的职责分离。
- `retriever.py`：不做 chunk/embedding/search，只映射正文与 citation、score、rank、untrusted metadata。
- `agent.py`：只调用一次高层 `create_agent()`，不增加会话、Memory、Planning 或恢复能力。

## 边界

- 不依赖、不修改 Leon Agent。
- 08 不直接编写 LangGraph State、Node、Edge 或 Checkpoint；状态编排仍在 09 学。
- 当前 LangChain 1.x 的 `create_agent()` 内部会返回基于 LangGraph 的 compiled graph，这是框架实现事实，
  但不作为 08 的学习与扩展边界。
- 不做 Web、会话库、长期 Memory、Trace 平台或多 Agent。
- 自动测试只用 fake model；真实模型必须显式运行。
- 不复制 `03-rag-lab` 的 chunk / embedding / retrieval 实现，只写薄适配。

## 验收

当前 provider-free 入口：

```powershell
uv run langchain-lab --demo
```

`--live` 只在显式选择时读取共享实验 provider；自动测试不会访问真实模型。
这个 CLI 仍只演示 Model / Prompt / Structured Output；Tool、Retriever 和高层 Agent 使用各自的
provider-free 测试，避免把组件实验重新包装成一个产品入口：

```powershell
uv run pytest -q projects/08-langchain-lab/tests
```

完成后需要能用自己的话解释：

1. `ChatModel`、Prompt、Runnable 和 LCEL 分别是什么。
2. Pydantic parser 与 provider-native structured output 有什么差别。
3. Tool schema 如何约束模型参数，业务代码为什么仍需做权限校验。
4. Retriever 和 RAG pipeline 的边界是什么。
5. 为什么这里的高层 Agent 示例不能替代 Leon 或 LangGraph Lab。
