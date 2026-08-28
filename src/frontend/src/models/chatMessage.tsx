/**
 * Chat message models for conversational mode (P0)
 * Maps to backend ChatMessageRequest / ChatMessageResponse
 */

/** Request sent to POST /v4/chat/message */
export interface ChatMessageRequest {
  session_id?: string;
  message: string;
  model?: string;
  file_ids?: string[]; // Foundry file IDs from /v4/chat/upload-file
  plan_id?: string; // Set when the message originates from an open plan
  /** Active workspace slug. Artefacts produced by agents land here. Omit = Blob-only fallback. */
  workspace_id?: string;
  // Chat|Plan selector: false = this message may never create a plan.
  // Plan position never uses this flag — it calls /process_request directly.
  allow_plan?: boolean;
}

/** Response from POST /v4/chat/message */
export interface ChatMessageResponse {
  session_id: string;
  intent: 'task' | 'conversational' | 'mcp_query';
  confidence: number;
  response: string;
  agent: string;
  redirect_to_plan?: string | null;
}

/** A single message in the chat history */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
  intent?: string;
  agent?: string;
  confidence?: number;
}

/** Chat session state */
export interface ChatSession {
  session_id: string;
  messages: ChatMessage[];
  created_at: Date;
}
