/**
 * Settings Modal Component
 * Manages user settings including MCP server connections, personalization, and notifications.
 */

import React, { useState, useEffect } from 'react';
import {
  Dialog,
  DialogSurface,
  DialogBody,
  DialogTitle,
  DialogContent,
  Tab,
  TabList,
  Button,
  Badge,
  Spinner,
  Tooltip,
  Switch,
} from '@fluentui/react-components';
import {
  Settings24Regular,
  Dismiss24Regular,
  CheckmarkCircle24Filled,
  Circle24Regular,
  Link24Regular,
  Delete24Regular,
  Add24Regular,
  Edit24Regular,
} from '@fluentui/react-icons';
import { apiService } from '../../api/apiService';
import { AddMcpServerForm, EditableMcpServer } from './AddMcpServerForm';
import './SettingsModal.css';

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
}

type SettingsTab = 'general' | 'notifications' | 'personalization' | 'applications' | 'calendars';

interface MCPConnection {
  server: {
    id: string;
    server_name: string;
    display_name: string;
    description?: string;
    endpoint: string;
    transport: string;
    auth_type: string;
    auth_fields?: string[];
    oauth_scopes?: string[];
    oauth_authorize_url?: string | null;
    oauth_token_url?: string | null;
    oauth_client_id_env?: string | null;
    capabilities?: string[];
    tool_count?: number;
    icon_url?: string;
    allowed_agents?: string[];
  };
  connection: {
    status: string;
    connected_at?: string;
    last_error?: string;
  } | null;
  is_connected: boolean;
  needs_auth: boolean;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({ open, onClose }) => {
  const [activeTab, setActiveTab] = useState<SettingsTab>('applications');
  const [connections, setConnections] = useState<MCPConnection[]>([]);
  const [loading, setLoading] = useState(false);
  const [connectingServer, setConnectingServer] = useState<string | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [editingServer, setEditingServer] = useState<EditableMcpServer | null>(null);

  useEffect(() => {
    if (open && activeTab === 'applications') {
      loadConnections();
    }
  }, [open, activeTab]);

  const loadConnections = async () => {
    setLoading(true);
    try {
      const data = await apiService.getUserMcpConnections();
      setConnections(data.connections);
    } catch (e) {
      console.error('Error loading MCP connections:', e);
    } finally {
      setLoading(false);
    }
  };

  const handleConnect = async (serverName: string, needsAuth: boolean, authType?: string) => {
    setConnectingServer(serverName);
    try {
      // If requires API key or bearer token, prompt user
      if (needsAuth && (authType === 'api_key' || authType === 'bearer_token')) {
        const token = window.prompt(
          `Ingrese el token de acceso para ${serverName}:\n\n` +
          `Para GitHub Copilot MCP, use su Personal Access Token (PAT) de GitHub.\n` +
          `El token será almacenado de forma segura en Azure Key Vault.`,
          ''
        );

        if (!token || !token.trim()) {
          setConnectingServer(null);
          return; // User cancelled
        }

        // Connect with credentials
        await apiService.connectToMcpServer(serverName, {
          access_token: token.trim(),
        });

        await loadConnections();
      } else if (needsAuth && authType === 'oauth2') {
        // OAuth flow: try to get oauth_url from backend
        const result = await apiService.connectToMcpServer(serverName);

        if (result.oauth_url) {
          const width = 600;
          const height = 700;
          const left = window.screen.width / 2 - width / 2;
          const top = window.screen.height / 2 - height / 2;

          const popup = window.open(
            result.oauth_url,
            'oauth',
            `width=${width},height=${height},left=${left},top=${top}`
          );

          // Poll for popup closure and refresh connections
          const interval = setInterval(async () => {
            if (popup?.closed) {
              clearInterval(interval);
              // Wait a bit for backend to process OAuth callback
              setTimeout(async () => {
                await loadConnections();
              }, 1000);
            }
          }, 1000);
        } else {
          alert('OAuth flow no está configurado para este servidor. Use auth_type=api_key.');
          await loadConnections();
        }
      } else {
        // No auth required → connect directly
        await apiService.connectToMcpServer(serverName);
        await loadConnections();
      }
    } catch (e) {
      console.error('Error connecting to MCP server:', e);
      alert(`Error al conectar: ${e instanceof Error ? e.message : 'Error desconocido'}`);
    } finally {
      setConnectingServer(null);
    }
  };

  const handleDisconnect = async (serverName: string) => {
    try {
      await apiService.disconnectMcpServer(serverName);
      await loadConnections();
    } catch (e) {
      console.error('Error disconnecting from MCP server:', e);
    }
  };

  const handleEditServer = (server: MCPConnection['server']) => {
    setEditingServer({
      id: server.id,
      server_name: server.server_name,
      display_name: server.display_name,
      endpoint: server.endpoint,
      transport: server.transport,
      auth_type: server.auth_type,
      description: server.description,
      icon_url: server.icon_url,
      auth_fields: server.auth_fields,
      oauth_scopes: server.oauth_scopes,
      oauth_authorize_url: server.oauth_authorize_url,
      oauth_token_url: server.oauth_token_url,
      oauth_client_id_env: server.oauth_client_id_env,
      capabilities: server.capabilities,
      allowed_agents: server.allowed_agents,
    });
    setShowAddForm(true);
  };

  const handleDeleteServer = async (server: MCPConnection['server']) => {
    if (
      !window.confirm(
        `¿Eliminar el servidor "${server.display_name}"? Esta acción no se puede deshacer.`
      )
    ) {
      return;
    }
    try {
      await apiService.deleteMcpServer(server.id);
      await loadConnections();
    } catch (e) {
      console.error('Error deleting MCP server:', e);
      alert(`Error al eliminar: ${e instanceof Error ? e.message : 'Error desconocido'}`);
    }
  };

  const connectedCount = connections.filter(c => c.is_connected).length;

  return (
    <Dialog open={open} onOpenChange={(_, d) => !d.open && onClose()}>
      <DialogSurface style={{ maxWidth: 720, width: '90vw', minHeight: 500 }}>
        <DialogBody>
          <DialogTitle
            action={
              <Button
                appearance="subtle"
                icon={<Dismiss24Regular />}
                onClick={onClose}
                aria-label="Cerrar"
              />
            }
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Settings24Regular />
              Configuración
            </div>
          </DialogTitle>

          <DialogContent style={{ display: 'flex', gap: 0, padding: 0 }}>
            {/* Sidebar de tabs */}
            <TabList
              vertical
              selectedValue={activeTab}
              onTabSelect={(_, d) => setActiveTab(d.value as SettingsTab)}
              style={{
                minWidth: 180,
                borderRight: '1px solid var(--colorNeutralStroke2)',
                padding: '16px 0'
              }}
            >
              <Tab value="general">General</Tab>
              <Tab value="notifications">Notificaciones</Tab>
              <Tab value="personalization">Personalización</Tab>
              <Tab value="applications">
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  Aplicaciones
                  {connectedCount > 0 && (
                    <Badge appearance="filled" color="brand" size="small">
                      {connectedCount}
                    </Badge>
                  )}
                </div>
              </Tab>
              <Tab value="calendars">Calendarios</Tab>
            </TabList>

            {/* Contenido del tab activo */}
            <div style={{ flex: 1, padding: '24px 28px', overflowY: 'auto', maxHeight: 500 }}>
              {activeTab === 'applications' && (
                showAddForm ? (
                  <AddMcpServerForm
                    editingServer={editingServer}
                    onSuccess={() => {
                      setShowAddForm(false);
                      setEditingServer(null);
                      loadConnections();
                    }}
                    onCancel={() => {
                      setShowAddForm(false);
                      setEditingServer(null);
                    }}
                  />
                ) : (
                  <ApplicationsTab
                    connections={connections}
                    loading={loading}
                    connectingServer={connectingServer}
                    onConnect={handleConnect}
                    onDisconnect={handleDisconnect}
                    onAddServer={() => {
                      setEditingServer(null);
                      setShowAddForm(true);
                    }}
                    onEditServer={handleEditServer}
                    onDeleteServer={handleDeleteServer}
                  />
                )
              )}
              {activeTab === 'general' && <GeneralTab />}
              {activeTab === 'personalization' && <PersonalizationTab />}
              {activeTab === 'notifications' && (
                <div style={{ color: 'var(--colorNeutralForeground3)' }}>
                  Configuración de notificaciones — próximamente
                </div>
              )}
              {activeTab === 'calendars' && (
                <div style={{ color: 'var(--colorNeutralForeground3)' }}>
                  Integración de calendarios — próximamente
                </div>
              )}
            </div>
          </DialogContent>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
};

// ── Applications Tab ──────────────────────────────────────────────────────

interface ApplicationsTabProps {
  connections: MCPConnection[];
  loading: boolean;
  connectingServer: string | null;
  onConnect: (name: string, needsAuth: boolean, authType?: string) => void;
  onDisconnect: (name: string) => void;
  onAddServer: () => void;
  onEditServer: (server: MCPConnection['server']) => void;
  onDeleteServer: (server: MCPConnection['server']) => void;
}

const ApplicationsTab: React.FC<ApplicationsTabProps> = ({
  connections,
  loading,
  connectingServer,
  onConnect,
  onDisconnect,
  onAddServer,
  onEditServer,
  onDeleteServer,
}) => {
  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}>
        <Spinner label="Cargando aplicaciones..." />
      </div>
    );
  }

  if (connections.length === 0) {
    return (
      <div style={{ color: 'var(--colorNeutralForeground3)', textAlign: 'center', padding: 40 }}>
        No hay aplicaciones disponibles en el catálogo.
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <h3 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Aplicaciones habilitadas</h3>
          <p style={{ color: 'var(--colorNeutralForeground3)', fontSize: 13, marginTop: 4, marginBottom: 0 }}>
            Conecta servicios externos para que el agente pueda actuar en tu nombre.
          </p>
        </div>
        <Button
          appearance="primary"
          icon={<Add24Regular />}
          onClick={onAddServer}
          size="small"
        >
          Agregar servidor
        </Button>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {connections.map(({ server, connection, is_connected, needs_auth }) => (
          <div
            key={server.server_name}
            className="mcp-connection-card"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 16,
              padding: '14px 16px',
              borderRadius: 8,
              border: '1px solid var(--colorNeutralStroke2)',
              background: is_connected
                ? 'var(--colorBrandBackground2)'
                : 'var(--colorNeutralBackground2)',
              transition: 'all 0.2s ease',
            }}
          >
            {/* Icono */}
            <div style={{ fontSize: 28, lineHeight: 1 }}>
              {server.icon_url ? (
                <img
                  src={server.icon_url}
                  width={28}
                  height={28}
                  alt=""
                  style={{ borderRadius: 4 }}
                />
              ) : (
                getServerIcon(server.server_name)
              )}
            </div>

            {/* Info */}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 2 }}>
                {server.display_name}
              </div>
              <div
                style={{
                  color: 'var(--colorNeutralForeground3)',
                  fontSize: 12,
                  marginBottom: 4,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {server.description || server.endpoint}
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 4 }}>
                {server.capabilities?.map(cap => (
                  <Badge key={cap} appearance="outline" size="small">
                    {cap}
                  </Badge>
                ))}
                {server.tool_count !== undefined && server.tool_count > 0 && (
                  <Badge appearance="outline" size="small" color="informative">
                    {server.tool_count} tools
                  </Badge>
                )}
              </div>
              {connection?.last_error && (
                <div style={{ color: 'var(--colorPaletteRedForeground1)', fontSize: 12, marginTop: 4 }}>
                  Error: {connection.last_error}
                </div>
              )}
            </div>

            {/* Estado + acción */}
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 8 }}>
              {is_connected ? (
                <>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 4,
                      color: 'var(--colorBrandForeground1)',
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    <CheckmarkCircle24Filled style={{ width: 16, height: 16 }} />
                    Conectado
                  </div>
                  <Tooltip content="Desconectar de este servicio" relationship="label">
                    <Button
                      appearance="subtle"
                      size="small"
                      icon={<Delete24Regular />}
                      onClick={() => onDisconnect(server.server_name)}
                    >
                      Desconectar
                    </Button>
                  </Tooltip>
                </>
              ) : (
                <>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 4,
                      color: 'var(--colorNeutralForeground3)',
                      fontSize: 12,
                    }}
                  >
                    <Circle24Regular style={{ width: 16, height: 16 }} />
                    No conectado
                  </div>
                  <Tooltip
                    content={needs_auth ? 'Requiere autenticación OAuth' : 'Conectar sin autenticación'}
                    relationship="label"
                  >
                    <Button
                      appearance="primary"
                      size="small"
                      icon={
                        connectingServer === server.server_name ? (
                          <Spinner size="tiny" />
                        ) : (
                          <Link24Regular />
                        )
                      }
                      disabled={connectingServer === server.server_name}
                      onClick={() => onConnect(server.server_name, needs_auth, server.auth_type)}
                    >
                      {needs_auth ? (server.auth_type === 'oauth2' ? 'Conectar con OAuth' : 'Conectar con Token') : 'Conectar'}
                    </Button>
                  </Tooltip>
                </>
              )}

              {/* Editar / Eliminar — gestión del catálogo */}
              <div style={{ display: 'flex', gap: 4 }}>
                <Tooltip content="Editar este servidor" relationship="label">
                  <Button
                    appearance="subtle"
                    size="small"
                    icon={<Edit24Regular />}
                    aria-label="Editar"
                    onClick={() => onEditServer(server)}
                  >
                    Editar
                  </Button>
                </Tooltip>
                <Tooltip content="Eliminar este servidor del catálogo" relationship="label">
                  <Button
                    appearance="subtle"
                    size="small"
                    icon={<Delete24Regular />}
                    aria-label="Eliminar"
                    onClick={() => onDeleteServer(server)}
                  >
                    Eliminar
                  </Button>
                </Tooltip>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

