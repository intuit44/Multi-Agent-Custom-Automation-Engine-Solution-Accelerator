import { apiClient } from './apiClient';
import {
  AgentMessage,
  HumanClarification,
  InputTask,
  InputTaskResponse,
  Plan,
  PlanApprovalRequest,
  PlanApprovalResponse,
  AgentMessageBE,
  MPlanBE,
  TeamConfigurationBE,
  PlanFromAPI,
  AgentMessageResponse,
  ChatMessageRequest,
  ChatMessageResponse,
} from '../models';

// Constants for endpoints
const API_ENDPOINTS = {
  PROCESS_REQUEST: '/v4/process_request',
  PLANS: '/v4/plans',
  PLAN: '/v4/plan',
  PLAN_APPROVAL: '/v4/plan_approval',
  RESUME_PLAN: '/v4/resume_plan',
  HUMAN_CLARIFICATION: '/v4/user_clarification',
  USER_BROWSER_LANGUAGE: '/user_browser_language',
  AGENT_MESSAGE: '/v4/agent_message',
  CHAT_MESSAGE: '/v4/chat/message',
  CHAT_MESSAGE_STREAM: '/v4/chat/message/stream',
  CHAT_UPLOAD_FILE: '/v4/chat/upload-file',
  CHAT_SESSIONS: '/v4/chat/sessions',
  CHAT_SESSION_NEW: '/v4/chat/sessions/new',
};

// Simple cache implementation
interface CacheEntry<T> {
  data: T;
  timestamp: number;
  ttl: number; // Time to live in ms
}

class APICache {
  private cache: Map<string, CacheEntry<any>> = new Map();

  set<T>(key: string, data: T, ttl = 60000): void {
    // Default TTL: 1 minute
    this.cache.set(key, {
      data,
      timestamp: Date.now(),
      ttl,
    });
  }

  get<T>(key: string): T | null {
    const entry = this.cache.get(key);
    if (!entry) return null;

    // Check if entry is expired
    if (Date.now() - entry.timestamp > entry.ttl) {
      this.cache.delete(key);
      return null;
    }

    return entry.data;
  }

  clear(): void {
    this.cache.clear();
  }

  invalidate(pattern: RegExp): void {
    for (const key of this.cache.keys()) {
      if (pattern.test(key)) {
        this.cache.delete(key);
      }
    }
  }
}

// Request tracking to prevent duplicate requests
class RequestTracker {
  private pendingRequests: Map<string, Promise<any>> = new Map();

  async trackRequest<T>(key: string, requestFn: () => Promise<T>): Promise<T> {
    // If request is already pending, return the existing promise
    if (this.pendingRequests.has(key)) {
      return this.pendingRequests.get(key)!;
    }

    // Create new request
    const requestPromise = requestFn();

    // Track the request
    this.pendingRequests.set(key, requestPromise);

    try {
      const result = await requestPromise;
      return result;
    } finally {
      // Remove from tracking when done (success or failure)
      this.pendingRequests.delete(key);
    }
  }
}

export class APIService {
  private _cache = new APICache();
  private _requestTracker = new RequestTracker();

  /**
   * Create a new plan with RAI validation
   * @param inputTask The task description and optional session ID
   * @returns Promise with the response containing plan ID and status
   */
  // async createPlan(inputTask: InputTask): Promise<{ plan_id: string; status: string; session_id: string }> {
  //     return apiClient.post(API_ENDPOINTS.PROCESS_REQUEST, inputTask);
  // }

  async createPlan(inputTask: InputTask): Promise<InputTaskResponse> {
    return apiClient.post(API_ENDPOINTS.PROCESS_REQUEST, inputTask);
  }

  /**
   * Get all plans, optionally filtered by session ID
   * @param sessionId Optional session ID to filter plans
   * @param useCache Whether to use cached data or force fresh fetch
   * @returns Promise with array of plans with their steps
   */
  async getPlans(sessionId?: string, useCache = true): Promise<Plan[]> {
    const cacheKey = `plans_${sessionId || 'all'}`;
    const params = sessionId ? { session_id: sessionId } : {};
    const fetcher = async () => {
      const data = await apiClient.get(API_ENDPOINTS.PLANS, { params });
      if (useCache) {
        this._cache.set(cacheKey, data, 30000); // Cache for 30 seconds
      }
      return data;
    };

    if (useCache) {
      return this._requestTracker.trackRequest(cacheKey, fetcher);
    }

    return fetcher();
  }

