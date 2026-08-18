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
- [ ] Tool：理解 `@tool` / `StructuredTool` 和参数 schema
- [ ] Retriever：把 `03-rag-lab` 的检索接口适配为 LangChain Retriever
- [ ] Agent：只体验一次高层 Agent API，不扩成产品

## 边界

- 不依赖、不修改 Leon Agent。
- 不引入 LangGraph；状态、节点、边、checkpoint 在 09 学。
- 不做 Web、会话库、长期 Memory、Trace 平台或多 Agent。
- 自动测试只用 fake model；真实模型必须显式运行。
- 不复制 `03-rag-lab` 的 chunk / embedding / retrieval 实现，只写薄适配。

## 验收

当前 provider-free 入口：

```powershell
uv run langchain-lab --demo
```

`--live` 只在显式选择时读取共享实验 provider；自动测试不会访问真实模型。

完成后需要能用自己的话解释：

1. `ChatModel`、Prompt、Runnable 和 LCEL 分别是什么。
2. Pydantic parser 与 provider-native structured output 有什么差别。
3. Tool schema 如何约束模型参数，业务代码为什么仍需做权限校验。
4. Retriever 和 RAG pipeline 的边界是什么。
5. 为什么这里的高层 Agent 示例不能替代 Leon 或 LangGraph Lab。
