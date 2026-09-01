/**
 * useDictation — voz → texto al input (STT dictado, sin playback ni barge-in).
 *
 * Comparte el primitivo de captura PCM + URL de WS con useVoiceLive.
 * El backend en /api/v4/audio/stream devuelve sólo transcripciones (text).
 * Los chunks de transcript se concatenan y se pasan a onTranscript(text).
 *
 * Protocolo esperado del backend en modo dictado:
 *   { type:"transcript", text:"..." }   — chunk parcial/final
 *   { type:"transcript_end" }           — fin del turno → onTranscript con total
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getApiUrl, getUserId } from '../api/config';

// ---------------------------------------------------------------------------
// Primitivo compartido: URL del WS (idéntico a useVoiceLive)
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
  return `${base}${path}?user_id=${userId}&mode=dictation`;

// Worklet PCM16 — idéntico al de useVoiceLive (inline blob)
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
// Hook
// ---------------------------------------------------------------------------
export function useDictation(onTranscript: (text: string) => void) {
  const [recording, setRecording] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const actxRef = useRef<AudioContext | null>(null);
  const workletUrlRef = useRef<string | null>(null);
  const bufferRef = useRef('');

  const stop = useCallback(() => {
    actxRef.current?.close().catch(() => {});
    actxRef.current = null;
    if (workletUrlRef.current) {
      URL.revokeObjectURL(workletUrlRef.current);
      workletUrlRef.current = null;
    }
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.close(1000);
    wsRef.current = null;
    bufferRef.current = '';
    setRecording(false);
  }, []);

  const start = useCallback(async () => {
    if (recording) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ws = new WebSocket(buildAudioSocketUrl());
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;

      ws.onmessage = (e) => {
        if (typeof e.data !== 'string') return;
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'transcript' && msg.text) {
            bufferRef.current += msg.text;
          } else if (msg.type === 'transcript_end') {
            // El backend manda el texto completo en transcript_end.text; los
            // deltas pueden no llegar (Voice Live solo emite el 'completed'),
            // así que usar el buffer y, si está vacío, el texto del evento.
            const finalText = bufferRef.current || msg.text || '';
            console.log('[DICT] transcript_end → onTranscript(', finalText.slice(0, 50), ')');
            if (finalText) onTranscript(finalText);
            bufferRef.current = '';
          }
        } catch {
          /* ignore */
        }
      };

      ws.onerror = () => stop();
      ws.onclose = () => setRecording(false);

      await new Promise<void>((res, rej) => {
        ws.onopen = () => res();
        setTimeout(() => rej(new Error('ws timeout')), 8000);
      });

      const actx = new AudioContext({ sampleRate: 24000 });
      actxRef.current = actx;
      const workletUrl = createWorkletUrl();
      workletUrlRef.current = workletUrl;
      await actx.audioWorklet.addModule(workletUrl);

      const mediaSrc = actx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(actx, 'pcm-capture');
      worklet.port.onmessage = (ev: MessageEvent<ArrayBuffer>) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(ev.data);
      };
      mediaSrc.connect(worklet);

      setRecording(true);
    } catch (err) {
      console.error('[useDictation] start error', err);
      stop();
    }
  }, [recording, onTranscript, stop]);

  const toggle = useCallback(() => {
    recording ? stop() : start();
  }, [recording, start, stop]);

  useEffect(
    () => () => {
      stop();
    },
    [stop]
  );

  return { recording, toggle };
}
