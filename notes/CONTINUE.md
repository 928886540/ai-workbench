# 下次怎么和 Codex 续聊

## 结论

**别把“记忆”只押在某一个 session 文件上。**

长期可靠顺序：

1. **仓库状态**（代码 + notes）= 真记忆
2. **开场一句话** = 快速对齐
3. **Codex session 续接** = 加分项，有就用，没有也能继续

## 当前主线（2026-08-15）

- `workbench_core.agent`：共享 Agent Runtime / ToolRegistry 已完成
- `02-code-agent`：已迁移到共享 Runtime
- `02-leon-agent`：独立 `leon` CLI、SQLite、5 个生图工具已完成第一版
- `02-leon-agent` Web 五阶段已完成：FastAPI Gateway、SSE、PWA、token 登录、任务/图库/事件时间线
- CLI 与 Web 已接入 CCS 模型切换；Web 会话会继承已有 session 的模型选择
- 公网入口已部署到 `https://leon.928886540.xyz`，由 Cloudflare Tunnel 转发到 `127.0.0.1:8233`
- Windows 计划任务 `Leon Agent` 与 `CF Tunnel` 已运行，并配置登录自启/异常重试
- Leon 环境只读自检：19 模式、38 节点类型、39 LoRA，通过
- 架构决策：`notes/decisions/0002-leon-agent-boundary.md`
- 新需求计划：`notes/plans/leon-agent-expansion.md`
- Web 图片体验已升级：全屏查看器变可缩放相册（左右切换 / 计数 / 滑动翻页 / 定点缩放），生图完成后在底部追加新气泡并自动滚到可视区，模型选择改为可点击列表
- 前端演进评估：`projects/02-leon-agent/docs/web-client-evolution.md`
- **前端栈决策（2026-08-15，用户拍板）：迁 Vue 3 + Vite**。评估文档里「暂不迁 Vue3」的结论已被推翻，后续以本条为准。
  目标结构：`web/src/{api,stores,views,components}` + `vite.config.ts`，构建产物 `dist/` 交给 FastAPI 托管。
  决策依据：JS 已 807 行 / 48 个顶层可变变量 / 65 处 DOM 查询，近期修的 4 个 bug 全是「状态与 DOM 手动同步」这一类结构性问题；
  且组件一文件一职责后，外部 agent 改动边界清晰，不必再动 1250 行单文件。
- 前端 W1 已完成：`messages[]` + `messageIndex` 单一数据源，`createMessage/renderMessage/patchMessage/removeMessage`；
  复制 / 真重试 / 编辑 / 朗读工具栏、底部图片气泡、模型候选收起均已落地，SW 缓存已升到 v17
- Volink TTS 已接入：4 个模型 / 561 个中文音色；目录使用 `lang=zh-CN`，可搜「风韵少妇」，支持试听、收藏、
  手动朗读、自动朗读、loading / 波形状态，以及 iOS Chrome 的用户手势解锁与待播音频恢复；前后端会清理
  Markdown 列表横杠、emoji、链接、模式 ID、任务 ID 和计划 ID，避免 TTS 把 `-` 读成“减/简”
- 任务页现在优先显示中文生图模式、原始描述和中文状态；内部 job/plan ID 默认收进“任务详情”折叠区
- Volink `502` 已确认是偶发上游失败，不是 166 字文本过长：相同原文在本地和公网均实测过 `200 audio/mpeg`。
  网关现在记录 voice ID、原始/净化字符数和上游错误，Web 会显示 `detail`；未加入无依据的自动重试
- 可通过 `LEON_SYSTEM_PROMPT_FILE` 读取 UTF-8 TXT 并追加到 Agent system prompt；本机文件位于被 Git 忽略的
  `data/system-prompts/双人成行预设.txt`，CLI 与 Web Gateway 都已接入
- Web 修复（2026-08-15）：旧 session 聊天记录恢复渲染、任务/图库按 `created_at` 最新优先、
  全屏图片 edge-to-edge `cover` 显示；SW 缓存升到 v17
- CLI 已完成第一版全屏 TUI：交互终端使用上方滚动区 + 底部 Enter 输入框，启动面板展示
  model/provider/base URL/config source；非 TTY 继续使用 Rich fallback。`generate_images` 工具
  采用 `return_direct`，图片任务完成后直接输出结果，避免再发一轮 provider 请求（低 RPM provider 必须如此）。
