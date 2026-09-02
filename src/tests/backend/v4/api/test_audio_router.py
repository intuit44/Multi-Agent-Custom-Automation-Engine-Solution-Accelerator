import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.v4.api import audio_router
from azure.ai.voicelive.models import ServerEventType


class _WebSocket:
    def __init__(self, *messages):
        self.messages = iter(messages)
        self.sent_text: list[str] = []

    async def accept(self):
        pass

    async def receive(self):
        try:
            return next(self.messages)
        except StopIteration:
            await asyncio.Future()
            raise RuntimeError(
                "Unreachable: receive() resumed after waiting forever"
            )

    async def send_text(self, message):
        self.sent_text.append(message)

    async def send_bytes(self, _message):
        pass


class _VoiceLive:
    def __init__(self, events=()):
        self.events = events
        self.session = SimpleNamespace(update=AsyncMock())
        self.response = SimpleNamespace(
            create=AsyncMock(), cancel=AsyncMock()
        )
        self.input_audio_buffer = SimpleNamespace(append=AsyncMock())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def __aiter__(self):
        async def events():
            for event in self.events:
                yield event

        return events()


@pytest.mark.asyncio
async def test_audio_stream_speak_requests_audio_response():
    websocket = _WebSocket(
        {"text": json.dumps({"type": "speak", "text": "Hello"})},
    )
    voice_live = _VoiceLive()

    with (
        patch.object(audio_router.config, "get_shared_async_credential"),
        patch.object(audio_router, "vl_connect", return_value=voice_live),
    ):
        await audio_router.audio_stream(websocket)

    voice_live.response.create.assert_awaited_once()
    request = voice_live.response.create.await_args.kwargs["response"]
    assert request["modalities"] == ["audio"]
    assert "Hello" in request["instructions"]


@pytest.mark.asyncio
async def test_audio_stream_forwards_user_transcript():
    websocket = _WebSocket()
    voice_live = _VoiceLive(
        [
            SimpleNamespace(
                type=ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
                transcript="What time is it?",
            )
        ]
    )

    with (
        patch.object(audio_router.config, "get_shared_async_credential"),
        patch.object(audio_router, "vl_connect", return_value=voice_live),
    ):
        await audio_router.audio_stream(websocket)

    assert websocket.sent_text == [
        json.dumps({"type": "user_transcript", "text": "What time is it?"})
    ]
