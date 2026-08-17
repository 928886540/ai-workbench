# Leon Agent Memory MVP

状态：Phase 1～4 已接入生产 `LeonAgent`、CLI 与 Gateway；Memory 不暴露给 MCP，也没有 Web
专用管理 API。

## 当前实现

- `MemoryStore` 与 `SessionStore` 复用 `LEON_SESSION_DB`，但只维护独立 `memories` 表；当前单用户
  principal 固定为 `local-owner`，不把会轮换的 Gateway token 当身份。
- 正常 Agent 注册 `memory_get`、`memory_upsert`、`memory_delete`；CLI 的 `/nsfw` 与 Gateway 直达
  生图不注册 Memory，避免绕过 per-turn gate。
- 每轮从 SQLite 重新构造最多 12 条、2,400 字符的独立 untrusted system context；user 记录优先，
  同 key 覆盖 global，单条 value 自动注入最多 512 字符，超长值只提示按 key 调 `memory_get`。
- 只有当前用户原话明确包含“记住/保存/以后默认”或“忘掉/删除”时才允许写；每轮第一次写入
  尝试即消费额度，第二次稳定返回 `write_limit_reached`，下一轮重置。
- raw value 只进入当前 LLM transcript 与动态 context；`AgentEvent`、SSE、`ToolStep` 和 SQLite
  `tool_calls` 只保存 metadata projection。删除硬删主记录，不会抹掉用户自己发送过的聊天文本。
- Memory 写入与文件写入一样是已完成的副作用：若工具完成后用户取消本轮，写入不会回滚，但只
  持久化脱敏 audit；upsert/delete 后的动态 context 从下一轮重新读取并生效。

## 目标

Memory 要解决的是“跨 session 的少量、可解释、可删除的长期偏好”，不是把聊天记录换个
名字，也不是先上向量数据库。第一版应能稳定完成这条闭环：

```text
用户明确说“记住/保存”
  -> Agent 调用 memory_upsert
  -> 本地校验、拒绝高风险秘密、写入独立 SQLite 表
  -> 后续 turn 注入有预算的记忆上下文
  -> 用户说“忘掉”时调用 memory_delete，主记录硬删除
```

默认规则：普通聊天不会偷偷抽取记忆；文件、网页、图片任务和模型自己的推测都不能
自动成为长期记忆。

## 设计基线（实现前审计）

实现前先固定现有边界，避免把 Memory 做成隐式重构：

| 边界 | 当前行为 | 对 Memory 的影响 |
|---|---|---|
| `SessionStore` | `session.py:30-74` 建立 `sessions`、`messages`、`message_revisions`、`tool_calls`、`image_jobs` | 这些表仍代表短期会话/执行审计，不承载长期记忆 |
| 对话历史 | `load_messages()`（`session.py:267`）只返回指定 session 的 user/assistant 消息 | 记忆必须独立查询，再按 turn 注入；不能伪装成历史消息 |
| CLI | `cli.py:1316-1351` 创建一个 `SessionStore` 和一个 `LeonAgent`；每轮在 `cli.py:1698-1718` 读取历史、运行 Agent、再持久化 | CLI 的 Agent 生命周期跨多轮，Memory 上下文不能只在构造函数生成 |
| Gateway | `gateway/app.py:97-106` 按进程初始化 store；`send_message()`（`app.py:769`）按请求创建 Agent，并以 token 做鉴权 | 当前 token 不是稳定用户身份；Memory 需要显式的 principal 抽象 |
| 工具循环 | `AgentRuntime`（`workbench_core/agent/runtime.py:250-293`）把工具参数/结果发到事件、`ToolStep` 和下一次 LLM 请求 | Memory raw 值若直接复用，会泄露到 SSE 回放和 `tool_calls`；必须先有审计投影 |
| 工具注册 | `leon_agent/tools.py:48-67` 创建进程内 `ToolRegistry`，搜索、文件、TTS 以可选 service 注册 | Memory 工具应是独立 service + schema 适配，不塞进图片 `LeonToolService` |

