# 02-code-agent

Phase 01 主项目：会使用工具的代码分析 Agent。

## 目标用户故事

```text
用户：帮我分析这个项目
Agent：
1. 查看目录
2. 读取关键文件
3. 总结结构
4. 给建议
5. 输出结构化报告
```

## 为什么这是第一阶段主项目

一次覆盖 AI 工程核心肌肉：
- LLM
- tool calling
- agent loop
- context 控制
- 安全边界
- 结构化输出

比“做一个聊天机器人”更接近真实 Agent 工程。

## 当前进度

- [x] workspace 路径沙箱
- [x] `list_dir` / `read_file` 只读工具
- [x] 确定性 seed analysis（不依赖模型）
- [ ] tool schema + model function calling
- [ ] 最小 ReAct / tool loop
- [ ] 分析报告 schema
- [ ] CLI：`analyze <path> --question "..."` 

## 运行（当前骨架）

```bash
uv sync
uv run python -m code_agent.analyze
uv run pytest projects/02-code-agent/tests -q
```

## 架构草图

```text
CLI / entry
   |
   v
Agent Loop
   |-- LLM (plan / tool calls / final answer)
   |-- Tool Runtime
   |     |-- list_dir
   |     |-- read_file
   |     |-- search_text (next)
   |-- Workspace Guard
   |
   v
Structured Report
```

## 设计原则

1. 先只读，后写入
2. 所有路径必须限制在 workspace root 内
3. 工具输出要机器可读，方便 loop 消化
4. 先自研最小 loop，再考虑框架
