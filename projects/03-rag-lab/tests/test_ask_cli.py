from __future__ import annotations

from rag_lab.ask_cli import main


def test_rag_ask_defaults_to_fake_providers(capsys) -> None:  # noqa: ANN001
    exit_code = main(["RAG 的实现顺序是什么？"])

    output = capsys.readouterr()
    assert exit_code == 0
    assert "embedding_provider=fake:" in output.out
    assert "answer_provider=fake:" in output.out
    assert "[CITE:" in output.out
    assert "citation_precision=1.000" in output.out


def test_rag_ask_requires_live_opt_in_for_judge(capsys) -> None:  # noqa: ANN001
    exit_code = main(["question", "--judge"])

    output = capsys.readouterr()
    assert exit_code == 2
    assert "requires explicit --live" in output.err


def test_rag_ask_requires_live_opt_in_for_reranker(capsys) -> None:  # noqa: ANN001
    exit_code = main(["question", "--rerank"])

    output = capsys.readouterr()
    assert exit_code == 2
    assert "requires explicit --live" in output.err
