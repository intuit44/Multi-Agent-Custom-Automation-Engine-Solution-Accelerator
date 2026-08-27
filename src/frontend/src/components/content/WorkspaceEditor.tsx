/**
 * WorkspaceEditor — Monaco Code + Diff tabs bound to the per-session workspace.
 *
 * Architecture
 * ────────────
 *   Monaco  ←→  /api/v4/workspace/{workspaceId}/files/{path}  ←→  server-side
 *   Git     ←→  /api/v4/workspace/{workspaceId}/commit|diff|restore   resolver:
 *                                                    {root}/{user_id}/{workspaceId}/
 *
 * The editor only ever knows the workspace IDENTIFIER (today: the chat
 * session id) — the server resolves it to a per-user data directory with its
 * own git repo. Identical in dev and prod; never the app source tree.
 *
 * Tabs vs actions
 * ───────────────
 *   Tab BUTTONS render only when the parent does not control `tab` (the
 *   panel header already renders Preview/Code/Diff). The ACTION buttons
 *   (Save / Commit / Revert / diff reload) always render: the parent owns
 *   tab navigation, the editor owns file actions.
 */

import Editor, { DiffEditor } from '@monaco-editor/react';
import { Button, Spinner, Tooltip } from '@fluentui/react-components';
import {
  Save20Regular,
  ArrowCounterclockwise20Regular,
  Checkmark20Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ensureFreshToken,
  headerBuilder,
  resolveApiUrl,
} from '../../api/config';

// ── helpers ────────────────────────────────────────────────────────────────

/** Map common extensions to Monaco language IDs. */
function langFromFilename(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() ?? '';
  const MAP: Record<string, string> = {
    ts: 'typescript',
    tsx: 'typescript',
    js: 'javascript',
    jsx: 'javascript',
    py: 'python',
    pyw: 'python',
    html: 'html',
    htm: 'html',
    css: 'css',
    scss: 'scss',
    json: 'json',
    jsonc: 'json',
    yaml: 'yaml',
    yml: 'yaml',
    md: 'markdown',
    sh: 'shell',
    bash: 'shell',
    bicep: 'bicep',
    sql: 'sql',
    xml: 'xml',
    svg: 'xml',
    cs: 'csharp',
    java: 'java',
    cpp: 'cpp',
    c: 'c',
    h: 'c',
    rs: 'rust',
    go: 'go',
    toml: 'toml',
  };
  return MAP[ext] ?? 'plaintext';
}

/** Derive the workspace-relative path from an artifact title.
 *  e.g. "app/main.py" → "app/main.py", "main.py" → "main.py" */
function workspacePath(title: string): string {
  // Strip leading slash if present; keep subdirs as-is
  return title.replace(/^\/+/, '');
}

