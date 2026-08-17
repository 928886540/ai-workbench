# 02-leon-agent

独立运行的个人 Agent：终端聊天，必要时调用现有 Leon / ComfyUI 生图能力。

## 当前能力

- `leon` 日用 inline TUI：无可见滚动条的聊天记录、1～6 行动态输入框、运行中消息队列、后台生图通知、SQLite 输入历史和 Rich 非 TTY fallback
- CLI 新会话使用最大 66 列的 LEON 双网格启动界面，恢复会话和窄屏自动降级；`YOU ❯`、`LEON ╱>` 与执行状态前缀明确区分用户、助手和工具流
- `leon-server`：FastAPI Gateway + SSE + 手机 PWA Web Client
- 普通问题直接聊天，不调用工具
- 明确生图请求优先路由工具，用户原话作为 `source_text` 透传，不由 Agent 扩写 Prompt
- 7 个 Leon 生图工具：模式、环境、自助生成、任务、取消任务、会话图库、全局最近图片（数量可调）
- SQLite 持久化会话、消息、tool call、`generationPlanId` 和 `jobId`
- 复用 `leon-image` 的 `executor-core.js + executor-assets.js`
- 调用现有 `/ios/*` HTTP 接口，不复制 Prompt、Workflow 或 LoRA 配置
- 任务与图库返回的图片地址统一补全成绝对 URL，可直接打开
- CLI 与 Web 设置页从当前 LLM provider 的 `/models` 动态读取模型目录，手输模型 ID 保持原始大小写
- 聊天与图库共用全屏相册：整组图片、计数、左右按钮、键盘和移动端滑动切图，图片保持原比例不裁切
- 生图完成后丢弃骨架屏，在聊天底部追加一条新图片气泡并自动跟随到底
- 任务页以生图模式和中文状态为主信息，完成任务直接显示可点击缩略图，不暴露内部任务 ID
- 模型选择改为可点击列表（不再依赖手机浏览器不友好的 `<datalist>`）
- Volink TTS 已接入：4 个模型 / 561 个中文音色，支持搜索、收藏、试听、手动朗读与自动朗读；自动朗读
  会按 `assistant.delta` 分段并发合成、顺序播放，重试同一气泡时按新 revision 重新生成且保持所选音色
- TTS 会在前后端清理 Markdown 列表符号、emoji、链接、模式 ID 和任务 ID，换行转成自然停顿
- 最近图片查询会直接渲染工具返回的结构化图片；LLM 附带的“查看图片”链接不会重复显示或进入自动朗读
- SSE 的 `voice.ready` 事件会在聊天中追加可播放的语音气泡；播放器支持单例复用、iOS 用户手势解锁和待播恢复
- 助手气泡支持复制、重试、编辑和朗读；编辑后的文字会成为后续朗读内容
- iOS 有声播放使用单例播放器 + 用户手势解锁，被拦截时保留待播音频并显示开启按钮
- Vue 3 + Vite 迁移已覆盖聊天、任务、图库和设置视图；聊天输入支持 `/nsfw --model` 模式名称补全，按名称、ID 和别名过滤
- Web 右上角提供独立历史会话面板：置顶优先、最近更新倒序；切换会话会同步恢复聊天、图库与生图任务，不重复刷新模型和音色目录
- Agent Timeline 可查看最近 100 条 SSE 决策事件，并过滤高频 `assistant.delta` 字符增量
- SSE 事件带进程内 `id`；Gateway 按 `Last-Event-ID` 补发最近 100 条断线事件，连接标记不推进游标
- LLM 回复使用真实流式输出；最终事件携带实际模型、耗时和 provider 可用时的 token usage
- Web session 会把 provider identity 与 base URL 持久化到 SQLite，Gateway 重启后不会静默切换 provider
- ASR 语音输入已接入 OpenAI-compatible `/audio/transcriptions`，录音转写后只回填输入框，不自动发送
- 可选 Tavily `web_search` 已接入 CLI 与 Web：只在后端配置 Key 后注册，并返回可引用的结构化实时搜索结果
- 可选 File Tools 已接入 CLI 与 Gateway/Web：通过配置的目录白名单查找、读取文件；用户用严格首行命令明确授权当前回合时，还可新建或整体替换受限文本文件
- 显式长期 Memory 已接入 CLI 与 Gateway/Web：只保存用户明确要求记住的偏好/默认值，跨 session
  共享、支持硬删除，并以有预算的 untrusted context 注入后续 turn
