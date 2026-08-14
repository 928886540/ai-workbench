# CC Switch 接入

## 结论

- CC Switch **没有**现成的“导出配置 MCP”
- 真实配置在本地 SQLite：`%USERPROFILE%\.cc-switch\cc-switch.db`
- `ai-workbench` 直接读这个库，按 **provider 名字** 取 `base_url / api_key / model`

## 用法

```bash
# 查看 codex 供应商
uv run python -m workbench_core.ccs_cli list

# 查看某个名字（支持模糊）
uv run python -m workbench_core.ccs_cli show 薄荷
uv run python -m workbench_core.ccs_cli show 大黑客

# .env
LLM_SOURCE=ccs
CCS_APP=codex
CCS_PROVIDER=薄荷
```

对 Codex 说：

> 用 CCS 的「薄荷」
> 用 CCS 的「大黑客」
> 用 CCS 当前配置

即可，不必再手填 key。
