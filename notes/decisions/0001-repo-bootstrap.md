# 0001 - Repo Bootstrap

## 决策

把 `ai-workbench` 建成 monorepo 学习实验室：
- 共享能力：`packages/workbench_core`
- 阶段项目：`projects/0N-xxx`
- 设计记录：`notes/decisions`

## 为什么

1. 长期维护需要稳定的共享底座，否则每个项目重复造 client/config。
2. 分阶段项目目录能保持学习主线，同时允许单独深化。
3. 先写决策笔记，避免和 AI 协作时每次重新发明架构。

## 第一阶段主项目为什么是 code-agent

- 比 chat UI 更接近真实 Agent 工程
- 能自然带出 tool calling、权限、上下文管理
- 产出可验证（报告是否基于真实文件）

## 技术栈为什么先选 Python + OpenAI-compatible + 自研 loop

- Python：AI 工程主生态
- OpenAI-compatible：兼容云端与本地网关
- 自研最小 loop：先理解原理，再引入 LangGraph 等框架

## 后果

- 短期会多写一点基础设施代码
- 长期切换项目时成本更低，架构更一致