## 术语与作用域

- `user`：同一用户的所有 session、CLI 和 Web 请求共享。当前 Leon 是单用户产品，先使用稳定的
  逻辑 principal `local-owner`；不要把会轮换的 API token 当作 user id。
- `global`：Leon 实例级共享记录，未来多用户时对所有 principal 可见。当前只在用户明确说“全局”或
  “所有会话都记住”时允许写入。
- 同一个 key 同时存在时，`user` 覆盖 `global`。删除 user 记录后，global 记录会重新成为有效值；
  工具结果必须明确告知是否存在 fallback。
- `effective` 只用于读取：先合并 global，再覆盖同 key 的 user；写入和删除必须指定 `user` 或
  `global`，禁止让模型用模糊的“当前”作用域改数据。

当前 Gateway 只有一个 token 校验（`gateway/app.py:140-154`），没有多账户身份协议。因此本 MVP
不假装提供真正的多租户隔离：principal 由 CLI/Gateway composition root 注入，未来接入账户系统时
再把认证主体映射为 `owner_id`，不改记忆表的 key 结构。

## 存储方案

新增独立 `MemoryStore`（建议路径 `src/leon_agent/memory/store.py`），复用 `LEON_SESSION_DB`
指向的 SQLite 文件，但不把 CRUD 塞进 `SessionStore`。初始化采用幂等 `CREATE TABLE IF NOT EXISTS`
和索引，不回填旧聊天、不做隐式摘要：

```sql
CREATE TABLE IF NOT EXISTS memories (
    scope TEXT NOT NULL CHECK (scope IN ('user', 'global')),
    owner_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (scope, owner_id, key)
);

CREATE INDEX IF NOT EXISTS idx_memories_owner_updated
    ON memories (owner_id, scope, updated_at DESC);
```

约束：

- `global` 统一使用 `owner_id='*'`，`user` 使用 composition root 注入的稳定 principal。
- `key` 先 `casefold()`，只接受分层的小写 ASCII 标识（例如 `image.preference`、
  `profile.language`），长度最多 80；不接受路径、换行或控制字符。
- `value_json` 是规范化 JSON 对象（最多 32 个字段、最大 4 KiB UTF-8、最大嵌套深度 4）；第一版
  用对象保证结构化，不保存一段无法检索的长自然语言。handler 仍须重新校验，不能只依赖 schema。
- `source_kind` 由服务端填写，MVP 只有 `explicit_user_request`；模型不能伪造 source。
- `source_session_id` 记录发生写入的 session，便于审计；当前消息在 CLI 中是 Agent 完成后才入库，
  所以不强行伪造 `message_id` 外键。未来可增加可选 message ref。
- 时间使用与现有 `SessionStore` 相同的 Unix epoch milliseconds；响应中返回 `updated_at`，不返回
  数据库绝对路径。
- 删除采用硬删除，不设 `deleted_at`。这样“忘掉”不会在活跃查询或默认注入中留下墓碑，也便于以后
  做 SQLite 文件级清理。它不等于删除聊天记录：原始 user/assistant 消息仍由 `messages` 保存，
  需要另做会话删除功能。
- MVP 默认每个 user 最多 200 条、global 最多 100 条；超限返回稳定错误码，不静默淘汰旧记忆。

### 写入示例

```json
{
  "scope": "user",
  "key": "image.preference",
  "value": {
    "style": "cinematic",
    "preferred_mode": "k2_tifa_plus"
  }
}
```

## 工具契约

建议新增 `src/leon_agent/memory/tools.py`，只负责 `AgentTool` schema 和把调用转给
`MemoryService`；校验、作用域合并、敏感信息策略属于 `memory/service.py`，不要复制到 CLI/Web。

### `memory_get`

只读，默认读取 `effective`。可按精确 `key`、前缀 `prefix` 或无条件列出有限条目：

