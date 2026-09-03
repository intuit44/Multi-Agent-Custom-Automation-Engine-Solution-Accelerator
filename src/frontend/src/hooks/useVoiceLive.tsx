/**
 * useVoiceLive — Voice Gateway alrededor del MODEL ROUTER (no paralelo a él)
 *
 * Usuario ──voz──▶ STT (Voice Live, sin auto-respuesta) ──▶ user_transcript
 *   ──▶ onUserTranscript(texto) → el composer lo envía al MODEL ROUTER (mismo
 *   flujo que texto escrito: SSE → mensaje en el DOM). Al terminar el stream,
 *   el composer llama voiceLiveSpeak(respuestaFinal) → TTS → playback.
 *
 * browser → backend (binario): PCM16 LE mono 24 kHz, ~20 ms chunks
 * browser → backend (JSON):   { type:"speak", text } · { type:"barge_in" }
 * backend → browser:
 *   { type:"user_transcript", text } — lo que dijo el usuario (STT)
 *   { type:"barge_in_ack" }          — servidor confirmó barge-in
 *   ArrayBuffer                      — TTS PCM16 binario
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getApiUrl, getUserId } from '../api/config';

// ---------------------------------------------------------------------------
// AudioWorklet processor inlined como blob — Float32 → PCM16 LE
// ---------------------------------------------------------------------------
const WORKLET_SRC = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0]?.[0];
    if (!ch) return true;
    const pcm = new Int16Array(ch.length);
    for (let i = 0; i < ch.length; i++)
      pcm[i] = Math.max(-32768, Math.min(32767, ch[i] * 32767));
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}
registerProcessor('pcm-capture', PcmCaptureProcessor);
`;

function createWorkletUrl() {
  return URL.createObjectURL(
    new Blob([WORKLET_SRC], { type: 'application/javascript' })
  );
}

// ---------------------------------------------------------------------------
// WS URL — mismo patrón que WebSocketService.buildSocketUrl
// ---------------------------------------------------------------------------
function buildAudioSocketUrl(): string {
  const baseUrl = getApiUrl() || '';
  let base = baseUrl.trim().replace(/\/+$/, '');
  if (base.startsWith('/')) {
    const wsOrigin = window.location.origin
      .replace(/^http:\/\//i, 'ws://')
      .replace(/^https:\/\//i, 'wss://');
    base = `${wsOrigin}${base}`;
  } else {
    base = base
      .replace(/^http:\/\//i, 'ws://')
      .replace(/^https:\/\//i, 'wss://');
  }
  const hasApi = /\/api(\/|$)/i.test(base);
  const path = hasApi ? '/v4/audio/stream' : '/api/v4/audio/stream';
  const userId = encodeURIComponent(getUserId() || '');
  return `${base}${path}?user_id=${userId}`;
}

// ---------------------------------------------------------------------------
// Singleton: permite a los composers verbalizar sin tener el hook en scope y
// saber si hay sesión de voz. ÚNICO dueño del ciclo de turno de voz.
//
// Contrato de turno (determinista por construcción):
//   user_transcript ──▶ turno ABIERTO (id++)
//     carril 1  voiceLiveAck()      acuse corto, TTS literal, 1 por turno
//     carril 2  voiceLiveNarrate()  "Consultando X…" por tool, TTS literal
//     carril 3  voiceLiveSpeak()    contenido final del router, parafraseo, 1 por turno
//   barge_in_ack ──▶ turno CANCELADO: se corta playback, se invalida el turno
//     (un speak tardío del turno viejo NO habla) y se avisa al composer para
//     que aborte el SSE y cierre la burbuja. El siguiente user_transcript abre
//     un turno NUEVO → burbuja nueva, nunca se anexa a la anterior.
// ---------------------------------------------------------------------------
let activeVoiceWs: WebSocket | null = null;

type VoiceTurn = {
  id: number;
  open: boolean; // true entre user_transcript y speak (o barge-in)
  acked: boolean; // carril 1 ya emitido
  narrated: Set<string>; // carril 2: tools ya narradas en este turno
};
let voiceTurn: VoiceTurn = {
  id: 0,
  open: false,
  acked: false,
  narrated: new Set(),
};

/** Composers registran acá qué hacer cuando el usuario interrumpe (barge-in). */
const bargeInListeners = new Set<(turnId: number) => void>();

