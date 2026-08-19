# Leon Agent Evaluation

Evaluation 验证 Agent 在任务层面的行为质量；pytest 验证代码契约。两者不能互相替代。

## 最小闭环

`src/leon_agent/evaluation/` 复用生产 `LeonAgent`、system prompt、Planning 和 tool schema。
每个 case 的工具后端都是临时目录、静态搜索结果和内存 SQLite，不访问真实 Tavily、ComfyUI、用户
SQLite 或密钥。

```text
cases/core.json
        |
        v
LeonAgent + production tool schemas
        |
        +--> fake provider（默认，确定性回放）
        +--> live provider（仅 --live，显式 opt-in）
        |
        v
Task Success / Tool Selection / Plan Adherence / Answer Quality / Safety
Latency / Tool Calls / Tokens / Cost
```

当前包含 53 个 case：前 20 个建立核心基线，后 33 个补齐精确工具参数、搜索深度/主题、文件行窗、
Memory consent、prompt injection、失败路径、Planning 组合和多轮生图追问边界。case 可声明历史消息，
fake provider 还能断言当前 transcript 必须包含或禁止包含指定上下文。新增 case 必须声明用户原话、能力开关、
必须/禁止工具、关键参数、Planning 约束、答案断言和审计脱敏断言。

## 运行

```powershell
# 默认 fake，不读取 .leon，不请求任何 provider
uv run leon-eval

# 只跑指定 case
uv run leon-eval --case planned_file_and_web_research

# 保存 baseline；成本参数可选，单位为每百万 token
uv run leon-eval --output evals/results/fake-baseline.json \
  --input-cost-per-million 1 --output-cost-per-million 2

# 真实 provider 必须显式 opt-in；工具后端仍是无副作用模拟服务
uv run leon-eval --live

# 比较已有 baseline，检测 pass -> fail 回归
uv run leon-eval --baseline evals/results/fake-baseline.json
```

`--live` 只读取当前 `%USERPROFILE%\.leon\config.toml` 的 provider。不要把真实响应、密钥、带个人
信息的 case 或运行结果提交到 Git。

## 指标解释

- `Task Success`：本 case 的工具、Planning、答案和安全断言全部通过。
- `Tool Selection`：必须调用、禁止调用、调用次数和关键参数约束。参数只在当次内存 transcript 中评分，
  报告不会回显原始值。
- `Plan Adherence`：计划创建时机、步骤范围、顺序推进和终态完成。
- `Answer Quality`：最终答案必须包含/禁止包含的结论、引用或内部 ID。
- `Safety`：审计投影不得泄露 case 声明的敏感文本；最终答案也必须符合安全断言。
- `Latency` / `Tool Calls`：每 case 及套件总耗时、轮数和工具调用数。
- `Tokens` / `Cost`：provider 返回 usage 时累计；cost 只有显式传入单价才计算。

Fake provider 只证明评估框架、工具闭环和评分器可重复；真实模型的行为结论必须来自显式 `--live` 报告。
