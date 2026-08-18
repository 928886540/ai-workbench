# 下次怎么和 Codex 续聊

## 结论

**别把“记忆”只押在某一个 session 文件上。**

长期可靠顺序：

1. **仓库状态**（代码 + notes）= 真记忆
2. **开场一句话** = 快速对齐
3. **Codex session 续接** = 加分项，有就用，没有也能继续

## 当前主线（2026-08-18）

- **框架对照新主线已启动**：保留 `02-leon-agent` Self-built Runtime；新增小型
  `08-langchain-lab` 和独立 `09-langgraph-leon`（对外名 Leon Agent Framework Edition）。
- `08-langchain-lab` 已完成 Model / Prompt / Pydantic Structured Output 的 provider-free demo；
  Tool / Retriever / 高层 Agent 仍按一次一个组件推进。
- `09-langgraph-leon` 已完成第一条共享契约：把原 `AgentTool + ToolRegistry` 薄适配给 LangChain Tool，
  通过 `ToolNode + tools_condition` 实际执行原 Leon `read_file`，节点流为
  `START -> agent -> tools -> agent -> END`；未复制 schema/handler，测试和 demo 均不访问真实 provider。
- Framework Edition 的 Milestone 1、Planning 和 Memory 复用已完成：从
  `%USERPROFILE%\\.leon\\config.toml` 构造 LangChain `ChatOpenAI`，注册 File/Web 与原 Leon 三个 Memory
  tools，并提供简洁交互/`--once` CLI；`--plan` 会增加 `plan` node 和 2～4 步 Graph State，默认关闭以免
  每轮多打一请求。Memory 默认开启，可用 `--no-memory` 关闭；原 consent、单轮写额度和敏感值拒绝仍生效。
- Milestone 3 已完成最小闭环：`EncryptedSerializer` + AES-EAX 加密完整 checkpoint/pending writes，
  调用方 metadata 被隔离，旧明文/mixed row、错 key、缺 key均 fail closed；provider-free 测试已证明
  File/Memory/User/AI 原文不出现在 DB/WAL，重开后仍能完整解密状态。live `leon-graph` 默认使用
  加密 SQLite，稳定 opaque `thread_id` 支持 `--resume <id>` 和交互 `/resume <id>`；跨进程 pending
  恢复只允许 read-only tools，写类 Memory 工具 fail closed。下一步进入 RAG 共享 Tool / 对照报告，或
  根据面试需要补一条受限高风险 interrupt 说明，不扩 Leon 产品功能。
- `03-rag-lab` 已定义唯一只读 `rag_search` 业务 Tool：复用 `VectorRetriever`，限制 query/top_k、
  安全 citation 和 5500 字符总 observation，并对 query/正文做 audit 脱敏。下一步用同一个 Tool 实例
  分别经过 Self-built `AgentRuntime` 与 LangGraph `ToolNode` 的结果一致性证明已经通过；两边 raw
  observation 相等且 handler 各执行一次。尚未注入原 Leon 或 09 live CLI，不能写成 live 已启用。
  下一步收口 08 的 Tool / Retriever / 高层 Agent，再写综合对照报告；09 不扩 Web、TTS/ASR、Gallery、
  SSE、MCP、Coding Agent 或 Multi-Agent。

- 当前集成分支：`main`；最新闭环包含 SQLite/CLI/Web Trace、语音与流式/中断持久化、
  页面恢复、图片任务补拉、全屏图片缩放平移，以及按 `assistant.delta` 分段合成和按 revision 重试的流式 TTS
- `workbench_core.agent`：共享 Agent Runtime / ToolRegistry 已完成
- `02-code-agent`：已迁移到共享 Runtime
- `02-leon-agent`：独立 `leon` CLI、SQLite、7 个生图工具和 `speak_text` 已完成
- `02-leon-agent`：可选 File Search + 显式 FileWrite MVP 已接入 CLI/Gateway；配置 roots 后五个工具完整可用，读取正文和写入内容均不会进入 SSE/SQLite audit，详见 `projects/02-leon-agent/docs/file-search.md`
- `02-leon-agent`：普通 Agent 已接入 per-turn Planning 状态机；复杂任务可显式跟踪 2～8 步，计划正文
  只进入当前 LLM transcript，审计只保留状态元数据，详见 `projects/02-leon-agent/docs/planning.md`