- per-turn Planning 状态机已接入普通 Agent：复杂请求可创建 2～8 步计划并按服务端规则推进；简单聊天、
  `/nsfw` 直达生图和 Leon MCP 不注册 Planning，计划正文不进入 SSE/SQLite 审计
- Agent Evaluation 第一版已接入：50 个可审查 case，默认 fake provider，显式 `--live` 才使用真实 provider；
  指标覆盖 Task Success、Tool Selection、Plan Adherence、Safety、Latency、Tool Calls 和 Tokens/Cost
- 本地 Trace / Observability MVP 已接入：CLI/Web 统一 `trace_id + turn_id`，SQLite 仅保存脱敏 metadata；
  root、iteration、LLM、Tool、Planning spans 共用唯一 `AgentRuntime`，CLI 可用 `/trace` 查看最近一次 span 树
- 可用 `LEON_SYSTEM_PROMPT_FILE` 从项目私有 TXT 追加 system prompt，CLI 与 Web 共用

Codex、Notion AI 或其他 Agent 开发前先读
[AI 协作状态](docs/AI-COLLABORATION.md)，其中记录唯一源码路径、事件协议、模型选择契约和交接格式。
联网搜索的目录边界、工具契约和中断接手步骤见 [联网搜索](docs/web-search.md)。
本地文件检索的目录白名单、安全边界和工具契约见 [File Search](docs/file-search.md)。
复杂任务规划的状态机、审计边界和非目标见 [Planning](docs/planning.md)。

## 运行边界

```text
leon Agent（独立进程）
  -> Agent Runtime 和工具列表（ai-workbench）
  -> Node bridge（只做请求转换）
  -> leon-image 现有执行资产
  -> Leon / ComfyUI 后端
```

原 Tavo 插件不依赖 Agent，也不需要为了第一版增加 Agent 代码。现已通过独立
Leon MCP Server 让 CLI、Codex 或其他 Host 共用同一套 `LeonToolService`。

完整图见 [Leon Agent 架构](../../docs/leon-agent-architecture.md)。

## 快速开始

在仓库根目录运行：

```powershell
uv sync
uv run leon-config init
uv run leon
```

启动手机 Web Client：

```powershell
uv sync
npm --prefix projects/02-leon-agent/web ci
npm --prefix projects/02-leon-agent/web run build
uv run leon-server --host 127.0.0.1 --port 8233
```

本机浏览器打开 `http://127.0.0.1:8233`。通过 Cloudflare Tunnel 暴露时，使用
`https://leon.928886540.xyz`，并在登录页输入用户配置中的 `LEON_API_TOKEN`。Windows 部署与
Tunnel 缓存规则见 [Windows + Cloudflare 部署](docs/windows-cloudflare-deploy.md)。

恢复已有会话：

```powershell
leon resume 6b34ef29606447d395f05899ba30abf7
```

恢复后会按顺序重放该 session 最近 240 条 SQLite 消息，并继续在同一会话中保存；`/retry`、
`/last` 和 `/copy` 仍只指向最新一轮。旧写法
`leon --session <session_id>` 继续兼容。

交互命令：

