/**
 * Streaming Slice — SSE Events & Real-time Updates
 *
 * Gestiona:
 * - Eventos de streaming (tokens, intents, plan creation)
 * - Actividad de herramientas
 * - Archivos generados
 */

import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '../store';

export interface ToolActivity {
  tool: string;
  activity: 'calling' | 'result' | 'thinking';
  server?: string;
  success?: boolean;
  message?: string;
}

export interface GeneratedFile {
  file_id: string;
  filename: string;
  download_url: string;
  container_id?: string;
}

export interface StreamingState {
  currentIntent: string | null;
  currentConfidence: number | null;
  toolActivities: ToolActivity[];
  generatedFiles: GeneratedFile[];
  planCreatedId: string | null;
  lastEvent: any | null;
}

const initialState: StreamingState = {
  currentIntent: null,
  currentConfidence: null,
  toolActivities: [],
  generatedFiles: [],
  planCreatedId: null,
  lastEvent: null,
};

const streamingSlice = createSlice({
  name: 'streaming',
  initialState,
  reducers: {
    setIntent(state, action: PayloadAction<{ intent: string; confidence: number }>) {
      state.currentIntent = action.payload.intent;
      state.currentConfidence = action.payload.confidence;
    },

    addToolActivity(state, action: PayloadAction<ToolActivity>) {
      state.toolActivities.push(action.payload);
    },

    clearToolActivities(state) {
      state.toolActivities = [];
    },

    addGeneratedFile(state, action: PayloadAction<GeneratedFile>) {
      state.generatedFiles.push(action.payload);
    },

    clearGeneratedFiles(state) {
      state.generatedFiles = [];
    },

    setPlanCreated(state, action: PayloadAction<string | null>) {
      state.planCreatedId = action.payload;
    },

    setLastEvent(state, action: PayloadAction<any>) {
      state.lastEvent = action.payload;
    },

    reset(state) {
      state.currentIntent = null;
      state.currentConfidence = null;
      state.toolActivities = [];
      state.generatedFiles = [];
      state.planCreatedId = null;
      state.lastEvent = null;
    },
  },
});

export const {
  setIntent,
  addToolActivity,
  clearToolActivities,
  addGeneratedFile,
  clearGeneratedFiles,
  setPlanCreated,
  setLastEvent,
  reset,
} = streamingSlice.actions;

// Selectors
export const selectCurrentIntent = (state: RootState) => state.streaming.currentIntent;
export const selectCurrentConfidence = (state: RootState) => state.streaming.currentConfidence;
export const selectToolActivities = (state: RootState) => state.streaming.toolActivities;
export const selectGeneratedFiles = (state: RootState) => state.streaming.generatedFiles;
export const selectPlanCreatedId = (state: RootState) => state.streaming.planCreatedId;
export const selectLastEvent = (state: RootState) => state.streaming.lastEvent;

export default streamingSlice.reducer;