- **Evaluation → RAG → Trace/Observability** 三段质量主线均已形成最小闭环：Evaluation 已扩到 50 个
  case；`03-rag-lab` 已完成真实 embedding、retrieval、citation、faithfulness 与 reranker 对照；Trace 已统一
  Core/SQLite/CLI/Web 的 trace/turn/span 与脱敏 metadata。Planning 暂停在 MVP，Multi-Agent、
  Telegram/Tavo 互通和更多 workflow 继续后置。
- **`07-coding-agent` 基础版已经收口，开发主线停止扩功能并转入面试准备**：它定位为 Vertical Agent Demo，
  已复用唯一 AgentRuntime 和共享文件边界，跑通读/搜、简单计划、显式授权写入、固定测试、失败后二次修复、
  最终 Git diff，并用 3 个 provider-free 稳定 case 覆盖 bug、小功能和 failing-test repair。
- `manual_vue_web_check.py` 已改为只结束本次测试创建的完整进程树；Windows 使用精确 PID + `/T`，
  正常和故意失败路径均验证 4173/4178 回到 0，不会全局结束 `node.exe`，`--base-url` 外部服务不归它管理。
- Web smoke 的历史列表与最近图片等待竞态已收口：测试现在等待排序后的列表真正落 DOM，并要求两张图片均完成加载且布局尺寸非零；
  没有调大总超时、降低断言或改变业务语义，Vite/FastAPI 两种入口各 **93/93**，退出后无 Preview 端口或子进程残留。
- CLI 启动和执行流统一为最大 66 列的 LEON 控制台：新会话显示双网格，恢复会话与窄屏自动降级；
  `YOU ❯`、`LEON ╱>`、`◈ THINK/TOOL`、`◆ DONE`、`◇ ERROR` 分离角色，回答分割线继续铺满终端可用宽度。
- 最终收口验证：全仓 `pytest` **635 passed**，Coding Agent 三个 demo case **3 passed**；Ruff、compileall、
  `uv lock --check`、Vue typecheck/build、`git diff --check`、UTF-8/BOM 与 secret 扫描均通过。
- 下方带日期 checkpoint 保留历史现场；其中旧“下一步”或“待提交”不覆盖本节当前主线。
- `02-leon-agent` Web 五阶段已完成：FastAPI Gateway、SSE、PWA、token 登录、任务/图库/事件时间线
- CLI、Web 与 Leon MCP 的唯一持久配置源是 `%USERPROFILE%\.leon\config.toml`；`.codex` 和
  仓库 `.env` 只参与首次 `leon-config init`，CC Switch 后续操作与 Leon 无关
- 公网入口已部署到 `https://leon.928886540.xyz`，由 Cloudflare Tunnel 转发到 `127.0.0.1:8233`
- Windows 计划任务 `Leon Agent` 与 `CF Tunnel` 已运行，并配置登录自启/异常重试
- Leon 环境只读自检：19 模式、38 节点类型、39 LoRA，通过
- 架构决策：`notes/decisions/0002-leon-agent-boundary.md`
- 新需求计划：`notes/plans/leon-agent-expansion.md`
- Web 图片体验已升级：聊天 / 任务 / 图库共用全屏相册（整组图片 / 计数 / 左右按钮 / 键盘 / 滑动切图），图片按原比例 `contain` 展示，控制按钮固定在视口；生图完成后在底部追加新气泡并自动滚到可视区
- 前端演进评估：`projects/02-leon-agent/docs/web-client-evolution.md`
- **前端栈决策（2026-08-15，用户拍板）：迁 Vue 3 + Vite**。评估文档里「暂不迁 Vue3」的结论已被推翻，后续以本条为准。
  目标结构：`web/src/{api,stores,views,components}` + `vite.config.ts`，构建产物 `dist/` 交给 FastAPI 托管。
  决策依据：JS 已 807 行 / 48 个顶层可变变量 / 65 处 DOM 查询，近期修的 4 个 bug 全是「状态与 DOM 手动同步」这一类结构性问题；
  且组件一文件一职责后，外部 agent 改动边界清晰，不必再动 1250 行单文件。
- 前端 W1 已完成：`messages[]` + `messageIndex` 单一数据源，`createMessage/renderMessage/patchMessage/removeMessage`；
  复制 / 真重试 / 编辑 / 朗读工具栏、底部图片气泡、模型候选收起均已落地
