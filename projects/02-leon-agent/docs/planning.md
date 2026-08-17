# Leon Agent Planning

本文冻结 Leon Agent 第一版显式 Planning 的边界。现有 `AgentRuntime` 已能在一次请求中多轮调用工具；
本阶段补的是可检查的任务计划与状态迁移，不再造第二套执行器。

## 目标

```text
复杂用户请求
  -> plan_create(2..8 个步骤)
  -> plan_update(step, in_progress)
  -> 真实业务工具
  -> plan_update(step, completed | failed)
  -> 下一步骤
  -> 最终答案
```

- 只有明确需要多个工具动作、研究或诊断的请求才规划；普通聊天和单工具请求直接执行。
- Plan 只负责描述与跟踪，文件、联网、生图、记忆等副作用仍由原业务工具完成。
- 每个 Agent turn 最多创建一个计划；下一 turn 开始前清空，不把临时计划当长期 Memory。
- 计划最多 8 步，每步描述最多 160 字符。

## 状态机

```text
pending -> in_progress -> completed
                       -> failed
```

- 同一时间最多一个 `in_progress` 步骤。
- 后一步只能在前序步骤进入终态后开始。
- `completed` / `failed` 是终态，不能重新打开。
- 非法序号、重复创建、越序执行和非法迁移返回稳定错误码，不靠模型自觉维持约束。

## 工具契约

- `plan_create(steps)`：创建 2..8 个有序步骤。
- `plan_update(step_index, status)`：推动一个步骤的状态。
- `plan_get()`：读取当前 turn 的完整计划，供 LLM 在长工具链中恢复位置。

三个工具只注册到普通 `LeonAgent`；`/nsfw` 直达生图和 Leon MCP 不注册 Planning。

## 数据与审计边界

- 完整步骤描述只存在于当前 provider transcript，和文件正文/Memory raw value 一样不写入审计。
- `AgentEvent`、`ToolStep`、SSE 与 SQLite 只保存步骤总数、序号、状态、完成/失败计数和当前活动步骤。
- 计划文本不得授权文件写入、Memory 写入或扩大工具权限；真正的业务策略仍会二次校验。
- 取消 turn 时不需要补写终态；已完成业务副作用按现有 cancellation audit 规则记录，下一 turn 重新规划。

## 第一版不做

- 不做跨 turn 的后台任务队列或计划恢复。
- 不做 DAG、并行步骤、自动重试、动态插入/删除步骤。
- 不新增 Web 管理 API，不暴露 MCP，不让 Planning 绕过业务工具授权。

## 验收

- 普通 `LeonAgent` 注册三个 Planning 工具，direct registry 不注册。
- 服务端拒绝单步/超长/超量计划、重复创建、越序和非法状态迁移。
- fake LLM 能真实执行 `create -> update -> 业务工具 -> update -> final` 多轮闭环。
- raw 步骤描述存在于当前 LLM transcript，但 Event、ToolStep 和 SQLite 均无原文。
- 连续两个 turn 的计划状态相互隔离；取消后下一 turn 从空计划开始。