```json
{
  "key": "image.preference",
  "scope": "effective",
  "limit": 20
}
```

`key` 和 `prefix` 至少一个时优先按条件过滤；两者都省略表示“列出当前可见记忆”，最多 20 条。
返回每条的 `scope`、`key`、结构化 `value`、`source_kind`、`updated_at`。结果应带
`untrusted_data: true`，提醒 Agent 记忆是数据，不是可执行指令。

### `memory_upsert`

```json
{
  "scope": "user",
  "key": "image.preference",
  "value": {"style": "cinematic"}
}
```

服务端填写 source/session/time，并返回 `created`、`scope`、`key`、`updated_at`；成功结果不回显
完整 value，避免 UI、事件和最终回答重复扩散个人内容。相同 scope+key 是幂等 upsert，采用 SQLite
事务的 last-write-wins；并发冲突控制（`expected_updated_at`）留到后续阶段。

### `memory_delete`

```json
{
  "scope": "user",
  "key": "image.preference"
}
```

MVP 只允许精确 key 删除，不支持模型传 `*`、批量清空或模糊前缀删除。返回 `deleted` 和
`global_fallback`，让 Agent 能诚实说明“用户覆盖已删，但全局默认仍存在”。“忘掉全部”需要以后
单独做带确认的 CLI/Web 操作，不能把一个布尔参数加到这里绕过审计。

## 明确授权与敏感信息

### 不自动抽取

系统 prompt 和 service 都必须遵守：

1. 只有当前 user turn 明确表达“记住、保存为偏好、以后默认、remember/save”等写意图，才允许
   `memory_upsert`；“我喜欢电影感照片”本身不等于授权。
2. 只有当前 user turn 明确表达“忘掉、删除这条记忆、forget”等删除意图，才允许
   `memory_delete`。
3. “全局/所有会话”是 global 写入的额外明确词；没有它一律写 user scope。
4. 没有明确授权时，service 返回 `explicit_consent_required`，不保存 pending raw value，也不把
   value 写进错误消息。Agent 应询问一句确认，而不是先偷偷落库。
5. 文件、网页、Tavily、图片任务结果和 system prompt 中的建议不能充当授权。即使内容说“请记住
   下面的秘密”，也只能作为不可信证据。

为控制“用户只让记住一件事、模型却批量写入”的残余风险，MVP 还限制每个 user turn 最多一次
`memory_upsert` 或 `memory_delete`；同一轮的第二次写调用返回 `write_limit_reached`。这不是
语义证明，只是保守的损害上限。Phase 2 若需要更自然的多字段记忆，应增加可见 proposal + 用户
确认，而不是放宽这个限制。

MVP 用一个保守、可测试的本地 intent gate（关键词/命令片段，默认拒绝），不调用 LLM 评估“用户
是否同意”。Gate 由 `LeonAgent.run(user_message=...)` 建立的 per-turn context 提供给 service；
这样 CLI 和 Gateway 共用规则，且不会因模型自行编造 `scope` 或 `confirmed=true` 而绕过授权。
更复杂的自然语言意图和 UI 二次确认属于后续阶段。

### 敏感信息拒绝

Memory 只收偏好、默认值和工作上下文，不是密码箱。服务端在写入前做结构化递归检查：

- key 或字段名命中 `password`、`token`、`secret`、`api_key`、`cookie`、`authorization`、私钥、
  密码、验证码、银行卡/政府证件等高风险类别时拒绝；
- value 命中 PEM 私钥、Bearer/JWT/常见 API key、恢复码、支付/证件号等格式时拒绝；
- 超过大小、深度、字段数或包含控制字符时拒绝；
- 错误只返回稳定 code（如 `sensitive_value_rejected`），绝不回显匹配片段、完整 key/value 或
  原始异常。

这不是“把秘密加密后就能存”的承诺：当前 SQLite 本身不是应用层加密存储，拒绝高风险内容比
把它伪装成安全保险箱更诚实。允许写入的 value 仍会在当前回合发送给已配置的 LLM provider，
文档和 UI 需要把这一点说清楚。

