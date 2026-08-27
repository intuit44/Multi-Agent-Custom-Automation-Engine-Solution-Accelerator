/**
 * WorkspaceService — manage named, persistent workspaces per user.
 *
 * Each workspace maps to {MACAE_WORKSPACE_ROOT}/{user_id}/{workspace_id}/
 * on the server (Azure File Share in prod, ~/.macae/workspaces in dev).
 * Users can create named workspaces, switch between them across sessions,
 * and delete ones they no longer need.
 */

export interface WorkspaceSummary {
  workspace_id: string;
  name: string;
  created_at: string; // ISO-8601
  file_count: number;
}

export interface WorkspaceCreateRequest {
  name: string;
  workspace_id?: string; // client-supplied slug; server generates if omitted
}

const BASE = '/api/v4/workspaces';

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(
      `WorkspaceService ${init?.method ?? 'GET'} ${url}: ${res.status} ${text}`
    );
  }
  if (res.status === 204) return undefined as unknown as T;
  return res.json() as Promise<T>;
}

export const WorkspaceService = {
  /** List all workspaces belonging to the authenticated user. */
  async list(): Promise<WorkspaceSummary[]> {
    const res = await apiFetch<{ workspaces: WorkspaceSummary[] }>(BASE);
    return res.workspaces ?? [];
  },

  /** Create (or re-open) a named workspace. Returns the workspace summary. */
  async create(req: WorkspaceCreateRequest): Promise<WorkspaceSummary> {
    return apiFetch<WorkspaceSummary>(BASE, {
      method: 'POST',
      body: JSON.stringify(req),
    });
  },

  /** Delete a workspace and all its files (irreversible). */
  async remove(workspaceId: string): Promise<void> {
    await apiFetch<void>(`${BASE}/${workspaceId}`, { method: 'DELETE' });
  },
};
