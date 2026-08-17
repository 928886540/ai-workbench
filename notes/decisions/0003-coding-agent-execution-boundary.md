# 0003：Coding Agent Demo 执行边界

- 日期：2026-08-17
- 状态：接受
- 适用范围：`projects/07-coding-agent`

## 背景

Leon 已形成 Evaluation、RAG 和 Trace 最小闭环。下一阶段只需要用 Coding Agent 证明共享 Runtime 能支撑
一个垂直场景，不需要做能替代 Codex/Claude Code 的产品。已有 `02-code-agent` 和共享文件工具负责读/搜；
本阶段补最小写入、固定测试、失败反馈后二次修复和最终 diff。

## 决策

1. `07-coding-agent` 独立于 Leon，复用唯一 `AgentRuntime`、Trace、`Workspace`、File Search 和 FileWrite。
2. 这是 **Coding Agent Lab / Vertical Agent Demo**，不是生产产品；只准备 3 个稳定 provider-free case。
3. 不向模型暴露任意 shell string。composition root 固定测试 argv，底层 `shell=False`。
4. 写文件与跑测试默认拒绝，只有服务端 authorization hook 能放行。
5. 任务必须从 clean Git worktree 开始，只覆写已跟踪的现有文本文件；不实现 patch apply engine。
6. 每个任务最多写两次、跑两次测试，使“第一次失败 -> 读错误 -> 再修一次”成为明确边界。
7. 最终只读取 Git diff；不自动 commit/push/reset/checkout。Trace 只保存脱敏 metadata。

## 为什么这样做

- 固定 argv 把“模型选择测试动作”和“服务端决定实际命令”分开，避免 shell 拼接。
- clean baseline 让最终 diff 只包含本次演示修改。
- 两次写入/测试足以证明 Agent 能消费失败反馈，不需要无限自动修复。
- pytest 验证工具契约和 3 个 scripted case；Evaluation 衡量 live 模型成功率；Trace 解释一次运行。

## 非目标

- 不做 patch parser、任意终端、IDE 插件、多仓库、后台 daemon、自动 PR 或 OS sandbox。
- 不做自动 commit/push、分支切换、reset/checkout 或 dirty baseline。
- 不做无限自动修复、并行 executor、LangGraph 或 Multi-Agent。
- 不把 stdout/stderr、patch 正文或 diff 正文写入持久 Trace；后续 audit 只保留 profile、exit code、耗时、
  changed path/count 等脱敏 metadata。

## 后续演进门槛

基础版完成后停止扩功能。只有面试反馈明确要求时，才补同一组 case 的显式 live model 对照；不默认进入
新建/删除文件、复杂 patch、容器执行或框架重写。