- `/new`：创建新会话
- `/history`：查看本地会话
- `/resume <会话ID或历史序号>`：在当前 TUI 内切换已有会话
- `/retry`：重试上一条请求，并追加一轮会话记录
- `/last` / `/copy`：重新显示或复制上一条回答
- `/tools` / `/status`：查看已注册工具或当前运行状态
- `/trace`：只读查看当前 session 最近一次本地 Trace 摘要与 span 树
- `/model`：显示当前模型与可选模型
- `/model <序号或任意模型ID>`：切换当前 session 的模型
- `/model default`：恢复用户配置快照中的默认模型
- `/clear`：清空当前终端滚动区
- `/nsfw <描述>`：绕过 LLM，使用默认的玛莉卡模式直接生图
- `/nsfw --model <中文名或模式ID> <描述>`：指定生图模式，例如
  `/nsfw --model 蒂法增强 生成一张雨夜人像`
- `/nsfw` 或 `/nsfw --models`：列出当前安装模式的中文名和真实 ID
- `/exit`：退出

全屏 TUI 中 Enter 发送，Shift+Enter 换行；终端不支持修饰键时可用 Ctrl+Enter 或 Esc+Enter。
输入框首行使用 `YOU ❯` 提示符，续行、运行状态和底栏不做额外缩进；助手回答使用 `LEON ╱>`，避免用户与系统共用同一身份。光标使用显式显隐节拍保证 Windows Terminal 下闪烁。CLI 使用 inline 模式，
鼠标滚轮滚动外层会话记录，拖选归宿主终端处理，选中后可用 Ctrl+Shift+C 复制；
聊天记录也可用 PageUp/PageDown 翻页。图片使用不依赖应用鼠标捕获的 OSC 8 原生超链接，`/open` 是后备；
输出区与输入区保留一行呼吸空间。历史回答标记为绿色，最新回答保持正文色；
工具运行/成功/失败/取消/警告分别使用青/绿/红/粉/黄。每轮完成后显示唯一一条 `Worked for` 耗时分割线，
下一轮发送时旧耗时会退化为纯分割线，resume 历史只渲染纯分割线。
Esc/Ctrl+C 会协作式取消当前轮并丢弃迟到结果，Ctrl+D/Q 退出。LLM 响应默认不设读取时限，
长推理会一直等待到 provider 完成；取消时会主动关闭当前连接，不会继续后续 LLM/tool 轮，
也不会持久化或渲染迟到结果。当前轮运行时再次按 Enter 会把消息加入队列，当前轮收敛后按顺序自动发送。
交互式 CLI 的生图工具只等待任务提交成功，随后释放输入区并在后台跟踪；图片完成后自动追加可点击链接并
持久化到原 session。`leon --once` 没有常驻界面，仍同步等待图片结果，避免进程退出后丢失通知。

启动时只读取 `%USERPROFILE%\.leon\config.toml` 中复制的 provider，使用其中的
`base_url`、`experimental_bearer_token` 和顶层 `model`。`/model` 接受列表序号或任意新
model ID，只替换当前会话请求中的 model；URL 与密钥仍跟随这份配置快照。选择会写入当前
SQLite session，之后执行 `leon resume <session_id>` 仍会恢复该模型。没有有效用户配置时直接
报错退出，不读取 CC Switch 或 `%USERPROFILE%\.codex\config.toml`。

单次调用：

```powershell
uv run leon --once "解释一下 Agent loop"
uv run leon --once "用 k2_tifa 给我生成一张雨夜街头图片"
```

要在任意目录直接输入 `leon`，安装本地命令：

```powershell
uv tool install --editable .\projects\02-leon-agent `
  --with-editable .\packages\workbench_core
