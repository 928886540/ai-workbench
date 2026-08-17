# 07-coding-agent

面试用 **Coding Agent Lab / Vertical Agent Demo**，目标不是替代 Codex 或 Claude Code，而是证明共享
`AgentRuntime` 能落到明确的垂直场景：

```text
读取/搜索代码 -> 制定简单计划 -> 显式授权写入 -> 跑固定测试
-> 观察测试结果 -> 失败后最多再修一次 -> 再跑测试 -> 输出 Git diff
```

## 已完成：基础闭环

基础版直接复用现有能力，只新增最薄的场景层：

- 复用唯一 `AgentRuntime` 作为多轮执行循环，复用 `ToolRegistry` 注册受控工具。
- 复用 Planning、File Search / File Read、显式授权 File Write 和 Trace，不创建第二套 executor。
- 模型先创建 2～4 步简单计划，再修改一个已跟踪的现有文本文件。
- 测试命令由服务端固定，始终 `shell=False`；写入和测试默认拒绝，必须显式授权。
- 每个任务最多写两次、跑两次测试；第一次失败后只允许再修一次。
- 最后用 Git 输出 changed paths 和可审查 diff，不自动 commit、push、reset 或切分支。
- Trace 只保留 `trace_id`、`turn_id`、父子 span、工具名、状态和耗时等 metadata；测试 attempt、
  pass/fail、exit code 及写入授权结果通过 `trace_id/span_id` 关联脱敏 tool audit，不复制进 Trace。

## 架构与数据流

```text
task
  -> AgentRuntime
     -> File Search / File Read
     -> Planning span
     -> authorized File Write
     -> fixed Test Runner
     -> optional one repair + second test
     -> GitWorkspace.diff()
  -> AgentResult + correlated Trace/tool audit
```

Trace 可以按顺序解释 root、turn、LLM iteration、文件工具、Planning、写入、两次测试和最终 diff；持久
Trace 不保存 prompt、文件正文、完整写入内容、搜索 query、测试输出、diff、绝对路径、Memory value、
secret/token/key 或图片/音频 URL。

`cwd=workspace` 不是 OS 沙箱。这个 Lab 只用于受信任的临时 Git 仓库和可控演示 case，不宣称能安全执行
陌生项目代码。

## 最小 API

```python
from pathlib import Path
from coding_agent import CodingAgent, Workspace

workspace = Workspace(Path.cwd())
agent = CodingAgent(
    workspace,
    test_command=("uv", "run", "pytest", "-q"),
    authorize_write=lambda request: request.relative_path == "src/example.py",
    authorize_test=lambda request: request.profile_id == "tests",
)
result = agent.run("修复 example.py 中的边界条件，并运行测试。")
```

## 本阶段验证

```powershell
uv run pytest projects/07-coding-agent/tests -q
uv run ruff check projects/07-coding-agent
```

当前 3 个 provider-free 稳定案例：

1. 修一个明确 bug。
2. 增加一个小功能，补充并实际执行对应测试。
3. 第一次测试失败后读取反馈，再修一次并通过。

测试只使用临时 Git workspace、本机 Python/Git 和 scripted fake client，不调用网络、真实 provider 或用户仓库。

## 当前非目标

- IDE 插件或替代 Codex、Claude Code、Cursor。
- 通用复杂 Patch Engine、任意 Shell 或多仓库操作。
- 自动 Git commit、push、分支操作或自动提交 PR。
- 长时间后台 Coding Task。
- Multi-Agent Coder / Reviewer / Tester。

基础版完成后停止扩产品功能。后续只在面试准备确有需要时，用同一组 case 做一次显式 live model 对照，
并整理 Runtime、Evaluation、Trace 和失败后二次修复的讲解。
