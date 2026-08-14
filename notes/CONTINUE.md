# 下次怎么和 Codex 续聊

## 结论

**别把“记忆”只押在某一个 session 文件上。**

长期可靠顺序：

1. **仓库状态**（代码 + notes）= 真记忆
2. **开场一句话** = 快速对齐
3. **Codex session 续接** = 加分项，有就用，没有也能继续

## 当前主线（2026-08-14）

- `workbench_core.agent`：共享 Agent Runtime / ToolRegistry 已完成
- `02-code-agent`：已迁移到共享 Runtime
- `02-leon-agent`：独立 `leon` CLI、SQLite、5 个生图工具已完成第一版
- Leon 环境只读自检：19 模式、38 节点类型、39 LoRA，通过
- 架构决策：`notes/decisions/0002-leon-agent-boundary.md`
- 新需求计划：`notes/plans/leon-agent-expansion.md`
- 下一步优先级：面试用 Leon MCP Server -> 共享 Service -> Telegram Bot
- Tavo 路线：先做 Leon Agent -> Tavo MCP；Tavo -> 外部 Leon MCP 等宿主支持
- 流式输出和完成通知跟进；完整 TUI 后置

---

## 三种续聊方式

### 方式 A：最稳（推荐）

1. 打开目录：`D:\apiWorkSpace\ai-workbench`
2. 新开 / 续开 Codex
3. 直接发下面「续聊开场」

即使旧 session 丢了，也能接上。

### 方式 B：续同一个 Codex thread/session

如果 Codex 界面里还能看到这次对话：

- 直接继续说就行
- 适合短时间内接着改同一块代码

注意：
- session 可能过期、换模型、换机器后不好用
- 不要假设它永远记得所有细节

### 方式 C：只丢一句“看仓库”

```text
读 AGENTS.md、notes/career/transition-plan.md、projects/02-code-agent/README.md，
从当前进度继续。
```

---

## 推荐续聊开场（直接复制）

```text
继续 ai-workbench。

我是 Java 后端转 AI 应用/Agent。
当前主线：把 02-leon-agent 做成日用 Agent，并继续理解 tool-calling loop。
模型配置走 CC Switch（CCS），默认薄荷。

请先快速确认：
1. 仓库当前进度
2. 上一步完成了什么
3. 下一步最小动作

然后直接接着干，先设计后编码。
```

如果你已经想好下一步，直接更具体：

```text
继续 ai-workbench 的 02-code-agent。
下一步：给 leon CLI 增加流式输出，并补事件流测试。
先设计，再改代码。
```

---

## 这个仓库里谁负责“记忆”

| 文件 | 作用 |
|------|------|
| `AGENTS.md` | 和 AI 协作的固定规则 |
| `LEARNING_ROADMAP.md` | 学习主线 |
| `notes/career/transition-plan.md` | 转型 90 天计划 |
| `notes/decisions/` | 架构为什么这样设计 |
| `notes/learning/ccs-integration.md` | 如何从 CCS 读模型配置 |
| `projects/02-code-agent/README.md` | 当前 Agent 项目状态 |
| 本文件 | 下次怎么续聊 |

---

## 实操建议

1. **每次告一段落**，让我更新对应 README / notes（比只靠聊天记录稳）
2. **换天继续**，优先用上面开场，不要只说“继续”
3. **CCS 配置**继续说名字即可：`用 CCS 的薄荷` / `用 CCS 的大黑客`
4. session 能续就续；**不能续也没问题**，以仓库为准
