# 02-code-agent

Phase 02 主项目：会使用工具的代码分析 Agent。

## 你在学什么

这就是 **Agent 主流程**：

```text
用户问题
  -> 模型思考并选择工具
  -> 运行时真正执行工具
  -> 把观察结果塞回对话
  -> 循环
  -> 输出最终报告
```

不是聊天机器人，是 tool-using agent。

## 当前能力

- [x] workspace 路径沙箱
- [x] `list_dir` / `read_file` / `search_text`
- [x] tool schema
- [x] tool-calling loop
- [x] CLI 分析入口
- [ ] 更强的报告 schema 校验
- [ ] 记忆/摘要压缩
- [ ] 写入类工具（暂不做）

## 运行

```bash
# 确定性预览（不调模型）
uv run python -m code_agent.analyze --seed

# 真正的 Agent loop（默认读 CCS 配置）
uv run python -m code_agent.analyze

# 自定义问题
uv run python -m code_agent.analyze --question "这个仓库第一阶段在学什么？"
```

## 架构

```text
analyze CLI
   -> CodeAgent.run
        -> workbench_core.AgentRuntime
        -> LLMClient.chat_turn(tools=...)
        -> ToolRegistry / ToolRuntime.execute(...)
        -> messages append observation
        -> loop until final answer
```

通用 loop 已抽到 `packages/workbench_core/src/workbench_core/agent/`，Leon Agent 复用同一运行时；
本项目只保留代码分析 Prompt、工作区沙箱和文件工具。

## 设计原则

1. 先只读，后写入
2. 所有路径限制在 workspace root
3. 工具输出机器可读
4. 先自研最小 loop，再考虑框架
