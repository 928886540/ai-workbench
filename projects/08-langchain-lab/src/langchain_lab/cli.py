"""Provider-free component demo plus an explicitly selected live path."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from langchain_lab.model import build_chat_model
from langchain_lab.structured_output import build_triage_chain

_INCIDENT = "支付服务连续超时，刚发布过版本。"
_FAKE_RESPONSE = {
    "severity": "high",
    "service": "payment",
    "summary": "支付服务在发布后出现连续超时。",
    "needs_runbook": True,
    "next_step": "核对发布变更、错误率和依赖健康度。",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the small LangChain component lab.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="run with a fake model")
    mode.add_argument("--live", action="store_true", help="use shared provider settings")
    parser.add_argument("--incident", default=_INCIDENT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    model = (
        build_chat_model()
        if args.live
        else FakeListChatModel(responses=[json.dumps(_FAKE_RESPONSE, ensure_ascii=False)])
    )
    result = build_triage_chain(model).invoke({"incident_text": args.incident})
    print(result.model_dump_json(indent=2))
    return 0