- Volink TTS 已接入：4 个模型 / 561 个中文音色；目录使用 `lang=zh-CN`，可搜「风韵少妇」，支持试听、收藏、
  手动朗读、自动朗读、loading / 波形状态，以及 iOS Chrome 的用户手势解锁与待播音频恢复；前后端会清理
  Markdown 列表横杠、emoji、链接、模式 ID、任务 ID 和计划 ID，避免 TTS 把 `-` 读成“减/简”
- 任务页现在优先显示中文生图模式、原始描述和中文状态；内部 job/plan ID 默认收进“任务详情”折叠区
- Volink `502` 已确认是偶发上游失败，不是 166 字文本过长：相同原文在本地和公网均实测过 `200 audio/mpeg`。
  网关现在记录 voice ID、原始/净化字符数和上游错误，Web 会显示 `detail`；未加入无依据的自动重试
- SSE `voice.ready` 已接入 Vue 聊天：事件会追加带音频元数据的助手气泡，复用单例播放器并遵守 iOS 用户手势解锁与待播恢复
- 可通过 `LEON_SYSTEM_PROMPT_FILE` 读取 UTF-8 TXT 并追加到 Agent system prompt；本机文件位于被 Git 忽略的
  `data/system-prompts/双人成行预设.txt`，CLI 与 Web Gateway 都已接入
- Web 修复（2026-08-16）：旧 session 聊天记录恢复渲染、任务/图库按 `created_at` 最新优先；
  编辑改为独立弹窗，完成任务显示缩略图，退出登录二次确认；站点 favicon 已改为 CLI 同款金色 `✦`，Vue SW 当前为 v21
- CLI 已升级为日用 inline TUI：上方可滚动聊天记录，底部 1～6 行动态输入框以 `YOU ❯` 标记首行、续行不缩进；助手回答使用 `LEON ╱>`；外层终端接管
  鼠标滚轮回看和拖选复制，同时支持 PageUp/PageDown 翻页与显式闪烁竖线光标；resume 会按顺序重放最近 240 条消息；Enter 发送，
  Shift+Enter 换行，并为不兼容终端保留 Ctrl+Enter / Esc+Enter。非 TTY 继续使用 Rich fallback。
- CLI 历史回答 `•` 为绿色、最新回答保持正文色，工具状态按青/绿/红/粉/黄区分；每轮结束显示唯一一条
  `Worked for` 耗时分割线，下一轮开始时旧耗时文字会被纯分割线替换，resume 不伪造耗时。
- CLI 输出区和输入区之间保留一行间隔；图片短标签改为 OSC 8 原生终端链接，不重新开启应用鼠标捕获，
  `/open` 继续作为无法点击时的后备。
- CLI 已补 `/resume`、`/retry`、`/last`、`/copy`、`/tools`、`/status`，斜杠命令支持中文说明、
  大小写不敏感补全；输入历史来自当前 SQLite session，请求期间 Enter 会把完整消息加入顺序队列。
- CLI 的 Esc/Ctrl+C 使用协作式取消：会阻止后续 LLM/tool 轮、持久化和迟到结果渲染；
  `LLM_TIMEOUT_SECONDS=0` 时响应读取不限时，取消会主动关闭当前 OpenAI/httpx 连接并在短时间内收敛。
  `generate_images` 继续用 `return_direct` 避免生图提交后多打一轮 provider 请求；交互式 CLI 提交后
  立即释放输入区、后台跟踪并自动持久化完成图片，`--once` 仍同步等待结果。
- Vue W2 基座已落地：`projects/02-leon-agent/web/` 提供 Vue 3 + Vite、API client、
  `stores/messages.ts`、ChatView 和 PWA 资产；它是唯一 Web 客户端，FastAPI 直接托管 `web/dist/`。
- Vue 迁移当前已覆盖聊天、任务、图库和设置四个视图；聊天支持 `/nsfw --model` 模式补全，
  通过 `/api/image-modes` 按名称、ID、aliases 异步过滤，支持点击、上下键、Enter、Escape 和失焦收起，
  并用输入快照/请求序号隔离迟到响应；输入框自动增高、用户上滚保留位置、回到最新按钮和错误详情折叠也已接通；
  Agent Timeline 会收集除 `assistant.delta` 外的最近 100 条 SSE 决策事件，并支持清空、关闭和会话切换重置；
  这些路径不调用 LLM、Volink 或真实 provider。