  /**
   * Get a single plan by plan ID
   * @param planId Plan ID to fetch
   * @param useCache Whether to use cached data or force fresh fetch
   * @returns Promise with the plan and its steps
   */
  async getPlanById(planId: string, useCache = true): Promise<PlanFromAPI> {
    const cacheKey = `plan_by_id_${planId}`;
    const params = { plan_id: planId };

    const fetcher = async () => {
      const data = await apiClient.get(API_ENDPOINTS.PLAN, { params });

      // The API returns an array, but with plan_id filter it should have only one item
      if (!data) {
        throw new Error(`Plan with ID ${planId} not found`);
      }
      console.log('Fetched plan by ID:', data);
      const results = {
        plan: data.plan as Plan,
        messages: data.messages as AgentMessageBE[],
        m_plan: data.m_plan as MPlanBE | null,
        team: data.team as TeamConfigurationBE | null,
        streaming_message: data.streaming_message as string | null,
      } as PlanFromAPI;
      if (useCache) {
        this._cache.set(cacheKey, results, 30000); // Cache for 30 seconds
      }
      return results;
    };

    if (useCache) {
      const cachedPlan = this._cache.get<PlanFromAPI>(cacheKey);
      if (cachedPlan) return cachedPlan;

      return this._requestTracker.trackRequest(cacheKey, fetcher);
    }

    return fetcher();
  }

  /**
   * Approve a plan for execution
   * @param planApprovalData Plan approval data
   * @returns Promise with approval response
   */
  async approvePlan(
    planApprovalData: PlanApprovalRequest
  ): Promise<PlanApprovalResponse> {
    const requestKey = `approve-plan-${planApprovalData.m_plan_id}`;

    return this._requestTracker.trackRequest(requestKey, async () => {
      console.log('📤 Approving plan via v4 API:', planApprovalData);

      const response = await apiClient.post(
        API_ENDPOINTS.PLAN_APPROVAL,
        planApprovalData
      );

      // Invalidate cache since plan execution will start
      this._cache.invalidate(new RegExp(`^plans_`));
      if (planApprovalData.plan_id) {
        this._cache.invalidate(
          new RegExp(`^plan.*_${planApprovalData.plan_id}`)
        );
      }

      console.log('✅ Plan approval successful:', response);
      return response;
    });
  }

  /**
   * Resume orchestration for an orphaned in_progress plan (m_plan is null).
   * Called by PlanPage when it detects a plan stuck without steps.
   */
  async triggerPlanOrchestration(planId: string): Promise<void> {
    console.log('🔄 Re-triggering orchestration for plan:', planId);
    await apiClient.post(API_ENDPOINTS.RESUME_PLAN, { plan_id: planId });
    console.log('✅ Orchestration re-triggered for plan:', planId);
  }

  /**
   * Submit clarification for a plan
   * @param planId Plan ID
   * @param sessionId Session ID
   * @param clarification Clarification text
   * @returns Promise with response object
   */
  async submitClarification(
    request_id: string = '',
    answer: string = '',
    plan_id: string = '',
    m_plan_id: string = ''
  ): Promise<{ status: string; session_id: string }> {
    const clarificationData: HumanClarification = {
      request_id,
      answer,
      plan_id,
      m_plan_id,
    };

    const response = await apiClient.post(
      API_ENDPOINTS.HUMAN_CLARIFICATION,
      clarificationData
    );

    // Invalidate cached data
    this._cache.invalidate(new RegExp(`^(plan|steps)_${plan_id}`));
    this._cache.invalidate(new RegExp(`^plans_`));

    return response;
  }

  /**
   * Clear all cached data
   */
  clearCache(): void {
    this._cache.clear();
  }

  /**
   * Send the user's browser language to the backend
   * @returns Promise with response object
   */
  async sendUserBrowserLanguage(): Promise<{ status: string }> {
    const language = navigator.language || navigator.languages[0] || 'en';
    const response = await apiClient.post(API_ENDPOINTS.USER_BROWSER_LANGUAGE, {
      language,
    });
    return response;
  }
  async sendAgentMessage(data: AgentMessageResponse): Promise<AgentMessage> {
    const t0 = performance.now();
    const result = await apiClient.post(API_ENDPOINTS.AGENT_MESSAGE, data);
    console.log('[agent_message] sent', {
      ms: +(performance.now() - t0).toFixed(1),
      agent: data.agent,
      type: data.agent_type,
    });
    return result;
  }

  /**
   * Upload a file to Foundry for use with code_interpreter.
   * Returns { file_id, filename, size } to attach to subsequent chat messages.
   */
  async uploadChatFile(
    file: File
  ): Promise<{ file_id: string; filename: string; size: number }> {
    const formData = new FormData();
    formData.append('file', file, file.name);
    return apiClient.upload(API_ENDPOINTS.CHAT_UPLOAD_FILE, formData);
  }

