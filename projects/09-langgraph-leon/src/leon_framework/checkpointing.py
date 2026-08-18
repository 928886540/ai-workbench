"""Encrypted SQLite checkpoint boundary for Leon Framework Edition.

LangGraph checkpoints contain the executable message state, including raw tool
results. Replacing that state with an audit projection would make resume
incorrect, so this module encrypts LangGraph's serialized checkpoint and
pending writes instead. SQLite routing metadata such as ``thread_id`` remains
plain text and must therefore stay opaque and non-sensitive.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

_CHECKPOINT_KEY_BYTES = 32


class CheckpointSecurityError(ValueError):
    """Checkpoint storage cannot be opened without weakening its safety boundary."""


class EncryptedSqliteSaver(SqliteSaver):
    """SqliteSaver that prevents caller metadata from bypassing blob encryption."""

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        configurable = config.get("configurable", {})
        storage_config = {
            **config,
            "metadata": {},
            "configurable": {
                key: value
                for key, value in configurable.items()
                if key in {"thread_id", "checkpoint_ns", "checkpoint_id"}
                or key.startswith("__")
            },
        }
        return super().put(storage_config, checkpoint, metadata, new_versions)


def checkpoint_key_path(database_path: str | Path) -> Path:
    """Return the sidecar key path without ever placing the key in SQLite."""

    path = Path(database_path).expanduser()
    return path.with_name(f"{path.name}.key")


def _read_checkpoint_key(key_path: Path) -> bytes:
    try:
        key = key_path.read_bytes()
    except OSError as exc:
        raise CheckpointSecurityError("Cannot read the checkpoint encryption key.") from exc
    if len(key) != _CHECKPOINT_KEY_BYTES:
        raise CheckpointSecurityError(
            f"Checkpoint encryption key must contain exactly {_CHECKPOINT_KEY_BYTES} bytes."
        )
    return key


def _load_or_create_checkpoint_key(database_path: Path, key_path: Path) -> bytes:
    if key_path.exists():
        return _read_checkpoint_key(key_path)
    if database_path.exists():
        raise CheckpointSecurityError(
            "Checkpoint encryption key is missing for an existing database."
        )

    key = secrets.token_bytes(_CHECKPOINT_KEY_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(key_path, flags, 0o600)
    except FileExistsError:
        return _read_checkpoint_key(key_path)
    except OSError as exc:
        raise CheckpointSecurityError("Cannot create the checkpoint encryption key.") from exc

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        key_path.chmod(0o600)
    except OSError as exc:
        try:
            key_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CheckpointSecurityError("Cannot persist the checkpoint encryption key.") from exc
    return key


def _validate_encrypted_rows(
    connection: sqlite3.Connection,
    serializer: EncryptedSerializer,
) -> None:
    for table, payload_column in (
        ("checkpoints", "checkpoint"),
        ("writes", "value"),
    ):
        invalid = connection.execute(
            f"SELECT 1 FROM {table} "  # noqa: S608 - table/column names are constants above
            "WHERE type IS NULL OR type NOT LIKE '%+aes' LIMIT 1"
        ).fetchone()
        if invalid is not None:
            raise CheckpointSecurityError(
                "Checkpoint database contains plaintext or unsupported legacy rows."
            )
        encrypted_row = connection.execute(
            f"SELECT type, {payload_column} FROM {table} LIMIT 1"  # noqa: S608
        ).fetchone()
        if encrypted_row is None:
            continue
        try:
            serializer.loads_typed((str(encrypted_row[0]), encrypted_row[1]))
        except Exception as exc:  # noqa: BLE001 - MAC/decode errors fail closed
            raise CheckpointSecurityError(
                "Checkpoint database cannot be decrypted with the configured key."
            ) from exc


@contextmanager
def open_encrypted_sqlite_checkpointer(
    database_path: str | Path,
    *,
    key_path: str | Path | None = None,
) -> Iterator[SqliteSaver]:
    """Open a full-fidelity LangGraph saver whose state blobs are AES encrypted."""

    resolved_database = Path(database_path).expanduser()
    resolved_key = (
        Path(key_path).expanduser()
        if key_path is not None
        else checkpoint_key_path(resolved_database)
    )
    if resolved_database.resolve() == resolved_key.resolve():
        raise CheckpointSecurityError(
            "Checkpoint database and encryption key must use different files."
        )
    try:
        resolved_database.parent.mkdir(parents=True, exist_ok=True)
        resolved_key.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CheckpointSecurityError("Cannot create the checkpoint storage directory.") from exc

    key = _load_or_create_checkpoint_key(resolved_database, resolved_key)
    serializer = EncryptedSerializer.from_pycryptodome_aes(key=key)
    try:
        connection = sqlite3.connect(str(resolved_database), check_same_thread=False)
    except sqlite3.Error as exc:
        raise CheckpointSecurityError("Cannot open the checkpoint database.") from exc

    checkpointer = EncryptedSqliteSaver(connection, serde=serializer)
    try:
        checkpointer.setup()
        _validate_encrypted_rows(connection, serializer)
        yield checkpointer
    finally:
        connection.close()
