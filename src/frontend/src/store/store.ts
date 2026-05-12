/**
 * Redux Store Configuration
 *
 * Single source of truth para toda la lógica de agentes:
 * - Conversación directa (chat + streaming)
 * - Modo plan (orchestration)
 * - Team/agent selection
 * - Real-time updates (SSE)
 */

import { configureStore, Middleware } from '@reduxjs/toolkit';
import chatReducer from './slices/chatSlice';
import planReducer from './slices/planSlice';
import teamReducer from './slices/teamSlice';
import streamingReducer from './slices/streamingSlice';
import appReducer from './slices/appSlice';
import { agentMiddleware } from './middleware/agentMiddleware';

const preloadedState = undefined;

export const store = configureStore({
  reducer: {
    chat: chatReducer,
    plan: planReducer,
    team: teamReducer,
    streaming: streamingReducer,
    app: appReducer,
  },
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware({
      serializableCheck: {
        ignoredActions: [
          'chat/addMessage',
          'plan/setPlanData',
          'streaming/setLastEvent',
        ],
        ignoredPaths: [
          'chat.messages',
          'plan.planData',
          'streaming.lastEvent',
        ],
      },
    }).concat(agentMiddleware as Middleware),
  devTools: true,
  preloadedState,
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;

export default store;