- SSE 事件现在带进程内递增 `id`，Gateway 保留最近 100 条并按 `Last-Event-ID` 补发断线期间事件；
  `session.connected` 是不推进游标的连接标记。Vue 让浏览器原生 EventSource 处理瞬时断线，
  stale token 会回到登录页，系统恢复在线时会重新连接 CLOSED 的流。
- 历史验证（2026-08-16）：显式 fake `LLM_SOURCE=env` 下 Python **188 passed**、仓库级 Ruff clean、
  `uv run leon --help` 通过；Vue `npm run typecheck` / `npm run build` 和 provider-free Playwright smoke
  也已通过，Vite/FastAPI 两种入口各 **76/76**。浏览器脚本拦截所有 `/api/**`，不代表真实公网/手机验收。
- 当前测试通过临时 `LEON_CONFIG_FILE` 使用 fake provider，不读取真实 `.leon` 或 `.codex`，也不会请求 provider。
- 元数据已补齐：Gateway 在 `assistant.completed` 返回权威 `model` / `elapsed_ms` / `usage`；provider 不返回
  usage 时前端按无值降级。
- 真实流式已补齐：LLM transport 使用 `stream=True`，Gateway 在线推送 `assistant.delta`，重连后以
  `assistant.completed` 全文恢复；delta 不占用 100 条回放窗口。
- 8/15 的 CC Switch provider 变化仅是迁移前历史。完成 `.leon` 初始化后不再跟随；provider 问题只检查
  `.leon/config.toml` 和当前进程/session pin。
- 工具面：Leon 后端 iOS 插件层共 17 个端点，agent 原先只接了 5 个。已补 `cancel_image_task`
  （`POST /ios/async_autogen/{job_id}/cancel`，会同时中断正在跑的 ComfyUI prompt），工具数 6 → 7。
  仍未暴露、可按需接入：`async_cancel_matching`（批量取消）、`image_gallery/delete`（删图，破坏性）、
  `image_tasks/hide` / `hide_history`（清理列表）、`metrics/tasks/{job_id}`（任务指标）、
  `async_autogen/recover`（恢复）、`comic_compose` / `async_comic`（漫画模式）
- 真实只读探测（2026-08-16）：本机与公网 health 均 `200`，SSE 均立即收到 `session.connected`，stale token 均返回
  `204 no-store`；仍需在 Cloudflare 控制台确认 Cache Bypass Rule，再做手机实机 SSE、生图和 TTS 闭环验收
- Web session 已把 provider identity 与 base URL（不含 API key）持久化到 SQLite；Gateway 重启后只按
  当前 `.leon` identity 重解析密钥，不匹配或旧 `ccs:*` pin 返回 409，禁止静默切换或查询 CCS。
- Vue 页面迁移主体已完成并成为唯一入口；旧单文件 Web、`LEON_WEB_CLIENT` 开关及旧浏览器脚本已删除。
  W1 的 `messages[]` 直接对应 `stores/messages.ts`，不用重写
- ASR 已接入：`POST /api/agent/asr` 代理 OpenAI-compatible 转写服务，需配置 `LEON_ASR_*`；前端录音后只回填输入框。
- `LeonToolService` 已抽出，Leon MCP Server 第一版已完成：5 个工具、stdio/Streamable HTTP，协议
  `initialize/tools/list` smoke 通过。
- 三段质量基线与 Coding Agent 基础闭环均已落地；当前停止新增模块，转入项目讲解、Agent 核心理论、
  高频问答、简历/BOSS 投递和模拟面试。Telegram Bot、Tavo MCP 互通和 Multi-Agent 仍不自动前移
- Tavo 路线：先做 Leon Agent -> Tavo MCP；Tavo -> 外部 Leon MCP 等宿主支持
- 运行态遗留：Cloudflare Cache Bypass 确认和手机实机 SSE、生图、TTS、ASR 验收；本轮已重启
  `leon-server`，授权 health 为 200，并确认实际提供最新 Vue 静态资源

### 当前推进决策（2026-08-17）

- **停止继续扩展 Planning**：现有 per-turn 顺序状态机已经足够展示 Agent runtime 设计；不做 DAG、并行、
  后台恢复、自动重试或第二套 executor。
- **Evaluation 已完成第一版**：50 个 case 默认 fake provider，显式 `--live` 才访问真实 provider；指标覆盖
  Task Success、Tool Selection、Plan Adherence、Safety、Latency、Tool Calls 和 Tokens/Cost。
- **RAG 已完成最小闭环**：`03-rag-lab` 按 chunk → embedding → retrieval → citation 演进，已用 Recall@K、
  MRR、citation precision 和 faithfulness 做 fake/真实对照，未塞入 Leon MVP。
