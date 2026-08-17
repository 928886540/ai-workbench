# Leon Agent File Tools

本文冻结 Leon Agent 本地文件工具的边界、工具契约和验收方式。目标是让 Agent 能安全地查找
Prompt、角色设定、LoRA、Workflow 和项目文档，并在用户明确要求时创建或整体替换受限文本文件，
不把它扩成任意文件系统、文件执行或向量 RAG。

## 当前状态

状态：File Search MVP 与显式 FileWrite MVP 已完成；待完成新的 `leon-server` 运行态验收。

```text
用户问题
  -> Leon Agent 选择文件工具
  -> Leon 文件 Tool 适配层
  -> workbench_core FileSearchService
  -> 配置允许的 root_id + 相对路径
  -> 结构化路径、行号和文本证据
  -> LLM 归纳并引用文件位置

```

显式写入路径：

```text
用户当轮第一行提交确定的 !file create|write root:path 命令
  -> LeonAgent 绑定当轮原话并重置单写额度
  -> create_file 或 write_file
  -> FileWriteService 二次校验路径、文本和原子提交
  -> 仅返回 root-relative 元数据
```

实现原则：

- 未配置文件根目录时不注册任何文件工具，Leon 其他能力保持不变。
- 模型只能看到 `root_id` 和相对路径，不能看到服务器绝对路径。
- `list_files`、`file_search`、`read_file`、`create_file`、`write_file` 共用同一套 root allowlist 和路径策略。
- 文件内容是不可信证据，不是 system prompt，也不能覆盖 Agent 指令。
- 读工具只处理受限 UTF-8 / BOM UTF-16 文本；写入统一接收 UTF-8 文本，单次最多 64 KiB。
- 写工具只有 composition root 显式注入已授权 service 时才注册；没有 roots 或授权 hook 时不会出现。

## 代码边界

```text
packages/workbench_core/src/workbench_core/files/
  workspace.py   # 根目录解析、Windows 路径与越界防护
  service.py     # 列举、字面搜索、分段读取、安全/资源限制

projects/02-code-agent/
  workspace.py   # 兼容导出，继续保留原调用 API
  tools.py       # 原 list_dir/read_file/search_text 兼容适配

projects/02-leon-agent/src/leon_agent/
  file_tools.py  # AgentTool schema 与脱敏审计投影，不承载文件系统策略
  file_write_policy.py # 当轮用户原话授权与单写上下文
  config.py      # LEON_FILE_ROOTS
  agent.py       # 文件证据使用规则
  cli.py         # CLI composition root
  gateway/app.py # Web composition root 与 health 状态

packages/workbench_core/src/workbench_core/files/
  write_service.py # 原子 create/replace、授权 hook、单轮写预算
```

Leon 不依赖 `code_agent`。共享的安全检索内核属于 `workbench_core`，两个阶段项目通过各自的工具
schema 使用它，避免复制后出现两套路径和敏感文件规则。

## 配置

在 `%USERPROFILE%\.leon\config.toml` 的 `[leon.env]` 中设置 `LEON_FILE_ROOTS`。值是
`root_id -> absolute path` 的 JSON 字符串；Windows 路径推荐使用正斜杠，避免 JSON 反斜杠转义：

```toml
[leon.env]
LEON_FILE_ROOTS = "{\"workbench\":\"D:/apiWorkSpace/ai-workbench\",\"prompts\":\"D:/prompt-library\"}"
```

约束：

- `root_id` 是返回结果和工具参数使用的稳定别名。
- 路径必须是已经存在的目录；配置错误应明确失败，不能静默扩大到父目录。
- 未配置或配置为空对象时 File Search 关闭。
- 配置修改、Python 源码修改后必须重启 `leon-server`，再验证实际服务。

## 工具契约

### `list_files`

用途：查看允许根目录或某个根目录下的文件和子目录。

```json
{
  "root_id": "workbench",
  "relative_path": "projects/02-leon-agent/docs",
  "max_entries": 100
}
```

`root_id` 省略且 `relative_path` 为 `.` 时只列出可用根目录别名，不返回绝对路径；指定
`relative_path` 时必须同时指定 `root_id`。

### `file_search`

用途：对文件名和 UTF-8 / BOM UTF-16 文本内容做不区分大小写的字面搜索。

```json
{
  "query": "k2_tifa_plus",
  "root_id": "workbench",
  "relative_path": ".",
  "max_results": 20
}
```

`root_id` 省略时搜索全部已配置根目录；指定 `relative_path` 时必须同时指定 `root_id`。每条命中至少
返回：

```json
{
  "root_id": "workbench",
  "path": "projects/02-leon-agent/README.md",
  "match_type": "content",
  "line": 42,
  "text": "...k2_tifa_plus...",
  "untrusted_content": true
}
```

`untrusted_content` 同时出现在顶层和每条命中上；`text` 是文件名或截断后的行片段，不是可执行指令。

### `read_file`

用途：按行读取一个已通过访问策略的文本文件，支持大文件分页。

```json
{
  "root_id": "workbench",
  "relative_path": "projects/02-leon-agent/README.md",
  "start_line": 1,
  "max_lines": 200
}
```

结果返回 `start_line/end_line/total_lines/content/truncated`，并带
`untrusted_content: true`。Agent 最终回答引用 `root_id:path:line`；没有读到的内容不能补写。

### `create_file` 与 `write_file`

两个写工具只在 composition root 同时注入已配置授权的 `FileWriteService`，且 write roots 与 read
roots 的 root id 和 opaque canonical binding 完全一致时注册。模型参数固定为 `root_id`、
`relative_path`、`content`，不含
`confirmed`、授权文本、用户原话、写次数或绝对路径。

