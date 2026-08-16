# Leon 配置

Leon CLI 和 Web Gateway 共用同一套进程配置。用户级配置文件位于：

```text
%USERPROFILE%\.leon\config.toml
```

Python 通过 `Path.home()` 解析这个路径，所以计划任务、终端和以后改用其他账户时都会使用
当前账户的 profile；不要把用户名硬编码进代码。也可以用进程环境变量
`LEON_CONFIG_FILE` 临时指定另一份配置文件（它不能写在 `[leon.env]` 里）。

## 初始化

在仓库根目录运行：

```powershell
uv run leon-config init
```

初始化器会把当前 `%USERPROFILE%\.codex\config.toml` 复制到 `.leon\config.toml`，然后追加
`[leon.env]`，把仓库 `.env` 中当前支持的 Leon、LLM、Tavily 和 Volink 配置迁移进去。迁移的是
本机值，不会把 `.env` 或新文件写入仓库。

这是一次性迁移。目标文件已经存在时，`leon-config init` 会停止，之后直接编辑
`.leon\config.toml`；命令没有 `--force`、同步或覆盖入口。源文件已经包含 `[leon.env]` 时也会
停止，避免生成重复 TOML table。

初始化命令不会打印任何 API key。生成文件本身包含 Codex provider token、`LEON_API_TOKEN`、
`TAVILY_API_KEY` 和 `VOLINK_API_KEY` 等本机密钥，只应留在当前用户 profile 下。Windows 默认会
继承 profile 的 ACL；多人共用机器时应检查并收紧 `%USERPROFILE%\.leon` 的访问权限，不能把它
复制到 Git、截图或日志中。

## 唯一配置源

```text
%USERPROFILE%\.leon\config.toml
  -> 顶层 provider / model
  -> [leon.env] 中的 Leon、Tavily、Volink 和运行参数
  -> 文件未声明的项目使用代码默认值
```

`[leon.env]` 会在 CLI、Gateway 或 Leon MCP 创建设置对象前注入，并覆盖同名进程环境；文件没有
声明的受管环境键会从进程中移除，因此仓库 `.env` 和启动器残留值不会偷偷补入运行配置。持久配置
只来自 `.leon`，`LEON_CONFIG_FILE` 仅用于在启动前选择另一份绝对路径文件。CLI 的显式命令行参数
仍可以覆盖本次运行的后端 URL、图片 URL、插件目录或数据库路径。

配置文件只允许 Leon 当前支持的环境键和 `CODEX_CONFIG_PATH`，不会接受 `PATH`、
`PYTHONPATH` 等进程路径键或任意未知键。缺少文件或有效 provider 时，入口会直接报错退出，不会
回退到 `.codex`、CCS 数据库或仓库 `.env`。

## Provider 快照语义

`.codex` 只参与第一次迁移：

```text
CC Switch -> %USERPROFILE%\.codex\config.toml --(init)--> %USERPROFILE%\.leon\config.toml
```

迁移完成后，Leon 强制使用 `LLM_SOURCE=toml`，并把 `CODEX_CONFIG_PATH` 固定到当前
`.leon\config.toml`。之后切换、编辑或删除 CC Switch / `.codex` 配置都不会影响 Leon；也不存在
`LLM_SOURCE=ccs/env` 的 Leon 运行兼容入口。要换 provider，直接修改 `.leon\config.toml` 的
`model_provider`、`model` 和对应 `[model_providers.<name>]`。

旧 SQLite 会话若还保存 `ccs:*` provider pin，Gateway 会返回 `409` 并要求创建新会话，不会为了
恢复旧会话而查询 CCS。

## 验证与排错

不输出配置内容，只检查解析和入口：

```powershell
uv run python -m leon_agent.config_file --help
uv run leon --help
uv run leon-server --help
```

修改 `.leon\config.toml` 或 Python 源码后必须重启 CLI/Gateway；Gateway 是进程级读取，
不会在运行中热加载密钥。健康检查只报告功能是否配置，不返回密钥：

```powershell
curl.exe -H "Authorization: Bearer <token>" http://127.0.0.1:8233/api/health/detail
```

如果计划任务身份发生变化，`Path.home()` 也会变化，先确认新账户的 `.leon\config.toml` 是否
存在，再重新运行 `leon-config init`。不要为了迁移而把个人密钥目录加入 File Search 白名单。
