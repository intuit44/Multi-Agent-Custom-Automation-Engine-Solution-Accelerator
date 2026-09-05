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

JSON commands from browser (voicelive) — tres carriles, orden determinista:
  { "type": "ack",   "user_text" }  carril 1: acuse generado por el modelo a
                                     partir del enunciado del usuario (no plantilla).
  { "type": "say",   "text" }       carril 2: narración de tools. TTS literal.
  { "type": "speak", "text", "acked", "narrated" }
                                     carril 3: contenido final del router,
                                     parafraseado, excluyendo lo ya dicho en 1 y 2.
  { "type": "barge_in" }             cancela la respuesta activa.
Cada say/speak cancela la respuesta activa anterior (Voice Live admite una).

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
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

from common.config.app_config import config

audio_router = APIRouter()


@audio_router.post("/audio/diag")
async def audio_diag(request: Request) -> dict:
    """Beacon de diagnóstico del cliente de voz (navigator.sendBeacon).

    El frontend lo dispara en ws.onclose / pagehide / mic_track_ended con
    {ev, code, reason, ctx, sampleRate, audioSessionApi, bundle}. Se loguea en
    WARNING para que aparezca destacado en el log del Container App: una sola
    prueba desde el dispositivo deja registrado QUIÉN cerró el WS y con qué
    código, sin Web Inspector. sendBeacon manda text/plain (simple request,
    sin preflight CORS) y sobrevive a la navegación de la página — captura
    incluso el caso reauthSilently() → redirect.
    """
    body = await request.body()
    try:
        payload: object = json.loads(body or b"{}")
    except Exception:
        payload = {"raw": body[:300].decode(errors="replace")}
    logging.warning("[audio/diag] %s", payload)
    return {"ok": True}


async def _cancel_active_response(vl) -> None:
    """Best-effort cancel of the in-flight Voice Live response.

    Voice Live admite UNA respuesta activa por sesión. Antes de `response.create`
    (say/speak) cancelamos la anterior: si no hay ninguna el server responde con
    un error inocuo que se ignora.
    """
    try:
        await vl.response.cancel()
    except Exception as exc:
        logging.debug("[audio/stream] response.cancel (ignored): %s", exc)


