# 01-llm-core

Phase 01：把 LLM 调用底座做稳。

## 为什么先做这个

后面所有 Agent / RAG / MCP 都依赖：
- 稳定的模型客户端
- 清晰的配置
- 结构化输出
- 可读的失败信息

如果这里漂着，后面项目会反复重写。

## 本阶段范围

- [x] 共享 `LLMClient`
- [x] `.env` 配置模型
- [x] hello 连通性入口
- [x] Pydantic structured output 雏形
- [ ] 重试/超时策略细化
- [ ] prompt 模板管理
- [ ] 更完整的错误分类

## 运行

```bash
# 仓库根目录
copy .env.example .env
uv sync
uv run python -m llm_core.hello
```

## 设计要点

1. 业务代码不直接 new OpenAI client，统一走 `workbench_core.llm.LLMClient`
2. 配置集中在 `Settings`，避免每个脚本自己读 env
3. structured output 先用 JSON + Pydantic，再考虑 provider 专有能力
