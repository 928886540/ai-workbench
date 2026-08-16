# Leon Provider 独立配置

## 结论

- Leon CLI、Web Gateway 和 Leon MCP 只读取 `%USERPROFILE%\.leon\config.toml`。
- `%USERPROFILE%\.codex\config.toml` 只在首次执行 `leon-config init` 时作为复制源。
- 仓库 `.env` 同样只在首次初始化时迁移受支持的 `[leon.env]` 值。
- 初始化完成后，CC Switch 的切换、删除或配置变化都不会影响 Leon。
- 缠留在 SQLite 中的旧 `ccs:*` provider pin 会返回 `409`，不会查询 CC Switch DB。

## 首次迁移

```powershell
uv run leon-config init
```

生成：

```text
C:\Users\Administrator\.leon\config.toml
```

命令只允许创建不存在的目标文件，没有 `--force` 或同步模式。文件已存在后，直接维护其中的：

```toml
model_provider = "provider-name"
model = "model-id"

[model_providers.provider-name]
base_url = "https://example.com/v1"
experimental_bearer_token = "..."

[leon.env]
LLM_SOURCE = "toml"
```

运行时会强制 `LLM_SOURCE=toml` 和 `CODEX_CONFIG_PATH=<当前 .leon 文件>`。缺少文件、provider、
base URL 或 token 都会直接启动失败，不回退到 `.codex`、CCS DB、仓库 `.env` 或手工 LLM env。

## 修改生效

`.leon` 是进程启动快照。修改后退出并重新启动 `leon`，或重启 `Leon Agent` 计划任务；已有
Gateway session 仍保持进程内 provider pin，建议重新登录创建新 session。

真实配置含 provider、Leon Web、Tavily 和 Volink 密钥，只保存在用户 profile；不要提交、打印、
截图或加入 File Search 白名单。
