# CC Switch 接入

## 结论

- CC Switch 切换 Codex provider 后会更新 `%USERPROFILE%\.codex\config.toml`
- Leon Agent 默认直接读取该文件中的当前 `model_provider`、顶层 `model`，以及对应 provider
  的 `base_url / experimental_bearer_token`
- 旧的 `%USERPROFILE%\.cc-switch\cc-switch.db` 读取方式仍作为 `LLM_SOURCE=ccs` 兼容入口

## 用法

```bash
# 查看 codex 供应商
uv run python -m workbench_core.ccs_cli list

# 查看某个名字（支持模糊）
uv run python -m workbench_core.ccs_cli show 薄荷
uv run python -m workbench_core.ccs_cli show 大黑客

# 默认：跟随 CC Switch 当前写入的 Codex 配置
LLM_SOURCE=toml

# 可选：指定其他 Codex 配置路径
CODEX_CONFIG_PATH=C:\Users\Administrator\.codex\config.toml
```

Leon Agent 重启时重新读取配置。会话内可用 `/model <任意model ID>` 只覆盖 model，
provider 的 URL 与 token 继续来自当前 `config.toml`。
