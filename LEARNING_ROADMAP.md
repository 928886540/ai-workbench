# Learning Roadmap

## 总原则

用项目驱动，不按教材章节堆概念。

每个阶段必须同时交付：
- 可运行模块
- 至少一个真实任务
- 设计决策说明
- 最小测试

## 阶段总览

```text
01-llm-core
   调用模型 API
   Prompt 工程
   Structured Output
        |
        v
02-code-agent          <--- 第一阶段主项目
   Tool calling
   Agent loop
   项目分析报告
        |
        v
02-leon-agent          <--- 当前实战
   独立 CLI / Session
   通用 Agent Runtime
   Leon 生图工具适配
        |
        v
03-rag-lab
   文档切分 / 检索 / 引用
        |
        v
04-mcp-lab
   自己写 MCP server
   把本地能力暴露给 Agent
        |
        v
05-workflow
   任务编排、重试、状态机
        |
        v
06-multi-agent
   角色分工、消息总线、仲裁
        |
        v
07-coding-agent
   更接近 Codex 风格的工程 Agent
```

## Phase 0 / 1：`01-llm-core`

### 要解决什么
如果模型调用本身不稳，后面所有 Agent 都是空中楼阁。

### 实现目标
- 统一 OpenAI-compatible 客户端
- 环境变量配置与校验
- Chat / System / User 消息组织
- JSON / Pydantic 结构化输出
- 基础重试、超时、错误分类
- 可观测：请求摘要日志（不记录密钥）

### 验收标准
- 能调用至少一个真实模型
- 能稳定拿到结构化 JSON
- 失败时错误可读，而不是裸 traceback 糊脸

### 为什么先做这个
Agent 的本质是：
`LLM + tools + loop + state`
没有稳的 LLM 层，loop 只会放大混乱。

## Phase 1 主项目：`02-code-agent`

### 要解决什么
做一个能分析本地代码仓库的 Agent Assistant。

### 用户故事
```text
输入：一个本地项目路径 + 问题
过程：Agent 自主决定看哪些目录/文件
输出：结构化分析报告（结构、风险、建议、证据路径）
```

### 必须学会的能力
- function / tool calling
- tool schema 设计
- ReAct 或最小 plan-execute loop
- 上下文窗口管理（读文件不能无脑塞）
- 安全边界（只能访问工作区）
- 报告结构化输出

### 建议工具集（第一版）
- `list_dir`
- `read_file`
- `search_text`
- `path_stat`
- `write_report`（可选）

### 为什么不是聊天机器人
聊天机器人太容易停在“会说话”。
这个项目逼你做：
- 工具接口
- 权限边界
- 多步推理
- 结果可验证

## Phase 1.5：`02-leon-agent`

### 目标
把最小 Agent loop 变成真正日用的个人 Agent，而不是另做一套聊天壳子。

### 已完成
- 共享 `AgentRuntime + ToolRegistry`
- `leon` 交互式 CLI
- SQLite session / message / tool call
- 复用原 Leon 插件资产的生图工具
- 任务与图库查询

### 下一步
- 面试可演示的 Leon MCP Server（最高优先级）
- Telegram Bot channel adapter
- Leon Agent 连接 Tavo MCP Server
- 流式模型输出与生成任务完成通知
- 上下文摘要压缩
- 核心闭环稳定后再评估 Textual TUI

## Phase 2：`03-rag-lab`

### 目标
把“模型临场看文件”升级成“可检索的知识系统”。

### 关键点
- chunk 策略
- embedding
- 向量库或先用简单本地索引
- citation（答案必须带来源）
- 评估：命中率 / 幻觉率

### 为什么放在 Agent 后
先会用工具读文件，再理解检索只是另一种“受控取证”。
顺序反了容易变成“接了向量库但不会做 Agent”。

## Phase 3：`04-mcp-lab`

### 目标
把本地能力标准化成 MCP server，让不同 Host/Agent 复用。

### 关键点
- MCP tool / resource 模型
- server 生命周期
- 与自研 Agent 的对接
- 权限与输入校验

## Phase 4：`05-workflow`

### 目标
从单次 Agent run，升级到可恢复工作流。

### 关键点
- 状态持久化
- 重试 / 补偿
- 人工确认节点
- 超时与失败策略

## Phase 5：`06-multi-agent`

### 目标
拆分角色：研究员 / 工程师 / 审查员。

### 关键点
- 共享状态 vs 私有上下文
- 任务分配
- 冲突仲裁
- 成本控制

## Phase 6：`07-coding-agent`

### 目标
做一个更接近生产的 coding agent 子集：
- 改文件
- 跑测试
- 根据失败继续修
- 产出可审查 diff

### 注意
这是后期项目。前期如果直接做，会同时踩工具、权限、测试、记忆、工作流所有坑。

## 每个阶段的固定节奏

1. 设计：架构、数据流、边界
2. 骨架：目录、接口、空实现
3. 最小闭环：一条 happy path
4. 测试：至少覆盖核心行为
5. 复盘：写入 `notes/decisions/`
6. 增强：错误处理、可观测、配置、文档

## 第一周建议执行顺序

Day 1：
- 确认仓库结构
- 配好 `.env`
- 跑通 `01-llm-core` hello

Day 2-3：
- 完成 structured output
- 统一错误与日志

Day 4-7：
- 启动 `02-code-agent`
- 先实现 3 个只读工具 + 最小 agent loop
- 能对当前仓库输出第一份分析报告

## 完成定义（Definition of Done）

一个阶段只有同时满足才算完成：
- 有真实代码，不只是 markdown
- 有 README 说明怎么跑
- 有至少 1 个自动测试或可重复验证脚本
- 有设计决策记录
- 能讲清：输入 -> 处理 -> 输出 -> 失败怎么表现