// ── General Tab ───────────────────────────────────────────────────────────

const GeneralTab: React.FC = () => {
  const [darkMode, setDarkMode] = useState(false);
  const [compactMode, setCompactMode] = useState(false);

  return (
    <div>
      <h3 style={{ marginTop: 0, fontSize: 16, fontWeight: 600 }}>Configuración general</h3>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 20, marginTop: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>Modo oscuro</div>
            <div style={{ color: 'var(--colorNeutralForeground3)', fontSize: 12, marginTop: 2 }}>
              Usar tema oscuro en toda la aplicación
            </div>
          </div>
          <Switch checked={darkMode} onChange={(_, data) => setDarkMode(data.checked)} />
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>Modo compacto</div>
            <div style={{ color: 'var(--colorNeutralForeground3)', fontSize: 12, marginTop: 2 }}>
              Reducir espaciado entre elementos
            </div>
          </div>
          <Switch checked={compactMode} onChange={(_, data) => setCompactMode(data.checked)} />
        </div>
      </div>
    </div>
  );
};

// ── Personalization Tab ───────────────────────────────────────────────────

const PersonalizationTab: React.FC = () => {
  return (
    <div>
      <h3 style={{ marginTop: 0, fontSize: 16, fontWeight: 600 }}>Personalización</h3>
      <p style={{ color: 'var(--colorNeutralForeground3)', fontSize: 13 }}>
        Opciones de personalización — próximamente
      </p>
    </div>
  );
};

// ── Helper Functions ──────────────────────────────────────────────────────

function getServerIcon(serverName: string): string {
  const icons: Record<string, string> = {
    github: '🐙',
    gmail: '📧',
    'google-calendar': '📅',
    slack: '💬',
    jira: '📋',
    'youtube-api': '▶️',
    'macae-local': '🤖',
    salesforce: '☁️',
    trello: '📌',
    notion: '📝',
  };
  return icons[serverName] ?? '🔌';
}
