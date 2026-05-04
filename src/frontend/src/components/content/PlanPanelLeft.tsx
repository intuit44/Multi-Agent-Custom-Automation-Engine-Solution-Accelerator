import PanelLeft from "@/coral/components/Panels/PanelLeft";
import PanelLeftToolbar from "@/coral/components/Panels/PanelLeftToolbar";
import {
  Body1Strong,
  Caption1,
  Divider,
  Toast,
  ToastBody,
  ToastTitle,
  Tooltip,
  useToastController,
} from "@fluentui/react-components";
import {
  Chat20Regular,
  ChatAdd20Regular,
} from "@fluentui/react-icons";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PlanPanelLefProps, UserInfo } from "@/models";
import { ChatService } from "@/services/ChatService";
import ContosoLogo from "../../coral/imports/ContosoLogo";
import "../../styles/PlanPanelLeft.css";
import "../../styles/EnhancedChat.css";
import PanelFooter from "@/coral/components/Panels/PanelFooter";
import PanelUserCard from "../../coral/components/Panels/UserCard";
import { getUserInfoGlobal } from "@/api/config";
import TeamSelector from "../common/TeamSelector";
import { TeamConfig } from "../../models/Team";
import TeamSelected from "../common/TeamSelected";
import type { ChatSessionSummary } from "../../lib/types";

const PlanPanelLeft: React.FC<PlanPanelLefProps> = ({
  reloadTasks,
  onNewTaskButton,
  restReload,
  onTeamSelect,
  onTeamUpload,
  isHomePage,
  selectedTeam: parentSelectedTeam,
  onNavigationWithAlert,
  isLoadingTeam
}) => {
  const { dispatchToast } = useToastController("toast");
  const navigate = useNavigate();

  const [userInfo, setUserInfo] = useState<UserInfo | null>(
    getUserInfoGlobal()
  );
  const [recentChats, setRecentChats] = useState<ChatSessionSummary[]>([]);

  // Use parent's selected team if provided, otherwise use local state
  const [localSelectedTeam, setLocalSelectedTeam] = useState<TeamConfig | null>(null);
  const selectedTeam = parentSelectedTeam || localSelectedTeam;


  // Fetch recent chats — refresh when reloadTasks fires
  const loadRecentChats = useCallback(async () => {
    try {
      const sessions = await ChatService.getRecentSessions();
      setRecentChats(sessions);
    } catch {}
  }, []);

  // Mount-only init (loadRecentChats is stable — empty useCallback deps)
  useEffect(() => {
    setUserInfo(getUserInfoGlobal());
    loadRecentChats();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (reloadTasks) {
      loadRecentChats();
      if (restReload) restReload();
    }
  }, [reloadTasks, loadRecentChats, restReload]);


  const handleLogoClick = useCallback(() => {
    const performNavigation = () => {
      navigate("/");
    };

    if (onNavigationWithAlert) {
      onNavigationWithAlert(performNavigation);
    } else {
      performNavigation();
    }
  }, [navigate, onNavigationWithAlert]);

  const handleTeamSelect = useCallback(
    (team: TeamConfig | null) => {
      if (onTeamSelect) {
        onTeamSelect(team);
      } else {
        if (team) {
          setLocalSelectedTeam(team);
          dispatchToast(
            <Toast>
              <ToastTitle>Team Selected</ToastTitle>
              <ToastBody>
                {team.name} team has been selected with {team.agents.length} agents
              </ToastBody>
            </Toast>,
            { intent: "success" }
          );
        } else {
          // Handle team deselection (null case)
          setLocalSelectedTeam(null);
          dispatchToast(
            <Toast>
              <ToastTitle>Team Deselected</ToastTitle>
              <ToastBody>
                No team is currently selected
              </ToastBody>
            </Toast>,
            { intent: "info" }
          );
        }
      }
    },
    [onTeamSelect, dispatchToast]
  );

  return (
    <div className="panel-left-container">
      <PanelLeft panelWidth={280} panelResize={true}>
        <PanelLeftToolbar
          linkTo={onNavigationWithAlert ? undefined : "/"}
          onTitleClick={onNavigationWithAlert ? handleLogoClick : undefined}
          panelTitle="Contoso"
          panelIcon={<ContosoLogo />}
        >
          <Tooltip content="New task" relationship={"label"} />
        </PanelLeftToolbar>

        {/* Team Selector right under the toolbar */}

        <div className="team-selector-container">
          {isHomePage && (
            <TeamSelector
              onTeamSelect={handleTeamSelect}
              onTeamUpload={onTeamUpload}
              selectedTeam={selectedTeam}
              isHomePage={isHomePage}
            />
          )}

          {!isHomePage && (
            <TeamSelected
              selectedTeam={selectedTeam}
            />
          )}

        </div>
        <div
          className="tab tab-new-task"
          onClick={onNewTaskButton}
          tabIndex={0} // ✅ allows tab focus
          role="button" // ✅ announces as button
          aria-label="New task"
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onNewTaskButton();
            }
          }}
        >
          <div className="tab tab-new-task-icon">
            <ChatAdd20Regular />
          </div>
          <Body1Strong>New task</Body1Strong>
        </div>

        {/* ── Recent Chats ─────────────────────────────────── */}
        {recentChats.length > 0 && (
          <div className="echat-recent-section">
            <Divider style={{ margin: '8px 0' }} />
            <div className="echat-recent-header">
              <Chat20Regular />
              <Caption1 style={{ fontWeight: 600 }}>Recent Chats</Caption1>
            </div>
            <div className="echat-recent-list">
              {recentChats.slice(0, 10).map((chat) => (
                <div
                  key={chat.id}
                  className="echat-recent-item"
                  onClick={() => navigate(`/chat/${chat.id}`)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      navigate(`/chat/${chat.id}`);
                    }
                  }}
                >
                  <Caption1 className="echat-recent-item-name">
                    {chat.session_name || 'Untitled Chat'}
                  </Caption1>
                  <Caption1 className="echat-recent-item-meta">
                    {chat.message_count || 0}
                  </Caption1>
                </div>
              ))}
            </div>
          </div>
        )}

        <PanelFooter>
          <div className="panel-footer-content">
            {/* User Card */}
            <PanelUserCard
              name={userInfo?.user_first_last_name || "Guest"}
              // alias={userInfo ? userInfo.user_email : ""}
              size={32}
            />
          </div>
        </PanelFooter>
      </PanelLeft>
    </div>
  );
};

export default PlanPanelLeft;
