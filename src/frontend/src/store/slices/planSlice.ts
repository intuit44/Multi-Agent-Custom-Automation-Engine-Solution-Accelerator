/**
 * Plan Slice — Orchestration Runtime
 *
 * Gestiona:
 * - Plan activo
 * - Estado de aprobación
 * - Progreso de ejecución
 * - Agentes involucrados
 */

import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '../store';

export interface PlanState {
  activePlanId: string | null;
  planData: any | null;
  approvalRequest: any | null;
  status: 'idle' | 'waiting_approval' | 'executing' | 'completed' | 'failed';
  error: string | null;
  showApprovalButtons: boolean;
  processingApproval: boolean;
}

const initialState: PlanState = {
  activePlanId: null,
  planData: null,
  approvalRequest: null,
  status: 'idle',
  error: null,
  showApprovalButtons: false,
  processingApproval: false,
};

const planSlice = createSlice({
  name: 'plan',
  initialState,
  reducers: {
    setPlanData(state, action: PayloadAction<{ planId: string; data: any }>) {
      state.activePlanId = action.payload.planId;
      state.planData = action.payload.data;
      state.status = 'waiting_approval';
    },

    setApprovalRequest(state, action: PayloadAction<any | null>) {
      state.approvalRequest = action.payload;
      if (action.payload) {
        state.showApprovalButtons = true;
      }
    },

    setStatus(
      state,
      action: PayloadAction<'idle' | 'waiting_approval' | 'executing' | 'completed' | 'failed'>
    ) {
      state.status = action.payload;
    },

    setProcessingApproval(state, action: PayloadAction<boolean>) {
      state.processingApproval = action.payload;
    },

    clearPlan(state) {
      state.activePlanId = null;
      state.planData = null;
      state.approvalRequest = null;
      state.status = 'idle';
      state.showApprovalButtons = false;
      state.error = null;
    },

    setPlanError(state, action: PayloadAction<string | null>) {
      state.error = action.payload;
      if (action.payload) {
        state.status = 'failed';
      }
    },
  },
});

export const {
  setPlanData,
  setApprovalRequest,
  setStatus,
  setProcessingApproval,
  clearPlan,
  setPlanError,
} = planSlice.actions;

// Selectors
export const selectActivePlanId = (state: RootState) => state.plan.activePlanId;
export const selectPlanData = (state: RootState) => state.plan.planData;
export const selectApprovalRequest = (state: RootState) => state.plan.approvalRequest;
export const selectPlanStatus = (state: RootState) => state.plan.status;
export const selectPlanError = (state: RootState) => state.plan.error;
export const selectShowApprovalButtons = (state: RootState) => state.plan.showApprovalButtons;
export const selectProcessingApproval = (state: RootState) => state.plan.processingApproval;

export default planSlice.reducer;
