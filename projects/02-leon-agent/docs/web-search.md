# Leon Agent 联网搜索

本文记录 Leon Agent 的 Tavily `web_search` 最小闭环，供后续 Codex 在任务被中断后直接接手。

## 当前状态

第一版已经接入 CLI 和 Web Gateway：

```text
用户问题
  -> Leon Agent 判断是否需要联网
  -> web_search Agent tool
  -> WebSearchService
  -> TavilySearchProvider（主）
     -> 失败时 FailoverSearchProvider
     -> TavilySearchProvider（备）
  -> POST <base_url>/search
  -> 结构化搜索证据
  -> LLM 归纳并引用 URL
```

- 配置主站 `TAVILY_API_KEY` 或完整的备用 Key/Base URL 时注册 `web_search`。
- 没有 Key 时 Leon 正常启动，搜索工具不会出现在工具列表中。
- CLI 和 Web Gateway 共用同一套 service、tool schema 和 system prompt 规则。
- 搜索是只读工具，不提交生图任务，也不修改外部页面。
- Tavily Key 只通过后端 `Authorization: Bearer ...` 请求头发送，不进入工具结果。

## 目录边界

搜索实现集中在 `projects/02-leon-agent`，没有放进图片服务，也没有放进共享
`packages/workbench_core`：

| 路径 | 职责 |
|---|---|
| `src/leon_agent/search/provider.py` | `SearchProvider` 协议和 Tavily HTTP 适配器 |
| `src/leon_agent/search/service.py` | 参数校验、外部错误收口、结果标准化 |
| `src/leon_agent/search/__init__.py` | 根据 Key 可选创建 `WebSearchService` |
| `src/leon_agent/config.py` | Tavily 环境变量配置和启用状态 |
| `src/leon_agent/tools.py` | 可选注册 `web_search` Agent tool |
| `src/leon_agent/agent.py` | 搜索时机、引用和不可信网页内容规则 |
| `src/leon_agent/cli.py` | CLI composition root，注入搜索 service |
| `src/leon_agent/gateway/app.py` | Web Gateway 每轮创建 Agent 时注入搜索 service |
| `tests/test_search.py` | provider-free 单元测试和工具注册测试 |

边界原则：provider 只处理 Tavily 协议，service 只处理稳定的领域输入输出，CLI、Web、未来
MCP 等 channel 只负责注入和展示。更换搜索供应商时应新增 provider adapter，不改 Agent tool
契约。

## 配置

在 `%USERPROFILE%\.leon\config.toml` 的 `[leon.env]` 中配置，不要把真实 Key 写入 Git：

```toml
[leon.env]
TAVILY_API_KEY = "<new-tavily-api-key>"
TAVILY_BASE_URL = "https://api.tavily.com"
TAVILY_FALLBACK_API_KEY = "<fallback-key>"
TAVILY_FALLBACK_BASE_URL = "https://tavily.ivanli.cc/api/tavily"
TAVILY_TIMEOUT_SECONDS = 15
TAVILY_MAX_RESULTS = 5
```

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TAVILY_API_KEY` | 空 | 非空时启用 `web_search`；只保留在后端 |
| `TAVILY_BASE_URL` | `https://api.tavily.com` | Tavily API 根地址 |
| `TAVILY_FALLBACK_API_KEY` | 空 | 主站请求失败后使用的备用 Bearer Token |
| `TAVILY_FALLBACK_BASE_URL` | 空 | 备用 Tavily 风格 HTTP API 根地址 |
| `TAVILY_TIMEOUT_SECONDS` | `15` | 单次搜索 HTTP 超时秒数 |
| `TAVILY_MAX_RESULTS` | `5` | 工具默认结果数，允许 `1..10` |

已经粘贴到聊天、日志或截图中的 Key 应先在 Tavily 控制台撤销，再生成新 Key。配置修改后要
重启对应进程；`src/` 代码修改后也必须重启 `leon-server`，并验证实际运行中的服务。

## 工具契约

Agent 看见的 `web_search` 输入 schema：

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "max_results": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10,
      "default": 5
    },
    "search_depth": {
      "type": "string",
      "enum": ["basic", "advanced"],
      "default": "basic"
    },
    "topic": {
      "type": "string",
      "enum": ["general", "news"],
      "default": "general"
    }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

