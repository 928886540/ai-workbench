from pathlib import Path

from leon_agent.session import SessionStore
from workbench_core.agent import AgentResult, ToolStep


def test_session_store_persists_messages_and_job_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "leon.db")
    session_id = store.create_session()
    store.add_message(session_id, "user", "生成一张图")
    store.add_message(session_id, "assistant", "已提交")
    store.record_result(
        session_id,
        AgentResult(
            answer="已提交",
            steps=[
                ToolStep(
                    name="generate_images",
                    arguments={"source_text": "雨夜"},
                    result={
                        "ok": True,
                        "generation_plan_id": "plan-1",
                        "jobs": [{"job_id": "job-1", "status": "queued"}],
                    },
                )
            ],
        ),
    )

    assert store.load_messages(session_id) == [
        {"role": "user", "content": "生成一张图"},
        {"role": "assistant", "content": "已提交"},
    ]
    assert store.list_sessions()[0]["message_count"] == 2


def test_session_store_persists_model_selection(tmp_path: Path) -> None:
    db_path = tmp_path / "leon.db"
    store = SessionStore(db_path)
    session_id = store.create_session()

    store.set_model_selection(
        session_id,
        provider="薄荷 level3",
        model="gpt-5.6-sol",
    )

    reopened = SessionStore(db_path)
    assert reopened.get_model_selection(session_id) == ("薄荷 level3", "gpt-5.6-sol")

    reopened.set_model_selection(session_id, provider=None, model=None)
    assert reopened.get_model_selection(session_id) is None


def test_load_messages_ignores_failed_cli_turns(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "leon.db")
    session_id = store.create_session()
    store.add_message(session_id, "user", "第一次请求")
    store.add_message(session_id, "assistant", "请求失败：InternalServerError: stale request")
    store.add_message(session_id, "user", "成功请求")
    store.add_message(session_id, "assistant", "正常答案")

    assert store.load_messages(session_id) == [
        {"role": "user", "content": "成功请求"},
        {"role": "assistant", "content": "正常答案"},
    ]