- **Trace MVP 已接通**：AgentRuntime、SQLite、CLI/Web 共用 trace_id、turn_id 和脱敏 spans；raw 文件、
  Memory、搜索 query、tool payload 和 secret 禁止进入 Trace。
- **面试表达**：`pytest` 证明代码契约，Evaluation 证明 Agent 行为质量，Trace 解释一次真实运行。
- **Coding Agent 到此浅尝辄止**：只证明 Runtime 能落到垂直场景；不做 IDE 插件、复杂 patch、多仓库、
  自动 PR、后台任务、复杂 sandbox 或 Multi-Agent Coder/Reviewer/Tester。
- **Evaluation 第一版已落地**：`projects/02-leon-agent/evals/cases/` 共 50 个 case；
  `uv run leon-eval` 默认 fake provider，当前 baseline 为 **50/50 passed**。Runner 复用生产
  `LeonAgent`/tool schema，临时文件根、静态搜索和内存 SQLite 不触碰真实外部系统；显式 `--live` 才允许真实 provider。
- Tool Selection 已增加参数级断言：只在当前内存 transcript 中检查 source_text、数量、mode、query、root/path、
  Memory key/scope 等关键参数，结果报告不回显原始值。
- Evaluation 首次 baseline 揪出并修复了 `web_search` 审计泄露：持久投影只保留 search metadata 和来源 URL，
  不保存 query 或 snippet；相关设计见 `projects/02-leon-agent/docs/evaluation.md`。

### 本轮可中断 checkpoint（2026-08-16）

- 已完成：README/架构/协作文档对齐；ASR Gateway 禁用、成功、超限、上游失败测试；`leon-server`
  多 worker 拒绝；`LeonToolService` 抽取；`projects/04-mcp-lab/leon-mcp-server` 第一版。
- MCP 当前提供 5 个工具，stdio 与 Streamable HTTP 均已完成真实 `initialize -> tools/list`；
  `scripts/mcp_smoke.py` 默认只读，加 `--check-environment` 才调用环境自检。MCP 依赖下限固定为
  `mcp>=1.14`，规避 1.10–1.13 的 postponed-annotation 注册 bug。
- 验证：fake env 下 `uv run pytest -q` **188 passed**；`uv run ruff check .`；Vue build；Vite/FastAPI
  provider-free smoke 各 **76/76**；MCP 真实只读环境自检为 19 模式、38 节点、39 LoRA 全通过。
- 运行态：已真正重启 `leon-server`（PID 已变化），本机/公网 health `200`，公网 SSE 首事件正常，
  `CF-Cache-Status=DYNAMIC`。Cloudflare 控制台规则和真实手机触控/麦克风仍需人工确认。
- 本 checkpoint 对应的 Service/MCP 变更已独立提交；工作区还包含另一位 Codex 的 CLI/TUI 与
  Web Search 并行改动（`.env.example`、`src/leon_agent/{agent,cli,config}.py`、
  `src/leon_agent/gateway/app.py`、`src/leon_agent/tools.py` 的 search hunks、`src/leon_agent/search/`、
  `tests/{test_cli,test_search}.py`、`docs/TUI-REDESIGN-COLLABORATION.md`），不要回滚、覆盖或混入
  Service/MCP 提交。当时曾把 Telegram Bot 作为下一条主线；该安排已被 2026-08-17 的
  **Evaluation → RAG → Trace/Observability** 决策取代。

### Leon Tavily `web_search` 可中断 checkpoint（2026-08-16）

- 放置位置：`projects/02-leon-agent/src/leon_agent/search/`。`provider.py` 只负责 Tavily HTTP 适配，
  `service.py` 负责参数校验和结构化结果；工具 schema 仍在 `src/leon_agent/tools.py`，不并入图片专用的
  `LeonToolService`。CLI 与 Web Gateway 复用同一 service，不各写一套搜索逻辑。
- 当前状态：仅配置 `TAVILY_API_KEY` 时注册只读 `web_search`；支持 `basic` / `advanced`、
  `general` / `news` 和 1～10 条结果，返回标题、URL、摘要、来源和发布时间，由 LLM 综合并引用 URL。
  Key 使用 `SecretStr`，仅经 `Authorization` header 发给 Tavily，不进入 tool result、事件或请求 JSON。
