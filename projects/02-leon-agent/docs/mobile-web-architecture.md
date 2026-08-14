# Leon Agent Mobile Web Client — 架构设计文档

> 状态：**Phase 1-5 已完成**
> 原始实现分支：`feat/leon-agent-mobile-web`
> 最后更新：2026-08-14

---

## 1. 项目目标

为 Leon Agent 提供一个适合手机竖屏使用的远程 Web Client，让用户可以通过手机浏览器（或 PWA）与本机运行的 Leon Agent 进行自然语言对话，并实时看到：

- Agent 决策过程（Tool Call 卡片）
- 生图任务全生命周期（queued → running → completed → 图片）
- 会话历史与图库

面试演示核心诉求：让观察者清楚看到「自然语言 → Agent 决策 → Tool Call → 异步任务 → Tool Result → 最终结果」的完整链路，而不是「输入 prompt → 出图」的黑盒。

---

## 2. 当前 Leon Agent 架构

```
CLI (leon)
    ↓
Agent Runtime (workbench_core.agent)
   ├─ LLM Provider      (workbench_core.llm)
   ├─ Agent Loop        (AgentRuntime)
   ├─ Tool Registry     (ToolRegistry / AgentTool)
   ├─ Session / Memory  (SQLite, leon-agent.db)
   └─ Event Stream      (内部 Python 事件)
        ↓
Leon Image Tools (tools.py)
   ├─ check_image_environment
   ├─ generate_images          ← 轮询 /ios/image_tasks/sync
   ├─ get_image_tasks
   ├─ get_recent_images
   └─ get_image_modes
        ↓
LeonImageClient (leon_client.py)
   ├─ _absolute_media_url()    ← 相对路径 → 绝对 URL
   └─ HTTP → /ios/async_autogen, /ios/image_tasks/sync, /ios/image_gallery/sync
        ↓
ComfyUI (localhost:8188)
```

关键标识符：`LeonImageClient`、`LeonNodeBridge`、`LeonSettings`、`LeonImageError`
关键环境变量：`LEON_BACKEND_URL`、`LEON_PUBLIC_IMAGE_BASE_URL`、`LEON_SESSION_DB`

---

## 3. 为什么手机端只是 Client

**错误架构**（禁止）：
```
手机 App → 第三方 LLM → Leon Agent
```

**正确架构**：
```
手机浏览器 / PWA
        ↓
Leon Agent HTTP API          ← 本文要设计的新层
        ↓
Leon Agent Runtime
   ├─ LLM
   ├─ Agent Loop
   ├─ Tool Registry
   ├─ Session / Memory
   └─ Event Stream
        ↓
Leon Image Adapter
        ↓
现有 Leon 生图系统
        ↓
ComfyUI
```

理由：

- **LLM 配置在服务端**：手机端不知道也不应该知道 LLM key、模型名称、system prompt
- **Tool 逻辑在服务端**：`generate_images` 的轮询、URL 补全、异常处理全部在 Python 层
- **Session 持久化在服务端**：SQLite 在本机，手机只持有 `session_id`
- **ComfyUI 不暴露**：端口 8188 不对外，所有生图请求经由 Leon Agent API 代理

CLI 是 Client，手机 Web 是 Client，未来 iOS App 也只是 Client。三者共享同一个 Leon Agent Runtime。

---

## 4. 手机端竖屏 UI

**布局原则**：单列聊天时间线，不做桌面双栏。

```
┌─────────────────────┐
│ Leon Agent      在线 │  ← 顶栏：标题 + 连接状态
├─────────────────────┤
│                     │
│  用户消息            │  ← 右对齐气泡
│                     │
│  Agent 回复          │  ← 左对齐气泡，流式渲染
│                     │
│  ┌─ Tool Call 卡片 ┐ │
│  │ ✓ check_env    │ │  ← 工具调用结果卡片
│  └────────────────┘ │
│                     │
│  ┌─ Tool Call 卡片 ┐ │
│  │ ⏳ generate_img │ │  ← 进行中工具
│  │ queued → running│ │
│  └────────────────┘ │
│                     │
│  ┌──── 图片结果 ────┐ │
│  │  [生成图片]      │ │  ← 完成后自动展示
│  └────────────────┘ │
│                     │
├─────────────────────┤
│ [+]  输入内容  [发送] │  ← 输入栏固定底部
└─────────────────────┘
```

