/**
 * useVoiceLive — Azure Voice Live / Realtime relay (voz↔voz dúplex)
 *
 * Captura PCM16 mono 24 kHz via AudioWorklet y lo envía por WS al backend
 * relay (/api/v4/audio/stream). El backend hace proxy hacia Azure Voice Live
 * (audience ai.azure.com) reutilizando el agente Foundry existente.
 *
 * browser → backend (binario):  PCM16 LE mono 24 kHz, ~20 ms chunks
 * backend → browser (JSON o binario):
 *   { type:"transcript_start" }           — nuevo turno asistente
 *   { type:"transcript", text:"..." }     — transcripción parcial/final
 *   { type:"transcript_end" }             — turno completado
 *   { type:"audio_chunk", data:"<b64>" }  — TTS PCM16 del servidor
 *   { type:"barge_in_ack" }               — servidor confirmó barge-in
 *   ArrayBuffer                           — TTS PCM16 binario alternativo
 *
 * Barge-in: si el usuario habla mientras el modelo reproduce TTS el hook
 * envía { type:"barge_in" } y detiene la reproducción local.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAppDispatch } from '../store/hooks';
import {
  addStreamToken,
  finishStreaming,
  initAssistantMessage,
  startStreaming,
} from '../store/slices/chatSlice';
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
  return `${base}${path}?user_id=${getUserId() || ''}`;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------
export function useVoiceLive() {
  const dispatch = useAppDispatch();
  const [recording, setRecording] = useState(false);

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
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.close(1000);
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
            case 'transcript_start':
              dispatch(initAssistantMessage());
              dispatch(startStreaming());
              break;
            case 'transcript':
              if (msg.text) dispatch(addStreamToken(msg.text));
              break;
            case 'transcript_end':
              dispatch(finishStreaming({}));
              break;
            case 'audio_chunk':
              if (msg.data) enqueueAudio(msg.data);
              break;
            case 'barge_in_ack':
              stopPlayback();
              break;
          }
        } catch {
          /* no-JSON ignorado */
        }
      };

      ws.onerror = (ev) => {
        console.log('[VL] ws error', ev);
        stop();
      };
      ws.onclose = (e) => {
        console.log('[VL] ws closed code=', e.code, 'reason=', e.reason);
        setRecording(false);
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
  }, [recording, drainQueue, dispatch, enqueueAudio, stopPlayback, stop]);

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
