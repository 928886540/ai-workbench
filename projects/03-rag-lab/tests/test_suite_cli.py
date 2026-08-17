from __future__ import annotations

from rag_lab.suite_cli import main


def test_rag_suite_cli_defaults_to_fake_providers(capsys) -> None:  # noqa: ANN001
    exit_code = main(["--case-id", "eval-vs-pytest"])

    output = capsys.readouterr()
    assert exit_code == 0
    assert "embedding_provider=fake:" in output.out
    assert "cases=1" in output.out
    assert "faithfulness=n/a" in output.out


def test_rag_suite_cli_requires_live_opt_in_for_judge(capsys) -> None:  # noqa: ANN001
    exit_code = main(["--judge"])

    output = capsys.readouterr()
    assert exit_code == 2
    assert "requires explicit --live" in output.err


def test_rag_suite_cli_requires_live_opt_in_for_reranker(capsys) -> None:  # noqa: ANN001
    exit_code = main(["--rerank"])

    output = capsys.readouterr()
    assert exit_code == 2
    assert "requires explicit --live" in output.err
