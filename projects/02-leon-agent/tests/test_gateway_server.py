"""Tests for the ``leon-server`` process boundary."""

from __future__ import annotations

import sys

import pytest
from leon_agent.gateway.server import main


def test_server_rejects_multiple_workers(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["leon-server", "--workers", "2"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "only supports one worker" in capsys.readouterr().err
