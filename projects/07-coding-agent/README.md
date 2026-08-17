# 07-coding-agent

面试用 **Coding Agent Lab / Vertical Agent Demo**，目标不是替代 Codex 或 Claude Code，而是证明共享
`AgentRuntime` 能落到明确的垂直场景：

```text
读取/搜索代码 -> 制定简单计划 -> 改文件 -> 跑固定测试 -> 失败后再修一次 -> 输出 diff
```

## 已完成：基础闭环

基础版直接复用现有能力，只新增最薄的场景层：

- 复用唯一 `AgentRuntime`、`ToolRegistry`、Trace 和 `workbench_core.files` 的读/搜/写边界。
- 模型先创建 2～4 步简单计划，再修改一个已跟踪的现有文本文件。
- 测试命令由服务端固定，始终 `shell=False`；写入和测试默认拒绝，必须显式授权。
- 每个任务最多写两次、跑两次测试；第一次失败后只允许再修一次。
- 最后用 Git 输出 changed paths 和可审查 diff，不自动 commit、push、reset 或切分支。
- Trace 只保留工具名、路径、次数、exit code、是否通过等 metadata，不持久化文件、输出或 diff 正文。

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
2. 增加一个小功能并验证。
3. 第一次测试失败后读取反馈，再修一次并通过。

测试只使用临时 Git workspace、本机 Python/Git 和 scripted fake client，不调用网络、真实 provider 或用户仓库。

## 下一步

停止扩产品功能。后续只在面试准备需要时，用同一组 case 做一次显式 live model 对照，并整理 Runtime、Eval、
Trace 和失败后二次修复的讲解；不做 IDE 插件、复杂 patch engine、多仓库、自动 PR、后台任务或 Multi-Agent。
