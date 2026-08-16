# Leon MCP Server

复用 `02-leon-agent` 的 `LeonToolService`，通过 MCP 暴露现有 Leon / ComfyUI 能力；不复制
Prompt、Workflow 或 LoRA。

## 工具

- `list_image_modes`：只读模式目录
- `check_image_environment`：只读环境自检
- `generate_images`：提交且只提交 1 张图片，明确标记为有副作用、非幂等
- `get_image_tasks`：查询当前 MCP session 的任务
- `get_recent_images`：查询当前 MCP session 的完成图片

`generate_images` 原样透传 `source_text`，立即返回 `generation_plan_id` / `job_id`，不在 MCP
请求中同步等待图片完成。

## 运行

在仓库根目录：

```powershell
uv sync
uv run leon-mcp --help
uv run leon-mcp --transport stdio
uv run leon-mcp --transport streamable-http --host 127.0.0.1 --port 8240
uv run python projects/04-mcp-lab/leon-mcp-server/scripts/mcp_smoke.py
```

Streamable HTTP 默认只监听本机，MCP endpoint 为 `http://127.0.0.1:8240/mcp`。公网暴露前需
单独设计鉴权，不应直接绑定 `0.0.0.0`。

配置沿用 Leon Agent 的 `LEON_BACKEND_URL`、`LEON_PUBLIC_IMAGE_BASE_URL`、
`LEON_PLUGIN_DIR`、`LEON_DEFAULT_IMAGE_MODES` 和超时配置。可用 `LEON_MCP_SESSION_ID`
固定任务/图库 scope，默认值为 `default`。

面试演示可加 `--check-environment` 展示只读自检；脚本默认不调用 `generate_images`：

```powershell
uv run python projects/04-mcp-lab/leon-mcp-server/scripts/mcp_smoke.py --check-environment
uv run python projects/04-mcp-lab/leon-mcp-server/scripts/mcp_smoke.py --transport http
```

## 安全验证

单元测试只使用 fake service；MCP 协议 smoke 只调用 `tools/list` 和两个只读工具。除非明确调用
`generate_images`，不会创建真实生图任务。
