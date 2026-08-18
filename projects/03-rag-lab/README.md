# 03-rag-lab

独立的 RAG 实验项目，固定按 `chunk → embedding → retrieval → citation` 推进。

## 当前进度：Chunk → Embedding → Retrieval → Citation → Answer → Eval

第一棒只建立可验证的文本证据契约：

- 输入是已由上层授权读取的 Markdown、TXT 或 JSON 文本。
- `TextDocument` 只接受 root-relative path，不接触绝对路径或文件系统。
- `chunk_document` 按字符上限确定性切分，优先保留完整行；超长单行才硬切。
- 每个 `Chunk` 保留稳定 `chunk_id`、原始行号和 `root_id:path:start-end` citation。
- Chunk 内容始终标记为 `untrusted_content`，后续不能当成指令执行。
- `EmbeddingProvider` 隔离供应商，`OpenAIEmbeddingProvider` 只接收调用方注入的 client/model，
  不读取 Leon 或仓库密钥。
- `embed_chunks` 使用有上限的 batch，校验数量、维度、有限值和零向量，再做 L2 归一化。
- `VectorRetriever` 在不可变内存快照上做 cosine top-K；同分结果保持索引顺序。
- `build_citation_context` 在字符预算内输出明确标记为 `UNTRUSTED` 的证据块，并原样保留来源。
- 确定性指标已覆盖 Recall@K、单 query reciprocal rank（套件平均即 MRR）和 citation precision。
- 固定 11 篇文档、13 个 query 的检索集已落地，其中 5 个 case 带相似主题 hard negative。
- `rag-suite` 一次建索引后批量聚合 retrieval、citation 与 faithfulness，支持按 case id 定向回归。
- CLI 默认使用确定性 fake provider；只有显式传入 `--live` 才允许请求真实 embedding API。
- 可选 reranker 只在显式 `--live --rerank` 时启用：先用 embedding 取候选池，再调用 SiliconFlow
  `/v1/rerank` 重排，最后截取目标 Top-K。
- `answer_query` 将检索、受限上下文、答案生成和 citation precision 串成一个可测试回路。
- `OpenAIAnswerGenerator` 要求每个事实使用 `[CITE:<exact-citation>]`，并继续把检索内容视为不可信数据。
- faithfulness judge 将答案拆成 atomic claims，再逐条判断证据支持；不使用字符串命中率替代判断。
- `rag_search` 将现有 `VectorRetriever` 暴露为唯一的只读 `AgentTool` 业务契约，供不同 Runtime
  后续共同注册；它只查预建本地索引，不负责实时搜索、答案生成或 Judge。

```text
TextDocument
   -> normalize line endings
   -> bounded chunks
   -> line-aware citation
   -> provider embedding
   -> normalized vectors
   -> cosine candidate Top-K retrieval
   -> optional reranking
   -> final Top-K retrieval
   -> bounded citation context
   -> grounded answer with exact citations
   -> optional claim/evidence faithfulness judge
```

## 共享 RAG Tool 边界

`create_rag_search_tool(RAGSearchService(retriever))` 返回标准 `AgentTool`，不依赖 LangChain 或
LangGraph。输入固定为 1～500 字符的 `query` 和 1～5 的 `top_k`；输出 evidence 一律标记为
`untrusted_content`，citation 必须满足安全的 `root_id:path:start-end` 格式。

单条正文最多 1000 字符，总 JSON observation 最多 5500 字符，低排名 evidence 会优先裁剪，避免
Self-built `AgentRuntime` 的 6000 字符边界再次截断而与 LangGraph 产生不同 observation。审计视图只
保留 `top_k / count / citations`，不记录 query 或证据正文。

当前完成的是共享 Tool 契约和 provider-free 检索验证；它尚未注入 Self-built Leon 的 CLI/Gateway，
因此不能宣称原 Leon live 已启用 RAG。

## 示例

```python
from rag_lab import TextDocument, chunk_document

document = TextDocument(
    root_id="docs",
    path="guide.md",
    text="# Leon\nEvaluation 验证 Agent 行为。",
)
chunks = chunk_document(document, max_chars=800)
print(chunks[0].citation)  # docs:guide.md:1-2
```

## 验证

```powershell
uv run pytest projects/03-rag-lab/tests -q
uv run ruff check projects/03-rag-lab
uv run rag-eval
uv run rag-ask "检索质量使用 Recall@K 和 MRR 怎么衡量？"
uv run rag-suite
```

当前 fake baseline 只验证评测管线，不代表真实语义模型质量：

```text
provider=fake:deterministic-lexical-v1
cases=13 top_k=3 recall@k=0.808 mrr=0.692
```

`tests/`、不带参数的 `rag-eval` 和 `rag-ask` 都不产生真实 API 请求或额度消耗。

## 真实 Embedding 怎么选

