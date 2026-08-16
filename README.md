# AI Workbench

长期 AI 工程实验室，不是课程笔记仓库。

目标：通过可运行、可演进的真实项目，掌握 LLM 应用开发与 Agent 工程。

## 定位

- 不是 demo 合集
- 不是概念笔记堆
- 是一个会持续变强的 **AI Engineering Lab**

每个知识点都要落到：
1. 可运行代码
2. 清晰设计决策
3. 可测试行为
4. 能逐步逼近生产级结构

## 学习方向

- LLM 应用开发
- RAG
- Agent 架构
- Tool calling
- MCP
- Workflow 自动化
- AI Coding Agent
- 多 Agent 协作

## 仓库结构

```text
ai-workbench/
├── packages/workbench_core/   # 共享基础设施（配置、LLM 客户端、日志、schema）
├── projects/
│   ├── 01-llm-core/           # 模型调用、Prompt、结构化输出
│   ├── 02-code-agent/         # 第一阶段主项目：会用工具的代码分析 Agent
│   ├── 02-leon-agent/         # 独立 CLI：聊天 + Leon 生图工具
│   ├── 03-rag-lab/            # 文件/知识库问答
│   ├── 04-mcp-lab/            # 自建 MCP server / client
│   ├── 05-workflow/           # 工作流编排与自动化
│   ├── 06-multi-agent/        # 多 Agent 协作
│   └── 07-coding-agent/       # 更接近生产的 AI Coding Agent
├── notes/                     # 设计决策与学习笔记
├── scripts/                   # 仓库级脚本
└── data/samples/              # 示例数据（不含密钥）
```

## 为什么这样设计

1. **monorepo + projects**：一条学习主线，多个可独立演进的项目，避免碎片仓库。
2. **先 shared core，再业务项目**：配置、LLM 客户端、日志、错误模型只写一次，后面项目复用。
3. **编号阶段目录**：强制学习顺序，同时允许后期回填增强。
4. **notes/decisions**：把“为什么这样设计”固化下来，防止 AI 协作时反复推翻架构。

## 当前阶段

**`02-leon-agent` 的 CLI 与手机 Web Client 均已完成；Web 端 5 个阶段已跑通。**

做一个能分析本地项目的 AI Agent Assistant：

```text
用户：帮我分析这个项目
Agent：
1. 查看目录
2. 读取关键文件
3. 总结结构
4. 给出风险/改进建议
5. 输出结构化报告
```

一次覆盖：
- LLM 调用
- Tool calling
- Agent loop
- 基础 memory / context 管理
- 结构化输出

现在可以从终端进入独立 Agent：

```powershell
uv run leon
```

普通问题直接聊天；明确的生图请求会调用现有 Leon / ComfyUI 工具。原 Tavo 插件保持独立，
Agent 不复制它的 Prompt、Workflow 或 LoRA。详见
[Leon Agent 项目](projects/02-leon-agent/README.md) 和
[总体架构](docs/leon-agent-architecture.md)。

在真正做 Agent 前，先用 `01-llm-core` 把模型调用与结构化输出底座打稳。

## 技术栈（第一阶段）

| 层级 | 选择 | 原因 |
|------|------|------|
| 语言 | Python 3.10+ | AI 工程主场，生态成熟 |
| 包管理 | `uv` + `pyproject.toml` | 快、可复现、适合 monorepo |
| LLM 接入 | OpenAI-compatible SDK | 可接 OpenAI / DeepSeek / 本地 NewAPI / vLLM |
| 数据模型 | Pydantic v2 | 结构化输出与配置校验 |
| 测试 | pytest | 从第一天就验证行为，不只“能跑” |
| 质量 | ruff | 轻量 lint/format，不引入重型工具链 |
| Agent 框架 | 先自研最小 loop | 先理解消息、工具、规划；框架后置 |

明确不做的事（第一阶段）：
- 不先上 LangChain 全家桶
- 不做只有聊天壳子、没有真实工具闭环的 Agent
- 不先堆 RAG 向量库花活

## Projects 进度

- [x] `01-llm-core`：模型调用 / Prompt / Structured Output
- [x] `02-code-agent`：Tool-using Code Analysis Agent
- [x] `02-leon-agent`：独立 CLI / 会话 / Leon 生图工具 / FastAPI + SSE 手机 Web Client
- [ ] `03-rag-lab`：文件问答与检索增强
- [ ] `04-mcp-lab`：Leon MCP Server 第一版已完成；其余 MCP 实验待做
- [ ] `05-workflow`：工作流自动化
- [ ] `06-multi-agent`：多 Agent 协作
- [ ] `07-coding-agent`：生产向 Coding Agent

## 协作方式（和 AI 一起开发时）

默认流程：

1. 先讲架构 / 数据流 / 设计理由
2. 确认后再写代码
3. 写完做最小验证
4. 记录设计决策

不直接说“给我代码”，而说：

> 先设计，再实现，再测试。


## 职业方向

近阶段目标：Java 后端 → **AI 应用工程师 / Agent 工程师**。

- 转型计划：`notes/career/transition-plan.md`
- 面试弹药：`D:/apiWorkSpace/面试准备/AI应用工程师面试准备.md`

## 快速开始

```bash
# 1. 安装依赖（仓库根目录）
uv sync

# 2. 复制环境变量
copy .env.example .env

# 3. 验证模型底座或进入 Leon Agent
uv run python -m llm_core.hello
uv run leon
```

## 文档

- [学习路线](LEARNING_ROADMAP.md)
- [设计决策](notes/decisions/0001-repo-bootstrap.md)
- [Leon Agent 架构决策](notes/decisions/0002-leon-agent-boundary.md)
- [AI 协作约定](AGENTS.md)