```

## 配置

推荐先运行 `uv run leon-config init`，让 CLI 和 Web 共用 `%USERPROFILE%\.leon\config.toml`。
首次初始化会复制当前 Codex TOML provider，并把本地 `.env` 中受支持的值迁入 `[leon.env]`
（包括 `TAVILY_API_KEY`、`VOLINK_API_KEY` 和 `LEON_API_TOKEN`）。迁移后该文件是唯一持久配置
源，会覆盖同名进程环境；仓库 `.env` 和 CC Switch 后续变化均不参与运行。详细初始化、ACL 和
provider 隔离语义见 [配置说明](docs/configuration.md)。真实密钥只应存在于用户文件，绝不提交 Git。

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `LEON_BACKEND_URL` | `http://192.168.8.100:8188` | Leon / ComfyUI 后端 |
| `LEON_PUBLIC_IMAGE_BASE_URL` | 空（回退到 `LEON_BACKEND_URL`） | 生成图片链接时使用的对外地址，例如 `https://comfyui.928886540.xyz` |
| `LEON_PLUGIN_DIR` | 自动发现同级 `ComfyUI-aki` | 原插件目录 |
| `LEON_DEFAULT_IMAGE_MODES` | `k2_tifa_plus` | 未指定模式时的默认值，逗号分隔 |
| `LEON_SESSION_DB` | `data/leon-agent.db` | 本地会话数据库 |
| `LLM_TIMEOUT_SECONDS` | `0` | LLM 响应读取时限；`0` 表示不限时，连接建立仍有短超时 |
| `LEON_API_TOKEN` | 空 | Web Gateway 鉴权 token；公网暴露时必须设置 |
| `LEON_SYSTEM_PROMPT_FILE` | 空 | 可选 UTF-8 TXT；内容原样追加到 Agent system prompt，相对路径从仓库根目录解析 |
| `LEON_FILE_ROOTS` | `{}` | 可选文件根目录 JSON 对象；读取和显式授权写入均限制在这些目录，例如 `{"workbench":"C:/workspace/ai-workbench"}` |
| `LEON_HTTP_TIMEOUT_SECONDS` | `30` | Leon HTTP 请求超时秒数 |
| `LEON_BRIDGE_TIMEOUT_SECONDS` | `20` | Node bridge 执行超时秒数 |
| `VOLINK_API_KEY` | 空 | Volink TTS 密钥；未配置时 Web 隐藏语音目录与朗读能力 |
| `VOLINK_BASE_URL` | `https://api.volink.org/v1` | Volink API 地址 |
| `VOLINK_DEFAULT_VOICE_ID` | `689334e84d3396ad1d28ee9e` | 默认 TTS 音色 ID |
| `LEON_VOICE_CLIP_TTL_SECONDS` | `3600` | Agent 语音气泡的进程内缓存时长 |
| `LEON_VOICE_CLIP_MAX_COUNT` | `200` | Agent 语音气泡的最大进程内缓存数量 |
| `LEON_ASR_BASE_URL` | 空 | OpenAI-compatible ASR 服务地址；需与 token 同时配置 |
| `LEON_ASR_TOKEN` | 空 | ASR 服务密钥，仅保留在 Gateway |
| `LEON_ASR_MODEL` | `whisper-1` | `/audio/transcriptions` 使用的模型 ID |
| `LEON_ASR_MAX_BYTES` | `15728640` | 单次 ASR 音频最大字节数 |
| `TAVILY_API_KEY` | 空 | Tavily 密钥；非空时启用 `web_search`，只保留在后端 |
| `TAVILY_BASE_URL` | `https://api.tavily.com` | Tavily API 根地址 |
| `TAVILY_FALLBACK_API_KEY` | 空 | 主站失败后使用的备用 Tavily-compatible Bearer Token |
| `TAVILY_FALLBACK_BASE_URL` | 空 | 备用 Tavily-compatible API 根地址；Leon 会自动追加 `/search` |
| `TAVILY_TIMEOUT_SECONDS` | `15` | 单次搜索请求超时秒数 |
| `TAVILY_MAX_RESULTS` | `5` | 搜索默认结果数，允许 `1..10` |

后端返回的图片可能是 `/view?filename=...` 这类相对路径。工具层会把它拼成
`LEON_PUBLIC_IMAGE_BASE_URL`（未配置时用 `LEON_BACKEND_URL`）下的绝对地址，已经是
`http(s)://` 的地址保持原样。也可以临时用 CLI 覆盖：

