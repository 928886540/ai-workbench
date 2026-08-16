# Web 客户端演进评估：是否迁 Vue3、聊天化改造、气泡工具栏、语音接入

> 状态：历史设计 + 实施状态（2026-08-16）
> 关联提交：`3aab43a fix(leon-web): 图片查看器改为可缩放相册，完成后新气泡回图`；
> `5aba213 feat(leon-web): 迁移 Agent Timeline`
> 唯一 Web 源码位于 `web/src/`；旧单文件客户端已删除
>
> 当前实施（2026-08-16）：Vue 3 + Vite 已覆盖聊天、任务、图库、设置视图和 Agent Timeline；助手气泡支持复制 /
> 真重试 / 编辑 / 朗读，Volink 提供 4 个模型和 561 个中文音色，SSE `voice.ready` 会追加可播放
> 语音气泡；聊天输入已支持 `/nsfw --model` 模式补全、自动增高、上滚保留和错误详情折叠，
> Agent Timeline 可查看最近 100 条 SSE 决策事件。ASR 输入、tokens/model 上屏、provider 钉选重启持久化、
> 时间分割线和 CSS 变量主题底座已实现（ASR 需配置 `LEON_ASR_*` 后方可用）；真实生图/TTS 与手机实机验收仍待进行；
> SSE 已加入进程内 `id` / `Last-Event-ID` 补发和 stale-token 登录恢复；provider-free Playwright smoke
> 已在 Vite/FastAPI 各 26/26 通过。
> Vue 已成为唯一入口，`LEON_WEB_CLIENT` 开关已删除。

---

## 1. 结论先行

| 议题 | 结论 |
| --- | --- |
| 现在迁 Vue3？ | **迁移完成并成为唯一入口**。Vue 3 + Vite 已覆盖聊天、任务、图库、设置和 Agent Timeline；provider-free smoke 已通过。 |
| 界面聊天化？ | **做**，优先级最高。纯前端改动，不动网关协议。 |
| 气泡工具栏（复制 / 重试 / 耗时 / tokens / 朗读）？ | **部分完成**。复制、真重试、编辑、朗读和前端耗时已完成；tokens 待后端 usage。 |
| 语音？ | **TTS 已完成，ASR 待做**。Volink 密钥仅在网关；前端支持目录、手动/自动朗读、`voice.ready`、单例播放器与 iOS 解锁。 |

---

## 2. 历史评估：为什么当时不迁 Vue3

本节记录 2026-08-15 之前的取舍，不代表当前路线。用户已决定迁移 Vue 3 + Vite，旧客户端已经删除，
现状与剩余门槛见第 6 节。

### 2.1 会直接废掉现有测试策略

`tests/test_gateway.py` 目前对**服务端返回的 HTML 字符串**做断言：

```python
assert "function zoomAt(" in html
assert "const result=createImageResult(href);" in html
assert "setViewerScale" not in html
assert "/sw.js?v=12" in html
```

这套断言便宜、快（79 个测试 2 秒跑完）、不需要浏览器。一旦上 Vue3 + 构建产物：

- HTML 退化成 `<div id="app">` 加一堆 hash 文件名的 bundle，上面每一条断言全部失效；
- 得换成 Vitest（组件单测）+ Playwright（端到端），仓库里要多一条 Node 工具链；
- 迁移成本明显大于「加个复制按钮」这件事本身。

### 2.2 要做的功能不需要框架

复制、重试、耗时、tokens、朗读，本质是**给消息对象加字段 + 气泡下方渲染一排按钮**。现有的 `createImageResult()`、`addImageSkeleton()`、`addErrorCard()`、`renderGallery()`、`renderModelList()` 已经是工厂函数，就是组件雏形。原生 DOM 完全够。

### 2.3 部署链路简单是优势

现在：`StaticFiles(directory=_WEB_DIR, html=True)` 直接抬单文件，配合 `disable_web_shell_cache` 中间件 + Service Worker 版本号，改完刷新就生效。上构建后多出一步 `npm run build`，手机上改一行样式都要进构建管道。

### 2.4 什么时候就该迁了

出现下面任意 **两条** 就开始迁：

1. 流式输出 + 重试 + 多图任务并发 + 语音播放态互斥，四者同时存在；
2. `index.html` 超过 ~2000 行，改一处要搜三个地方；
3. 需要多页面（会话列表 / 图库 / 设置 独立路由）；
4. 要上 TypeScript 给消息类型做约束。

预估：把下面的聊天化改造做完，就会命中第 1 和第 2 条。所以**迁移很可能就是下一个大动作**——但必须在功能需求先落地之后，不能边迁边改。

---

## 3. 先做的重构（同时就是 Vue3 的前置）

目标：把「渲染」和「状态」拆开。现在是每个事件直接 `appendChild`，没有一份可信的消息列表。

### 3.1 引入 `messages[]` 单一数据源

