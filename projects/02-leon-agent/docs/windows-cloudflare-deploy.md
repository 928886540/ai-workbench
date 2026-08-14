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

在仓库根 `.env` 中设置强随机 token，不能提交到 Git：

```dotenv
LEON_API_TOKEN=<a-long-random-secret>
LEON_PUBLIC_IMAGE_BASE_URL=https://comfyui.928886540.xyz
```

本地调试：

```powershell
uv sync
uv run leon-server --host 127.0.0.1 --port 8233
```

服务启动后访问 `https://leon.928886540.xyz`，首次打开输入 `LEON_API_TOKEN`。SSE 使用同一个
token 的 query 参数，因为浏览器原生 `EventSource` 不能添加 Authorization header；不要把该 URL
复制到不受信任的日志或截图中。

## 登录后自启

使用仓库脚本创建当前 Windows 用户的计划任务：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\install-leon-agent-task.ps1
```

任务会在登录时启动，并在异常退出后按 1 分钟间隔自动重试 3 次。用以下命令管理：

```powershell
Get-ScheduledTask -TaskName 'Leon Agent'
Start-ScheduledTask -TaskName 'Leon Agent'
Stop-ScheduledTask -TaskName 'Leon Agent'
Unregister-ScheduledTask -TaskName 'Leon Agent' -Confirm:$false
```