```powershell
uv run leon --public-image-base-url https://comfyui.928886540.xyz
```

模型配置只读取 `%USERPROFILE%\.leon\config.toml`。可用进程环境变量 `LEON_CONFIG_FILE`
在启动前选择另一份绝对路径文件；不存在 `.codex` / CCS / repo `.env` 回退。更换 provider 时直接
编辑 `.leon\config.toml`，然后重启 CLI/Gateway。

### 可选联网搜索

在用户配置的 `[leon.env]` 中保存 Key（首次 `leon-config init` 会从本地 `.env` 迁移）。不要提交
真实密钥：

```toml
[leon.env]
TAVILY_API_KEY = "<new-tavily-api-key>"
TAVILY_FALLBACK_API_KEY = "<fallback-sk-proxy-token>"
TAVILY_FALLBACK_BASE_URL = "https://search.604020.xyz/tavily"
```

重启 CLI 或 `leon-server` 后，`/tools` 应出现 `web_search`。没有 Key 时工具不会注册，Leon
其余能力保持可用。当前只实现 Tavily Search；`extract`、`crawl`、`map` 和搜索 MCP 暴露尚未实现，
完整契约和备用站地址见 [联网搜索](docs/web-search.md)。Leon 的 Base URL 不要带末尾 `/search`，
provider 会在请求时自动追加。

### 可选本地文件检索

File Search 默认关闭。需要让 Leon 查阅项目文档、Prompt 或角色设定时，在用户配置的
`[leon.env]` 中配置一个或多个绝对目录。值是 JSON 对象，`root_id` 是模型看到的稳定别名：

```toml
[leon.env]
LEON_FILE_ROOTS = "{\"workbench\":\"C:/workspace/ai-workbench\",\"prompts\":\"C:/workspace/prompts\"}"
```

启用后，`/tools` 至少会出现三个读取工具：`list_files`（列目录）、`file_search`（按文件名或正文做
不区分大小写的字面搜索）和 `read_file`（按行读取）。工具只返回根别名、相对路径和行号，隐藏
绝对路径；`.env`、密钥、数据库、隐藏目录、链接和不支持的二进制文件会被跳过。若 composition
注入了同 roots 的授权 write service，还会出现 `create_file`（只新建）和 `write_file`（整体替换）。
写工具只接受当轮第一行的确定授权命令：`!file create root_id:relative/path` 或
`!file write root_id:relative/path`，内容要求可写在后续行。普通自然语言只会让 Agent 提议这条确认命令，
不会直接写；每轮最多一次，不支持删除、追加、patch、移动、建目录或执行。没有配置 roots 时所有文件工具
都不会注册，聊天、生图和联网搜索不受影响。

修改 `LEON_FILE_ROOTS` 或 Python 源码后必须重启 `leon` / `leon-server`；不要把个人密钥目录加入
allowlist。文件内容始终是不可信证据，不能授权写入或改变 system prompt；当前仍没有 PDF/DOCX 解析或向量索引。
完整参数、限制和中断后接手步骤见 [File Search](docs/file-search.md)。

## 验证

```powershell
uv run pytest projects/02-leon-agent/tests/test_search.py -q
uv run pytest packages/workbench_core/tests/test_file_search.py projects/02-leon-agent/tests/test_leon_file_search.py -q
npm --prefix projects/02-leon-agent/web run build
uv run pytest projects/02-leon-agent/tests -q
uv run ruff check projects/02-leon-agent
uv run leon --help
uv run leon-server --help
```

单元测试不会提交真实生图。环境自检也是只读；只有模型明确调用 `generate_images` 才会创建任务。

## Mobile Web Client

