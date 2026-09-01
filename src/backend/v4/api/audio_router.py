"""
Voice Live relay - WebSocket endpoint.

mode=voicelive (default): full duplex conversation with TTS audio output.
mode=dictation: STT only - user speech transcribed, no model response.

JSON events sent to browser (voicelive):
  { "type": "transcript_start" }
  { "type": "transcript",   "text": "..." }
  { "type": "transcript_end" }
  { "type": "barge_in_ack" }

JSON events sent to browser (dictation):
  { "type": "transcript",     "text": "..." }
  { "type": "transcript_end", "text": "..." }

The backend never touches local audio (no PyAudio). The browser is mic + speaker.
Azure Voice Live SDK handles VAD, barge-in, TTS, and STT server-side.
Credential: shared Managed Identity (audience ai.azure.com).
Endpoint/model/voice derive from app_config; same AI Services resource as agents.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from azure.ai.voicelive.aio import connect as vl_connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureStandardVoice,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
)
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from common.config.app_config import config

audio_router = APIRouter()


@audio_router.websocket("/audio/stream")
async def audio_stream(
    websocket: WebSocket,
    user_id: str = Query(None),  # noqa: ARG001
    mode: str = Query("voicelive"),
) -> None:
    """Voice Live relay.

    mode=voicelive: full duplex conversation, PCM16 audio in/out.
    mode=dictation: STT only via azure-speech, no model response, no TTS.
    """
    await websocket.accept()
    credential = config.get_shared_async_credential()
    is_dictation = mode == "dictation"

    try:
        async with vl_connect(
            endpoint=config.VOICE_LIVE_ENDPOINT,
            credential=credential,
            model=config.VOICE_LIVE_MODEL,
        ) as vl:
            if is_dictation:
                # Dictation: transcribe user speech, suppress model reply.
                # Text-only NO admite echo cancellation ni noise reduction — el server
                # las rechaza ("not supported when modalities is text-only") y tumba la sesión.
                await vl.session.update(
                    session=RequestSession(
                        modalities=[Modality.TEXT],
                        input_audio_format=InputAudioFormat.PCM16,
                        input_audio_transcription=AudioInputTranscriptionOptions(
                            model="azure-speech"
                        ),
                        turn_detection=ServerVad(
                            threshold=0.5,
                            prefix_padding_ms=300,
                            silence_duration_ms=500,
                            create_response=False,
                        ),
                    )
                )
            else:
                # VoiceLive: full duplex conversation.
                voice_name = config.VOICE_LIVE_VOICE
                voice_cfg: AzureStandardVoice | str = (
                    AzureStandardVoice(name=voice_name)
                    if "-" in voice_name
                    else voice_name
                )
                await vl.session.update(
                    session=RequestSession(
                        modalities=[Modality.TEXT, Modality.AUDIO],
                        voice=voice_cfg,
                        input_audio_format=InputAudioFormat.PCM16,
                        output_audio_format=OutputAudioFormat.PCM16,
                        turn_detection=ServerVad(
                            threshold=0.5,
                            prefix_padding_ms=300,
                            silence_duration_ms=500,
                        ),
                        input_audio_echo_cancellation=AudioEchoCancellation(),
                        input_audio_noise_reduction=AudioNoiseReduction(
                            type="azure_deep_noise_suppression"
                        ),
                    )
                )

            async def _browser_to_vl() -> None:
                try:
                    while True:
                        data = await websocket.receive()
                        if data.get("bytes"):
                            b64 = base64.b64encode(data["bytes"]).decode()
                            await vl.input_audio_buffer.append(audio=b64)
                        elif data.get("text") and not is_dictation:
                            msg = json.loads(data["text"])
                            if msg.get("type") == "barge_in":
                                try:
                                    await vl.response.cancel()
                                except Exception as exc:
                                    logging.debug(
                                        "[audio/stream] barge_in cancel failed (ignored): %s",
                                        exc,
                                    )
                                await websocket.send_text(
                                    json.dumps({"type": "barge_in_ack"})
                                )
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logging.error("[audio/stream] _browser_to_vl: %s", exc)

            async def _vl_to_browser() -> None:
                audio_frames = 0
                resp_text: list[str] = []
                try:
                    async for event in vl:
                        etype = event.type

                        if is_dictation:
                            # dictation: forward user speech transcription only
                            if (
                                etype
                                == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA
                            ):
                                delta = getattr(event, "delta", None)
                                if delta:
                                    await websocket.send_text(
                                        json.dumps({"type": "transcript", "text": delta})
                                    )

                            elif (
                                etype
                                == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
                            ):
                                transcript = getattr(event, "transcript", None)
                                await websocket.send_text(
                                    json.dumps(
                                        {
                                            "type": "transcript_end",
                                            "text": transcript or "",
                                        }
                                    )
                                )

                            elif (
                                etype
                                == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED
                            ):
                                err = getattr(event, "error", None)
                                logging.warning(
                                    "[audio/stream] dictation transcription failed: %s",
                                    err,
                                )

                        else:
                            # voicelive: full duplex events
                            if etype == ServerEventType.RESPONSE_AUDIO_DELTA:
                                delta = getattr(event, "delta", None)
                                if delta:
                                    audio_frames += 1
                                    await websocket.send_bytes(delta)

                            elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
                                delta = getattr(event, "delta", None)
                                if delta:
                                    resp_text.append(delta)
                                    await websocket.send_text(
                                        json.dumps({"type": "transcript", "text": delta})
                                    )

                            elif etype == ServerEventType.RESPONSE_CREATED:
                                audio_frames = 0
                                resp_text = []
                                logging.info(
                                    "[audio/stream] 🔊 Voice Live: modelo RESPONDIENDO"
                                )
                                await websocket.send_text(
                                    json.dumps({"type": "transcript_start"})
                                )

                            elif etype == ServerEventType.RESPONSE_DONE:
                                logging.info(
                                    '[audio/stream] ✅ modelo terminó: %d audio frames — "%s"',
                                    audio_frames,
                                    "".join(resp_text)[:150],
                                )
                                await websocket.send_text(
                                    json.dumps({"type": "transcript_end"})
                                )

                            elif (
                                etype
                                == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED
                            ):
                                try:
                                    await vl.response.cancel()
                                except Exception as exc:
                                    logging.debug(
                                        "[audio/stream] Voice Live cancel failed during barge-in: %s",
                                        exc,
                                    )
                                await websocket.send_text(
                                    json.dumps({"type": "barge_in_ack"})
                                )

                        # error event applies to both modes
                        if etype == ServerEventType.ERROR:
                            err = getattr(event, "error", None)
                            msg_txt = (
                                getattr(err, "message", str(event))
                                if err
                                else str(event)
                            )
                            logging.error("[audio/stream] VoiceLive error: %s", msg_txt)

                except Exception as exc:
                    logging.error("[audio/stream] _vl_to_browser: %s", exc)

            # Cuando una tarea termina (el browser corta, o la sesión muere), cancelar
            # la otra — si no, _vl_to_browser sigue mandando tras el close (error ASGI
            # "websocket.send after websocket.close").
            t_in = asyncio.create_task(_browser_to_vl())
            t_out = asyncio.create_task(_vl_to_browser())
            _done, pending = await asyncio.wait(
                {t_in, t_out}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logging.error("[audio/stream] session error: %s", exc)
        try:
            await websocket.close(1011)
        except Exception:
            pass