- 当前验证（2026-08-15 实测）：全仓库 `pytest` **97 passed**、`ruff check .` 通过、
  浏览器端到端 `tests/manual_web_check.py` **56/56 通过**（真实 Chrome + 390×844 触屏模拟）
- ⚠️ LLM provider 已被 CC Switch 换过（`~/.codex/config.toml`，8/15 09:30）：
  `anyrouter.top` → `new-api.abrdns.com`，默认模型 `gpt-5.6-sol` → `DeepSeek-V4-Flash-0731`，目录从 17 个模型变成 96 个。
  会话里「同样的话上次能答、这次不能答」优先怀疑这里，而不是提示词。
- 工具面：Leon 后端 iOS 插件层共 17 个端点，agent 原先只接了 5 个。已补 `cancel_image_task`
  （`POST /ios/async_autogen/{job_id}/cancel`，会同时中断正在跑的 ComfyUI prompt），工具数 6 → 7。
  仍未暴露、可按需接入：`async_cancel_matching`（批量取消）、`image_gallery/delete`（删图，破坏性）、
  `image_tasks/hide` / `hide_history`（清理列表）、`metrics/tasks/{job_id}`（任务指标）、
  `async_autogen/recover`（恢复）、`comic_compose` / `async_comic`（漫画模式）
- 下一步最小动作：在 Cloudflare Dashboard 为 `/api/agent/*/events` 添加 Cache Bypass Rule，然后手机端验收 SSE 与生图闭环
- 前端下一步（W2）：按上面的决策启动 Vue 3 + Vite 迁移。先搭 `vite.config.ts` + 构建产物托管链路（含 SW / Cloudflare 隧道回归），
  再按 `views/` 逐页搬迁；W1 的 `messages[]` 直接对应 `stores/messages.ts`，不用重写
- ASR 尚未接入；TTS 已完成，网关使用 `POST /api/agent/tts` 和 `/api/voice/*`
- 后续优先级：面试用 Leon MCP Server -> 共享 Service -> Telegram Bot
- Tavo 路线：先做 Leon Agent -> Tavo MCP；Tavo -> 外部 Leon MCP 等宿主支持
- 下一步可选：按既定决策启动 Vue 3 + Vite W2；CLI TUI 不再是阻塞项

---

## 多 agent 协作约定（Codex / Notion AI / 本地 IDE 并行时）

现在可能有多个 agent 同时改这个仓库，遵守下面三条，否则会互相踩：

### 1. 提交前必须跑完这三条，全绿才提交

```bash
uv run pytest -q                                  # 期望：97 passed
uv run ruff check .                               # 期望：All checks passed
# 浏览器端到端（需网关在 127.0.0.1:8233 运行）
export LEON_TOKEN=$(grep -E "^LEON_API_TOKEN=" .env | cut -d= -f2- | tr -d '\r\n')
export CHROME_PATH="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
uv run --with playwright python projects/02-leon-agent/tests/manual_web_check.py   # 期望：56/56 通过
```

`pytest` 对前端只做字符串断言，**改了渲染链路必须跑第三条**，否则 ReferenceError 这类问题测不出来。

> 注：`~/AppData/Local/ms-playwright` 下的浏览器已在 8/11 被清空（0 字节），
> 所以走系统 Chrome (`CHROME_PATH`)，不要用 `p.chromium.launch()` 的默认路径。

### 2. 不要留半成品在磁盘上

重构如果分多步写，要么一次性补丁落地，要么改完立刻跑第 1 条。
曾出现过「变量已改名、引用点没跟上」导致 web 客户端一生图就 ReferenceError 的中间态。

### 3. 本地提交不推送 = 对 GitHub 侧 agent 不存在

Notion AI 通过 GitHub 读代码。任何没 `git push` 的 commit，它**完全看不到**，
会基于旧代码继续改并产生冲突。做完一段就推。

同理，只在聊天里达成的决定（比如上面的 Vue 3 决策）**必须写进 notes/**，
否则另一个 agent 无从知晓 —— 本文件开头那句「仓库状态 = 真记忆」就是这个意思。

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
