/**
 * Team Slice — Team & Agent Selection Runtime
 *
 * Gestiona:
 * - Equipo seleccionado
 * - Agentes disponibles
 * - Configuración del equipo
 */

import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '../store';
import { TeamConfig } from '../../models/Team';

export interface TeamState {
  selectedTeam: TeamConfig | null;
  teams: TeamConfig[];
  isLoading: boolean;
  error: string | null;
  reloadTasks: boolean;
}

const initialState: TeamState = {
  selectedTeam: null,
  teams: [],
  isLoading: true,
  error: null,
  reloadTasks: false,
};

const teamSlice = createSlice({
  name: 'team',
  initialState,
  reducers: {
    setSelectedTeam(state, action: PayloadAction<TeamConfig | null>) {
      state.selectedTeam = action.payload;
    },

    setTeams(state, action: PayloadAction<TeamConfig[]>) {
      state.teams = action.payload;
    },

    setIsLoading(state, action: PayloadAction<boolean>) {
      state.isLoading = action.payload;
    },

    setError(state, action: PayloadAction<string | null>) {
      state.error = action.payload;
    },

    setReloadTasks(state, action: PayloadAction<boolean>) {
      state.reloadTasks = action.payload;
    },

    addTeam(state, action: PayloadAction<TeamConfig>) {
      state.teams.push(action.payload);
    },

    updateTeam(state, action: PayloadAction<TeamConfig>) {
      const index = state.teams.findIndex(t => t.team_id === action.payload.team_id);
      if (index !== -1) {
        state.teams[index] = action.payload;
      }
    },
  },
});

export const {
  setSelectedTeam,
  setTeams,
  setIsLoading,
  setError,
  setReloadTasks,
  addTeam,
  updateTeam,
} = teamSlice.actions;

// Selectors
export const selectSelectedTeam = (state: RootState) => state.team.selectedTeam;
export const selectTeams = (state: RootState) => state.team.teams;
export const selectIsLoadingTeam = (state: RootState) => state.team.isLoading;
export const selectTeamError = (state: RootState) => state.team.error;
export const selectReloadTasks = (state: RootState) => state.team.reloadTasks;

export default teamSlice.reducer;