```js
// 消息对象（前端内存模型）
{
  id: 'm_17...',          // 客户端生成，用于定位 DOM
  role: 'user' | 'agent' | 'system',
  text: '',
  status: 'pending' | 'streaming' | 'done' | 'error',
  images: [],             // 图片结果列表
  meta: {
    model: 'gpt-5-codex',
    startedAt: 1755..., finishedAt: 1755...,
    elapsedMs: 4210,
    tokensIn: null, tokensOut: null,
  },
  audio: { state: 'idle' | 'loading' | 'playing', url: null },
}
```

规则：所有事件（SSE、图片轮询、错误）**只改 `messages[]`**，然后调 `patchMessage(id)` 重渲染单条气泡。不全量重画（会打断图片加载和滚动位置）。

### 3.2 抽出渲染函数

| 新函数 | 职责 |
| --- | --- |
| `renderMessage(msg)` | 返回完整气泡节点（含工具栏） |
| `renderBubbleBody(msg)` | 正文：Markdown / 图片 / 错误卡 |
| `renderBubbleToolbar(msg)` | 下方按钮排 + 元数据 |
| `patchMessage(id)` | 局部更新，不动已加载的 `<img>` |

现有 `addImageSkeleton` / `replaceSkeletonWithImage` / `addErrorCard` 改为操作 `messages[]` 后走 `patchMessage`。

这一步做完，日后迁 Vue3 就是把 `renderMessage` 换成 `<MessageBubble :msg="msg">`，数据模型一行不改。

---

## 4. 气泡工具栏规格

参照常见聊天客户端（grok / ChatGPT 手机版）：按钮**常驻在气泡下方**，低对比度图标，点击后有反馈。手机上不用 hover 才显现。

### 4.1 用户气泡

| 按钮 | 行为 |
| --- | --- |
| 复制 | `navigator.clipboard.writeText(msg.text)`，图标瞬变✓ 1.5s |
| 重试 | 删除该消息之后的所有回复，重发相同内容 |
| 编辑 | 回填输入框，原消息标为已改（第二批） |

### 4.2 Agent 气泡

| 按钮 / 字段 | 行为 |
| --- | --- |
| 复制 | 同上，复制原始 Markdown |
| 重试 | 重发上一条用户消息（已有 `lastUserText` 可复用），旧回复不删，追加新气泡 |
| 朗读 | 调 TTS，详见第 5 节 |
| 耗时 | `meta.elapsedMs` 格式化为 `4.2s` |
| tokens | `meta.tokensIn/Out`，无值则**不渲染**（不显示 `0 tokens`） |
| 模型名 | 当前回复实际用的模型，方便切模型后回溯 |

### 4.3 错误气泡

保留现有 `.error-card`，把 `.error-retry-btn` 并入统一工具栏；错误原文（如 `HTTP 424 upstream_error`）默认折叠，点「详情」展开。

### 4.4 需要网关配合的字段

这部分是后端小改动，在 `gateway/events.py` 的完成事件里带上：

```json
{
  "type": "message.completed",
  "model": "gpt-5-codex",
  "elapsed_ms": 4210,
  "usage": { "input_tokens": 1234, "output_tokens": 567 }
}
```

> 注：usage 取不到时统一传 `null`，前端靠「无值不渲染」规则自然降级。耗时宁可前端自己算（`finishedAt - startedAt`），不阻塞后端。

---

## 5. 语音接入（TTS 已完成，ASR 已实现）

TTS 已按网关代理方案落地；ASR 已实现网关代理端点，未配置环境变量时对前端隐藏。

### 5.1 输出（TTS，朗读）

新增网关端点，避免语音密钥落到前端：

```
POST /api/agent/tts
{ "text": "...", "voice_id": "689334e84d3396ad1d28ee9e" }
-> 200 audio/mpeg
```

目录由 `GET /api/voice/catalog` 提供。前端按钮覆盖 idle / loading / playing，使用**全局单例播放**；
iOS Chrome 拒绝自动播放时保留待播 URL，用户点击解锁按钮后继续播放。

### 5.2 输入（ASR，语音发消息）

```
POST /api/agent/asr   (multipart: audio 文件)
-> { "text": "..." }
```

前端：输入框旁加麦克风，`MediaRecorder` 录音 → 上传 → 转写回填输入框（**不自动发送**，给一次校对机会）。

### 5.3 环境变量预留

| 变量 | 用途 |
| --- | --- |
| `VOLINK_API_KEY` / `VOLINK_BASE_URL` | Volink 语音合成服务 |
| `VOLINK_DEFAULT_VOICE_ID` | 默认 24 位音色 ID |
| `LEON_ASR_BASE_URL` / `LEON_ASR_TOKEN` | 语音转写服务 |
| 未配置 Volink 时 | TTS 目录返回 disabled，前端隐藏朗读能力 |

---

## 6. Vue 迁移当前边界

Vue 源码位于 `web/src/`，按职责拆成 `api`、`stores`、`views`、`components` 和语音工具层，
并由 Vite 构建成 `web/dist/`。当前已接通：

