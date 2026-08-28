/**
 * WorkspaceSelector — panel en PlanPanelLeft que permite al usuario crear,
 * seleccionar y eliminar workspaces nombrados y persistentes.
 *
 * El workspace activo se guarda en localStorage bajo la clave
 * "macae_active_workspace_id" para que persista entre sesiones del navegador.
 * El backend lo isola automáticamente por user_id (EasyAuth).
 */
import {
  Body1Strong,
  Button,
  Caption1,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  DialogTrigger,
  Input,
  Spinner,
  Tooltip,
} from '@fluentui/react-components';
import {
  Add20Regular,
  Delete20Regular,
  FolderOpen20Regular,
  Checkmark20Regular,
} from '@fluentui/react-icons';
import { useCallback, useEffect, useState } from 'react';
import {
  WorkspaceService,
  WorkspaceSummary,
} from '../../services/WorkspaceService';

const LS_KEY = 'macae_active_workspace_id';

export interface WorkspaceSelectorProps {
  /** Called whenever the active workspace changes (including on first load). */
  onWorkspaceChange?: (workspaceId: string | null) => void;
}

export const WorkspaceSelector: React.FC<WorkspaceSelectorProps> = ({
  onWorkspaceChange,
}) => {
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(() =>
    localStorage.getItem(LS_KEY)
  );

  // Create dialog state
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newRepoUrl, setNewRepoUrl] = useState('');
  const [newRepoToken, setNewRepoToken] = useState('');
  const [newLocalPath, setNewLocalPath] = useState('');
  const [creating, setCreating] = useState(false);

  // Delete confirm state
  const [deleteTarget, setDeleteTarget] = useState<WorkspaceSummary | null>(
    null
  );
  const [deleting, setDeleting] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const list = await WorkspaceService.list();
      setWorkspaces(list);
      // If the stored active workspace no longer exists, clear it
      if (activeId && !list.find((w) => w.workspace_id === activeId)) {
        setActiveId(null);
        localStorage.removeItem(LS_KEY);
        onWorkspaceChange?.(null);
      }
    } catch {
      // silently ignore — panel is non-critical
    } finally {
      setLoading(false);
    }
  }, [activeId, onWorkspaceChange]);

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Notify parent on mount with the persisted value
  useEffect(() => {
    onWorkspaceChange?.(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectWorkspace = (id: string) => {
    setActiveId(id);
    localStorage.setItem(LS_KEY, id);
    onWorkspaceChange?.(id);
  };

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    try {
      const ws = await WorkspaceService.create({
        name,
        repo_url: newRepoUrl.trim() || undefined,
        repo_token: newRepoToken.trim() || undefined,
        local_path: newLocalPath.trim() || undefined,
      });
      setWorkspaces((prev) => {
        const exists = prev.find((w) => w.workspace_id === ws.workspace_id);
        if (exists)
          return prev.map((w) => (w.workspace_id === ws.workspace_id ? ws : w));
        return [...prev, ws];
      });
      selectWorkspace(ws.workspace_id);
      setCreateOpen(false);
      setNewName('');
      setNewRepoUrl('');
      setNewRepoToken('');
      setNewLocalPath('');
    } catch {
      // TODO: surface error toast
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await WorkspaceService.remove(deleteTarget.workspace_id);
      const next = workspaces.filter(
        (w) => w.workspace_id !== deleteTarget.workspace_id
      );
      setWorkspaces(next);
      if (activeId === deleteTarget.workspace_id) {
        const newActive = next[0]?.workspace_id ?? null;
        setActiveId(newActive);
        if (newActive) localStorage.setItem(LS_KEY, newActive);
        else localStorage.removeItem(LS_KEY);
        onWorkspaceChange?.(newActive);
      }
    } catch {
      // TODO: surface error toast
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  };

  return (
    <div style={{ padding: '8px 12px' }}>
      {/* Header row */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 6,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <FolderOpen20Regular />
          <Body1Strong>Workspaces</Body1Strong>
        </div>
        <Tooltip content="Nuevo workspace" relationship="label">
          <Button
            appearance="subtle"
            size="small"
            icon={<Add20Regular />}
            onClick={() => setCreateOpen(true)}
            aria-label="Nuevo workspace"
          />
        </Tooltip>
      </div>

      {/* List */}
      {loading && <Spinner size="tiny" style={{ margin: '4px 0' }} />}
      {!loading && workspaces.length === 0 && (
        <Caption1
          style={{
            color: 'var(--colorNeutralForeground3)',
            display: 'block',
            marginBottom: 4,
          }}
        >
          Sin workspaces. Crea uno para empezar.
        </Caption1>
      )}
      {workspaces.map((ws) => {
        const isActive = ws.workspace_id === activeId;
        return (
          <div
            key={ws.workspace_id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              padding: '3px 4px',
              borderRadius: 4,
              background: isActive
                ? 'var(--colorBrandBackground2)'
                : 'transparent',
              cursor: 'pointer',
            }}
            onClick={() => selectWorkspace(ws.workspace_id)}
            role="button"
            tabIndex={0}
            aria-pressed={isActive}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                selectWorkspace(ws.workspace_id);
              }
            }}
          >
            {isActive && (
              <Checkmark20Regular
                style={{ color: 'var(--colorBrandForeground1)', flexShrink: 0 }}
              />
            )}
            {!isActive && <span style={{ width: 20, flexShrink: 0 }} />}
            <Caption1
              style={{
                flex: 1,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontWeight: isActive ? 600 : undefined,
              }}
              title={ws.name}
            >
              {ws.name}
            </Caption1>
            <Caption1
              style={{ color: 'var(--colorNeutralForeground3)', flexShrink: 0 }}
            >
              {ws.file_count}
            </Caption1>
            <Tooltip content={`Eliminar "${ws.name}"`} relationship="label">
              <Button
                appearance="subtle"
                size="small"
                icon={<Delete20Regular />}
                aria-label={`Eliminar ${ws.name}`}
                style={{ flexShrink: 0 }}
                onClick={(e) => {
                  e.stopPropagation();
                  setDeleteTarget(ws);
                }}
              />
            </Tooltip>
          </div>
        );
      })}

      {/* Create dialog */}
      <Dialog
        open={createOpen}
        onOpenChange={(_e, { open }) => setCreateOpen(open)}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Nuevo workspace</DialogTitle>
            <DialogContent>
              <div
                style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}
              >
                <Input
                  autoFocus
                  placeholder="Nombre del workspace"
                  value={newName}
                  onChange={(_e, d) => setNewName(d.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleCreate();
                  }}
                  style={{ width: '100%' }}
                />
                <Input
                  placeholder="URL del repo https (opcional — clona tu proyecto)"
                  value={newRepoUrl}
                  onChange={(_e, d) => setNewRepoUrl(d.value)}
                  style={{ width: '100%' }}
                />
                {newRepoUrl.trim() && (
                  <Input
                    type="password"
                    placeholder="Token del repo (opcional, no se almacena)"
                    value={newRepoToken}
                    onChange={(_e, d) => setNewRepoToken(d.value)}
                    style={{ width: '100%' }}
                  />
                )}
                <Input
                  placeholder="Ruta local (opcional, solo dev — vincula tu carpeta)"
                  value={newLocalPath}
                  onChange={(_e, d) => setNewLocalPath(d.value)}
                  style={{ width: '100%' }}
                />
              </div>
            </DialogContent>
            <DialogActions>
              <DialogTrigger disableButtonEnhancement>
                <Button appearance="secondary" disabled={creating}>
                  Cancelar
                </Button>
              </DialogTrigger>
              <Button
                appearance="primary"
                onClick={handleCreate}
                disabled={!newName.trim() || creating}
                icon={creating ? <Spinner size="tiny" /> : undefined}
              >
                Crear
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>

      {/* Delete confirm dialog */}
      <Dialog
        open={!!deleteTarget}
        onOpenChange={(_e, { open }) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Eliminar workspace</DialogTitle>
            <DialogContent>
              ¿Eliminar <strong>{deleteTarget?.name}</strong>? Esta acción borra
              todos los archivos y no se puede deshacer.
            </DialogContent>
            <DialogActions>
              <DialogTrigger disableButtonEnhancement>
                <Button appearance="secondary" disabled={deleting}>
                  Cancelar
                </Button>
              </DialogTrigger>
              <Button
                appearance="primary"
                onClick={handleDelete}
                disabled={deleting}
                icon={deleting ? <Spinner size="tiny" /> : undefined}
              >
                Eliminar
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </div>
  );
};

export default WorkspaceSelector;