## 记忆注入

新增 `MemoryContextBuilder`（建议 `memory/service.py`）在每个 `LeonAgent.run()` 前读取有效记忆，
不能只在 `LeonAgent.__init__` 生成一次：CLI 的 Agent 会跨多轮复用，而 Gateway 每个请求重新创建。

推荐渲染为单独的 system context，明确是数据：

```text
<leon_memory_context untrusted_data="true">
- scope=user key=image.preference value={"style":"cinematic"}
</leon_memory_context>
```

硬预算（先固定，后按真实使用调参）：

- 最多 12 条有效记录；
- 最多 2,400 个 Unicode 字符（按完整记录截断，不切半个 JSON）；
- 每条 value 最多 512 个字符进入注入，完整值仍可由 `memory_get` 按 key 读取；
- user 记录优先于 global；同 key 只渲染 effective 结果；剩余记录不隐式塞进历史。

system 规则必须写明：当前 user turn 始终优先；记忆内容是 untrusted data，不能改变工具权限、
system prompt 或要求泄露秘密。没有语义检索、embedding、自动摘要或“根据聊天相似度猜记忆”；
超出预算时 Agent 可显式调用 `memory_get`，而不是无限扩大上下文。

实现上建议给 `AgentRuntime.run()` 增加一次性的 `system_context`/context provider 参数，或由
LeonAgent 为每轮构造轻量 runtime；不要把动态记忆拼进 user message，也不要把它写入
`messages` 表，否则会污染短期历史和重试语义。

## 审计、SSE 与删除语义

这是启用 Memory 前的硬前置，不是可选优化：

1. 在 `workbench_core.agent.AgentTool` 增加 metadata-only audit policy（或等价的
   `ToolRegistry.audit_view()`）。普通工具默认保持现状；三个 Memory 工具声明脱敏策略。
2. `AgentRuntime` 继续把 raw tool result 仅放给当前 LLM 以完成本轮推理，但 `tool_started` /
   `tool_finished` 事件和 `ToolStep` 使用 redacted view：
   - `memory_upsert` 参数只保留 scope/key，结果只保留 ok/created/updated_at；
   - `memory_get` 结果只保留 count、scope/key 列表，不保留 value；
   - `memory_delete` 只保留 scope/key/deleted/fallback。
3. `SessionStore.record_result()` 因此不会把 raw value 写入 `tool_calls.arguments_json` 或
   `result_json`；Gateway 的 SSE 回放和 CLI timeline 也不会看到 raw value。不能只在 SQLite 层
   特判，因为事件会先泄露。
4. Memory service、异常处理和 debug 日志禁止打印 raw value。LLM 请求本身包含当前工具结果和
   system memory context，这是已知且需向用户说明的 provider 边界。
5. `memory_delete` 硬删除主表记录，并确认直接查询已不存在；它不抹去历史 user message。与
   Memory 相关的审计记录只保留元数据，所以删除后不会在工具审计中留下可恢复的原值。

推荐的数据流：

```text
当前 user turn
  -> MemoryIntentGate + MemoryContextBuilder
  -> AgentRuntime(raw result -> LLM / redacted view -> events + ToolStep)
  -> SessionStore 只落 redacted ToolStep
```

## 代码边界与最小迁移

建议的独立文件，不在本设计阶段创建生产代码：

```text
projects/02-leon-agent/src/leon_agent/memory/
  __init__.py       # 导出稳定的 service/model
  store.py          # SQLite schema、CRUD、scope merge、分页/上限
  service.py        # key/value 校验、敏感拒绝、intent gate、context builder
  tools.py          # memory_get/upsert/delete 的 AgentTool schema
```

接入点只允许以下几处：