**底部导航**（4 个入口）：
```
[💬 聊天]  [📋 任务]  [🖼 图库]  [🔧 调试]
```

**重要原则**：
- 完整 SSE 日志不放主聊天界面
- Agent Event 转换成 UI 卡片（Tool Call 卡、任务状态卡、图片卡）
- 原始 SSE 调试数据单独放在「调试」页面

### 页面功能规划

| 页面 | 内容 |
|------|------|
| 聊天 | 用户消息、Agent 回复、Tool Call 卡片、Tool Result、任务状态、图片结果 |
| 任务 | 生图任务列表（queued / running / completed / failed） |
| 图库 | 最近生成图片、图片来源任务、generationPlanId、jobId |
| 调试 | SSE Events 原始流、Tool Calls、Session ID、连接状态、Agent Runtime 信息 |

---

## 5. HTTP API 设计

所有接口以 `/api/agent` 为前缀，仅暴露 Leon Agent HTTP 服务，不暴露 ComfyUI。

### Session 管理

```
POST   /api/agent/sessions                    创建新会话
GET    /api/agent/sessions                    列出会话
GET    /api/agent/sessions/{session_id}       获取会话详情
```

### 消息

```
POST   /api/agent/sessions/{session_id}/messages   发送消息（触发 Agent loop）
GET    /api/agent/sessions/{session_id}/messages   获取历史消息
```

### 事件流

```
GET    /api/agent/sessions/{session_id}/events     SSE 事件流
```

### 图片任务

```
GET    /api/agent/sessions/{session_id}/image-tasks          任务列表
GET    /api/agent/sessions/{session_id}/image-tasks/{job_id} 单个任务
GET    /api/agent/gallery                                    最近图片
```

### 健康检查

```
GET    /api/health         基础存活
GET    /api/health/detail  LLM + ComfyUI + Image Tool 状态
```

**链路要求**：
```
Web Client → Leon Agent API → Leon Agent → Leon Image Adapter → ComfyUI
```
浏览器绝对不直接调用 ComfyUI。

---

## 6. SSE Event Schema

所有事件统一格式：

```json
{
  "event": "<event_type>",
  "session_id": "<uuid>",
  "timestamp": "<ISO-8601>",
  "data": {}
}
```

### 事件类型一览

| 事件 | 说明 |
|------|------|
| `session.started` | 会话建立 |
| `user.message` | 用户消息已收到 |
| `assistant.started` | Agent 开始生成回复 |
| `assistant.delta` | 流式回复片段 |
| `assistant.completed` | 回复完成 |
| `tool.started` | Tool Call 开始 |
| `tool.finished` | Tool Call 结束（含结果摘要） |
| `image.task.created` | 生图任务已创建 |
| `image.task.updated` | 任务状态变更 |
| `image.completed` | 生图完成，含图片 URL |
| `agent.error` | Agent 异常 |

### 事件示例

**Tool Call 开始**：
```json
{
  "event": "tool.started",
  "session_id": "6b34ef29606447d395f05899ba30abf7",
  "timestamp": "2026-08-14T13:00:00Z",
  "data": {
    "tool_name": "generate_images",
    "tool_call_id": "call_abc123"
  }
}
```

**任务状态更新**：
```json
{
  "event": "image.task.updated",
  "session_id": "6b34ef29606447d395f05899ba30abf7",
  "timestamp": "2026-08-14T13:00:05Z",
  "data": {
    "generation_plan_id": "plan_xyz",
    "job_id": "job_456",
    "status": "running"
  }
}
```