- `ChatView`：会话恢复、SSE 事件消费、发送/重试/编辑、图片完成事件、语音气泡和播放解锁；
- `TasksView` / `GalleryView`：任务状态、中文模式名、图库及图片预览；
- `SettingsView`：会话模型选择、语音目录/偏好；
- `/nsfw --model` 补全：输入匹配命令前缀后异步读取 `GET /api/image-modes`，按 `name`、`id`、`aliases`
  过滤，支持点击、ArrowUp/ArrowDown、Enter、Escape 和失焦收起；请求序号与输入快照共同防止迟到响应覆盖新输入。
- 聊天交互：textarea 自动增高至约五行、超出后内部滚动；用户上滚后暂停自动跟随并提供“回到最新”；
  错误气泡保留重试入口，原始错误文本放在默认折叠的原生 `details` 中。
- Agent Timeline：记录除高频 `assistant.delta` 外的最近 100 条 SSE 决策事件，提供事件分类、详情、清空、
  Esc/按钮关闭，并在新会话、退出和组件卸载时重置。
- SSE 恢复：Gateway 保留最近 100 条事件并按 `Last-Event-ID` 回放；Vue 对瞬时断线使用原生 EventSource
  重连，检测到 stale token 后清理会话并回到登录页，`online` 事件会唤醒 CLOSED 的连接。

该补全只读取 Gateway 的模式目录，不调用 LLM、Volink 或真实 provider。`voice.ready` 事件同样只消费
Gateway 推送的音频元数据，播放器由前端单例管理。

当前验证包括静态契约、TypeScript 检查、Vite build，以及
`tests/manual_vue_web_check.py` 的 provider-free Playwright smoke（Vite/FastAPI 各 19/19）。该脚本只拦截
API，不证明真实 provider、Cloudflare 缓存规则或手机网络行为；本机/公网 SSE 已做只读首事件验收，
Vue 已全面替换旧单文件客户端；Gateway 只托管 `web/dist/`，缺少构建产物时启动会明确失败。
Gateway 自 W4 起在 `assistant.completed` 发送权威 `model` / `elapsed_ms` / `usage` 字段；取不到时为
`null`，Vue 按「无值不渲染」规则降级为客户端观测耗时。
Gateway 当前也没有真正发送 `assistant.delta`；Vue 保留兼容分支，真实回复仍以 `assistant.started` /
`assistant.completed` 事件为主。
SSE 回放窗口是进程内的最近 100 条，服务重启后不提供历史事件持久回放；页面刷新仍依赖 session/image-state
接口恢复可见状态。

---

## 7. 界面聊天化（视觉）

- **头部**：双行——会话名 + 当前模型胶囊，点模型胶囊直开 `model-list`（现有）
- **气泡**：用户右侧强色、agent 左侧低调；圆角 18px；同一角色连续消息收紧间距
- **时间分割线**：超过 10 分钟间隔插一条居中时间
- **输入区**：多行自增高 textarea（上限 5 行）+ 图片 / 麦克风 / 发送；保留 `font-size:16px` 防 iOS 缩放
- **主题**：先把颜色提成 CSS 变量，为深色 / 自定义壁纸铺路
- **滚动**：继续用 `autoFollowMessages`；用户上滚时显示「回到最新」悬浮按钮

---

## 8. 建议排期

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| **W1** | `messages[]` + `renderMessage` 重构，行为不变（已完成） | 无 |
| **W2** | 气泡工具栏：复制 / 真重试 / 编辑 / 朗读 / 耗时（已完成） | W1 |
| **W3** | 聊天化视觉 + CSS 变量主题 | W1 |
| **W4** | 网关补 `model` / `elapsed_ms` / `usage`，tokens 上屏（已完成） | W2 |
| **W5** | 语音：TTS 朗读（已完成）→ ASR 输入（已完成，需配置 `LEON_ASR_*`） | ASR API |
| **W6** | Vue 迁移：聊天、任务、图库、设置、Agent Timeline（已完成） | W1-W5 |
| **W7** | 模式补全与 `voice.ready` 事件（已完成） | W6 |
| **W8** | provider-free 浏览器回归（Vite/FastAPI 19/19） | W6-W7 |
| **W9** | 真实 Gateway/Cloudflare/手机验收并收口上线验证 | W8 |

每个阶段单独一个 commit，Vue 契约/浏览器 smoke 同步补断言，`sw.js` 缓存版本号递增。

---

## 9. 遗留风险

1. HTML 字符串断言很脆：改一行实现就可能红。W1 后将断言转向**函数名与 DOM id**，少碰具体语句。
2. IDEA 的缓存可能比磁盘旧（本次已踩到）：改 `index.html` 前先用磁盘读确认。
3. `manual_vue_web_check.py` 不进 CI，不能替代真实公网/手机验收。
4. Service Worker 缓存：每次前端改动必须同时改 `sw.js` 版本和注册 `?v=`，否则手机拿旧缓存。
5. Cloudflare 仍需确认 `/api/agent/*/events` 的 Cache Bypass Rule，避免公网 SSE 被缓存或缓冲。