@audio_router.websocket("/audio/stream")
async def audio_stream(
    websocket: WebSocket,
    user_id: str = Query(None),  # noqa: ARG001
    mode: str = Query("voicelive"),
    audio: str = Query("binary"),
) -> None:
    """Voice Live relay.

    mode=voicelive: full duplex conversation, PCM16 audio in/out.
    mode=dictation: STT only via azure-speech, no model response, no TTS.
    audio=b64: TTS como {"type":"audio_chunk","data":<base64>} en frames de
    TEXTO en vez de binarios. En el camino del dispositivo iOS los frames
    binarios matan el WS (1006 al primer frame; el mismo stream por WebKit/
    Node llega entero) — un middlebox local (VPN/bloqueador) o el stack del
    device tolera texto y corta binario. El frontend ya reproduce ambos.
    """
    await websocket.accept()
    credential = config.get_shared_async_credential()
    is_dictation = mode == "dictation"
    audio_b64 = audio == "b64"

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
                # VoiceLive GATEWAY: el modelo NO responde solo (create_response=False).
                # Flujo: STT del usuario → user_transcript → frontend lo manda al
                # MODEL ROUTER (misma lógica que texto escrito) → la respuesta final
                # vuelve por {type:"speak"} y Voice Live SOLO la verbaliza (TTS).
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
                        input_audio_transcription=AudioInputTranscriptionOptions(
                            model="azure-speech"
                        ),
                        turn_detection=ServerVad(
                            threshold=0.5,
                            prefix_padding_ms=300,
                            silence_duration_ms=500,
                            create_response=False,
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
                        # receive() crudo NO lanza WebSocketDisconnect: DEVUELVE
                        # {"type": "websocket.disconnect"}. Sin este break, la
                        # próxima iteración vuelve a llamar receive() →
                        # RuntimeError 'Cannot call "receive" once a disconnect
                        # message has been received' (el ERROR visto en prod).
                        if data.get("type") == "websocket.disconnect":
                            break
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
                            elif msg.get("type") == "ack":
                                # Carril 1 (acuse): lo GENERA el modelo a partir del
                                # enunciado real del usuario — no es plantilla. Un
                                # "hola" recibe un saludo; "¿en qué quedamos?" recibe
                                # un acuse coherente con eso. Brevísimo y sin responder
                                # de fondo: el contenido llega por 'speak'.
                                user_text = str(msg.get("user_text") or "")[:400]
                                await _cancel_active_response(vl)
                                await vl.response.create(
                                    response={
                                        "modalities": ["audio"],
                                        "instructions": (
                                            "El usuario acaba de decir lo siguiente y "
                                            "su petición se está procesando en segundo "
                                            "plano. Responde con UNA frase muy breve "
                                            "(máximo 10 palabras) que acuse recibo de "
                                            "forma natural y específica a lo que dijo, "
                                            "en su mismo idioma. Si es un saludo, "
                                            "saluda; si es una pregunta, indica que lo "
                                            "revisas. NO respondas la pregunta de fondo "
                                            "ni inventes datos.\n\n"
                                            f"USUARIO: {user_text}"
                                        ),
                                    }
                                )
                            elif msg.get("type") == "say" and msg.get("text"):
                                # Carril 2 (narración de tools): frase corta derivada
                                # del nombre real de la tool, TTS LITERAL, sin
                                # parafraseo. Cancela cualquier respuesta activa:
                                # Voice Live sólo admite una a la vez.
                                await _cancel_active_response(vl)
                                await vl.response.create(
                                    response={
                                        "modalities": ["audio"],
                                        "instructions": (
                                            "Di exactamente la siguiente frase, sin "
                                            "añadir, quitar ni comentar nada:\n"
                                            f"{str(msg['text'])[:200]}"
                                        ),
                                    }
                                )
                            elif msg.get("type") == "speak" and msg.get("text"):
                                # Carril 3 (contenido final): verbalizar la respuesta
                                # del MODEL ROUTER. instructions (no verbatim) →
                                # parafraseo natural. Único carril con parafraseo.
                                # Lo ya dicho en carriles 1 y 2 (acuse, "Consultando
                                # X") se pasa como exclusión: esos puestos ya se
                                # ocuparon en tiempo real, no se vuelven a narrar.
                                narrated = [
                                    str(n) for n in (msg.get("narrated") or []) if n
                                ]
                                acked = bool(msg.get("acked"))
                                exclusions: list[str] = []
                                if acked:
                                    exclusions.append(
                                        "Ya se dio un acuse de recibo al usuario: no "
                                        "saludes ni vuelvas a acusar; entra directo al "
                                        "contenido."
                                    )
                                if narrated:
                                    exclusions.append(
                                        "Ya se anunció en voz alta que se consultaron: "
                                        + "; ".join(narrated)
                                        + ". No repitas qué herramientas o fuentes se "
                                        "consultaron ni el proceso: ve a los resultados."
                                    )
                                exclusion_block = (
                                    (
                                        "\n\nYA DICHO (no repetir):\n- "
                                        + "\n- ".join(exclusions)
                                    )
                                    if exclusions
                                    else ""
                                )
                                await _cancel_active_response(vl)
                                await vl.response.create(
                                    response={
                                        "modalities": ["audio"],
                                        "instructions": (
                                            "Transmite el siguiente contenido en voz alta, "
                                            "de forma natural y conversacional, en el mismo "
                                            "idioma del contenido. No leas símbolos de "
                                            "Markdown, código ni URLs literalmente: "
                                            "descríbelos brevemente si aportan. No inventes "
                                            "información que no esté en el contenido."
                                            f"{exclusion_block}\n\n"
                                            f"CONTENIDO:\n{msg['text']}"
                                        ),
                                    }
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
                                        json.dumps(
                                            {"type": "transcript", "text": delta}
                                        )
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
                            # voicelive gateway events
                            if (
                                etype
                                == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
                            ):
                                # Lo que DIJO el usuario → el frontend lo envía al
                                # MODEL ROUTER como un mensaje normal de chat.
                                transcript = getattr(event, "transcript", None)
                                if transcript:
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "type": "user_transcript",
                                                "text": transcript,
                                            }
                                        )
                                    )

                            elif etype == ServerEventType.RESPONSE_AUDIO_DELTA:
                                delta = getattr(event, "delta", None)
                                if delta:
                                    audio_frames += 1
                                    if audio_b64:
                                        b64 = (
                                            base64.b64encode(delta).decode()
                                            if isinstance(delta, bytes)
                                            else delta
                                            + "=" * ((4 - len(delta) % 4) % 4)
                                        )
                                        await websocket.send_text(
                                            json.dumps(
                                                {
                                                    "type": "audio_chunk",
                                                    "data": b64,
                                                }
                                            )
                                        )
                                    elif isinstance(delta, bytes):
                                        await websocket.send_bytes(delta)
                                    else:
                                        padded = (
                                            delta + "=="[: (4 - len(delta) % 4) % 4]
                                        )
                                        await websocket.send_bytes(
                                            base64.b64decode(padded)
                                        )

                            elif (
                                etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA
                            ):
                                delta = getattr(event, "delta", None)
                                if delta:
                                    resp_text.append(delta)
                                    await websocket.send_text(
                                        json.dumps(
                                            {"type": "transcript", "text": delta}
                                        )
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
            await asyncio.gather(*pending, return_exceptions=True)

    except WebSocketDisconnect:
        logging.debug("[audio/stream] client disconnected; closing stream handler")
    except Exception as exc:
        logging.error("[audio/stream] session error: %s", exc)
        try:
            await websocket.close(1011)
        except Exception as close_exc:
            # Best-effort close during error handling; do not mask the original failure.
            logging.debug(
                "[audio/stream] websocket close failed during error cleanup: %s",
                close_exc,
                exc_info=True,
            )
