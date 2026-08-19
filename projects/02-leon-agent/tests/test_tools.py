from typing import Any

from leon_agent.agent import (
    SYSTEM_PROMPT,
    LeonAgent,
    _build_image_followup_context,
    build_system_prompt,
)
from leon_agent.tools import _format_generation_answer, create_leon_tools
from workbench_core.llm import ChatTurn


class FakeImageClient:
    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self.latest_limits: list[int] = []
        self.cancelled: list[str] = []

    def list_modes(self) -> dict[str, Any]:
        return {"ok": True, "modes": []}

    def check_environment(self) -> dict[str, Any]:
        return {"ok": True}

    def generate_images(self, **kwargs: Any) -> dict[str, Any]:
        self.generate_calls.append(kwargs)
        return {"ok": True, "jobs": []}

    def get_image_tasks(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "items": []}

    def get_recent_images(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "items": []}

    def get_latest_images(self, *, limit: int) -> dict[str, Any]:
        self.latest_limits.append(limit)
        return {"ok": True, "items": []}

    def cancel_image_task(self, *, job_id: str) -> dict[str, Any]:
        self.cancelled.append(job_id)
        return {"ok": True, "job_id": job_id, "status": "cancelled", "cancelled": True}


class ContextCaptureLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def chat_turn(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatTurn:
        del kwargs
        self.messages = messages
        return ChatTurn(
            content="收到。",
            raw_message={"role": "assistant", "content": "收到。"},
        )


def test_generate_tool_passes_source_text_verbatim() -> None:
    client = FakeImageClient()
    tools = create_leon_tools(
        client,  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )
    request = "生成一个美女在玩水，来 2 张"

    result = tools.execute(
        "generate_images",
        {"source_text": request, "batch_count": 2},
    )

    assert result["ok"] is True
    assert client.generate_calls[0]["source_text"] == request
    assert client.generate_calls[0]["workflow_ids"] == ["k2_tifa_plus"]
    assert client.generate_calls[0]["batch_count"] == 2
    assert result["source_text"] == request
    assert result["workflow_ids"] == ["k2_tifa_plus"]


def test_generate_tool_marks_background_submission_for_direct_answer() -> None:
    client = FakeImageClient()
    tools = create_leon_tools(
        client,  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
        wait_for_image_completion=False,
    )

    arguments = {"source_text": "生成一张海边人像"}
    result = tools.execute("generate_images", arguments)
    answer = tools.direct_answer("generate_images", arguments, result)

    assert result["waited_for_completion"] is False
    assert result["images"] == []
    assert answer == (
        "已提交 1 张图片任务，正在后台生成，请稍等；完成后会自动显示在这里。"
    )


def test_generation_answer_reports_terminal_backend_failure_truthfully() -> None:
    answer = _format_generation_answer(
        {"source_text": "生成图片"},
        {
            "ok": False,
            "submitted": True,
            "failed_count": 1,
            "error": "backend generation failed",
            "images": [{"image_url": None}],
        },
    )

    assert answer == "图片生成失败：backend generation failed"
    assert "同步结果" not in answer


def test_generation_answer_reports_retryable_missing_image_without_auto_display() -> None:
    answer = _format_generation_answer(
        {"source_text": "生成图片"},
        {
            "ok": False,
            "retryable": True,
            "error_code": "image_result_unavailable",
            "error": "图片任务已完成，但后端尚未返回可用的图片地址；请稍后重试查询最近图片",
            "images": [],
        },
    )

    assert answer == (
        "图片任务已完成，但后端尚未返回可用的图片地址；请稍后重试查询最近图片。"
    )
    assert "自动显示" not in answer
    assert "同步结果" not in answer


def test_generation_answer_keeps_partial_urls_when_another_result_is_unavailable() -> None:
    answer = _format_generation_answer(
        {"source_text": "生成两张图片"},
        {
            "ok": False,
            "retryable": True,
            "error_code": "image_result_unavailable",
            "error": "1 个图片任务已完成，但后端尚未返回可用的图片地址；请稍后重试查询最近图片",
            "images": [{"image_url": "https://example.test/ready.png"}],
        },
    )

    assert "已拿到 1 张图片" in answer
    assert "https://example.test/ready.png" in answer
    assert "自动显示" not in answer


def test_generation_answer_does_not_promise_auto_display_after_explicit_timeout() -> None:
    answer = _format_generation_answer(
        {"source_text": "生成图片"},
        {
            "ok": True,
            "timed_out": True,
            "jobs": [{"job_id": "job-1"}],
            "images": [],
        },
    )

    assert answer == "已提交 1 张图片任务，仍在生成；本次等待已结束，稍后可查询最近图片。"
    assert "自动显示" not in answer
    assert "正在同步" not in answer


def test_mode_catalog_exposes_human_names_and_exact_ids_to_the_model() -> None:
    client = FakeImageClient()
    client.list_modes = lambda: {  # type: ignore[method-assign]
        "ok": True,
        "modes": [
            {"id": "k2_red_craft"},
            {"id": "k2_queen_marika"},
        ],
    }
    tools = create_leon_tools(
        client,  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_red_craft"],
        wait_for_image_completion=False,
    )

    result = tools.execute("list_image_modes", {})

    assert result["modes"][0]["name"] == "红艺"
    assert result["modes"][1]["name"] == "玛莉卡"
    assert result["modes"][1]["id"] == "k2_queen_marika"


def test_generate_tool_schema_is_gemini_compatible() -> None:
    tools = create_leon_tools(
        FakeImageClient(),  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )
    generate_schema = next(
        item for item in tools.schemas if item["function"]["name"] == "generate_images"
    )
    batch_schema = generate_schema["function"]["parameters"]["properties"][
        "batch_count"
    ]

    assert "enum" not in batch_schema
    assert batch_schema["minimum"] == 1
    assert batch_schema["maximum"] == 10


def test_latest_images_tool_forwards_requested_count() -> None:
    client = FakeImageClient()
    tools = create_leon_tools(
        client,  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )

    result = tools.execute("get_latest_images", {"limit": 7})

    assert result == {"ok": True, "items": []}
    assert client.latest_limits == [7]
    latest_schema = next(
        item for item in tools.schemas if item["function"]["name"] == "get_latest_images"
    )["function"]["parameters"]
    assert latest_schema["required"] == ["limit"]
    assert "default" not in latest_schema["properties"]["limit"]


def test_agent_prompt_separates_mode_controls_from_image_request() -> None:
    assert "visual request in source_text" in SYSTEM_PROMPT
    assert "随便一种模式都行" in SYSTEM_PROMPT
    assert "random_workflow=true" in SYSTEM_PROMPT
    assert "do not translate, summarize, sanitize, expand, beautify" in SYSTEM_PROMPT
    assert "Do not choose Prompt, Workflow, LoRA" in SYSTEM_PROMPT
    assert "inherit the" in SYSTEM_PROMPT
    assert "routing metadata, not visual prompt text" in " ".join(SYSTEM_PROMPT.split())


def test_image_followup_context_carries_forward_the_latest_subject() -> None:
    context = _build_image_followup_context(
        [{"role": "user", "content": "生成一张猫"}],
        "用韩漫风再来一张看看",
    )

    assert context is not None
    assert "Immediately preceding standalone image request: 生成一张猫" in context
    assert "must not be a bare phrase" in context
    assert (
        _build_image_followup_context(
            [{"role": "user", "content": "生成一张猫"}],
            "那继续啊？用韩漫模式生成",
        )
        is not None
    )
    assert (
        _build_image_followup_context(
            [{"role": "user", "content": "生成一张猫"}],
            "继续",
        )
        is not None
    )
    assert (
        _build_image_followup_context(
            [{"role": "user", "content": "生成一张猫"}],
            "继续解释刚才的代码",
        )
        is None
    )
    assert (
        _build_image_followup_context(
            [
                {"role": "user", "content": "生成一张猫"},
                {"role": "assistant", "content": "已生成。"},
                {"role": "user", "content": "解释这段代码"},
                {"role": "assistant", "content": "这是一个循环。"},
            ],
            "继续",
        )
        is None
    )
    assert (
        _build_image_followup_context(
            [{"role": "user", "content": "生成一张猫"}],
            "继续生成代码",
        )
        is None
    )
    assert (
        _build_image_followup_context(
            [{"role": "user", "content": "生成一张猫"}],
            "今天天气怎么样",
        )
        is None
    )


def test_agent_wires_image_followup_context_into_the_runtime() -> None:
    llm = ContextCaptureLLM()
    agent = LeonAgent(
        llm_client=llm,  # type: ignore[arg-type]
        image_client=FakeImageClient(),  # type: ignore[arg-type]
        session_id="follow-up-session",
        default_mode_ids=["k2_tifa_plus"],
    )

    agent.run(
        "用韩漫风再来一张看看",
        history=[
            {"role": "user", "content": "生成一张猫"},
            {"role": "assistant", "content": "已提交图片任务。"},
        ],
    )

    assert any(
        message["role"] == "system"
        and "Immediately preceding standalone image request: 生成一张猫" in message["content"]
        for message in llm.messages
    )


def test_agent_prompt_hardens_live_tool_adherence() -> None:
    normalized = " ".join(SYSTEM_PROMPT.split())

    assert "native function-call mechanism" in normalized
    assert "do not substitute unrelated tools" in normalized
    assert "Inspect every tool result before answering" in normalized
    assert "make one search and do not retry" in normalized
    assert "Copy returned citation strings exactly" in normalized
    assert ".env" in normalized

    optional_prompt = " ".join(
        build_system_prompt(memory_enabled=True, planning_enabled=True).split()
    )
    assert "multiple independent preferences or values" in optional_prompt
    assert 'An explicit sequence such as "search, then read"' in optional_prompt


def test_agent_prompt_names_unavailable_optional_capabilities() -> None:
    agent = LeonAgent(
        llm_client=object(),  # type: ignore[arg-type]
        image_client=FakeImageClient(),  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )
    normalized = " ".join(agent.runtime.system_prompt.split())

    assert "Unavailable optional capabilities for this Agent instance" in normalized
    assert "web search (web_search)" in normalized
    assert "local file access (list_files, file_search, read_file)" in normalized
    assert "long-term memory (memory_get, memory_upsert, memory_delete)" in normalized
    assert "voice output (speak_text)" in normalized
    assert "do not call any tool to probe or replace it" in normalized


def test_cancel_tool_is_exposed_and_forwards_the_job_id() -> None:
    client = FakeImageClient()
    tools = create_leon_tools(
        client,  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )

    result = tools.execute("cancel_image_task", {"job_id": "job-7"})

    assert result["ok"] is True
    assert result["status"] == "cancelled"
    assert client.cancelled == ["job-7"]
    assert tools.direct_answer("cancel_image_task", {"job_id": "job-7"}, result) is None


def test_cancel_tool_answers_completed_no_op_truthfully() -> None:
    client = FakeImageClient()
    client.cancel_image_task = lambda job_id: {  # type: ignore[method-assign]
        "ok": True,
        "job_id": job_id,
        "status": "completed",
        "cancelled": False,
    }
    tools = create_leon_tools(
        client,  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )
    arguments = {"job_id": "job-complete"}

    result = tools.execute("cancel_image_task", arguments)
    answer = tools.direct_answer("cancel_image_task", arguments, result)

    assert answer == "任务已经完成，不能声称取消成功。"


def test_cancel_tool_schema_requires_a_job_id() -> None:
    tools = create_leon_tools(
        FakeImageClient(),  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )

    spec = next(
        item for item in tools.schemas if item["function"]["name"] == "cancel_image_task"
    )
    parameters = spec["function"]["parameters"]

    assert parameters["required"] == ["job_id"]
    assert parameters["properties"]["job_id"]["type"] == "string"
    assert parameters["additionalProperties"] is False


def test_agent_prompt_tells_the_model_it_can_cancel() -> None:
    # The agent used to tell users cancelling was impossible because no tool existed.
    assert "cancel_image_task" in SYSTEM_PROMPT
    assert "never tell the user cancelling is unsupported" in SYSTEM_PROMPT


def test_file_write_prompt_is_opt_in() -> None:
    read_only_prompt = build_system_prompt()
    writable_prompt = build_system_prompt(file_write_enabled=True)

    assert "!file create" not in read_only_prompt
    assert "!file write" not in read_only_prompt
    assert "!file create" in writable_prompt
    assert "!file write" in writable_prompt


def test_additional_system_prompt_is_appended_verbatim() -> None:
    additional = "自定义第一行\n自定义第二行"

    prompt = build_system_prompt(additional)

    assert prompt == f"{SYSTEM_PROMPT}\n\n{additional}"