这套中文/英文混合工程文档，第一候选是 `BAAI/bge-m3`：多语言检索能力和上下文长度都适合 RAG，
并且不少 OpenAI-compatible 服务直接暴露该模型。若现有服务只提供 OpenAI 官方 embedding：

- 先用 `text-embedding-3-small` 跑成本基线。
- 再用 `text-embedding-3-large` 做质量对照，不因维度更大就直接假设更好。
- 最终用同一评测集比较 Recall@K、MRR、延迟和费用后决定。

聊天模型名不能当作 embedding 模型名。索引和查询必须始终使用同一个模型；更换模型或维度后必须
重建全部向量。

真实调用必须显式 opt-in，并明确指定专用模型。默认复用 shared lab 的 OpenAI-compatible
base URL 与凭据，不读取 `%USERPROFILE%\.leon\config.toml`：

```powershell
uv run rag-eval --live --model BAAI/bge-m3
```

若 embedding 服务与聊天服务不是同一地址，使用独立环境变量；密钥不要写进仓库或命令参数：

```powershell
$env:RAG_EMBEDDING_BASE_URL = "https://embedding.example/v1"
$env:RAG_EMBEDDING_API_KEY = "<secret>"
$env:RAG_EMBEDDING_MODEL = "BAAI/bge-m3"
uv run rag-eval --live
```

真实 embedding 和 shared lab chat provider 都必须显式 opt-in：

```powershell
uv run rag-ask --live "pytest 和 Agent Evaluation 分别验证什么？"
```

faithfulness 会产生额外一次真实 judge 请求，因此需要第二个显式开关：

```powershell
uv run rag-ask --live --judge "pytest 和 Agent Evaluation 分别验证什么？"
```

hard case 可以单独或组合回归：

```powershell
uv run rag-suite --live --judge `
  --case-id rag-metrics-hard-negative `
  --case-id planning-recovery-hard-negative
```

### 可选 Reranker

reranker 与 embedding 使用同一 OpenAI-compatible transport 时，只需要额外指定模型：

```powershell
$env:RAG_EMBEDDING_BASE_URL = "https://embedding.example/v1"
$env:RAG_EMBEDDING_API_KEY = "<secret>"
uv run rag-eval --live --model BAAI/bge-m3 `
  --rerank --reranker-model BAAI/bge-reranker-v2-m3 --candidate-k 8 --top-k 3
```

也可以为 reranker 配置独立 transport：`RAG_RERANKER_BASE_URL`、`RAG_RERANKER_API_KEY` 和
`RAG_RERANKER_MODEL`。`--candidate-k` 必须大于等于最终 `--top-k`；默认候选池为 8，最终输出为 3。
reranker 没有 fake 实现，因此没有 `--live` 时拒绝启动，避免把离线 lexical baseline 当成语义重排结果。

当前用 SiliconFlow `BAAI/bge-reranker-v2-m3` 在同一 13-case、Top-8 → Top-3 流程做过真实对照：

```text
embedding-only:              Recall@3=1.000  MRR=0.923
embedding + bge-reranker:    Recall@3=1.000  MRR=0.923
```

aggregate MRR 没有提升，因此当前结论是“reranker 接口、配置和对照链路完成”，不是“reranker 已改善质量”。
细看 hard case：Planning case 从 Top-2 调到 Top-1；Trace/Audit case 反而由 audit 文档抢到 Top-1；
RAG metrics case 仍由 Web Search 文档抢到 Top-1。后续只有在扩大真实 query 集、明确延迟与费用预算后，
才决定是否保留该模型或对比其他 reranker。

回答中的 citation 使用机器可解析格式，例如
`[CITE:baseline:agent-evaluation.md:1-2]`。citation precision 只检查引用是否来自本次检索证据；
它不等于 faithfulness。faithfulness 报告会给出 atomic claim 数、支持数、逐条理由和证据引用。

## 最新真实验证

使用 SiliconFlow `BAAI/bge-m3`（1024 维）和 shared lab chat provider：

```text
retrieval: cases=13 top_k=3 recall@k=1.000 mrr=0.923
hard suite: cases=5 top_k=3 recall@k=1.000 mrr=0.800
hard suite: citation_rate=1.000 citation_precision=1.000
hard suite: faithfulness=1.000 unsupported_claims=0
negative control: unsupported_claims=1/1 score=0.000
```

negative control 故意声称“Evaluation 固定执行 1000 个生产 case”，judge 能指出检索证据没有该信息。
两个 hard case 的干扰文档排在 Top-1、正确文档排在第 2，因此 MRR 没有虚报满分；Top-3 仍完整召回。

## 当前非目标

- 不接入 Leon 生产工具。
- 不引入 Qdrant、Chroma 或其他向量数据库。
- 不读取 PDF/DOCX，也不绕过现有文件 root 安全边界。
- 不把当前小型合成数据集的分数当作生产质量结论。
