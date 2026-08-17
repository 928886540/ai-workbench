from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from leon_agent.agent import LeonAgent, build_system_prompt
from leon_agent.planning.service import PlanningService
from leon_agent.planning.tools import create_planning_tools
from leon_agent.session import SessionStore
from leon_agent.tools import create_leon_tools
from workbench_core.agent import AgentEvent, AgentRuntime, AgentTool, ToolRegistry
from workbench_core.llm import ChatTurn, ToolCall

RAW_PLAN_MARKER = "raw-plan-description-must-not-persist"


class FakeImageClient:
    def list_modes(self) -> dict[str, Any]:
        return {"ok": True, "modes": []}


class AnswerClient:
    def chat_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatTurn:
        del messages, tools, temperature
        return ChatTurn(
            content="完成。",
            raw_message={"role": "assistant", "content": "完成。"},
        )


class PlanningFlowClient:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[list[dict[str, Any]]] = []

    def chat_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatTurn:
        del tools, temperature
        self.calls += 1
        self.requests.append(deepcopy(messages))
        calls = {
            1: (
                "plan_create",
                {
                    "steps": [
                        {"description": f"检索资料 {RAW_PLAN_MARKER}"},
                        {"description": "汇总结论"},
                    ]
                },
            ),
            2: ("plan_update", {"step_index": 1, "status": "in_progress"}),
            3: ("probe", {}),
            4: ("plan_update", {"step_index": 1, "status": "completed"}),
            5: ("plan_update", {"step_index": 2, "status": "in_progress"}),
            6: ("plan_update", {"step_index": 2, "status": "completed"}),
        }
        if self.calls in calls:
            name, arguments = calls[self.calls]
            encoded = json.dumps(arguments, ensure_ascii=False)
            return ChatTurn(
                content=None,
                tool_calls=[
                    ToolCall(id=f"planning-{self.calls}", name=name, arguments=encoded)
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"planning-{self.calls}",
                            "type": "function",
                            "function": {"name": name, "arguments": encoded},
                        }
                    ],
                },
            )
        return ChatTurn(
            content="计划已执行。",
            raw_message={"role": "assistant", "content": "计划已执行。"},
        )


def test_planning_state_machine_enforces_order_and_terminal_steps() -> None:
    service = PlanningService()

    assert service.create([{"description": "only one"}])["error_code"] == (
        "invalid_argument"
    )
    assert service.create(
        [{"description": "first"}, {"description": "second\nline"}]
    )["error_code"] == "invalid_argument"

    created = service.create(
        [{"description": "first"}, {"description": "second"}]
    )
    assert created["step_count"] == 2
    assert created["active_step"] is None
    assert service.create(
        [{"description": "again"}, {"description": "again 2"}]
    )["error_code"] == "plan_exists"
    assert service.update(2, "in_progress")["error_code"] == "step_not_ready"
    assert service.update(1, "completed")["error_code"] == "invalid_transition"

    active = service.update(1, "in_progress")
    assert active["active_step"] == 1
    assert service.update(2, "in_progress")["error_code"] == "step_active"
    first_done = service.update(1, "completed")
    assert first_done["completed_count"] == 1
    assert service.update(1, "in_progress")["error_code"] == "invalid_transition"

    service.update(2, "in_progress")
    finished = service.update(2, "failed")
    assert finished["done"] is True
    assert finished["completed_count"] == 1
    assert finished["failed_count"] == 1

    service.reset()
    assert service.get()["error_code"] == "plan_not_found"


def test_planning_tools_hide_descriptions_from_audit_and_sqlite(tmp_path: Path) -> None:
    service = PlanningService()
    registry = ToolRegistry(
        [
            *create_planning_tools(service),
            AgentTool(
                name="probe",
                description="A deterministic domain tool used by the integration test.",
                parameters={"type": "object", "properties": {}},
                handler=lambda: {"ok": True, "evidence_count": 1},
            ),
        ]
    )
    events: list[AgentEvent] = []
    client = PlanningFlowClient()
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=registry,
        system_prompt="Use planning for this multi-step task.",
        max_turns=8,
        on_event=events.append,
    )

    result = runtime.run("先检索再汇总")

    assert result.answer == "计划已执行。"
    assert [step.name for step in result.steps] == [
        "plan_create",
        "plan_update",
        "probe",
        "plan_update",
        "plan_update",
        "plan_update",
    ]
    assert RAW_PLAN_MARKER in repr(result.messages)
    assert RAW_PLAN_MARKER not in repr(result.steps)
    assert RAW_PLAN_MARKER not in repr(events)
    assert result.steps[0].arguments == {"step_count": 2}
    assert result.steps[0].result["steps"] == [
        {"step_index": 1, "status": "pending"},
        {"step_index": 2, "status": "pending"},
    ]
    assert result.steps[-1].result["done"] is True

    db_path = tmp_path / "planning.db"
    store = SessionStore(db_path)
    session_id = store.create_session()
    store.record_result(session_id, result)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT arguments_json, result_json FROM tool_calls WHERE session_id = ?",
            (session_id,),
        ).fetchall()

    assert len(rows) == 6
    assert RAW_PLAN_MARKER not in repr(rows)


def test_leon_agent_registers_planning_and_resets_injected_service() -> None:
    planning_service = PlanningService()
    planning_service.create(
        [{"description": "stale first"}, {"description": "stale second"}]
    )
    agent = LeonAgent(
        llm_client=AnswerClient(),  # type: ignore[arg-type]
        image_client=FakeImageClient(),  # type: ignore[arg-type]
        session_id="planning-session",
        default_mode_ids=["k2_tifa_plus"],
        planning_service=planning_service,
    )

    assert {"plan_create", "plan_update", "plan_get"}.issubset(
        agent.runtime.tools.names
    )
    assert "Planning tools are enabled" in agent.runtime.system_prompt

    result = agent.run("普通聊天")

    assert result.answer == "完成。"
    assert planning_service.get()["error_code"] == "plan_not_found"


def test_direct_registry_does_not_advertise_planning_tools() -> None:
    direct_tools = create_leon_tools(
        FakeImageClient(),  # type: ignore[arg-type]
        session_id="direct-session",
        default_mode_ids=["k2_tifa_plus"],
    )

    assert {"plan_create", "plan_update", "plan_get"}.isdisjoint(direct_tools.names)
    assert "Planning tools are enabled" in build_system_prompt(planning_enabled=True)


def test_planning_tools_use_dedicated_trace_span_kind() -> None:
    tools = create_planning_tools(PlanningService())

    assert {tool.name: tool.span_kind for tool in tools} == {
        "plan_create": "planning",
        "plan_update": "planning",
        "plan_get": "planning",
    }
    assert AgentTool(
        name="probe",
        description="A normal domain tool.",
        parameters={"type": "object", "properties": {}},
        handler=lambda: {"ok": True},
    ).span_kind == "tool"
