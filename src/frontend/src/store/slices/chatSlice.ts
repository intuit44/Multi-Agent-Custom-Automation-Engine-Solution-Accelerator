/**
 * Chat Slice — Conversation Runtime
 *
 * Gestiona:
 * - Mensajes de usuario y agente
 * - Estado del streaming
 * - Clarificaciones
 * - Historial de sesión
 */

import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '../store';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  metadata?: {
    intent?: string;
    agent?: string;
    confidence?: number;
    fullResponse?: string;
    generatedFiles?: Array<{
      file_id: string;
      filename: string;
      download_url: string;
    }>;
  };
}

export interface ChatState {
  messages: ChatMessage[];
  sessionId: string;
  isStreaming: boolean;
  streamingContent: string;
  streamingBuffer: string;
  error: string | null;
  submittingDisabled: boolean;
}

const initialState: ChatState = {
  messages: [],
  sessionId: `session-${Date.now()}`,
  isStreaming: false,
  streamingContent: '',
  streamingBuffer: '',
  error: null,
  submittingDisabled: false,
};

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    // Session management
    setSessionId(state, action: PayloadAction<string>) {
      state.sessionId = action.payload;
    },

    resetChat() {
      return { ...initialState, sessionId: `session-${Date.now()}` };
    },

    // Message management
    addMessage(state, action: PayloadAction<ChatMessage>) {
      state.messages.push(action.payload);
    },

    addUserMessage(state, action: PayloadAction<string>) {
      state.messages.push({
        id: `msg-${Date.now()}-user`,
        role: 'user',
        content: action.payload,
        timestamp: Date.now(),
      });
    },

    initAssistantMessage(state) {
      state.messages.push({
        id: `msg-${Date.now()}-assistant`,
        role: 'assistant',
        content: '',
        timestamp: Date.now(),
      });
    },

    // Streaming
    startStreaming(state) {
      state.isStreaming = true;
      state.streamingContent = '';
      state.streamingBuffer = '';
      state.error = null;
    },

    addStreamToken(state, action: PayloadAction<string>) {
      state.streamingContent += action.payload;
      if (state.messages.length > 0) {
        const lastMsg = state.messages[state.messages.length - 1];
        if (lastMsg.role === 'assistant') {
          lastMsg.content = state.streamingContent;
        }
      }
    },

    finishStreaming(state, action: PayloadAction<{ metadata?: ChatMessage['metadata'] }>) {
      state.isStreaming = false;
      if (state.messages.length > 0) {
        const lastMsg = state.messages[state.messages.length - 1];
        if (lastMsg.role === 'assistant') {
          lastMsg.content = state.streamingContent;
          if (action.payload?.metadata) {
            lastMsg.metadata = action.payload.metadata;
          }
        }
      }
      state.streamingContent = '';
      state.streamingBuffer = '';
    },

    // UI State
    setSubmittingDisabled(state, action: PayloadAction<boolean>) {
      state.submittingDisabled = action.payload;
    },

    setError(state, action: PayloadAction<string | null>) {
      state.error = action.payload;
      state.isStreaming = false;
    },

    clearMessages(state) {
      state.messages = [];
      state.streamingContent = '';
    },

    loadSession(state, action: PayloadAction<{ sessionId: string; messages: ChatMessage[] }>) {
      state.sessionId = action.payload.sessionId;
      state.messages = action.payload.messages;
      state.isStreaming = false;
      state.streamingContent = '';
      state.streamingBuffer = '';
      state.error = null;
    },
  },
});

export const {
  setSessionId,
  resetChat,
  addMessage,
  addUserMessage,
  initAssistantMessage,
  startStreaming,
  addStreamToken,
  finishStreaming,
  setSubmittingDisabled,
  setError,
  clearMessages,
  loadSession,
} = chatSlice.actions;

// Selectors
export const selectMessages = (state: RootState) => state.chat.messages;
export const selectSessionId = (state: RootState) => state.chat.sessionId;
export const selectIsStreaming = (state: RootState) => state.chat.isStreaming;
export const selectStreamingContent = (state: RootState) => state.chat.streamingContent;
export const selectError = (state: RootState) => state.chat.error;
export const selectSubmittingDisabled = (state: RootState) => state.chat.submittingDisabled;

export default chatSlice.reducer;
