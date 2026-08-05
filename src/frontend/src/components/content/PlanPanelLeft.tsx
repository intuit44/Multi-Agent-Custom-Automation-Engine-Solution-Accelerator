import PanelLeft from '@/coral/components/Panels/PanelLeft';
import PanelLeftToolbar from '@/coral/components/Panels/PanelLeftToolbar';
import {
  Body1Strong,
  Caption1,
  Divider,
  DrawerBody,
  DrawerHeader,
  DrawerHeaderTitle,
  OverlayDrawer,
  Toast,
  ToastBody,
  ToastTitle,
  Tooltip,
  useToastController,
  Button,
} from '@fluentui/react-components';
import {
  Chat20Regular,
  ChatAdd20Regular,
  Dismiss24Regular,
  Navigation20Regular,
  Settings24Regular,
} from '@fluentui/react-icons';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PlanPanelLefProps, UserInfo } from '@/models';
import { ChatService } from '@/services/ChatService';
import ContosoLogo from '../../coral/imports/ContosoLogo';
import '../../styles/PlanPanelLeft.css';
import '../../styles/EnhancedChat.css';
import LoginButton from '../auth/LoginButton';
import PanelFooter from '@/coral/components/Panels/PanelFooter';
import { getUserInfoGlobal } from '@/api/config';
import TeamSelector from '../common/TeamSelector';
import { TeamConfig } from '../../models/Team';
import TeamSelected from '../common/TeamSelected';
import type { ChatSessionSummary } from '../../lib/types';
import { SettingsModal } from '../settings/SettingsModal';

