/**
 * WorkspaceEditor — Monaco-powered Code + Diff tabs for PreviewRightSlot.
 *
 * Architecture
 * ────────────
 *   Browser Monaco  ←→  PUT /api/v4/workspace/files/{path}  ←→  disk
 *   MCP filesystem  ←→  same disk path
 *
 * Both Monaco (user) and MCP agents write to the same physical workspace,
 * so they always share one source of truth.
 *
 * Tabs
 * ────
 *   Code  — editable Monaco; Save writes PUT /workspace/files/{path}
 *   Diff  — MonacoDiffEditor: git HEAD (original) vs current disk content
 *           loaded from GET /workspace/diff/{path}
 *
 * Availability
 * ────────────
 *   Endpoints are dev-only (backend returns 403 in prod).
 *   When unavailable the tab renders a graceful message instead of crashing.
 */

import Editor, { DiffEditor } from '@monaco-editor/react';
import { Button, Spinner, Tooltip } from '@fluentui/react-components';
import {
  Save20Regular,
  ArrowCounterclockwise20Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { resolveApiUrl } from '../../api/config';

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

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(resolveApiUrl(url), init);
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
}

// ── component ──────────────────────────────────────────────────────────────

export const WorkspaceEditor: React.FC<WorkspaceEditorProps> = ({
  title,
  content,
  lang,
}) => {
  const path = workspacePath(title);
  const language = lang ?? langFromFilename(title);

  const [tab, setTab] = useState<EditorTab>('code');

  // ── Code tab state ──
  const [editorValue, setEditorValue] = useState(content);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const lastSaved = useRef(content);

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
    setSaving(true);
    setSaveMsg(null);
    try {
      await apiFetch(`/api/v4/workspace/files/${path}`, {
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
      setSaving(false);
    }
  }, [editorValue, path]);

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
    setDiffLoading(true);
    setDiffError(null);
    try {
      const data = await apiFetch<{ original: string; modified: string }>(
        `/api/v4/workspace/diff/${path}`
      );
      setDiffData(data);
    } catch (e) {
      setDiffError((e as Error).message);
    } finally {
      setDiffLoading(false);
    }
  }, [path]);

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
      {/* Tab bar */}
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
        {tabBtn('code', 'Code')}
        {tabBtn('diff', 'Diff')}

        {tab === 'code' && (
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
                icon={saving ? <Spinner size="tiny" /> : <Save20Regular />}
                onClick={handleSave}
                disabled={saving || !dirty}
              >
                Save
              </Button>
            </Tooltip>
          </div>
        )}

        {tab === 'diff' && (
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
                {diffError.includes('403')
                  ? 'Workspace diff is only available in dev mode.'
                  : diffError}
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
