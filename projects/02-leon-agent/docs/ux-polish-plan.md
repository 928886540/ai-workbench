# Leon Agent — UX Polish Plan

> 历史设计稿。当前实现与协作真相以 [AI-COLLABORATION.md](AI-COLLABORATION.md) 为准。
> 二维码、未消毒 Markdown、伪 token streaming 和静态供应商模型列表均未进入合并结果。

> 分支：`feat/leon-ux-polish`
> 基于：`feat/leon-agent-mobile-web`（含 Phase 1-5）

---

## 场景定义

用户只有两个核心场景，非常清晰：

| 场景 | 触发 | 结果 |
|---|---|---|
| **生图** | 用户发一句话，LLM 判断需要生图 | Tool Call → 异步生图 → 返回图片 |
| **纯聊天** | 用户发一句话，LLM 直接回复 | 流式文字输出 |

两个端：

| 端 | 图片显示 | 体验上限 |
|---|---|---|
| **Web** | ✅ 直接内嵌图片 | 可以做得很精致 |
| **CLI** | ⚠️ 只能显示 URL / iTerm2 内联 | 受终端限制，优化空间有限 |

---

## Phase 6 — Web 体验精打细磨

### 6-A 打字机节流队列（最高优先级）

**问题**：现在 `assistant.delta` 直接 append，速度跟 SSE 推送速度走，忽快忽慢。
**目标**：匀速逐字打出，像 ChatGPT / Codex 那样丝滑。

```js
// 实现思路
const renderQueue = [];
let rendering = false;

function enqueue(chars) {
  renderQueue.push(...chars.split(''));
  if (!rendering) drainQueue();
}

function drainQueue() {
  rendering = true;
  if (!renderQueue.length) { rendering = false; return; }
  bubble.textContent += renderQueue.shift();
  setTimeout(drainQueue, 18); // ~55 chars/s，可调
}
```

**工作量**：前端 30 行，半小时。

---

### 6-B Thinking 状态动画

**问题**：`assistant.started` 到第一个 delta 之间有空白，用户不知道 Agent 在干什么。
**目标**：显示 `🤔 Thinking...` + 计时器，有回复后自动消失。

```
[ 🤔 Thinking...  2.3s ]
```

实现：`assistant.started` 时插入 thinking 气泡 + 启动计时器，`assistant.delta` 第一个 token 到来时移除。

**工作量**：前端 20 行。

---

### 6-C Tool Call 折叠展开

**问题**：Tool Card 现在只显示工具名和状态，细节不可见。
**目标**：点击可展开，看到 input 参数和 output 摘要，像 Codex 的 tool call 面板。

```
┌─ 🔧 generate_image  ✓ done          ▼ ┐
│  input:  { prompt: "赛博朋克城市.." }   │
│  output: job_id: abc123             │
└──────────────────────────────────────┘
```

后端需要：`tool.started` 带 `input` 字段，`tool.finished` 带 `output` 摘要。
前端：`<details>` 折叠，默认收起。

**工作量**：后端 events.py 加字段 + 前端 40 行。

---

### 6-D 生图体验专属升级

这是 Leon 的核心差异化，Codex / Claude Code 都没有。

**生图进行中**：
```
┌─────────────────────────────┐
│  🎨 正在生成图片...          │
│  ░░░░░░░░░░░░░░  queued     │
│  [模糊占位 skeleton]         │
└─────────────────────────────┘
```

**生图完成**：
```
┌─────────────────────────────┐
│  [图片全宽展示]               │
│  点击查看原图 → 长按保存      │
└─────────────────────────────┘
```

实现：`image.task.created` 时插入 skeleton card，`image.completed` 时用真实图片替换。

**工作量**：前端 60 行 + CSS。

---

### 6-E 消息气泡视觉升级

- 相邻同角色消息合并（减少头像/间距重复）
- Agent 消息支持 Markdown 渲染（`**bold**`、`code`、列表）
- 错误消息改为卡片样式 + retry 按钮
- 发送按钮：输入框有内容时高亮，空白时 disabled + 灰色

**工作量**：前端 50 行，引入轻量 MD 渲染（marked.js CDN，3KB）。

---

## Phase 7 — CLI 体验补齐

CLI 的核心限制：终端不能内嵌图片（除非 iTerm2 + Kitty protocol）。

### 7-A 图片展示策略分层

```
终端类型检测
  ├── iTerm2 / Kitty / WezTerm → 内联显示缩略图 (imgcat)
  ├── 普通终端 → 显示 URL + 二维码（qrcode-terminal）
  └── CI/无 TTY → 只显示 URL
```

### 7-B 流式输出体验

当前 CLI 是等 LLM 全部完成再打印，改为流式逐字输出：
- `assistant.delta` → 直接 `sys.stdout.write(delta)` + `flush()`
- `assistant.completed` → 换行

### 7-C 生图进度条

```
🎨 Generating image...
  ████████░░░░░░░░  queued → running → completed
  ✓ Done! http://192.168.8.100:8188/output/xxx.png
```

用 `rich` 库的 `Progress` 组件，`image.task.updated` 更新进度。

### 7-D 生图结果二维码

生图完成后在终端打印二维码，手机扫码直接看图：
```python
import qrcode
qr = qrcode.QRCode()
qr.add_data(image_url)
qr.print_ascii()
```

**工作量**：`pip install qrcode rich`，CLI 改动约 80 行。

---

## 执行顺序

```
Week 1（当前）
  [x] Phase 1-5 完成
  [ ] Phase 6-A  打字机队列        ← 先做，性价比最高
  [ ] Phase 6-B  Thinking 动画    ← 跟 6-A 一起
  [ ] Phase 6-D  生图 skeleton    ← 视觉冲击最大

Week 2
  [ ] Phase 6-C  Tool Call 折叠
  [ ] Phase 6-E  气泡 + Markdown
  [ ] Phase 7-B  CLI 流式输出
  [ ] Phase 7-C  CLI 进度条
  [ ] Phase 7-D  CLI 二维码

Week 3（可选）
  [ ] Phase 7-A  终端类型检测 + imgcat
  [ ] Web PWA 离线缓存优化
  [ ] 性能：SSE 重连指数退避
```

---

## 参考对象

| 产品 | 借鉴点 | 我们的差异化 |
|---|---|---|
| Codex | 打字机输出、Tool Call 折叠、Thinking 状态 | 生图内嵌体验 |
| Claude Code | 流式输出、错误卡片 | Timeline 视图 |
| Pi | 对话气泡设计、情感化 UX | 专注生图工作流 |

---

## 技术选型说明

- **Markdown 渲染**：`marked.js`（CDN 引入，3KB，零依赖）
- **CLI 进度条**：`rich`（项目已有 Python 环境）
- **CLI 二维码**：`qrcode[terminal]`
- **打字机队列**：纯原生 JS，不引入额外依赖
- **图片 skeleton**：纯 CSS animation，无额外依赖