const PlanPanelLeft: React.FC<PlanPanelLefProps> = ({
  reloadTasks,
  onNewTaskButton,
  restReload,
  onTeamSelect,
  onTeamUpload,
  isHomePage,
  selectedTeam: parentSelectedTeam,
  onNavigationWithAlert,
  isLoadingTeam: _isLoadingTeam,
}) => {
  const { dispatchToast } = useToastController('toast');
  const navigate = useNavigate();

  const [userInfo, setUserInfo] = useState<UserInfo | null>(
    getUserInfoGlobal()
  );
  const [recentChats, setRecentChats] = useState<ChatSessionSummary[]>([]);
  const loadingRecentChatsRef = useRef(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // collapsed: desktop→rail de 48px | móvil→overlay cerrado
  const [isMobile, setIsMobile] = useState(() => window.innerWidth <= 768);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia('(max-width: 768px)');
    const handler = (e: MediaQueryListEvent) => {
      setIsMobile(e.matches);
      if (e.matches) setCollapsed(true);
      else setCollapsed(false);
    };
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);

  // Use parent's selected team if provided, otherwise use local state
  const [localSelectedTeam, setLocalSelectedTeam] = useState<TeamConfig | null>(
    null
  );
  const selectedTeam = parentSelectedTeam || localSelectedTeam;

  // Fetch recent chats — refresh when reloadTasks fires
  const loadRecentChats = useCallback(async () => {
    if (loadingRecentChatsRef.current) return;
    loadingRecentChatsRef.current = true;
    try {
      const sessions = await ChatService.getRecentSessions();
      setRecentChats(sessions);
    } catch {
    } finally {
      loadingRecentChatsRef.current = false;
    }
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
      navigate('/');
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
                {team.name} team has been selected with {team.agents.length}{' '}
                agents
              </ToastBody>
            </Toast>,
            { intent: 'success' }
          );
        } else {
          // Handle team deselection (null case)
          setLocalSelectedTeam(null);
          dispatchToast(
            <Toast>
              <ToastTitle>Team Deselected</ToastTitle>
              <ToastBody>No team is currently selected</ToastBody>
            </Toast>,
            { intent: 'info' }
          );
        }
      }
    },
    [onTeamSelect, dispatchToast]
  );

  const FULL = 280;
  const RAIL = 48;
  const isRail = !isMobile && collapsed;
  const currentWidth = isMobile ? FULL : collapsed ? RAIL : FULL;

  const panelBody = (
    <>
      {/* Team selector — oculto en rail */}
      {!isRail && (
        <div className="team-selector-container">
          {isHomePage ? (
            <TeamSelector
              onTeamSelect={handleTeamSelect}
              onTeamUpload={onTeamUpload}
              selectedTeam={selectedTeam}
              isHomePage={isHomePage}
            />
          ) : (
            <TeamSelected selectedTeam={selectedTeam} />
          )}
        </div>
      )}

      {/* Nueva tarea */}
      <Tooltip content="New task" relationship="label" positioning="after">
        <div
          className="tab tab-new-task"
          onClick={onNewTaskButton}
          tabIndex={0}
          role="button"
          aria-label="New task"
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              onNewTaskButton();
            }
          }}
          style={
            isRail ? { justifyContent: 'center', padding: '8px 0' } : undefined
          }
        >
          <div className="tab tab-new-task-icon">
            <ChatAdd20Regular />
          </div>
          {!isRail && <Body1Strong>New task</Body1Strong>}
        </div>
      </Tooltip>

      {/* Recent Chats — ocultos en rail */}
      {!isRail && recentChats.length > 0 && (
        <div className="echat-recent-section">
          <Divider style={{ margin: '8px 0' }} />
          <div className="echat-recent-header">
            <Chat20Regular />
            <Caption1 style={{ fontWeight: 600 }}>Recent Chats</Caption1>
          </div>
          <div className="echat-recent-list">
            {recentChats.slice(0, 100).map((chat) => (
              <div
                key={chat.id}
                className="echat-recent-item"
                onClick={() => navigate(`/session/${chat.id}`)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    navigate(`/session/${chat.id}`);
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

      {/* Footer */}
      <PanelFooter>
        <div
          className="panel-footer-content"
          style={
            isRail
              ? { flexDirection: 'column', alignItems: 'center', gap: 8 }
              : undefined
          }
        >
          <LoginButton showName={false} />
          {!isRail && userInfo && (
            <Caption1 title={userInfo.user_email}>
              {userInfo.user_first_last_name || userInfo.user_email}
            </Caption1>
          )}
          <Tooltip
            content="Configuración"
            relationship="label"
            positioning="after"
          >
            <Button
              appearance="subtle"
              icon={<Settings24Regular />}
              onClick={() => setSettingsOpen(true)}
              aria-label="Configuración"
              style={{ minWidth: 32 }}
            />
          </Tooltip>
        </div>
      </PanelFooter>

      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </>
  );

  // ── MÓVIL: OverlayDrawer ──────────────────────────────────────────────────
  if (isMobile) {
    return (
      <>
        {collapsed && (
          <Button
            appearance="subtle"
            icon={<Navigation20Regular />}
            aria-label="Abrir panel"
            onClick={() => setCollapsed(false)}
            style={{
              position: 'fixed',
              top: 8,
              left: 8,
              zIndex: 200,
              background: 'var(--colorNeutralBackground1)',
              boxShadow: 'var(--shadow4)',
              borderRadius: '4px',
            }}
          />
        )}
        <OverlayDrawer
          open={!collapsed}
          onOpenChange={(_e, { open }) => setCollapsed(!open)}
          position="start"
          size="small"
          style={{ width: `${FULL}px` }}
        >
          <DrawerHeader>
            <DrawerHeaderTitle
              action={
                <Button
                  appearance="subtle"
                  aria-label="Cerrar panel"
                  icon={<Dismiss24Regular />}
                  onClick={() => setCollapsed(true)}
                />
              }
            >
              Contoso
            </DrawerHeaderTitle>
          </DrawerHeader>
          <DrawerBody style={{ padding: 0, overflow: 'hidden' }}>
            <PanelLeft panelWidth={FULL} panelResize={false}>
              <PanelLeftToolbar
                linkTo={onNavigationWithAlert ? undefined : '/'}
                onTitleClick={
                  onNavigationWithAlert ? handleLogoClick : undefined
                }
                panelTitle="Contoso"
                panelIcon={<ContosoLogo />}
              />
              {panelBody}
            </PanelLeft>
          </DrawerBody>
        </OverlayDrawer>
      </>
    );
  }

  // ── DESKTOP: rail colapsado o panel expandido ────────────────────────────
  return (
    <div
      className="panel-left-container"
      style={{
        width: `${currentWidth}px`,
        flexShrink: 0,
        transition: 'width 0.2s ease',
        overflow: 'hidden',
      }}
    >
      <PanelLeft panelWidth={currentWidth} panelResize={false}>
        <PanelLeftToolbar
          linkTo={collapsed || onNavigationWithAlert ? undefined : '/'}
          onTitleClick={onNavigationWithAlert ? handleLogoClick : undefined}
          panelTitle={collapsed ? undefined : 'Contoso'}
          panelIcon={<ContosoLogo />}
        >
          <Tooltip
            content={collapsed ? 'Expandir panel' : 'Colapsar panel'}
            relationship="label"
          >
            <Button
              appearance="subtle"
              icon={<Navigation20Regular />}
              aria-label={collapsed ? 'Expandir panel' : 'Colapsar panel'}
              onClick={() => setCollapsed((v) => !v)}
              style={{ minWidth: 32 }}
            />
          </Tooltip>
        </PanelLeftToolbar>
        {panelBody}
      </PanelLeft>
    </div>
  );
};

export default PlanPanelLeft;