  /**
   * Send a chat message through IntentRouter classification
   * @param chatRequest Chat message with optional session_id
   * @returns Promise with intent classification + response
   */
  async sendChatMessage(
    chatRequest: ChatMessageRequest
  ): Promise<ChatMessageResponse> {
    const t0 = performance.now();
    const result = await apiClient.post(
      API_ENDPOINTS.CHAT_MESSAGE,
      chatRequest
    );
    console.log(
      '[chat_message] intent=%s confidence=%.2f',
      result.intent,
      result.confidence,
      {
        ms: +(performance.now() - t0).toFixed(1),
        session_id: result.session_id,
      }
    );
    return result;
  }

  /**
   * Stream a chat message response via SSE.
   * Calls POST /v4/chat/message/stream and reads the event stream.
   */
  async sendChatMessageStream(
    chatRequest: ChatMessageRequest,
    callbacks: {
      onToken: (token: string) => void;
      onIntent: (data: {
        intent: string;
        confidence: number;
        session_id: string;
        plan_id?: string;
        m_plan_id?: string;
      }) => void;
      onDone: (data: {
        intent: string;
        agent: string;
        confidence: number;
        session_id: string;
        plan_id?: string;
        m_plan_id?: string;
      }) => void;
      /** Called when intent router detects a task and creates a plan inline (new unified flow). */
      onPlanCreated?: (planId: string) => void;
      /** Legacy redirect callback — kept for backward compat with HomeInput. */
      onRedirect?: (planId: string) => void;
      onError: (error: string) => void;
      onToolActivity?: (data: {
        activity: string;
        tool: string;
        server?: string;
        success?: boolean;
      }) => void;
      onGeneratedFile?: (data: {
        file_id: string;
        filename: string;
        download_url: string;
      }) => void;
      onOAuthConsentRequest?: (consentLink: string) => void;
    },
    signal?: AbortSignal
  ): Promise<void> {
    const t0 = performance.now();
    const response = await apiClient.stream(
      API_ENDPOINTS.CHAT_MESSAGE_STREAM,
      chatRequest,
      signal
    );

    if (!response.body) {
      throw new Error('No response body for streaming');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        let chunk: ReadableStreamReadResult<Uint8Array>;
        try {
          chunk = await reader.read();
        } catch (e: any) {
          // Barge-in / cancelación explícita del turno: cerrar limpio.
          if (e?.name === 'AbortError' || signal?.aborted) break;
          throw e;
        }
        const { done, value } = chunk;
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop() || '';

        for (const event of events) {
          const dataLine = event
            .split('\n')
            .find((l) => l.startsWith('data: '));
          if (!dataLine) continue;

          try {
            const data = JSON.parse(dataLine.slice(6));
            switch (data.type) {
              case 'token':
                callbacks.onToken(data.content);
                break;
              case 'intent':
                callbacks.onIntent(data);
                break;
              case 'done':
                callbacks.onDone(data);
                break;
              // New unified event: plan created inline (no redirect)
              case 'plan_created':
                callbacks.onPlanCreated?.(data.plan_id);
                break;
              // Legacy: kept for HomeInput backward compat
              case 'redirect':
                callbacks.onRedirect?.(data.redirect_to_plan);
                break;
              case 'error':
                callbacks.onError(data.message);
                break;
              case 'tool_activity':
                callbacks.onToolActivity?.(data);
                break;
              case 'generated_file':
                callbacks.onGeneratedFile?.(data);
                break;
              case 'oauth_consent_request':
                callbacks.onOAuthConsentRequest?.(data.consent_link);
                break;
            }
          } catch {
            console.warn('[chat_stream] Failed to parse SSE event:', dataLine);
          }
        }
      }
    } finally {
      reader.releaseLock();
    }

