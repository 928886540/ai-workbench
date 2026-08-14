# 0002：Leon Agent 独立运行，生图能力通过工具适配

- 状态：Accepted
- 日期：2026-08-14

## 背景

`02-code-agent` 已跑通最小 tool-calling loop。下一步需要把学习项目变成可日常使用的个人 Agent：
终端输入 `leon` 后既能聊天，也能调用现有 Leon / ComfyUI 生图系统。

现有 `leon-image` 已拥有稳定的 Prompt、Workflow、LoRA、任务队列和图库逻辑。若在 Python 中重写，
两套资产会迅速分叉。

## 决策

1. `leon-agent` 是 `ai-workbench` 内的独立项目、进程和会话空间。
2. 通用 Agent loop 下沉到 `workbench_core.agent`，CodeAgent 和 Leon Agent 共用。
3. Agent 侧注册工具；现有 Leon 后端继续暴露 HTTP API，不注入 Agent 运行时。
4. 生图请求通过 Node bridge 调用原插件 `executor-core.js + executor-assets.js` 构造。
5. 第一版采用 Rich REPL；全屏 Textual TUI 后置。
6. 第一版不要求 MCP；接口稳定后再把适配层迁移为 Leon MCP Server。
7. SQLite 只保存会话、工具摘要和任务 ID，不保存完整 workflow 或 provider key。

## 理由

- 独立进程让 Tavo 插件和 Agent 可以分别升级、分别故障。
- 复用执行资产可保证 CLI 与手机插件使用同一套生图策略。
- 先做 REPL 能验证 Agent、工具和状态闭环，避免 UI 工作掩盖核心问题。
- ToolRegistry 先作为进程内接口，后续迁移 MCP 时只替换适配边界。

## 代价

- 第一版依赖本机 Node.js 和 `leon-image` 源目录。
- 插件执行资产接口变化时 bridge 需要跟随验证。
- Agent 只能查询自己 `chat_id` 下的任务；全局图库访问需另行设计权限和过滤。

## 后续迁移条件

当 Leon 后端提供稳定的高层“按模式 + 场景生成”接口，或完成 MCP Server 后，删除 Node bridge，
Agent 的工具名和上层对话逻辑保持不变。