**生图完成**：
```json
{
  "event": "image.completed",
  "session_id": "6b34ef29606447d395f05899ba30abf7",
  "timestamp": "2026-08-14T13:00:30Z",
  "data": {
    "job_id": "job_456",
    "generation_plan_id": "plan_xyz",
    "image_url": "https://comfyui.example.com/view?filename=output_001.png"
  }
}
```

**原则**：不把 Agent Runtime 内部 Python 对象直接 JSON dump 给前端，建立稳定的 API Event Schema。

---

## 7. Session 生命周期

```
客户端 POST /api/agent/sessions
    → 服务端创建 session（写入 SQLite）
    → 返回 session_id

客户端 GET /api/agent/sessions/{id}/events  （SSE 长连接）
    → 服务端推送事件

客户端 POST /api/agent/sessions/{id}/messages
    → 触发 Agent Loop
    → Agent Loop 通过 Event Stream 推送事件
    → 完成后推送 assistant.completed

客户端断线
    → SSE 自动重连（Last-Event-ID）
    → 服务端补发断线期间事件
    → 手机切后台 → 重连 → session 恢复
```

---

## 8. Tool Call 展示方式

### 演示示例流程

**用户**：检查一下图片环境

```
[Tool Call 卡片]
● check_image_environment  调用中...
✓ check_image_environment  完成
  └ ComfyUI: Online | Plugin: Ready | Modes: k2_tifa
```

**用户**：生成一张雨夜东京街景

```
[Tool Call 卡片]
● generate_images  调用中...
  generationPlanId: plan_xyz
  jobId: job_456
  ⏳ queued
  ⏳ running
  ✓ completed

[图片卡片]
[图片缩略图]
https://comfyui.example.com/view?filename=...
```

**用户**：刚才那张怎么样了

```
Agent 调用: get_image_tasks
[任务卡] job_456 → completed
```

**用户**：把最近生成的图片给我看看

```
Agent 调用: get_recent_images
[图库卡片列表]
```

---

## 9. 生图任务实时状态

生图任务状态机：

```
created → queued → running → completed
                          ↘ failed
```

UI 展示策略：
- Tool Call 卡片展示当前状态（实时刷新）
- 状态变更通过 `image.task.updated` 事件推送
- `completed` 后自动展示图片，不需要用户再问
- `failed` 后展示错误信息和 jobId

服务端轮询逻辑（已在 `generate_images` tool 实现）将状态变化封装为 SSE 事件推送给前端，前端只需监听事件，不直接调用 ComfyUI。

---

## 10. 图片返回流程

```
1. 前端 POST /messages → 触发 Agent Loop
2. Agent 调用 generate_images tool
3. tool 内部：POST /ios/async_autogen → 获取 generationPlanId
4. tool 内部：轮询 /ios/image_tasks/sync
5. 每次状态变化 → 服务端发送 image.task.updated SSE
6. 完成后：GET /ios/image_gallery/sync → 获取 image_url
7. _absolute_media_url() 补全为绝对 URL
8. 服务端发送 image.completed SSE（含绝对 image_url）
9. 前端收到事件 → 渲染图片
```

前端从不直接调用 `/view?filename=...` 以外的 ComfyUI 接口。图片 URL 由服务端补全后透传。

---

## 11. 网络和鉴权

### 对外只暴露

```
Leon Agent HTTP 服务（待定端口，建议 8233）
```

**不对外暴露**：ComfyUI 8188、SQLite 文件、内部 Node Bridge

### 鉴权

```
Authorization: Bearer <token>
```

- Token 在服务端配置（环境变量 `LEON_API_TOKEN`）
- 前端启动时输入 Token，存 localStorage
- 所有 API 请求携带 Bearer Token
- SSE 连接通过 URL query 参数传 token（`?token=...`）或首次握手验证

### 其他安全

- HTTPS（通过反向代理或 Cloudflare Tunnel 终止）
- CORS：仅允许已知 origin
- 简单 Rate Limit：按 IP 或 Token 限流
- SSE 断线重连：客户端发送 `Last-Event-ID`，服务端补发事件
- 手机切后台：重连后恢复 session，续接事件流