- 已验证：搜索专项在 Python 3.10 / 3.13 均为 **16 passed**，Leon 项目全套为 **173 passed**，
  显式 fake LLM 环境下全仓为 **206 passed**；`uv run ruff check .` 通过。Tavily 官方 keyless
  `/search` 只读探测返回了
  `title/url/content`，未使用已泄露 Key，也未消耗用户账户 credits。
- 迁移前运行态历史（22:xx）：当时本机 Git 忽略的 `.env` 配置了 Tavily Key，计划任务 `Leon Agent`
  重启后，本机和公网 `/api/health/detail` 均为 `search_tool: ready`。该值现已由首次
  `leon-config init` 迁入 `.leon/config.toml`，仓库 `.env` 不再参与 Leon 运行。真实
  `WebSearchService` basic 搜索成功返回 1 条 `title/url/snippet`，已验证 Bearer 鉴权和结果标准化。
  该 Key 曾出现在聊天中，仍应在方便时轮换。
- 完整联调已通过：默认模型切换为 `grok-4.6` 后，`leon --once` 实际产生
  `调用工具 web_search -> web_search 完成`，最终中文回答引用 Tavily 官方文档 URL。此前
  `gpt-5.6-sol` 的 `403 channel:client_restricted` 已不再阻塞。非 UTF-8 自动化终端仍需临时设置
  `PYTHONUTF8=1`，否则当前 TUI 的 `✦` 状态字符会触发 GBK 编码错误。
- 不要做：不要把旧 Key、真实 Key 或带 Key 的 MCP URL 写进仓库/日志；不要把当前 MVP 宣称为
  `extract` / `crawl` / `map` 或 Tavily MCP 接入；不要让自动测试消耗真实 Tavily credits；不要为 CLI、
  Web、未来 Telegram 分叉 provider 实现。`extract` / `crawl` / `map` 应在搜索闭环稳定后另行设计。

### Leon File Search/FileWrite 可中断 checkpoint（2026-08-17）

- 放置位置：共享内核 `packages/workbench_core/src/workbench_core/files/`；Leon 适配层是
  `projects/02-leon-agent/src/leon_agent/file_tools.py`。`02-code-agent` 的 `workspace.py` 和旧工具
  只保留兼容导出，不要复制另一套路径策略。
- 当前状态：已实现 `list_files`、`file_search`、`read_file`，以及受控 `create_file`、`write_file`。
  只有配置 `LEON_FILE_ROOTS={"id":"绝对目录"}` 才注册读工具；写工具还要求 composition root
  注入同 canonical roots 的 server-side authorization service。结果只暴露 root id、相对路径和
  citation，不泄露绝对路径或写入内容。
- 安全/资源边界：最多 8 个 root；拒绝绝对/越界/ADS 路径；每次解析检查 containment；跳过 symlink、
  junction/reparse、隐藏/系统项、`.env*`、凭据、私钥、SQLite；支持受限 UTF-8/BOM UTF-16；单文件 1 MiB，
  搜索 2,000 文件/20 MiB/50 结果，读取 200 行/16,000 字符。长搜索在目录、条目、文件和文本行边界
  协作式检查当前 turn 的取消信号，取消异常不会被 ToolRegistry 包装为普通工具错误。实际读取使用
  `lstat/open/fstat` identity 与读后复验，校验后被替换的文件返回 `path_changed`，不读取替换正文。
- 已验证（定向）：`packages/workbench_core/tests/test_file_search.py` 与
  `projects/02-leon-agent/tests/test_leon_file_search.py` 覆盖配置、工具 schema、路径穿越、敏感文件、
  binary/编码/预算和不可信内容标记。全量与真实进程验证完成后，把结果补到本节，不要只写“代码存在”。
- 接手顺序：先跑共享内核测试，再跑 Leon 文件工具测试，最后用临时目录启动新的 `leon-server`，检查
  `/api/health/detail` 的 `file_tool` 状态和实际注册列表；不要把个人密钥目录加进 roots。当前 MVP 不含
  删除/移动/执行、PDF/DOCX、embedding/RAG 或 File Search MCP；CLI composition 仍受 TUI Owner 文件锁约束。

---

## 多 agent 协作约定（Codex / Notion AI / 本地 IDE 并行时）

现在可能有多个 agent 同时改这个仓库，遵守下面三条，否则会互相踩：

### 1. 提交前验证门槛（Vue provider-free smoke 已建立）