    console.log('[chat_stream] completed', {
      ms: +(performance.now() - t0).toFixed(1),
    });
  }

  // ── Chat Session CRUD ────────────────────────────────────────

  /**
   * Get all chat sessions for the current user.
   */
  async getChatSessions(): Promise<{
    sessions: Array<{
      id: string;
      user_id: string;
      session_name: string;
      message_count: number;
      created_at: string;
      updated_at: string;
      last_message_at: string | null;
      is_active: boolean;
    }>;
  }> {
    return apiClient.get(API_ENDPOINTS.CHAT_SESSIONS);
  }

  /**
   * Get a single chat session with messages.
   */
  async getChatSession(sessionId: string): Promise<{
    id: string;
    session_name: string;
    messages: Array<{
      id: string;
      content: string;
      sender: string;
      timestamp: string;
      metadata: Record<string, unknown>;
    }>;
  }> {
    return apiClient.get(`${API_ENDPOINTS.CHAT_SESSIONS}/${sessionId}`);
  }

  /**
   * Create a new chat session.
   */
  async createChatSession(): Promise<{
    success: boolean;
    data: { session_id: string; session_name: string; created_at: string };
  }> {
    return apiClient.post(API_ENDPOINTS.CHAT_SESSION_NEW, {});
  }

  /**
   * Delete a chat session.
   */
  async deleteChatSession(sessionId: string): Promise<{ success: boolean }> {
    return apiClient.delete(`${API_ENDPOINTS.CHAT_SESSIONS}/${sessionId}`);
  }

  // ── MCP Connections ────────────────────────────────────────

  /**
   * Get all available MCP servers from the catalog.
   */
  async getMcpServers(): Promise<{
    servers: Array<{
      id: string;
      server_name: string;
      display_name: string;
      description?: string;
      endpoint: string;
      transport: string;
      auth_type: string;
      capabilities?: string[];
      tool_count?: number;
      icon_url?: string;
      enabled?: boolean;
    }>;
    total: number;
  }> {
    return apiClient.get('/v4/mcp/connections/servers');
  }

  /**
   * Get user's MCP server connections with status.
   */
  async getUserMcpConnections(): Promise<{
    connections: Array<{
      server: {
        id: string;
        server_name: string;
        display_name: string;
        description?: string;
        endpoint: string;
        transport: string;
        auth_type: string;
        capabilities?: string[];
        tool_count?: number;
        icon_url?: string;
      };
      connection: {
        status: string;
        connected_at?: string;
        last_error?: string;
      } | null;
      is_connected: boolean;
      needs_auth: boolean;
    }>;
    user_id: string;
  }> {
    return apiClient.get('/v4/mcp/connections/user');
  }

  /**
   * Initiate connection to an MCP server.
   * Returns OAuth URL if authentication is required.
   */
  async connectToMcpServer(
    serverName: string,
    credentials?: Record<string, string>
  ): Promise<{
    status: string;
    needs_auth: boolean;
    oauth_url?: string;
    connection?: Record<string, unknown>;
  }> {
    return apiClient.post(
      `/v4/mcp/connections/user/${serverName}/connect`,
      credentials ? { credentials } : undefined
    );
  }

  /**
   * Activate MCP connection (called after OAuth callback).
   */
  async activateMcpConnection(
    serverName: string,
    secretRef?: string
  ): Promise<{
    connection: Record<string, unknown>;
    activated: boolean;
  }> {
    return apiClient.patch(
      `/v4/mcp/connections/user/${serverName}/activate`,
      secretRef ? { secret_ref: secretRef } : {}
    );
  }

  /**
   * Disconnect from an MCP server.
   */
  async disconnectMcpServer(serverName: string): Promise<{
    disconnected: boolean;
    server_name: string;
  }> {
    return apiClient.delete(`/v4/mcp/connections/user/${serverName}`);
  }

  /**
   * Register a new MCP server in the catalog (admin operation).
   */
  async registerMcpServer(entry: {
    server_name: string;
    display_name: string;
    description?: string;
    endpoint: string;
    transport: string;
    auth_type: string;
    auth_fields?: string[];
    oauth_scopes?: string[];
    capabilities?: string[];
    icon_url?: string | null;
    allowed_agents?: string[];
    enabled?: boolean;
  }): Promise<{
    server: Record<string, unknown>;
  }> {
    return apiClient.post('/v4/mcp/connections/servers', entry);
  }

  /**
   * Update an existing MCP server in the catalog (admin operation).
   */
  async updateMcpServer(
    serverId: string,
    payload: {
      server_name?: string;
      display_name?: string;
      description?: string;
      endpoint?: string;
      transport?: string;
      auth_type?: string;
      auth_fields?: string[];
      oauth_scopes?: string[];
      oauth_authorize_url?: string | null;
      oauth_token_url?: string | null;
      oauth_client_id_env?: string | null;
      capabilities?: string[];
      icon_url?: string | null;
      allowed_agents?: string[];
      enabled?: boolean;
    }
  ): Promise<{
    server: Record<string, unknown>;
    updated: boolean;
  }> {
    return apiClient.put(`/v4/mcp/connections/servers/${serverId}`, payload);
  }

  /**
   * Delete an MCP server from the catalog (admin operation).
   */
  async deleteMcpServer(serverId: string): Promise<{
    deleted: boolean;
    server_id: string;
  }> {
    return apiClient.delete(`/v4/mcp/connections/servers/${serverId}`);
  }
}

// Export a singleton instance
export const apiService = new APIService();