const ACK_PHRASES = [
  'Dame un segundo, lo reviso.',
  'Un momento, déjame revisar.',
  'Ok, lo estoy mirando.',
];

/** Nombre humano y corto para narrar una tool (sin párrafos, sin parafraseo). */
function humanizeTool(tool: string, server?: string): string {
  const t = (tool || '').replace(/[_-]+/g, ' ').trim();
  if (!t) return server ? `Consultando ${server}` : 'Consultando';
  return server ? `Consultando ${t} en ${server}` : `Consultando ${t}`;
}

function sendJson(payload: Record<string, unknown>): boolean {
  if (activeVoiceWs?.readyState !== WebSocket.OPEN) return false;
  activeVoiceWs.send(JSON.stringify(payload));
  return true;
}

function openVoiceTurn(): number {
  voiceTurn = {
    id: voiceTurn.id + 1,
    open: true,
    acked: false,
    narrated: new Set(),
  };
  return voiceTurn.id;
}

function cancelVoiceTurn(): void {
  voiceTurn = { ...voiceTurn, open: false };
}

export function isVoiceLiveActive(): boolean {
  return activeVoiceWs?.readyState === WebSocket.OPEN;
}

/** Id del turno de voz en curso (0 = ninguno). Útil para descartar resultados tardíos. */
export function currentVoiceTurnId(): number {
  return voiceTurn.open ? voiceTurn.id : 0;
}

/** Suscribirse al barge-in del usuario. Devuelve el unsubscribe. */
export function onVoiceBargeIn(cb: (turnId: number) => void): () => void {
  bargeInListeners.add(cb);
  return () => bargeInListeners.delete(cb);
}

/** Carril 1 — acuse inmediato. Una vez por turno. Plantilla, TTS literal. */
export function voiceLiveAck(): void {
  if (!voiceTurn.open || voiceTurn.acked) return;
  voiceTurn.acked = true;
  const phrase = ACK_PHRASES[voiceTurn.id % ACK_PHRASES.length];
  sendJson({ type: 'say', text: phrase });
}

/** Carril 2 — narración de tool en el momento. Dedupe por tool en el turno. */
export function voiceLiveNarrate(tool: string, server?: string): void {
  if (!voiceTurn.open) return;
  const key = `${server ?? ''}/${tool}`;
  if (voiceTurn.narrated.has(key)) return;
  voiceTurn.narrated.add(key);
  sendJson({ type: 'say', text: humanizeTool(tool, server) });
}

