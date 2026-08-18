# Provider-free Runtime Comparison

> Deterministic fake models and local read-only tools only. Timings measure local
> orchestration overhead on this machine; they do not represent provider latency or
> claim that one runtime is universally faster.

- Cases: 10
- Repeats per runtime/case: 7
- Self-built task success: 10/10
- LangGraph task success: 10/10
- Raw observation parity: 10/10

| Case | Tools | Self-built | LangGraph | Observation | Model calls | Median ms (Self / Graph) |
|---|---|---:|---:|---:|---:|---:|
| direct-chat | none | PASS | PASS | SAME | 1 / 1 | 0.138 / 1.226 |
| read-intro | read_file | PASS | PASS | SAME | 2 / 2 | 2.008 / 9.191 |
| read-recovery | read_file | PASS | PASS | SAME | 2 / 2 | 1.659 / 27.215 |
| missing-file | read_file | PASS | PASS | SAME | 2 / 2 | 0.836 / 34.910 |
| rag-runtime | rag_search | PASS | PASS | SAME | 2 / 2 | 0.560 / 9.680 |
| rag-checkpoint | rag_search | PASS | PASS | SAME | 2 / 2 | 0.510 / 26.348 |
| rag-memory | rag_search | PASS | PASS | SAME | 2 / 2 | 0.547 / 22.801 |
| file-then-rag | read_file -> rag_search | PASS | PASS | SAME | 3 / 3 | 2.268 / 35.263 |
| rag-then-file | rag_search -> read_file | PASS | PASS | SAME | 3 / 3 | 2.504 / 40.567 |
| three-step | read_file -> rag_search -> read_file | PASS | PASS | SAME | 4 / 4 | 3.373 / 56.367 |

## Physical source-line snapshot

| Boundary | Lines |
|---|---:|
| self_runtime | 743 |
| self_tools | 136 |
| graph | 74 |
| adapter | 44 |
| planning | 97 |
| checkpointing | 216 |

The line counts are descriptive, not a productivity score: Self-built includes
streaming, cancellation, events, trace and audit projection, while framework library
internals are outside the LangGraph-side count.

