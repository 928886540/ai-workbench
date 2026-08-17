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

## Gateway 自启 / IDEA MCP 按需启动

安装器为当前 Windows 用户注册 `Leon Agent` 任务。它使用隐藏 PowerShell wrapper、
单实例策略 `IgnoreNew` 和 `StartWhenAvailable`（错过触发后会在任务计划可用时补启动）。隐藏
wrapper 自身会监督 Gateway 子进程：子进程意外结束后默认每分钟重启一次，最多重试 20 次；即使
`pythonw` shim 报告退出码 `0`，wrapper 也会继续重试。任务计划的 `RestartOnFailure` 同时保留为
wrapper 崩溃时的外层兜底。wrapper、stdout 和 stderr 都写到
`%USERPROFILE%\.leon\logs`：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\install-leon-autostart.ps1
```

`Leon Agent` 在登录或开机后由隐藏 wrapper 运行仓库
`.venv\Scripts\pythonw.exe -m leon_agent.gateway.server`，通过 `%USERPROFILE%\.leon\config.toml` 启动
`127.0.0.1:8233`。`pythonw.exe` 和隐藏窗口参数让 Gateway 静默驻留后台，不创建控制台窗口或
任务栏按钮；旧的
`install-leon-agent-task.ps1` 仅保留作历史兼容，不要再用它覆盖新任务。

如果 8233 已被另一个进程占用，wrapper 不会强杀或抢占；它会保持隐藏任务运行并监测端口，原进程
释放后再启动受管 Gateway。这样手动恢复或升级期间不会产生第二个服务树。

如果这次只需要安装或更新 Gateway 任务，不希望停止当前 `64343` 代理，也不希望改写 IDEA
快捷方式，可以显式跳过 IDEA 代理生命周期处理：

```powershell
.\scripts\windows\install-leon-autostart.ps1 -SkipIdeaProxyLifecycle
```

该开关会保留当前代理 listener 和 IDEA 快捷方式状态；安装器仍会注册 `Leon Agent`，并清理可能
残留的旧 `IDEA MCP Auth Proxy` 计划任务。

IDEA MCP 代理没有计划任务，不会登录或开机自启。安装器会把桌面、开始菜单和任务栏中的
IDEA 快捷方式改为隐藏 launcher，并保留相邻的 `.leon-original` 备份。从这些快捷方式
打开 IDEA 时，launcher 直接启动代理；最后一个 IDEA 进程退出后，launcher 只在端口 owner、
Node 可执行文件和代理脚本命令行全部匹配时停止该代理。

代理运行真实存在的
`D:\sfotwore\nodejs\node.exe D:\cloudflared\idea-mcp-auth-proxy.mjs`，监听
`127.0.0.1:64343`。它只负责校验 Bearer token，并把请求转发到 IDEA 2026.1 内置 MCP
监听的 `127.0.0.1:64342`。不要把 Cloudflare Tunnel 改为直连未鉴权的 `64342`。

端口职责固定如下：`64342/stream` 是 IDEA 自己提供的 Streamable HTTP MCP，只有本机
IDEA 进程存活时才存在，未提供公网鉴权；`64343/stream` 是本机 Node Bearer 代理，
也是 Cloudflare `idea.928886540.xyz` 的唯一上游。`64343` 不是需要单独维护的 IDEA
服务，也不应注册为登录/开机自启任务；它只随改造后的 IDEA 快捷方式启动，并在最后一个
IDEA 进程退出后由 launcher 停止。

直接双击 `idea64.exe`、新建未改造的快捷方式或通过其他 launcher 启动 IDEA，会绕过代理
launcher。需要完整 MCP 公网访问时，使用安装器更新过的桌面、开始菜单或任务栏入口。

检查任务与日志：

```powershell
Get-ScheduledTask -TaskName 'Leon Agent'
Get-ScheduledTaskInfo -TaskName 'Leon Agent'
Get-ChildItem "$env:USERPROFILE\.leon\logs"
Start-ScheduledTask -TaskName 'Leon Agent'
Stop-ScheduledTask -TaskName 'Leon Agent'
```

恢复原始 IDEA 快捷方式：

```powershell
.\scripts\windows\configure-idea-mcp-shortcuts.ps1 -Restore
```
