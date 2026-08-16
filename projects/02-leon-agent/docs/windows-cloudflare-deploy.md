# Leon Agent Windows + Cloudflare Tunnel

当前机器的公网入口由 Cloudflare Tunnel 提供，HTTPS 在 Cloudflare 边缘终止。因此不需要
Nginx、certbot 或 systemd；Gateway 只监听 `127.0.0.1:8233`，避免绕过 token 直接暴露给局域网。

## 域名路由

在 `D:\cloudflared\config.yml` 的 `ingress` 列表最后一条兜底规则之前加入：

```yaml
  - hostname: leon.928886540.xyz
    service: http://127.0.0.1:8233
    originRequest:
      disableChunkedEncoding: true
```

然后验证并重启现有 Tunnel 计划任务：

```powershell
& 'D:\cloudflared\cloudflared.exe' tunnel --config 'D:\cloudflared\config.yml' ingress validate
Stop-ScheduledTask -TaskName 'CF Tunnel'
Start-ScheduledTask -TaskName 'CF Tunnel'
```

在 Cloudflare Dashboard 添加一条 Cache Rule：

```text
When incoming requests match: leon.928886540.xyz/api/agent/*/events
Then: Cache eligibility = Bypass cache
```

这条规则确保 SSE 心跳和任务事件不被 CDN 缓存。`disableChunkedEncoding` 用于避免 Tunnel
对持续响应做分块编码处理。

## Gateway 配置

在 `%USERPROFILE%\.leon\config.toml` 的 `[leon.env]` 中设置强随机 token，不能提交到 Git：

```toml
[leon.env]
LEON_API_TOKEN = "<a-long-random-secret>"
LEON_PUBLIC_IMAGE_BASE_URL = "https://comfyui.928886540.xyz"
```

本地调试：

```powershell
uv sync
uv run leon-server --host 127.0.0.1 --port 8233
```

服务启动后访问 `https://leon.928886540.xyz`，首次打开输入 `LEON_API_TOKEN`。SSE 使用同一个
token 的 query 参数，因为浏览器原生 `EventSource` 不能添加 Authorization header；不要把该 URL
复制到不受信任的日志或截图中。

## 登录 / 开机自启

使用新的安装器为当前 Windows 用户注册两个独立任务。它们使用隐藏 PowerShell wrapper，
登录和开机都触发，单实例策略为 `IgnoreNew`，启动前检查端口，失败后每分钟重试 5 次，
并把 wrapper、stdout 和 stderr 写到 `%USERPROFILE%\.leon\logs`：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\install-leon-autostart.ps1
```

`Leon Agent` 运行仓库 `.venv\Scripts\leon-server.exe`，通过
`%USERPROFILE%\.leon\config.toml` 启动 `127.0.0.1:8233`；旧的
`install-leon-agent-task.ps1` 仅保留作历史兼容，不要再用它覆盖新任务。

`IDEA MCP Auth Proxy` 运行真实存在的
`D:\sfotwore\nodejs\node.exe D:\cloudflared\idea-mcp-auth-proxy.mjs`，监听
`127.0.0.1:64343`。这个脚本只是 Bearer 认证代理，会把请求转发到固定的
`127.0.0.1:64342`；只有 64342 的 IDEA MCP 后端也在运行时，64343 才能提供完整 MCP 能力。
安装器不会伪造或猜测 64342 的入口，代理上游不可用时日志会记录连接错误。

检查任务与日志：

```powershell
Get-ScheduledTask -TaskName 'Leon Agent','IDEA MCP Auth Proxy'
Get-ScheduledTaskInfo -TaskName 'Leon Agent'
Get-ScheduledTaskInfo -TaskName 'IDEA MCP Auth Proxy'
Get-ChildItem "$env:USERPROFILE\.leon\logs"
Start-ScheduledTask -TaskName 'Leon Agent'
Start-ScheduledTask -TaskName 'IDEA MCP Auth Proxy'
Stop-ScheduledTask -TaskName 'Leon Agent'
Stop-ScheduledTask -TaskName 'IDEA MCP Auth Proxy'
```