async function apiFetch<T>(url: string, init: RequestInit = {}): Promise<T> {
  // Same identity as every other API call (apiClient/WorkspaceService):
  // explicit principal headers. Without them the backend dev-fallback invents
  // a SECOND user root and the editor writes where the selector cannot see.
  await ensureFreshToken();
  const res = await fetch(resolveApiUrl(url), {
    ...init,
    headers: headerBuilder({
      'Content-Type': 'application/json',
      ...(init.headers as Record<string, string> | undefined),
    }),
    credentials: 'include',
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

// ── types ──────────────────────────────────────────────────────────────────

type EditorTab = 'code' | 'diff';

interface WorkspaceEditorProps {
  /** Artifact title used to derive the workspace path (e.g. "src/app/main.py"). */
  title: string;
  /** Initial content from the artifact context (may be updated by streaming). */
  content: string;
  /** Monaco language override; auto-detected from title if omitted. */
  lang?: string;
  /** Workspace identifier (the chat session id). Without it the editor is
   *  read-only: there is no workspace to save into. */
  workspaceId?: string | null;
  /** Controlled active tab. When provided the parent owns tab state. */
  tab?: EditorTab;
  /** Called when the user switches tabs. Required when `tab` is provided. */
  onTabChange?: (tab: EditorTab) => void;
}

// ── component ──────────────────────────────────────────────────────────────

export const WorkspaceEditor: React.FC<WorkspaceEditorProps> = ({
  title,
  content,
  lang,
  workspaceId,
  tab: tabProp,
  onTabChange,
}) => {
  const path = workspacePath(title);
  const language = lang ?? langFromFilename(title);
  const base = workspaceId
    ? `/api/v4/workspace/${encodeURIComponent(workspaceId)}`
    : null;

  const [tabInternal, setTabInternal] = useState<EditorTab>('code');
  const tab = tabProp ?? tabInternal;
  const setTab = useCallback(
    (t: EditorTab) => {
      if (onTabChange) onTabChange(t);
      else setTabInternal(t);
    },
    [onTabChange]
  );

  // ── Code tab state ──
  const [editorValue, setEditorValue] = useState(content);
  const [busy, setBusy] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const lastSaved = useRef(content);
  const prevPath = useRef(path);

  // If the user switches to a different artifact/file, reset editor state even if
  // the previous file had unsaved edits.
  useEffect(() => {
    if (prevPath.current !== path) {
      prevPath.current = path;
      setTab('code');
      setEditorValue(content);
      lastSaved.current = content;
      setDirty(false);
      setSaveMsg(null);
    }
  }, [path, content, setTab]);

  // Keep editor in sync when artifact content updates (e.g. streaming finishes)
  // but do NOT overwrite unsaved user edits.
  useEffect(() => {
    if (!dirty) {
      setEditorValue(content);
      lastSaved.current = content;
    }
  }, [content, dirty]);

  const handleEditorChange = useCallback((val: string | undefined) => {
    const v = val ?? '';
    setEditorValue(v);
    setDirty(v !== lastSaved.current);
    setSaveMsg(null);
  }, []);

  const handleSave = useCallback(async () => {
    if (!base) return;
    setBusy(true);
    setSaveMsg(null);
    try {
      await apiFetch(`${base}/files/${path}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editorValue }),
      });
      lastSaved.current = editorValue;
      setDirty(false);
      setSaveMsg('Saved ✓');
      setTimeout(() => setSaveMsg(null), 2000);
    } catch (e) {
      setSaveMsg(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [base, editorValue, path]);

  const handleCommit = useCallback(async () => {
    if (!base) return;
    setBusy(true);
    setSaveMsg(null);
    try {
      const r = await apiFetch<{ committed: boolean; sha: string }>(
        `${base}/commit`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: `Update ${path}` }),
        }
      );
      setSaveMsg(
        r.committed ? `Committed ${r.sha.slice(0, 7)} ✓` : 'Nothing to commit'
      );
      setTimeout(() => setSaveMsg(null), 3000);
    } catch (e) {
      setSaveMsg(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [base, path]);

  const handleRevert = useCallback(() => {
    setEditorValue(lastSaved.current);
    setDirty(false);
    setSaveMsg(null);
  }, []);

  // ── Diff tab state ──
  const [diffData, setDiffData] = useState<{
    original: string;
    modified: string;
  } | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  const loadDiff = useCallback(async () => {
    if (!base) return;
    setDiffLoading(true);
    setDiffError(null);
    setDiffData(null);
    try {
      const data = await apiFetch<{ original: string; modified: string }>(
        `${base}/diff/${path}`
      );
      setDiffData(data);
    } catch (e) {
      setDiffData(null);
      setDiffError((e as Error).message);
    } finally {
      setDiffLoading(false);
    }
  }, [base, path]);

  useEffect(() => {
    if (tab === 'diff') loadDiff();
  }, [tab, loadDiff]);

  // ── render ──
  const tabBtn = (t: EditorTab, label: string) => (
    <Button
      appearance={tab === t ? 'primary' : 'subtle'}
      size="small"
      onClick={() => setTab(t)}
      style={{ minWidth: 'auto' }}
    >
      {label}
    </Button>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Action bar: tab buttons only when uncontrolled; actions always. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '4px',
          padding: '4px 8px',
          borderBottom: '1px solid var(--colorNeutralStroke1)',
          background: 'var(--colorNeutralBackground2)',
          flexShrink: 0,
        }}
      >
        {!tabProp && (
          <>
            {tabBtn('code', 'Code')}
            {tabBtn('diff', 'Diff')}
          </>
        )}

        {!base && (
          <span
            style={{
              fontSize: '11px',
              color: 'var(--colorNeutralForeground3)',
            }}
          >
            Read-only: no active workspace for this view.
          </span>
        )}

        {tab === 'code' && base && (
          <div
            style={{
              display: 'flex',
              gap: '4px',
              marginLeft: 'auto',
              alignItems: 'center',
            }}
          >
            {saveMsg && (
              <span
                style={{
                  fontSize: '11px',
                  color: saveMsg.startsWith('Error')
                    ? 'var(--colorStatusDangerForeground1)'
                    : 'var(--colorStatusSuccessForeground1)',
                }}
              >
                {saveMsg}
              </span>
            )}
            {dirty && (
              <Tooltip content="Revert to last saved" relationship="label">
                <Button
                  appearance="subtle"
                  size="small"
                  icon={<ArrowCounterclockwise20Regular />}
                  onClick={handleRevert}
                />
              </Tooltip>
            )}
            <Tooltip content={`Save to workspace/${path}`} relationship="label">
              <Button
                appearance={dirty ? 'primary' : 'subtle'}
                size="small"
                icon={busy ? <Spinner size="tiny" /> : <Save20Regular />}
                onClick={handleSave}
                disabled={busy || !dirty}
              >
                Save
              </Button>
            </Tooltip>
            <Tooltip
              content="Commit all workspace changes to git"
              relationship="label"
            >
              <Button
                appearance="subtle"
                size="small"
                icon={<Checkmark20Regular />}
                onClick={handleCommit}
                disabled={busy || dirty}
              >
                Commit
              </Button>
            </Tooltip>
          </div>
        )}

        {tab === 'diff' && base && (
          <div style={{ marginLeft: 'auto' }}>
            <Tooltip
              content="Reload diff from git HEAD vs disk"
              relationship="label"
            >
              <Button
                appearance="subtle"
                size="small"
                icon={<ArrowCounterclockwise20Regular />}
                onClick={loadDiff}
                disabled={diffLoading}
              />
            </Tooltip>
          </div>
        )}
      </div>

      {/* Editor area */}
      <div style={{ flex: 1, minHeight: 0 }}>
        {tab === 'code' && (
          <Editor
            height="100%"
            language={language}
            value={editorValue}
            onChange={handleEditorChange}
            options={{
              readOnly: !base,
              minimap: { enabled: false },
              fontSize: 13,
              lineNumbers: 'on',
              wordWrap: 'on',
              scrollBeyondLastLine: false,
              renderWhitespace: 'boundary',
              bracketPairColorization: { enabled: true },
              tabSize: language === 'python' ? 4 : 2,
            }}
          />
        )}

        {tab === 'diff' && (
          <>
            {!base && (
              <div
                style={{
                  padding: '16px',
                  fontSize: '12px',
                  color: 'var(--colorNeutralForeground3)',
                }}
              >
                No active workspace — diff unavailable.
              </div>
            )}
            {diffLoading && (
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'center',
                  padding: '24px',
                }}
              >
                <Spinner size="small" label="Loading diff…" />
              </div>
            )}
            {diffError && (
              <div
                style={{
                  padding: '16px',
                  fontSize: '12px',
                  color: 'var(--colorStatusDangerForeground1)',
                }}
              >
                {diffError}
              </div>
            )}
            {diffData && !diffLoading && (
              <DiffEditor
                height="100%"
                language={language}
                original={diffData.original}
                modified={diffData.modified}
                options={{
                  readOnly: true,
                  minimap: { enabled: false },
                  fontSize: 13,
                  wordWrap: 'on',
                  renderSideBySide: true,
                  scrollBeyondLastLine: false,
                }}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
};