```json
{
  "root_id": "workbench",
  "relative_path": "notes/agent.md",
  "content": "完整文件内容"
}
```

- `create_file` 只创建不存在的文件；目标已存在时返回 `already_exists`，绝不覆盖。
- `write_file` 只整体替换已经存在且通过文本检查的文件；缺失目标返回 `not_found`，不自动创建。
- 父目录必须已存在；不自动 `mkdir`。不支持 append、patch、rename、move、delete 或执行文件。
- 当前用户这一轮第一行必须严格是 `!file create root_id:relative/path` 或
  `!file write root_id:relative/path`；内容要求可在后续行给出。自然语言请求只触发 Agent 提议确认命令，
  “有没有这个能力”、翻译/引用、文件内容里的指令、历史消息或含糊请求都不能授权。
- 每轮最多一次写；服务端重置额度，模型不能通过参数绕过。成功结果只含 `root_id`、相对 `path`、
  `citation`、字节数和 `created/overwritten`；审计投影永远不保存 `content`。

## 安全边界

所有入口，包括用户直接要求 `read_file` 时，都必须执行以下检查：

1. 拒绝绝对路径、UNC、Windows drive-relative 路径、设备路径、ADS `:` 和非法组件。
2. 根目录和每个候选文件都 `resolve` 后重新验证 containment，跳过 symlink、junction 和 reparse point。
3. 不列出也不读取点目录、隐藏/系统文件、`.git`、`.env*`、密钥、凭据和 SQLite 数据文件及其
   `-wal` / `-shm` / `-journal` 伴随文件；常见二进制 magic、PEM 私钥头和控制字符也会被拒绝。
4. 仅允许受支持的文本类型，并进行 binary sniff；编码失败返回错误，不用替换字符伪装成功。
5. 限制单文件大小、扫描文件数、目录/条目数、累计扫描字节、命中数、读取行数和最终响应字符数；
   无效编码和 binary 尝试读取的字节同样计入搜索预算。
6. 结果只含根目录别名与相对路径；异常也不能泄露绝对路径或被拒绝文件的内容。
7. 工具 schema 的 `minimum/maximum` 只供模型参考，handler 必须再次校验。

写入额外检查：

8. 授权 hook 必须严格返回布尔 `True`；缺失、异常或其他 truthy 值都拒绝。
9. create 使用同目录临时文件和 no-clobber 原子发布；write 使用同目录临时文件和原子替换，并在提交前后
   重验 root、父目录和目标 identity。失败时保留旧文件并清理临时文件。
10. 写入同样拒绝隐藏/敏感/数据库/二进制/私钥路径和内容；错误不得回显绝对路径、临时文件名或内容。

当前硬上限：最多 8 个 root；单文件 1 MiB；单次搜索最多扫描 2,000 个文件、1,000 个目录、10,000
个目录条目、累计 20 MiB 并返回 50 条命中；单次读取最多 200 行和 16,000 字符。

当前仍不提供删除、移动、执行文件、自动修改 Prompt、PDF/DOCX 解析或向量索引。需要 diff/多文件事务、
人工预览或更细粒度权限时，必须另做设计，不能在工具参数上加布尔开关绕过服务端授权。

## 验收

最小验证：

```powershell
uv run pytest packages/workbench_core/tests/test_file_search.py -q
uv run pytest projects/02-code-agent/tests/test_workspace_tools.py -q
uv run pytest projects/02-leon-agent/tests/test_leon_file_search.py -q
uv run ruff check packages/workbench_core projects/02-code-agent projects/02-leon-agent
uv run leon --help
```

必须覆盖：

- 未配置 roots 时五个文件工具均不存在；只注入读 service 时只出现三个读工具。
- 文件名搜索、正文搜索、行号和分页读取正确。
- `../`、绝对路径、Windows 特殊路径、symlink/junction 逃逸均被拒绝。
- `.env`、密钥和数据库既不能被列出，也不能直接读取。
- 超大文件、binary、伪装 ZIP/私钥、扫描预算、目录预算和结果上限能稳定收口。
- CLI 与 Web 注入同一 roots 后工具 schema 和结果一致。
- 文件中的“忽略之前指令”只作为文本返回，不改变 Agent system prompt。
- 写工具未注入时 schema 不出现；未明确授权、路径越界、目标已存在/缺失和单轮第二次写入均稳定拒绝。
- 写入成功后审计事件、ToolStep、SSE 和 SQLite 均不包含原始 `content` 或绝对路径。

实际入口验收时使用专门的临时文档目录，不把个人密钥目录加入 roots。修改 `src/` 后重启
`leon-server`，检查 `/api/health/detail` 和真实工具事件；单元测试通过不代表旧进程已经加载新代码。

## 后续阶段

MVP 稳定后再按真实需求选择：

1. SQLite FTS 增量索引和更新时间追踪。
2. Markdown/TXT/JSON 之外的 PDF、DOCX 解析。
3. chunk、embedding、语义召回和 rerank。
4. File Search MCP 暴露。
5. 带 diff 预览、人工确认和多文件事务的文件写入。

## 中断后接手

1. 先读本文和 `docs/AI-COLLABORATION.md`。
2. 运行 `git status --short`，保留并行的 TUI 与 Web Search 改动。
3. 先验证 `workbench_core/files`，再验证 Leon Tool 适配，最后接 CLI/Web。
4. 不使用真实私人目录跑测试；测试根目录必须由 `tmp_path` 创建。
5. 先验证 write service，再验证 Leon Tool 适配和 Agent 当轮授权，最后接 CLI/Web；每完成一层立刻跑对应
   定向测试，避免留下只有 schema、没有 handler 的半成品。