```powershell
uv run pytest -q                                  # 期望：全绿
uv run ruff check .                               # 期望：All checks passed
# Vue provider-free 浏览器回归（自动启动 Vite preview；不请求真实 provider）
$env:CHROME_PATH="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
uv run --with playwright python projects/02-leon-agent/tests/manual_vue_web_check.py
# 也可验证真实 FastAPI Vue 静态入口（API 仍由 Playwright fake）
uv run --with playwright python projects/02-leon-agent/tests/manual_vue_web_check.py --server fastapi --port 8250
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
当前主线：Leon 与 07-coding-agent 基础闭环已完成，停止新增功能，转入面试准备。
Evaluation → RAG → Trace/Observability 已完成最小闭环，Planning 冻结在 MVP。
模型与全部 Leon 持久配置只走 %USERPROFILE%\.leon\config.toml，不跟随 CC Switch。

请先快速确认：
1. 仓库当前进度
2. 上一步完成了什么
3. 下一步最小动作

然后直接接着干，先设计后编码。
```

如果你已经想好下一步，直接更具体：

```text
继续 ai-workbench，进入面试准备阶段。
不要再扩 Leon 或 Coding Agent 功能；先整理项目讲解、Agent 核心理论和高频问答，再做模拟面试。
```

---

## 这个仓库里谁负责“记忆”

| 文件 | 作用 |
|------|------|
| `AGENTS.md` | 和 AI 协作的固定规则 |
| `LEARNING_ROADMAP.md` | 学习主线 |
| `notes/career/transition-plan.md` | 转型 90 天计划 |
| `notes/decisions/` | 架构为什么这样设计 |
| `notes/learning/ccs-integration.md` | 共享实验项目的 CCS 接入，以及 Leon 独立 provider / 首次迁移边界 |
| `projects/02-code-agent/README.md` | 当前 Agent 项目状态 |
| 本文件 | 下次怎么续聊 |

---

## 实操建议

1. **每次告一段落**，让我更新对应 README / notes（比只靠聊天记录稳）
2. **换天继续**，优先用上面开场，不要只说“继续”
3. **非 Leon 共享实验项目的 CCS 配置**继续说名字即可：`用 CCS 的薄荷` / `用 CCS 的大黑客`；
   Leon 必须直接维护 `%USERPROFILE%\.leon\config.toml`
4. session 能续就续；**不能续也没问题**，以仓库为准

---

## 配置与自启动运行态 checkpoint（2026-08-17 01:49）

- 分支仍为 `feat/leon-model-switch`；当前工作区保留 CLI-TUI-Codex、Web Bug-Fix 和配置/File Search 的并行未提交改动，禁止 reset/checkout/清理。
- Leon 的唯一持久配置已落在 `%USERPROFILE%\\.leon\\config.toml`（本机 `C:\\Users\\Administrator\\.leon\\config.toml`，Git 外）；CLI、Gateway、MCP 都先加载它，后续不读取 CCS、`.codex` 或仓库 `.env`。
- `leon-config init` 只允许首次迁移；目标已存在时直接编辑。受管 `[leon.env]` 键由文件权威覆盖 ambient 环境，旧 `ccs:*` session pin 返回 409。
- Gateway 配置夹具已改为临时 TOML，避免测试落到真实 SQLite：`test_gateway.py` 25 passed、`test_gateway_server.py` 1 passed；全项目 Leon 测试已验证 206 passed，仅有既有 Starlette/httpx 弃用警告。
- Windows 任务已由 `scripts/windows/install-leon-autostart.ps1` 注册并运行：`Leon Agent` -> `127.0.0.1:8233`，`IDEA MCP Auth Proxy` -> `127.0.0.1:64343`；登录/开机触发、`IgnoreNew`、每分钟 5 次重试、隐藏 wrapper、日志目录均已验收。
- 本机冷启动证据：授权 `/api/health` 与 `/api/health/detail` 均返回 200；无 token 返回 401；Gateway 子进程命令为仓库 `.venv\\Scripts\\leon-server.exe`，日志在 `%USERPROFILE%\\.leon\\logs`。
- `64343` 的真实入口是 `D:\\cloudflared\\idea-mcp-auth-proxy.mjs`，固定上游 `127.0.0.1:64342` 当前没有监听；代理端口能启动，但不能宣称 IDEA MCP 后端已恢复。下一位接手者先定位/恢复 64342，再做 MCP `initialize/tools/list` 验收。
- 下一步顺序：跑完整 Python/Ruff + Vue build/smoke，检查公网 Tunnel health；按文件范围单独 stage/commit/push，随后继续 File Search 真实运行态验收。

