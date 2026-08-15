from __future__ import annotations

import json

import httpx
from leon_agent.voice_client import VolinkVoiceClient, prepare_speech_text

SAMPLE_TEXT = """✅ 已用「玛莉卡」模式（k2queenmarika）提交生成一张极光旅游照片～  提交信息：
- 任务状态：排队中
- 任务 ID：2668cea0b6124e1983e10128ff6c4d72
- 生成计划 ID：leonpluginmsu3fzdyrjid8hoi 等渲染完成后我会把图片给你看，也可以随时问我查看进度～"""


def test_prepare_speech_text_removes_list_markers_and_internal_ids() -> None:
    spoken = prepare_speech_text(SAMPLE_TEXT)

    assert spoken == (
        "已用「玛莉卡」模式提交生成一张极光旅游照片～，任务状态：排队中，"
        "等渲染完成后我会把图片给你看，也可以随时问我查看进度～"
    )
    for hidden in (
        "✅",
        "-",
        "k2queenmarika",
        "2668cea0b6124e1983e10128ff6c4d72",
        "leonpluginmsu3fzdyrjid8hoi",
    ):
        assert hidden not in spoken


def test_synthesize_sends_only_prepared_text_to_volink() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=b"mp3")

    client = VolinkVoiceClient(
        api_key="test-key",
        base_url="https://volink.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    audio = client.synthesize(
        text=SAMPLE_TEXT,
        voice_id="689334e84d3396ad1d28ee9e",
        model="index-tts2",
    )

    assert audio == b"mp3"
    assert seen["input"] == prepare_speech_text(SAMPLE_TEXT)
