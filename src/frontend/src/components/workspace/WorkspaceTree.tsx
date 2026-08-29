/**
 * WorkspaceTree — lazy project explorer for the artifact panel's resting slot.
 *
 * Navigates the AUTHORIZED workspace filesystem as a real tree: one
 * `GET /workspace/{id}/entries?path=` request PER LEVEL, fetched on first
 * expand — never a recursive walk of the whole repo on load. Directories
 * carry an aggregate git marker (M/?) when anything beneath them changed;
 * files carry their own. Clicking a file hands its full path to the viewer
 * (Monaco); clicking a directory toggles and lazily loads its children.
 */
import { Button, Input, Spinner, Tooltip } from '@fluentui/react-components';
import {
  ArrowClockwise20Regular,
  ChevronDown16Regular,
  ChevronRight16Regular,
  Document16Regular,
  Folder16Regular,
  Search16Regular,
} from '@fluentui/react-icons';
import { useCallback, useEffect, useState } from 'react';
import { apiClient } from '../../api/apiClient';

interface DirEntry {
  name: string;
  type: 'directory' | 'file';
  size?: number | null;
  status?: string | null; // "M" | "?" | null
}

interface WorkspaceTreeProps {
  workspaceId: string;
  onOpenFile: (path: string) => void;
}

const statusColor = (s?: string | null): string =>
  s === 'M' || s === '?'
    ? 'var(--colorStatusWarningForeground1)'
    : 'var(--colorStatusSuccessForeground1)';

export const WorkspaceTree: React.FC<WorkspaceTreeProps> = ({
  workspaceId,
  onOpenFile,
}) => {
  // childrenMap key = workspace-relative dir path ('' = root).
  const [childrenMap, setChildrenMap] = useState<
    Record<string, DirEntry[] | 'loading'>
  >({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const load = useCallback(
    async (path: string) => {
      setChildrenMap((prev) => ({ ...prev, [path]: 'loading' }));
      try {
        const r: { entries?: DirEntry[] } = await apiClient.get(
          `/v4/workspace/${encodeURIComponent(workspaceId)}/entries`,
          { params: path ? { path } : undefined }
        );
        setChildrenMap((prev) => ({ ...prev, [path]: r.entries ?? [] }));
      } catch {
        setChildrenMap((prev) => ({ ...prev, [path]: [] }));
      }
    },
    [workspaceId]
  );

  // Workspace switch: reset and load the root level only.
  useEffect(() => {
    setChildrenMap({});
    setExpanded(new Set());
    load('');
  }, [load]);

  const toggleDir = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
    if (childrenMap[path] === undefined) load(path);
  };

  const refresh = () => {
    setChildrenMap({});
    setExpanded(new Set());
    load('');
  };

  // ── search: whole-workspace filename filter (git ls-files server-side) ──
  const [q, setQ] = useState('');
  const [results, setResults] = useState<string[] | null>(null);
  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setResults(null);
      return;
    }

    let cancelled = false;
    const t = setTimeout(() => {
      apiClient
        .get(`/v4/workspace/${encodeURIComponent(workspaceId)}/search`, {
          params: { q: query },
        })
        .then((r: { matches?: string[] } | null) => {
          if (!cancelled) setResults(r?.matches ?? []);
        })
        .catch(() => {
          if (!cancelled) setResults([]);
        });
    }, 250);

    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q, workspaceId]);

  const renderLevel = (parent: string, depth: number): React.ReactNode => {
    const children = childrenMap[parent];
    if (children === 'loading')
      return (
        <Spinner
          size="tiny"
          style={{ margin: `2px 0 2px ${12 + depth * 14}px` }}
        />
      );
    if (!children) return null;
    return children.map((e) => {
      const full = parent ? `${parent}/${e.name}` : e.name;
      const isOpen = expanded.has(full);
      return (
        <div key={full}>
          <Button
            appearance="subtle"
            size="small"
            style={{
              width: '100%',
              justifyContent: 'flex-start',
              paddingLeft: `${4 + depth * 14}px`,
              minHeight: '24px',
            }}
            icon={
              e.type === 'directory' ? (
                isOpen ? (
                  <ChevronDown16Regular />
                ) : (
                  <ChevronRight16Regular />
                )
              ) : (
                <Document16Regular />
              )
            }
            onClick={() =>
              e.type === 'directory' ? toggleDir(full) : onOpenFile(full)
            }
          >
            {e.type === 'directory' && (
              <Folder16Regular style={{ marginRight: 4, flexShrink: 0 }} />
            )}
            <span
              style={{
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                flex: 1,
                textAlign: 'left',
              }}
            >
              {e.name}
            </span>
            {e.status && (
              <span
                style={{
                  color: statusColor(e.status),
                  fontWeight: 700,
                  fontSize: '11px',
                  flexShrink: 0,
                  marginLeft: 6,
                }}
              >
                {e.status}
              </span>
            )}
          </Button>
          {e.type === 'directory' && isOpen && renderLevel(full, depth + 1)}
        </div>
      );
    });
  };

  const root = childrenMap[''];
  // Empty workspace: yield the slot (same behavior as the old flat panel).
  if (Array.isArray(root) && root.length === 0) return null;

  return (
    <div
      style={{
        width: '280px',
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        borderLeft: '1px solid var(--colorNeutralStroke1)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '4px',
          padding: '6px 8px',
          borderBottom: '1px solid var(--colorNeutralStroke1)',
        }}
      >
        <Input
          size="small"
          placeholder="Buscar archivo…"
          value={q}
          onChange={(_e, d) => setQ(d.value)}
          contentBefore={<Search16Regular />}
          style={{ flex: 1, minWidth: 0 }}
          aria-label="Buscar archivo en el workspace"
        />
        <Tooltip content="Refrescar" relationship="label">
          <Button
            appearance="subtle"
            size="small"
            icon={<ArrowClockwise20Regular />}
            aria-label="Refrescar archivos del workspace"
            onClick={refresh}
          />
        </Tooltip>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px' }}>
        {results !== null ? (
          results.length === 0 ? (
            <div
              style={{
                padding: '8px',
                fontSize: '12px',
                color: 'var(--colorNeutralForeground3)',
              }}
            >
              Sin coincidencias.
            </div>
          ) : (
            results.map((p) => (
              <Button
                key={p}
                appearance="subtle"
                size="small"
                icon={<Document16Regular />}
                style={{
                  width: '100%',
                  justifyContent: 'flex-start',
                  minHeight: '24px',
                }}
                onClick={() => onOpenFile(p)}
              >
                <span
                  style={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {p}
                </span>
              </Button>
            ))
          )
        ) : (
          renderLevel('', 0)
        )}
      </div>
    </div>
  );
};

export default WorkspaceTree;