- `agent.py`：接收 `MemoryService`/principal，按 turn 注入 context，把写入授权绑定当前 user text；
- `tools.py`：注册 Memory tools，保持图片 `LeonToolService` 不变；
- `cli.py` 和 `gateway/app.py`：在各自 composition root 创建同一个 DB-backed MemoryService，传入
  相同 principal 规则；
- `workbench_core/agent/{tools,runtime}.py`：提供通用审计投影和动态 system context 的最小扩展；
- `session.py`：只消费已经 redacted 的 `ToolStep`，不读取 Memory raw 数据。

不做：把 Memory 暴露给 Leon MCP Server、从旧消息回填、向量数据库、自动总结、文件写入、
“清空全部”模型工具、多用户账户系统、加密存储承诺。这些会显著扩大权限和迁移面。

### 数据库迁移策略

1. 先在独立 `MemoryStore` 中幂等创建表/索引；旧 `leon-agent.db` 的现有表和消息零修改。
2. 新工具未注册前，只有空表，旧版本可继续启动；失败时可安全移除 Memory 注册而不影响会话。
3. 不做消息扫描或模型摘要，因此升级不会突然产生大量不可审计记忆。
4. 首次启用后由显式用户操作逐条产生记录；删除/降级不需要回滚旧消息。

## 分阶段实现

### Phase 0：契约冻结（已完成）

- 评审本文件的 scope、principal、敏感拒绝和删除边界。
- 选定是否把 `local-owner` 写入 `.leon/config.toml`；默认建议先由 composition root 固定，避免
  为一个单用户 MVP 扩大配置 allowlist。

### Phase 1：Store 与纯策略（已完成）

- 实现 `MemoryStore`、规范化 key/value、scope merge、硬删除和容量限制。
- 实现敏感扫描与 `MemoryIntentGate`，只做纯函数/SQLite 单元测试。
- 不注册工具、不改变 Agent loop。

### Phase 2：工具与审计投影（已完成）

- 添加三个 `AgentTool`，所有 handler 再校验参数。
- 扩展 `ToolRegistry`/`AgentRuntime` 的 redacted audit view。
- 先用 fake LLM 验证 raw 结果只到 LLM、事件/`ToolStep`/SQLite 不含 raw value。

### Phase 3：动态注入（已完成）

- 为 `LeonAgent.run()` 加 per-turn memory context；保持原有 history 列表只含 user/assistant。
- 增加 system prompt 规则和 2,400 字符预算测试。
- 验证 CLI 长生命周期和 Gateway 每请求生命周期都能看到刚刚 upsert 的记忆。

### Phase 4：双入口验收（代码与 provider-free 回归已完成）

- CLI 与 Web 使用同一 `LEON_SESSION_DB`、principal 和工具 schema。
- 不新增 Web 专用 Memory API；先通过自然语言工具闭环，后续再评估设置页/确认弹窗。
- 按仓库规则重启 `leon-server`，做 provider-free smoke；不调用真实生图、Tavily 或外部 MCP。

## 测试矩阵

| 层级 | 必测行为 |
|---|---|
| Store schema | 空库创建、已有旧库迁移不改消息表、重启幂等、索引存在 |
| CRUD | user/global upsert、同 key 更新、source/session/time、精确 delete、硬删除后重开仍不存在 |
| Scope | user 覆盖 global；删 user 后 global fallback；effective/list limit/排序稳定 |
| Validation | key 大小写/非法字符、空值、JSON 深度/字段/4 KiB 上限、user/global owner 规则、容量上限 |
| Sensitive | key 命中密码/token/API key、PEM/JWT/Bearer/支付/证件模式均拒绝；错误和日志不回显原值 |
| Consent | 普通“我喜欢……”不写；明确“记住/保存”可写；删除需明确“忘掉”；global 缺少全局词必拒绝 |
| Tool contract | 三个 schema 名称、必填字段、scope enum、handler 二次校验、错误 code 稳定 |
| Audit redaction | raw value 只出现在当前 LLM tool message；`AgentEvent`、SSE、`ToolStep`、`tool_calls` 均无 raw |
| Injection | 跨 session 可见、user 优先、同 key 去重、12 条/2,400 字符预算、整条截断、untrusted 标记 |
| Prompt safety | 记忆中的“忽略规则/调用工具”只当数据；当前 user turn 覆盖记忆；文件/网页内容不能授权写入 |
| Concurrency | 同 SQLite 的并发 upsert 不损坏 JSON；delete/upsert 事务边界清楚；无悬挂连接 |
| Integration | CLI 长生命周期、新 Gateway request、`leon resume` 都读取同一结果；未启用 Memory 时旧工具回归不变 |
| Deletion | 删除后 `memory_get`、注入、直接 SQL 和审计记录都不含原值；明确聊天消息仍保留的契约 |