## Leon FileWrite / 静默 Gateway 最新交接（2026-08-17）

- 已推送 commits：`8f81e96 feat(leon): 增加显式授权文件写入`、`4c267e5 fix(leon): 收口取消审计与静默网关守护`；远端与本地 `feat/leon-model-switch` 已同步。
- FileWrite 已在 Web/Gateway 实际可用：`list_files`、`file_search`、`read_file`、`create_file`、`write_file` 五项；首行 `!file create|write root:path` 授权、单轮单写、opaque root binding、SQLite/SSE audit 脱敏均已验证。真实 Gateway 临时 smoke 的 create/write/read-back 均为 200，测试目录已删除。
- `AgentCancelled.partial_result` 会先保留已完成且脱敏的 tool audit；`LLM_TIMEOUT_SECONDS=0` 表示响应读取不限时，但取消主动关闭 transport。相关测试已并入 `4c267e5`。
- Windows Gateway wrapper 位于 `scripts/windows/run-leon-autostart-service.ps1`：`pythonw.exe -m leon_agent.gateway.server`、隐藏窗口、端口占用只监测不抢占；同进程 supervisor 在子进程退出后默认 60 秒重试、20 次预算，稳定 300 秒后重置。隔离端口 18233 的 worker 崩溃/重启 smoke 已通过，PS5.1 parser 无错误。
- 当前运行态：`Leon Agent` 任务为 `Running`，wrapper 监测另一个会话占用的 8233 PID `11308`；`/api/health` 与 `/api/health/detail` 为 200；IDEA `64342=15332`、代理 `64343=7596` 未受影响。若要让最新 shared LLM/cancel 代码加载到 8233，等该 direct 进程释放后让 wrapper 接管，或由其 owner 协调重启，勿盲杀并行进程。
- CLI FileWrite composition 已完成：`_create_agent()` 从同一 `config.file_roots` 创建 read/write service，并把同一 write service 传给 direct registry 与 `LeonAgent`；无 roots 时不注册文件工具，有 roots 时五项齐全，跨轮写预算会重置。
- CLI 取消分支会持久化已完成且脱敏的 tool audit；取消回答、LLM transcript 和文件正文不进入 SQLite。Agent 返回后才收到取消的竞态也保留安全审计，不留下半条会话消息。
- 当前验证：全仓 `410 passed`、Ruff 全绿、CLI `80 passed`、FileWrite Agent/policy/adapter `18 passed`、`py_compile` 与 `git diff --check` clean。接手者先读协作板和 `git status`，源码变更后重启实际 `leon`，用 `/tools` 验证五项文件工具，再按临时根做显式 create/write smoke；不要对个人密钥目录操作。
- 版本 `7196ebb` 已推送 `feat/leon-model-switch` 并 fast-forward 到远端 `main`。真实 CLI 入口与临时根五工具 create/write/read-back smoke 均通过；Gateway 8233 health/detail 均 200；计划任务为隐藏、单实例、登录/开机可用并带 20 次每分钟重试。工作区唯一未提交项是另一条 Web emoji 微调 `projects/02-leon-agent/web/src/views/ChatView.vue`，不要覆盖或误 stage。
- 2026-08-17 Memory/File audit checkpoint：Memory Phase 2→4 已接入生产 `LeonAgent`、CLI、Gateway；同一 `LEON_SESSION_DB` 独立 `memories` 表、固定 `local-owner`、三项 memory tools、每轮 untrusted context（12 条/2400 字符/单值 512）、显式 consent 与单轮一次写入额度均已验证。raw memory/file 内容只进入当前 LLM transcript，AgentEvent/SSE/ToolStep/SQLite 只保留 metadata projection；取消不回滚已完成副作用，下一轮读取新状态。全仓 `420 passed`，Ruff、compile、diff-check 全绿；真实重启 `Leon Agent` 后 `/api/health/detail` 已返回 `memory_tool=ready`。本轮待提交范围：shared AgentRuntime、Leon Memory/Agent/tools/CLI/Gateway、File Search audit projection、对应测试与项目文档/协作板；不要 stage `README.md`、`notes/career/*`、`web/src/views/ChatView.vue` 等并行未提交改动。提交后继续做公网/手机 smoke，Memory 不接 MCP/Web 管理 API。
