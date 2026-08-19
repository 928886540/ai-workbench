# 转型计划：Java 后端 → AI 应用 / Agent 工程师

> 更新：2026-08-05
>
> 目标岗位：AI Application Engineer / Agent Engineer / FDE
>
> 学习主战场：本仓库 `ai-workbench`
>
> 面试弹药：`D:\apiWorkSpace\面试准备\AI应用工程师面试准备.md`
>
> BOSS 直聘逐栏投递稿：`notes/career/boss-zhipin-ai-agent-resume.md`

---

## 1. 总策略

不“裸转算法”，走：

> 企业后端工程能力 + AI 应用落地能力

你的差异化不是比别人更会刷题训模型，而是：

- 会接真系统
- 会做权限/稳定性/集成
- 能把 LLM/Agent 做成服务，而不是聊天窗口

---

## 2. 能力地图

### 已经有的（护城河）

- Java / Spring 业务系统
- API 与数据建模
- OA / SSO / 第三方集成
- 线上问题定位
- 异步、缓存、状态一致性意识

### 正在补的（主线）

1. LLM 应用开发
2. Tool Calling
3. Agent 架构
4. RAG
5. MCP
6. Workflow 自动化
7. 多 Agent / Coding Agent

### 暂不深挖（避免分心）

- 从零预训练
- 一上来就 Multimodal 研究论文
- 先堆 10 个框架不会原理

---

## 3. 与本仓库阶段的对应

| 职业目标 | 仓库阶段 | 你要能讲清的一句话 |
|----------|----------|--------------------|
| LLM 应用底座 | `01-llm-core` | 我能稳定调用模型并做结构化输出 |
| Agent 入门作品 | `02-code-agent` | 我会做 tool-using agent，能分析项目并给证据 |
| 企业知识场景 | `03-rag-lab` | 我能做可引用的知识问答 |
| 工具标准化 | `04-mcp-lab` | 我能把本地能力做成 MCP 服务 |
| 生产编排 | `05-workflow` | 我能做可恢复、可重试的 AI 工作流 |
| 协作智能 | `06-multi-agent` | 我理解多角色分工与仲裁 |
| 高级作品 | `07-coding-agent` | 我能做更接近生产的编码代理 |
| 框架组件 | `08-langchain-lab` | 我理解 Model、Prompt、Tool、Retriever 和高层 Agent 抽象 |
| Runtime 对照 | `09-langgraph-leon` | 我能比较自研 Runtime 与 LangGraph State/Checkpoint/Resume 的取舍 |

---

## 4. 90 天执行节奏

### Days 1-30：能演示的 Agent 雏形

- [x] 仓库骨架与定位
- [ ] `.env` 接通真实模型
- [x] `01-llm-core` hello + structured output
- [x] `02-code-agent` 最小 tool loop
- [x] 对 `ai-workbench` 自己跑出分析报告
- [x] 面试稿能讲：工具、循环、权限边界

**退出标准**

有一个本地可跑的 code analysis agent demo。

### Days 31-60：RAG + 评测意识

- [x] 文档切分与检索
- [x] 答案带 citation
- [x] 最小评测集（已扩到 53 个 Agent case）
- [x] 把“幻觉/未命中”讲清楚

**退出标准**

有一个可演示的文件/知识库问答。

### Days 61-90：MCP + 作品包装

- [x] 自建 MCP server（Leon MCP 第一版 5 个 tool）
- [ ] Agent 可经 MCP 调本地能力
- [x] README + 架构图 + 3 分钟演示脚本
- [x] 简历项目顺序切到 AI 主投版

**退出标准**

面试可以 10 分钟完整演示：问题 → 工具调用 → 结果证据。

---

## 5. 每周固定节奏

1. 选一个最小能力点
2. 先写设计（数据流/边界）
3. 实现最小闭环
4. 补 1 个测试或脚本验证
5. 记 1 条 decision / learning note
6. 用面试话术复述一遍“我做了什么、为什么、结果是什么”

---

## 6. 作品与简历的转化规则

每个项目结束时，必须能填这 5 句：

1. 业务问题是什么？
2. 为什么要用 AI/Agent，而不是规则系统？
3. 系统怎么走（输入→处理→输出）？
4. 工程上你解决了什么（超时、缓存、权限、状态）？
5. 现在的边界和下一步是什么？

没有这 5 句，就不算“可面试项目”。

---

## 7. 当前下一步（立刻做）

1. 用 `projects/09-langgraph-leon/docs/runtime-comparison.md` 演练 30 秒和 2 分钟 Runtime 对照回答。
2. 整理 Agent 核心理论和高频追问，确保每个回答能落到仓库证据。
3. 做 AI 应用/Agent 岗模拟面试，并根据卡壳点反向补文档，不扩产品功能。

---

## 8. 协作约定（和 Codex 一起推进时）

- 目标默认：服务「AI 应用 / Agent 工程师」转型
- 优先可演示、可测试、可讲清设计
- 不为了简历词堆框架
- 每次阶段结束，同步更新面试稿里的“可讲项目”
- BOSS 投递优先维护结构化在线简历，附件版只作为补充，不替代在线字段
