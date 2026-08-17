from __future__ import annotations

from rag_lab.cli import main


def test_rag_eval_requires_live_opt_in_for_reranker(capsys) -> None:  # noqa: ANN001
    exit_code = main(["--rerank"])

    output = capsys.readouterr()
    assert exit_code == 2
    assert "requires explicit --live" in output.err