`max_results.default` 实际取 `TAVILY_MAX_RESULTS`。默认使用 `basic`；只有用户明确要求深入研究时
才应使用 `advanced`。新闻问题可使用 `topic=news`。

成功结果示例：

```json
{
  "ok": true,
  "provider": "tavily",
  "query": "latest Python release",
  "search_depth": "basic",
  "topic": "general",
  "results": [
    {
      "title": "Python release page",
      "url": "https://example.com/python",
      "snippet": "Normalized evidence snippet",
      "source": "example.com",
      "published_at": "2026-08-16T00:00:00Z"
    }
  ],
  "searched_at": "2026-08-16T00:00:00+00:00"
}
```

service 只保留 `http://` 和 `https://` 结果，并限制标题、URL、摘要和发布时间长度。主 provider
出现 HTTP、网络或 JSON 异常时只尝试一次备用 provider；主站成功返回空结果不会触发切换。
取消当前 Agent 轮次仍会向上抛出取消信号，不会触发备用请求或伪装成搜索失败。成功结果的
`provider` 为实际命中的 `tavily-primary` 或 `tavily-fallback`，Key 不进入工具结果。

Tavily 请求当前固定关闭 `include_answer`、`include_raw_content` 和 `include_images`。返回内容是
供 LLM 判断的证据，不是可执行指令；最终回答应引用结果 URL，且不得补写搜索结果没有支持的事实。

## 验证

先跑不消耗 Tavily credits 的单元测试：

```powershell
uv run pytest projects/02-leon-agent/tests/test_search.py -q
```

再使用一枚有效的新 Key 做手工闭环：

```powershell
uv run leon
```

进入 CLI 后运行 `/tools`，应看到 `web_search`；再询问一条明确需要最新信息且容易核对来源的
问题。Web 场景需要先重启 Gateway：

```powershell
uv run leon-server --host 127.0.0.1 --port 8233
```

验收点：

- 未配置 Key 时 `/tools` 不包含 `web_search`，普通聊天和生图不受影响。
- 配置 Key 后，CLI 与 Web 的工具事件包含 `web_search`，但不包含 API Key。
- 默认请求使用 `basic`、`general` 和配置的结果数。
- 主站失败时最多请求一次备用 `/search`；主站成功或取消时不调用备用站。
- 最终回答引用真实返回 URL；空结果或上游错误不会被描述成搜索成功。
- 修改后端代码后，验证的是重启后的实际服务，而不是旧进程。

## 当前未做

- 未实现 Tavily `extract`、`crawl`、`map` 和 `research`。
- 未实现 `open_url` 或网页正文读取，因此当前只有 Tavily 返回的搜索摘要。
- 仅实现一个 Tavily 风格 HTTP 备用 provider；未接入 SearXNG、Brave 或多级轮换。
- 未做搜索缓存、credits 统计、预算上限或 rate-limit 策略。
- 未提供 domain、日期范围、国家或语言等高级过滤参数。
- 未在 Web 设置页录入 Key；Key 只能通过后端环境变量配置。
- 未给搜索结果做专用 UI，Web 仍通过通用 tool timeline 和最终 Markdown 回答展示。
- system prompt 要求引用 URL，但当前没有程序化引用完整性校验。
- `projects/04-mcp-lab/leon-mcp-server` 当前不暴露 `web_search`。

不要把上述能力提前写进 schema。下一步优先级建议是：先稳定基础搜索和引用，再按真实需求选择
`extract/open_url`、缓存或 MCP 暴露，避免一次请求无控制地消耗多个 credits。

## 中断后接手

1. 先读项目 `README.md`、`docs/AI-COLLABORATION.md` 和本文。
2. 运行 `git status --short`，保留工作区已有改动；不要提交 `.leon` 配置或任何真实 Key。
3. 从 `tests/test_search.py` 开始确认现有契约，再读 `search/`、`tools.py` 和两个 composition root。
4. 先跑 provider-free 测试；需要真实联网时再放入一枚未泄露的新 Key。
5. 修改 `src/` 后重启 `leon-server`，分别验证 CLI `/tools` 和 Web 的真实工具事件。
6. 契约或边界变化时同步更新本文、项目 README 和 `docs/AI-COLLABORATION.md`。
