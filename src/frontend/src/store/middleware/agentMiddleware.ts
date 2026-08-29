/**
 * Agent Middleware — Orquesta ChatService + WebSocket
 *
 * Intercepta sendMessage actions y:
 * 1. Llama ChatService.sendMessageStream()
 * 2. Despacha actions Redux según SSE events
 * 3. Detecta intención (conversational vs task)
 * 4. Navega automáticamente si se crea plan
 */

import { Middleware } from '@reduxjs/toolkit';
import { ChatService, StreamCallbacks } from '../../services/ChatService';
import {
  addUserMessage,
  initAssistantMessage,
  startStreaming,
  addStreamToken,
  finishStreaming,
  setError,
} from '../slices/chatSlice';
import {
  setIntent,
  addToolActivity,
  addGeneratedFile,
  setPlanCreated,
} from '../slices/streamingSlice';
import type { AppDispatch } from '../store';

export interface SendMessagePayload {
  message: string;
  sessionId?: string;
  fileIds?: string[];
}

export const agentMiddleware: Middleware =
  (store) => (next) => async (action: any) => {
    // Pasar acción normal primero
    const result = next(action);

    // Interceptar sendMessage
    if (action.type === 'chat/sendMessage' && action.payload) {
      const dispatch = store.dispatch as AppDispatch;
      const state = store.getState();
      const { message, sessionId, fileIds } = action.payload as SendMessagePayload;

      try {
        // 1. Agregar mensaje del usuario
        dispatch(
          addUserMessage(message)
        );

        // 2. Inicializar mensaje del asistente
        dispatch(initAssistantMessage());

        // 3. Iniciar streaming
        dispatch(startStreaming());
        dispatch(setError(null));

        // 4. Callbacks para SSE events
        const callbacks: StreamCallbacks = {
          onToken: (token: string) => {
            dispatch(addStreamToken(token));
          },

          onIntent: (data: { intent: string; confidence: number; session_id: string; plan_id?: string; m_plan_id?: string }) => {
            dispatch(
              setIntent({
                intent: data.intent,
                confidence: data.confidence,
              })
            );
          },

          onToolActivity: (data: any) => {
            dispatch(addToolActivity(data));
          },

          onGeneratedFile: (data: any) => {
            dispatch(addGeneratedFile(data));
          },

          onPlanCreated: (planId: string) => {
            dispatch(setPlanCreated(planId));
            // Automaticamente navega a /plan/:planId
            // (será manejado por un componente que escucha planCreatedId)
          },

          onRedirect: (planId: string) => {
            dispatch(setPlanCreated(planId));
          },

          onDone: (data: { intent: string; agent: string; confidence: number; session_id: string; plan_id?: string; m_plan_id?: string }) => {
            dispatch(
              finishStreaming({
                metadata: {
                  intent: data.intent,
                  agent: data.agent,
                  confidence: data.confidence,
                },
              })
            );

            // Si la intención fue "task", un plan se creó automáticamente
            // El componente que renderiza detectará planCreatedId y navegará
          },

          onError: (error: string) => {
            dispatch(setError(error));
            dispatch(
              finishStreaming({
                metadata: {
                  intent: 'error',
                  agent: 'system',
                  confidence: 0,
                },
              })
            );
          },
        };

        // 5. Llamar ChatService con streaming
        const activeWorkspaceId =
          typeof window !== 'undefined'
            ? window.localStorage.getItem('macae_active_workspace_id')
            : null;
        await ChatService.sendMessageStream(
          message,
          sessionId || state.chat.sessionId,
          callbacks,
          fileIds,
          undefined,   // planId — no aplicable desde este carril
          undefined,   // allowPlan — usa default
          activeWorkspaceId,
        );
      } catch (error: any) {
        dispatch(setError(error?.message || 'Error sending message'));
        dispatch(
          finishStreaming({
            metadata: {
              intent: 'error',
              agent: 'system',
              confidence: 0,
            },
          })
        );
      }
    }

    return result;
  };