/** Carril 3 — contenido final del MODEL ROUTER (parafraseo). Un solo speak por turno. */
export function voiceLiveSpeak(text: string): void {
  if (!text) return;
  if (!voiceTurn.open) return; // sin turno abierto (2º caller, tecleado, o cancelado) → no vocea
  voiceTurn = { ...voiceTurn, open: false }; // consumir el turno
  sendJson({ type: 'speak', text });
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------
export function useVoiceLive(onUserTranscript?: (text: string) => void) {
  const [recording, setRecording] = useState(false);
  const onUserTranscriptRef = useRef(onUserTranscript);
  onUserTranscriptRef.current = onUserTranscript;

  const wsRef = useRef<WebSocket | null>(null);
  const actxRef = useRef<AudioContext | null>(null);
  const workletUrlRef = useRef<string | null>(null);
  const playingRef = useRef(false);
  const playQueueRef = useRef<ArrayBuffer[]>([]);
  // Diagnóstico: localizar el fallo (captura vs recepción vs playback).
  const statsRef = useRef({ sent: 0, recvA: 0, recvT: 0, played: 0 });
  const statsIvRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Guard SÍNCRONO contra doble-start: el estado `recording` sigue false durante
  // el setup async (~3s de getUserMedia+WS+AudioContext), así que un 2º clic se
  // colaría y abriría una 2ª sesión huérfana. Un ref se ve al instante.
  const activeRef = useRef(false);

  // ---- playback -----------------------------------------------------------
  const drainQueueRef = useRef<() => void>(() => {});
  const drainQueue = useCallback(() => {
    if (!playQueueRef.current.length) {
      playingRef.current = false;
      return;
    }
    const chunk = playQueueRef.current.shift()!;
    // Reusar el AudioContext de CAPTURA (nace en el gesto del clic → activo). Un
    // context NUEVO creado acá (al recibir audio, sin gesto) queda SUSPENDIDO por
    // autoplay: src.start() cuenta (played++) pero NO sale sonido. Ese era el bug.
    const ctx = actxRef.current;
    if (!ctx) {
      playingRef.current = false;
      return;
    }
    if (ctx.state === 'suspended') void ctx.resume();
    const pcm = new Int16Array(chunk);
    const float = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) float[i] = pcm[i] / 32768;
    const buf = ctx.createBuffer(1, float.length, 24000);
    buf.copyToChannel(float, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.onended = () => drainQueueRef.current();
    src.start();
    statsRef.current.played++;
    playingRef.current = true;
  }, []);
  drainQueueRef.current = drainQueue;

  const enqueueAudio = useCallback(
    (b64: string) => {
      const raw = atob(b64);
      const buf = new ArrayBuffer(raw.length);
      const view = new Uint8Array(buf);
      for (let i = 0; i < raw.length; i++) view[i] = raw.charCodeAt(i);
      playQueueRef.current.push(buf);
      if (!playingRef.current) drainQueue();
    },
    [drainQueue]
  );

  const stopPlayback = useCallback(() => {
    // No cerramos el context acá: es el de captura (actxRef), lo cierra stop().
    playQueueRef.current = [];
    playingRef.current = false;
  }, []);

  // ---- stop capture -------------------------------------------------------
  const stop = useCallback(() => {
    activeRef.current = false;
    if (statsIvRef.current) {
      clearInterval(statsIvRef.current);
      statsIvRef.current = null;
    }
    actxRef.current?.close().catch(() => {});
    actxRef.current = null;
    if (workletUrlRef.current) {
      URL.revokeObjectURL(workletUrlRef.current);
      workletUrlRef.current = null;
    }
    cancelVoiceTurn();

    if (
      wsRef.current &&
      wsRef.current.readyState !== WebSocket.CLOSING &&
      wsRef.current.readyState !== WebSocket.CLOSED
    ) {
      wsRef.current.close(1000);
    }
    if (activeVoiceWs === wsRef.current) activeVoiceWs = null;
    wsRef.current = null;
    stopPlayback();
    setRecording(false);
  }, [stopPlayback]);

  // ---- start capture -------------------------------------------------------
  const start = useCallback(async () => {
    if (recording || activeRef.current) return; // ref = síncrono, corta el doble-clic
    activeRef.current = true;
    console.log('[VL] start');
    try {
      // Crear + reanudar el AudioContext DENTRO del gesto del clic (antes de todo
      // await). Si se crea después de getUserMedia queda SUSPENDIDO por autoplay y
      // el playback no suena aunque src.start() se llame (played++ pero silencio).
      const actx = new AudioContext({ sampleRate: 24000 });
      actxRef.current = actx;
      if (actx.state === 'suspended') await actx.resume();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ws = new WebSocket(buildAudioSocketUrl());
      ws.binaryType = 'arraybuffer'; // el TTS binario llega como ArrayBuffer, no Blob
      wsRef.current = ws;
      activeVoiceWs = ws;
      ws.addEventListener('close', () => {
        if (wsRef.current === ws) stop();
      });

      ws.onmessage = (e) => {
        // TTS binario directo: encolar el ArrayBuffer tal cual (sin round-trip
        // a base64 — String.fromCharCode(...) revienta con RangeError en frames
        // grandes de audio).
        if (e.data instanceof ArrayBuffer) {
          statsRef.current.recvA++;
          playQueueRef.current.push(e.data);
          if (!playingRef.current) drainQueue();
          return;
        }
        try {
          statsRef.current.recvT++;
          const msg = JSON.parse(e.data as string);
          switch (msg.type) {
            case 'user_transcript':
              // STT del usuario → abre un turno NUEVO (una utterance = un turno) y
              // va al MODEL ROUTER vía el composer (flujo normal).
              if (msg.text) {
                openVoiceTurn();
                onUserTranscriptRef.current?.(msg.text);
              }
              break;
            case 'audio_chunk':
              if (msg.data) enqueueAudio(msg.data);
              break;
            case 'barge_in_ack': {
              // El usuario habló encima: transición EXPLÍCITA de turno. Cortar
              // audio, invalidar el turno (un speak tardío ya no habla) y avisar
              // al composer para que aborte el SSE y cierre su burbuja. El
              // próximo user_transcript abre otro turno → burbuja nueva.
              const cancelled = voiceTurn.id;
              stopPlayback();
              cancelVoiceTurn();
              bargeInListeners.forEach((cb) => {
                try {
                  cb(cancelled);
                } catch (e) {
                  console.warn('[VL] bargeIn listener error', e);
                }
              });
              break;
            }
          }
        } catch {
          /* no-JSON ignorado */
        }
      };

      ws.onerror = (ev) => {
        console.log('[VL] ws error', ev);
        if (wsRef.current === ws) stop();
      };
      ws.onclose = (e) => {
        console.log('[VL] ws closed code=', e.code, 'reason=', e.reason);
        if (wsRef.current === ws) setRecording(false);
      };

      await new Promise<void>((res, rej) => {
        ws.onopen = () => res();
        setTimeout(() => rej(new Error('ws timeout')), 8000);
      });

      const workletUrl = createWorkletUrl();
      workletUrlRef.current = workletUrl;
      await actx.audioWorklet.addModule(workletUrl);

      const mediaSrc = actx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(actx, 'pcm-capture');
      worklet.port.onmessage = (ev: MessageEvent<ArrayBuffer>) => {
        // El barge-in lo detecta el server (ServerVad → barge_in_ack); el cliente
        // solo corta playback en ese ack, no por cada frame de mic (ruido/eco).
        if (ws.readyState === WebSocket.OPEN) {
          statsRef.current.sent++;
          ws.send(ev.data);
        }
      };
      mediaSrc.connect(worklet);
      // no conectar worklet → destination (evita feedback)

      setRecording(true);
      console.log('[VL] recording ON — capturando');
      statsRef.current = { sent: 0, recvA: 0, recvT: 0, played: 0 };
      statsIvRef.current = setInterval(() => {
        const s = statsRef.current;
        console.log(
          `[VL] stats sent=${s.sent} recvAudio=${s.recvA} recvText=${s.recvT} played=${s.played} ctx=${actxRef.current?.state}`
        );
      }, 2000);
    } catch (err) {
      console.error('[useVoiceLive] start error', err);
      stop();
    }
  }, [recording, drainQueue, enqueueAudio, stopPlayback, stop]);

  const toggle = useCallback(() => {
    recording ? stop() : start();
  }, [recording, start, stop]);

  useEffect(
    () => () => {
      console.log('[VL] unmount → cleanup');
      stop();
    },
    [stop]
  );

  return { recording, toggle };
}
