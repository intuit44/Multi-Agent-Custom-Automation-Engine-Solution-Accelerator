/**
 * App Slice — Global App State
 *
 * Gestiona:
 * - User info & auth
 * - Session state
 * - Global UI state
 */

import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '../store';

export interface UserInfo {
  userId: string;
  userPrincipalId: string;
  tenantId?: string;
  displayName?: string;
  userRoles?: string[];
}

export interface AppState {
  user: UserInfo | null;
  isAuthenticated: boolean;
  isInitializing: boolean;
  globalError: string | null;
}

const initialState: AppState = {
  user: null,
  isAuthenticated: false,
  isInitializing: true,
  globalError: null,
};

const appSlice = createSlice({
  name: 'app',
  initialState,
  reducers: {
    setUser(state, action: PayloadAction<UserInfo | null>) {
      state.user = action.payload;
      state.isAuthenticated = action.payload !== null;
    },

    setIsInitializing(state, action: PayloadAction<boolean>) {
      state.isInitializing = action.payload;
    },

    setGlobalError(state, action: PayloadAction<string | null>) {
      state.globalError = action.payload;
    },

    clearGlobalError(state) {
      state.globalError = null;
    },

    logout(state) {
      state.user = null;
      state.isAuthenticated = false;
    },
  },
});

export const {
  setUser,
  setIsInitializing,
  setGlobalError,
  clearGlobalError,
  logout,
} = appSlice.actions;

// Selectors
export const selectUser = (state: RootState) => state.app.user;
export const selectIsAuthenticated = (state: RootState) => state.app.isAuthenticated;
export const selectIsInitializing = (state: RootState) => state.app.isInitializing;
export const selectGlobalError = (state: RootState) => state.app.globalError;

export default appSlice.reducer;