Vue Web 客户端的 5 个阶段均已完成：HTTP Gateway、SSE 事件流、手机 PWA 聊天、任务/图库视图，以及运行时间线。
Web Gateway 提交生图任务后立即通过 SSE 推送任务模式和内部 job id，并在后台跟踪状态和完成图片；交互式 CLI
采用同样的“提交即释放输入区”语义，并在终端后台跟踪结果。图片完成后 Gateway 会主动持久化并推送一条带图片的助手消息，Web
聊天气泡直接显示图片，不需要再次询问 LLM。Web 设置页可为当前 session 选择模型，或恢复用户 `.leon`
配置快照中的默认模型；选择会与 CLI 共用同一份 SQLite 会话状态。
右上角的历史会话与 Agent Timeline 是两个独立入口；会话标题取首条用户消息，置顶状态持久化到 SQLite。
点击历史项会重连该 session 的 SSE，并恢复对应消息、语音气泡、图库和生图任务；模型与音色目录保持客户端缓存。

任务页默认显示中文模式名、用户原始描述和中文状态；完成任务显示可点击缩略图，内部
`job_id` / `generation_plan_id` 不在界面暴露。TTS 的 `502` 代表 Volink 上游失败，不是本项目的文本长度限制；网关会记录
原始字符数、净化后字符数和上游错误，Web 会显示返回的 `detail`，当前不做无依据的自动重试。

Vue 客户端已迁移聊天、任务、图库和设置四个主要视图，并复用同一套 Gateway/SSE 协议。
聊天输入在识别 `/nsfw --model ` 前缀后按模式名称、真实 ID 和 aliases 提供异步候选，支持点击、
上下键、Enter 选择和 Esc/失焦收起；用户上滚时会保留阅读位置并显示“回到最新”，输入框按内容自动增高，
错误原文默认折叠；Agent Timeline 收集最近 100 条 SSE 决策事件，支持清空并跳过高频 `assistant.delta`；
候选目录请求只走 `/api/image-modes`，不会触发 LLM 或真实 provider。

架构与接口边界见 [Mobile Web 架构](docs/mobile-web-architecture.md)。

Vue 客户端是唯一 Web 实现，源码位于 `web/src/`，构建产物位于 `web/dist/` 并由 FastAPI 托管。
每次前端改动需先运行 Vite build；影响缓存契约时同步递增 `web/public/sw.js` 的缓存名与
`web/src/main.ts` 注册版本，否则手机可能命中旧缓存。迁移历史与验收状态见
[Web 客户端演进评估](docs/web-client-evolution.md)。

Vue 3 + Vite 迁移基座位于 `web/`，当前已覆盖聊天、任务、图库、设置视图和 Agent Timeline。provider-free
Playwright 回归脚本 `tests/manual_vue_web_check.py` 已在 Vite preview 和 FastAPI Vue 入口各跑通 **93/93**；
它拦截所有 `/api/**`，不会触发真实 LLM、Volink 或图片 provider。本机与公网 Gateway/SSE 已完成只读验收；
Cloudflare 控制台 Cache Bypass 规则和手机实机验收仍待做。旧单文件 Web 客户端及其浏览器脚本已删除，
Gateway 不再提供前端选择开关。

## 后续路线

Web 前端的 Vue3 迁移边界、模式补全、Agent Timeline、TTS/`voice.ready` 事件和浏览器回归见
[Web 客户端演进评估](docs/web-client-evolution.md)。

已记录三条扩展需求：面试 MCP、Telegram Bot、Tavo 互通。详细边界与实施顺序见
[Leon Agent 扩展路线](../../notes/plans/leon-agent-expansion.md)。

Evaluation -> `03-rag-lab` -> Trace/Observability 质量主线和 `07-coding-agent` 基础闭环均已完成。
Leon 与 Coding Agent 到此停止扩功能，转入项目讲解、核心理论、简历投递和模拟面试；Telegram Bot、
Tavo MCP 互通和 Multi-Agent 继续后置。Evaluation 的运行方式和 case 契约见 [Evaluation](docs/evaluation.md)。