### 公网暴露方案（评估，实现阶段不绑定单一供应商）

| 方案 | 优点 | 缺点 |
|------|------|------|
| Cloudflare Tunnel | 无需公网 IP，免费 | 依赖 CF |
| Tailscale | 点对点，低延迟 | 需要设备加入 Tailnet |
| 反向代理（Nginx/Caddy）| 完全自控 | 需要公网 IP |

---

## 12. PWA / Safari 使用方式

- **Add to Home Screen**：manifest.json，icon，standalone 模式
- **Safari 限制**：SSE 在后台会被挂起，需要断线重连机制
- **离线缓存**：Service Worker 缓存静态资源，API 请求不缓存
- **手机网络恢复**：监听 `online` 事件，自动重连 SSE
- **输入法适配**：输入框在键盘弹出时不被遮挡（`viewport-fit=cover`）
- **图片预览**：原生 `<img>` + 点击放大，不依赖第三方库

---

## 13. 后续原生 iOS App 扩展方式

Web Client 设计时保持 API 稳定性，未来 iOS App 只需：

1. 替换前端层（Swift/SwiftUI → 调用同一套 HTTP API）
2. 复用完全相同的 SSE Event Schema
3. 使用 `URLSessionWebSocketTask` 或 `EventSource` 库消费事件
4. 不修改任何 Leon Agent Runtime 代码

迁移路径：PWA → iOS WebView → 原生 iOS App，每步都只换 Client，不动 Runtime。

---

## 14. 实施阶段拆分

### Phase 1：Agent HTTP Gateway

**目标**：
- Session API（创建、列出、查询）
- Chat API（发消息、获取历史）
- SSE Event Stream（连接、事件推送）
- 不修改 Agent 核心行为

**产出**：可用 curl / Postman 验证的 HTTP 服务

### Phase 2：Mobile Web MVP

**目标**：
- 手机竖屏单列聊天界面
- Agent 回复流式渲染
- Tool Call 卡片
- SSE 实时状态

**产出**：手机浏览器可访问的最简聊天页面

### Phase 3：Image Experience

**目标**：
- 生图任务卡（generationPlanId / jobId）
- 实时状态：queued / running / completed
- 图片完成后自动展示
- 图库页面

**产出**：完整生图体验，适合面试演示

### Phase 4：PWA / Remote Access

**目标**：
- Add to Home Screen
- HTTPS
- Bearer Token 鉴权
- SSE 断线重连
- 手机网络恢复后自动重连

**产出**：可从手机公网访问的 PWA

### Phase 5：Interview Mode（面试展示模式）

**目标**：
- Tool Call 明显可见（高亮卡片）
- Agent Event Timeline（时间轴视图）
- Runtime 状态面板
- 任务链路可视化
- 不展示敏感 Token
- 不暴露内部 Prompt

**产出**：适合对外演示的面试模式

---

## 15. 风险与非目标

### 非目标（当前阶段禁止）

- 在手机端重新实现 Agent
- 在手机端重新配置 LLM
- 复制 Prompt、Workflow、LoRA 或 ComfyUI 逻辑
- 删除或替换 CLI（CLI 继续作为 Client 存在）
- 暴露 ComfyUI 8188 到公网
- 修改 Agent Runtime、ComfyUI、Leon Image Plugin、Prompt Pipeline、Workflow、LoRA

### 风险

| 风险 | 缓解策略 |
|------|----------|
| SSE 在 Safari/iOS 后台被挂起 | 断线重连 + Last-Event-ID 补发 |
| 手机切后台 session 丢失 | session_id 存 localStorage，重连后恢复 |
| ComfyUI 生图时间长，SSE 超时 | 心跳 ping 事件，保持连接 |
| 公网暴露安全风险 | Bearer Token + HTTPS + Rate Limit |
| Agent Runtime 事件流与 HTTP SSE 桥接 | Phase 1 核心工程问题，优先验证 |
| 多个 SSE 客户端同时连接同一 session | Phase 1 明确单 session 单连接限制 |
