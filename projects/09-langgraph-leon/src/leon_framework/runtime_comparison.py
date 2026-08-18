"""Provider-free controlled comparison for Leon's two orchestration runtimes."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from leon_agent.file_tools import create_file_tools
from rag_lab import (
    RAGSearchService,
    TextDocument,
    VectorRetriever,
    chunk_document,
    create_rag_search_tool,
    embed_chunks,
)
from rag_lab.providers import DeterministicFakeEmbeddingProvider
from workbench_core.agent import AgentRuntime, ToolRegistry
from workbench_core.files import FileSearchService
from workbench_core.llm import ChatTurn, ToolCall

from leon_framework.graph import build_leon_graph


@dataclass(frozen=True, slots=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ComparisonCase:
    case_id: str
    prompt: str
    answer: str
    requests: tuple[ToolRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeExecution:
    answer: str
    tool_names: tuple[str, ...]
    observations: tuple[str, ...]
    model_calls: int


@dataclass(frozen=True, slots=True)
class CaseComparison:
    case_id: str
    expected_tools: tuple[str, ...]
    self_built_success: bool
    langgraph_success: bool
    observation_parity: bool
    self_built_model_calls: int
    langgraph_model_calls: int
    self_built_median_ms: float
    langgraph_median_ms: float


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    repeats: int
    cases: tuple[CaseComparison, ...]
    source_lines: dict[str, int]

    @property
    def all_passed(self) -> bool:
        return all(
            case.self_built_success
            and case.langgraph_success
            and case.observation_parity
            for case in self.cases
        )


class _SelfBuiltScriptedClient:
    def __init__(self, case: ComparisonCase) -> None:
        self.case = case
        self.model_calls = 0
        self.observations: tuple[str, ...] = ()

    def reset(self) -> None:
        self.model_calls = 0
        self.observations = ()

    def chat_turn(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        **_: Any,
    ) -> ChatTurn:
        del temperature
        self.model_calls += 1
        self.observations = tuple(
            str(message["content"])
            for message in messages
            if message.get("role") == "tool"
        )
        request_index = len(self.observations)
        if request_index < len(self.case.requests):
            request = self.case.requests[request_index]
            available = {
                str(schema["function"]["name"])
                for schema in (tools or [])
            }
            if request.name not in available:
                raise RuntimeError(f"missing shared tool schema: {request.name}")
            call_id = f"self-{self.case.case_id}-{request_index}"
            arguments = json.dumps(request.arguments, ensure_ascii=False)
            return ChatTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        name=request.name,
                        arguments=arguments,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": request.name,
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            )
        return ChatTurn(
            content=self.case.answer,
            raw_message={"role": "assistant", "content": self.case.answer},
        )

    def chat(self, messages: Sequence[dict[str, Any]], **_: Any) -> str:
        del messages
        raise RuntimeError("comparison script exceeded the expected tool loop")


class _LangGraphScriptedModel:
    def __init__(self, case: ComparisonCase) -> None:
        self.case = case
        self.model_calls = 0
        self.observations: tuple[str, ...] = ()
        self.bound_names: set[str] = set()

    def bind_tools(self, tools: list[Any]) -> _LangGraphScriptedModel:
        self.bound_names = {str(tool.name) for tool in tools}
        return self

    def reset(self) -> None:
        self.model_calls = 0
        self.observations = ()

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.model_calls += 1
        self.observations = tuple(
            str(message.content)
            for message in messages
            if isinstance(message, ToolMessage)
        )
        request_index = len(self.observations)
        if request_index < len(self.case.requests):
            request = self.case.requests[request_index]
            if request.name not in self.bound_names:
                raise RuntimeError(f"missing adapted tool: {request.name}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": request.name,
                        "args": request.arguments,
                        "id": f"graph-{self.case.case_id}-{request_index}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content=self.case.answer)


def comparison_cases() -> tuple[ComparisonCase, ...]:
    read_intro = ToolRequest(
        "read_file",
        {
            "root_id": "workspace",
            "relative_path": "runtime-notes.md",
            "start_line": 1,
            "max_lines": 2,
        },
    )
    read_recovery = ToolRequest(
        "read_file",
        {
            "root_id": "workspace",
            "relative_path": "recovery-notes.md",
            "start_line": 1,
            "max_lines": 3,
        },
    )
    missing_file = ToolRequest(
        "read_file",
        {
            "root_id": "workspace",
            "relative_path": "missing.md",
            "start_line": 1,
            "max_lines": 2,
        },
    )
    rag_runtime = ToolRequest(
        "rag_search",
        {"query": "Agent Runtime observation", "top_k": 2},
    )
    rag_checkpoint = ToolRequest(
        "rag_search",
        {"query": "LangGraph checkpoint resume", "top_k": 2},
    )
    rag_memory = ToolRequest(
        "rag_search",
        {"query": "long-term memory policy", "top_k": 2},
    )
    return (
        ComparisonCase("direct-chat", "概括 Leon 的定位", "direct answer"),
        ComparisonCase("read-intro", "读取 Runtime 说明", "file evidence", (read_intro,)),
        ComparisonCase(
            "read-recovery",
            "读取恢复说明",
            "recovery evidence",
            (read_recovery,),
        ),
        ComparisonCase(
            "missing-file",
            "读取不存在的文件并解释错误",
            "handled missing file",
            (missing_file,),
        ),
        ComparisonCase("rag-runtime", "检索 Runtime", "runtime evidence", (rag_runtime,)),
        ComparisonCase(
            "rag-checkpoint",
            "检索 checkpoint",
            "checkpoint evidence",
            (rag_checkpoint,),
        ),
        ComparisonCase("rag-memory", "检索 Memory", "memory evidence", (rag_memory,)),
        ComparisonCase(
            "file-then-rag",
            "先读文件再检索",
            "combined evidence",
            (read_intro, rag_runtime),
        ),
        ComparisonCase(
            "rag-then-file",
            "先检索再读恢复说明",
            "reverse combined evidence",
            (rag_checkpoint, read_recovery),
        ),
        ComparisonCase(
            "three-step",
            "完成三步证据收集",
            "three-step evidence",
            (read_intro, rag_memory, read_recovery),
        ),
    )


def _build_shared_registry(workspace: Path) -> ToolRegistry:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "runtime-notes.md").write_text(
        "Self-built AgentRuntime owns the explicit tool loop.\n"
        "Tool observations are returned to the model.\n",
        encoding="utf-8",
    )
    (workspace / "recovery-notes.md").write_text(
        "LangGraph stores workflow state in checkpoints.\n"
        "Interrupt pauses before a configured node.\n"
        "Resume continues from the stored execution boundary.\n",
        encoding="utf-8",
    )
    file_tools = [
        tool
        for tool in create_file_tools(FileSearchService({"workspace": workspace}))
        if tool.name == "read_file"
    ]
    documents = [
        TextDocument(
            root_id="knowledge",
            path="runtime.md",
            text="Agent Runtime returns tool results to the model as observations.",
        ),
        TextDocument(
            root_id="knowledge",
            path="checkpoint.md",
            text="LangGraph checkpoint and resume preserve workflow execution state.",
        ),
        TextDocument(
            root_id="knowledge",
            path="memory.md",
            text="Long-term memory stores explicit user preferences under a consent policy.",
        ),
    ]
    chunks = [
        chunk
        for document in documents
        for chunk in chunk_document(document, max_chars=200)
    ]
    embedding_provider = DeterministicFakeEmbeddingProvider(dimensions=64)
    retriever = VectorRetriever(
        embed_chunks(chunks, embedding_provider),
        embedding_provider,
    )
    rag_tool = create_rag_search_tool(RAGSearchService(retriever))
    return ToolRegistry([*file_tools, rag_tool])


def _run_self_built(
    runtime: AgentRuntime,
    client: _SelfBuiltScriptedClient,
    case: ComparisonCase,
) -> RuntimeExecution:
    client.reset()
    result = runtime.run(case.prompt)
    return RuntimeExecution(
        answer=result.answer,
        tool_names=tuple(step.name for step in result.steps),
        observations=client.observations,
        model_calls=client.model_calls,
    )


def _run_langgraph(
    graph: Any,
    model: _LangGraphScriptedModel,
    case: ComparisonCase,
) -> RuntimeExecution:
    model.reset()
    result = graph.invoke({"messages": [HumanMessage(content=case.prompt)]})
    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    return RuntimeExecution(
        answer=str(result["messages"][-1].content),
        tool_names=tuple(str(message.name or "tool") for message in tool_messages),
        observations=model.observations,
        model_calls=model.model_calls,
    )


def _measure_case(
    case: ComparisonCase,
    registry: ToolRegistry,
    repeats: int,
) -> CaseComparison:
    self_client = _SelfBuiltScriptedClient(case)
    self_runtime = AgentRuntime(
        client=self_client,  # type: ignore[arg-type]
        tools=registry,
        system_prompt="Follow the deterministic comparison script.",
    )
    graph_model = _LangGraphScriptedModel(case)
    graph = build_leon_graph(graph_model, registry)

    _run_self_built(self_runtime, self_client, case)
    _run_langgraph(graph, graph_model, case)
    self_durations: list[float] = []
    graph_durations: list[float] = []
    self_execution: RuntimeExecution | None = None
    graph_execution: RuntimeExecution | None = None
    for index in range(repeats):
        order = ("self", "graph") if index % 2 == 0 else ("graph", "self")
        for runtime_name in order:
            started = time.perf_counter_ns()
            if runtime_name == "self":
                self_execution = _run_self_built(self_runtime, self_client, case)
                self_durations.append((time.perf_counter_ns() - started) / 1_000_000)
            else:
                graph_execution = _run_langgraph(graph, graph_model, case)
                graph_durations.append((time.perf_counter_ns() - started) / 1_000_000)

    if self_execution is None or graph_execution is None:  # pragma: no cover
        raise RuntimeError("comparison did not execute both runtimes")
    expected_tools = tuple(request.name for request in case.requests)
    expected_model_calls = len(expected_tools) + 1
    self_success = (
        self_execution.answer == case.answer
        and self_execution.tool_names == expected_tools
        and self_execution.model_calls == expected_model_calls
    )
    graph_success = (
        graph_execution.answer == case.answer
        and graph_execution.tool_names == expected_tools
        and graph_execution.model_calls == expected_model_calls
    )
    return CaseComparison(
        case_id=case.case_id,
        expected_tools=expected_tools,
        self_built_success=self_success,
        langgraph_success=graph_success,
        observation_parity=self_execution.observations == graph_execution.observations,
        self_built_model_calls=self_execution.model_calls,
        langgraph_model_calls=graph_execution.model_calls,
        self_built_median_ms=statistics.median(self_durations),
        langgraph_median_ms=statistics.median(graph_durations),
    )


def _physical_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as source:
        return sum(1 for _ in source)


def _source_line_snapshot() -> dict[str, int]:
    project_root = Path(__file__).resolve().parents[2]
    repository_root = project_root.parent.parent
    paths = {
        "self_runtime": repository_root
        / "packages/workbench_core/src/workbench_core/agent/runtime.py",
        "self_tools": repository_root
        / "packages/workbench_core/src/workbench_core/agent/tools.py",
        "graph": project_root / "src/leon_framework/graph.py",
        "adapter": project_root / "src/leon_framework/tool_adapter.py",
        "planning": project_root / "src/leon_framework/planning.py",
        "checkpointing": project_root / "src/leon_framework/checkpointing.py",
    }
    return {name: _physical_lines(path) for name, path in paths.items()}


def run_controlled_comparison(*, repeats: int = 7) -> ComparisonReport:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    with tempfile.TemporaryDirectory(prefix="leon-runtime-comparison-") as directory:
        registry = _build_shared_registry(Path(directory))
        cases = tuple(
            _measure_case(case, registry, repeats)
            for case in comparison_cases()
        )
    return ComparisonReport(
        repeats=repeats,
        cases=cases,
        source_lines=_source_line_snapshot(),
    )


def render_markdown(report: ComparisonReport) -> str:
    passed_self = sum(case.self_built_success for case in report.cases)
    passed_graph = sum(case.langgraph_success for case in report.cases)
    parity = sum(case.observation_parity for case in report.cases)
    lines = [
        "# Provider-free Runtime Comparison",
        "",
        "> Deterministic fake models and local read-only tools only. Timings measure local",
        "> orchestration overhead on this machine; they do not represent provider latency or",
        "> claim that one runtime is universally faster.",
        "",
        f"- Cases: {len(report.cases)}",
        f"- Repeats per runtime/case: {report.repeats}",
        f"- Self-built task success: {passed_self}/{len(report.cases)}",
        f"- LangGraph task success: {passed_graph}/{len(report.cases)}",
        f"- Raw observation parity: {parity}/{len(report.cases)}",
        "",
        (
            "| Case | Tools | Self-built | LangGraph | Observation | Model calls | "
            "Median ms (Self / Graph) |"
        ),
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for case in report.cases:
        tools = " -> ".join(case.expected_tools) or "none"
        lines.append(
            "| "
            f"{case.case_id} | {tools} | "
            f"{'PASS' if case.self_built_success else 'FAIL'} | "
            f"{'PASS' if case.langgraph_success else 'FAIL'} | "
            f"{'SAME' if case.observation_parity else 'DIFF'} | "
            f"{case.self_built_model_calls} / {case.langgraph_model_calls} | "
            f"{case.self_built_median_ms:.3f} / {case.langgraph_median_ms:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Physical source-line snapshot",
            "",
            "| Boundary | Lines |",
            "|---|---:|",
            *[
                f"| {name} | {count} |"
                for name, count in report.source_lines.items()
            ],
            "",
            "The line counts are descriptive, not a productivity score: Self-built includes",
            "streaming, cancellation, events, trace and audit projection, while framework library",
            "internals are outside the LangGraph-side count.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the provider-free Leon Runtime comparison.",
    )
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_controlled_comparison(repeats=args.repeats)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    content = (
        json.dumps(asdict(report), ensure_ascii=False, indent=2)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.output is None:
        print(content)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
        print(f"Wrote {args.output}")
    return 0 if report.all_passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
