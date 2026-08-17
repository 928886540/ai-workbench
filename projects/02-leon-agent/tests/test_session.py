import sqlite3
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


def test_session_store_lists_pinned_sessions_first_with_topic(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    store = SessionStore(db_path)
    first_session = store.create_session()
    second_session = store.create_session()
    store.add_message(first_session, "user", "  第一段\n会话主题  ")
    store.add_message(first_session, "assistant", "第一段回复")
    store.add_message(second_session, "user", "最近的会话")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (100, first_session)
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (200, second_session)
        )

    assert store.set_session_pinned(first_session, True) is True
    assert store.set_session_pinned("missing", True) is False
    reopened = SessionStore(db_path)
    sessions = reopened.list_sessions()

    assert [item["id"] for item in sessions] == [first_session, second_session]
    assert sessions[0]["pinned"] is True
    assert sessions[0]["title"] == "第一段 会话主题"
    assert sessions[0]["message_count"] == 2
    assert sessions[1]["pinned"] is False

    assert reopened.set_session_pinned(first_session, False) is True
    assert [item["id"] for item in reopened.list_sessions()] == [
        second_session,
        first_session,
    ]


def test_session_store_migrates_legacy_database_for_pinning(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                llm_provider TEXT,
                llm_model TEXT,
                llm_base_url TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
            ("legacy-session", 1, 2),
        )

    store = SessionStore(db_path)
    assert store.list_sessions() == [
        {
            "id": "legacy-session",
            "created_at": 1,
            "updated_at": 2,
            "message_count": 0,
            "title": "新会话",
            "pinned": False,
        }
    ]
    assert store.set_session_pinned("legacy-session", True) is True
    assert SessionStore(db_path).list_sessions()[0]["pinned"] is True


def test_session_store_persists_voice_attachments_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "voice.db"
    store = SessionStore(db_path)
    session_id = store.create_session()

    clip = store.add_voice_clip(
        session_id,
        text="这是持久化语音",
        voice_id="voice-selected",
        voice_name="测试音色",
        audio=b"fake-mp3",
    )

    reopened = SessionStore(db_path)
    assert reopened.load_voice_clips(session_id) == [
        {
            "clip_id": clip["clip_id"],
            "text": "这是持久化语音",
            "voice_id": "voice-selected",
            "voice_name": "测试音色",
            "bytes": len(b"fake-mp3"),
            "created_at": clip["created_at"],
        }
    ]
    assert reopened.get_voice_clip_audio(str(clip["clip_id"])) == b"fake-mp3"
    assert "audio" not in reopened.load_voice_clips(session_id)[0]


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


def test_replace_latest_assistant_keeps_one_visible_turn(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "retry.db")
    session_id = store.create_session()
    store.add_message(session_id, "user", "再试一次")
    store.add_message(session_id, "assistant", "旧回答")

    store.replace_latest_user(session_id, "编辑后的问题")
    store.replace_latest_assistant(session_id, "新回答")

    assert store.load_messages(session_id) == [
        {"role": "user", "content": "编辑后的问题"},
        {"role": "assistant", "content": "新回答"},
    ]


def test_assistant_revisions_survive_store_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "revisions.db"
    store = SessionStore(db_path)
    session_id = store.create_session()
    store.add_message(session_id, "user", "重试这个问题")
    store.add_message(session_id, "assistant", "第一版")
    store.replace_latest_assistant(session_id, "第二版")

    reopened = SessionStore(db_path)
    reopened.replace_latest_assistant(session_id, "第三版")
    history = reopened.load_messages(session_id, include_created_at=True)

    assert history[-1]["content"] == "第三版"
    assert [revision["content"] for revision in history[-1]["revisions"]] == [
        "第一版",
        "第二版",
    ]
    assert all(revision["created_at"] > 0 for revision in history[-1]["revisions"])
