import { TeamConfig } from './Team';

export interface PlanPanelLefProps {
  reloadTasks: boolean;
  onNewTaskButton: () => void;
  restReload?: () => void;
  onTeamSelect?: (team: TeamConfig | null) => void;
  onTeamUpload?: () => Promise<void>;
  isHomePage: boolean;
  selectedTeam?: TeamConfig | null;
  onNavigationWithAlert?: (navigationFn: () => void | Promise<void>) => void;
  isLoadingTeam?: boolean;
  /** Called when the user selects or clears a workspace in the selector. */
  onWorkspaceChange?: (workspaceId: string | null) => void;
}