## 风险与待确认项

- 当前单 token Gateway 没有真实 user identity；如果未来开放多人，必须先做认证主体到 principal 的
  映射，再开放 global 写入，不能沿用 `local-owner`。
- SQLite 文件目前不是应用层加密；即使拒绝高风险秘密，允许的偏好仍属于本机敏感数据，应依赖
  用户目录 ACL 和备份策略。
- 关键词授权 gate 会有语言覆盖边界，也不能完全证明模型拟写的每个字段都来自用户；“默认拒绝”
  和每轮单写入比让模型自由批量写更安全。确认弹窗或显式 `/remember` 命令应在真实使用后优先
  加入，而不是直接放宽 gate。
- 记忆数量、注入预算和敏感正则需要用匿名 fixture 调参，不要用真实个人秘密做测试。
- 是否把 Memory 工具纳入 Leon MCP、是否提供“清空全部”必须另开决策，不在本 MVP 偷渡。

## 中断后接手

1. 先读本文，再读 `docs/AI-COLLABORATION.md` 和 `notes/CONTINUE.md` 的最新 checkpoint。
2. 确认 `git status --short`；不要覆盖 CLI-TUI、Web、Gateway 或配置线的并行改动。
3. 若开始实现，严格按 Phase 1 -> 2 -> 3 -> 4 小步推进，每完成一层先跑对应矩阵。
4. 首先证明“普通聊天不写、显式写入可见、删除不留 raw 审计”，再考虑检索/摘要扩展。
5. 任何生产代码改动后必须重启 owning process，并验证实际服务加载；文档设计本身不需要重启。

## Phase 1 实现补充

Phase 1 的纯 Store/Policy 实现已按以下细节收口，后续实现应以代码和本节为准：

- `MemoryStore` 每个连接设置 `PRAGMA busy_timeout=5000`，写事务使用短 `BEGIN IMMEDIATE` 并做有限
  locked/busy 重试；不启用 WAL，也不更新 `sessions.updated_at`。
- 除 `idx_memories_owner_updated` 外增加 `idx_memories_owner_key`，用于固定 principal 的精确 key/前缀
  读取；前缀中的 `_`、`%` 按字面处理，不能被 SQLite `LIKE` 当作通配符。
- `delete_with_fallback()` 在同一个写事务内完成 user 删除和 global fallback 检查，避免单独查询期间的
  可见性竞态；Phase 2 应复用该结果，不自行拆成两次写/读。
- Store 层拒绝 C0/C1/DEL 控制字符、非法 JSON/深度/字段/4 KiB 超限；Policy 层另外拒绝敏感 memory
  key、嵌套字段和常见 secret 格式。`MemoryService.get()` 对工具契约收紧为最多 20 条。
- 所有外部字符串中的孤立 Unicode surrogate 都在 SQLite 边界前拒绝并返回 `invalid_unicode`；时间戳
  必须位于 SQLite INTEGER 的 `0..2^63-1` 范围，损坏行中的非法时间戳统一返回 `storage_corrupt`。
- F1 的 intent gate 仍是纯函数且默认拒绝；否定表达（例如“不要记住/不要保存”）优先于正向 marker，
  不连接 AgentRuntime，也不接受模型自带的 source/confirmed 字段。
