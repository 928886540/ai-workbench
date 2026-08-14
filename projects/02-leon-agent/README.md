# 02-leon-agent

独立运行的个人 Agent：终端聊天，必要时调用现有 Leon / ComfyUI 生图能力。

![Leon Agent 总体架构](../../docs/assets/leon-agent-architecture.png)

## 当前能力

- `leon` 交互式 CLI / REPL
- 普通问题直接聊天，不调用工具
- 5 个 Leon 生图工具：模式、环境、自助生成、任务、图库
- SQLite 持久化会话、消息、tool call、`generationPlanId` 和 `jobId`
- 复用 `leon-image` 的 `executor-core.js + executor-assets.js`
- 调用现有 `/ios/*` HTTP 接口，不复制 Prompt、Workflow 或 LoRA 配置
- 任务与图库返回的图片地址统一补全成绝对 URL，可直接打开

## 运行边界

```text
leon Agent（独立进程）
  -> Agent Runtime 和工具列表（ai-workbench）
  -> Node bridge（只做请求转换）
  -> leon-image 现有执行资产
  -> Leon / ComfyUI 后端
```

原 Tavo 插件不依赖 Agent，也不需要为了第一版增加 Agent 代码。未来可把这些工具迁移成
Leon MCP Server，让 CLI、Codex 或其他 Host 共用。

完整图见 [Leon Agent 架构](../../docs/leon-agent-architecture.md)。

## 快速开始

在仓库根目录运行：

```powershell
uv sync
uv run leon
```

恢复已有会话：

```powershell
leon resume 6b34ef29606447d395f05899ba30abf7
```

恢复后会继续使用该 session 在 SQLite 中保存的消息历史。旧写法
`leon --session <session_id>` 继续兼容。

交互命令：

- `/new`：创建新会话
- `/history`：查看本地会话
- `/exit`：退出

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

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `LEON_BACKEND_URL` | `http://192.168.8.100:8188` | Leon / ComfyUI 后端 |
| `LEON_PUBLIC_IMAGE_BASE_URL` | 空（回退到 `LEON_BACKEND_URL`） | 生成图片链接时使用的对外地址，例如 `https://comfyui.928886540.xyz` |
| `LEON_PLUGIN_DIR` | 自动发现同级 `ComfyUI-aki` | 原插件目录 |
| `LEON_DEFAULT_IMAGE_MODES` | `k2_tifa` | 未指定模式时的默认值，逗号分隔 |
| `LEON_SESSION_DB` | `data/leon-agent.db` | 本地会话数据库 |

后端返回的图片可能是 `/view?filename=...` 这类相对路径。工具层会把它拼成
`LEON_PUBLIC_IMAGE_BASE_URL`（未配置时用 `LEON_BACKEND_URL`）下的绝对地址，已经是
`http(s)://` 的地址保持原样。也可以临时用 CLI 覆盖：

```powershell
uv run leon --public-image-base-url https://comfyui.928886540.xyz
```

模型配置继续走仓库现有 CC Switch / `LLM_SOURCE`，与生图后端配置分离。

## 验证

```powershell
uv run pytest projects/02-leon-agent/tests -q
uv run ruff check projects/02-leon-agent
uv run leon --help
```

单元测试不会提交真实生图。环境自检也是只读；只有模型明确调用 `generate_images` 才会创建任务。

## Mobile Web Client

> 规划中

为 Leon Agent 提供手机竖屏远程 Web Client（PWA），让用户在手机浏览器中与 Agent 对话、
实时查看 Tool Call 过程和生图任务状态。

详见 [docs/mobile-web-architecture.md](docs/mobile-web-architecture.md)。

## 后续路线

已记录三条扩展需求：面试 MCP、Telegram Bot、Tavo 互通。详细边界与实施顺序见
[Leon Agent 扩展路线](../../notes/plans/leon-agent-expansion.md)。

当前优先级是 MCP Server -> Telegram Bot -> Leon Agent 连接 Tavo MCP。Tavo v1.0.0 的聊天
工具调用暂不能接入外部 MCP Server，等宿主能力开放后再做 Tavo -> Leon MCP。
