import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from v4.api import audio_router


class FakeWebSocket:
    def __init__(self, incoming: list[dict[str, str]]):
        self.incoming = iter(incoming)
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.transcript_sent = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict[str, str]:
        try:
            return next(self.incoming)
        except StopIteration:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()

    async def send_text(self, value: str) -> None:
        self.sent_text.append(value)
        if json.loads(value).get("type") == "user_transcript":
            self.transcript_sent.set()

    async def send_bytes(self, value: bytes) -> None:
        self.sent_bytes.append(value)

    async def close(self, code: int) -> None:
        pass


class FakeVoiceLive:
    def __init__(self, events=()):
        self.events = events
        self.session = AsyncMock()
        self.input_audio_buffer = AsyncMock()
        self.response = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_audio_stream_speak_command_creates_audio_response():
    websocket = FakeWebSocket([{"text": '{"type":"speak","text":"Hello"}'}])
    voice_live = FakeVoiceLive()

    with (
        patch.object(audio_router.config, "get_shared_async_credential"),
        patch.object(audio_router, "vl_connect", return_value=voice_live),
    ):
        await audio_router.audio_stream(websocket)

    voice_live.response.create.assert_awaited_once()
    response = voice_live.response.create.await_args.kwargs["response"]
    assert response["modalities"] == ["audio"]
    assert "Hello" in response["instructions"]


@pytest.mark.asyncio
async def test_audio_stream_forwards_user_transcript_as_json_event():
    websocket = FakeWebSocket([])
    event = type(
        "TranscriptionCompleted",
        (),
        {
            "type": audio_router.ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
            "transcript": "What is the status?",
        },
    )()
    voice_live = FakeVoiceLive(iter([event]))

    async def receive_after_transcript():
        await websocket.transcript_sent.wait()
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect()

    websocket.receive = receive_after_transcript

    with (
        patch.object(audio_router.config, "get_shared_async_credential"),
        patch.object(audio_router, "vl_connect", return_value=voice_live),
    ):
        await audio_router.audio_stream(websocket)

    assert websocket.sent_text == [
        json.dumps({"type": "user_transcript", "text": "What is the status?"})
    ]
