/**
 * MCP Connection Models — Frontend types for external MCP server integrations
 */

export interface MCPServerEntry {
  id?: string;
  server_name: string;
  display_name: string;
  description?: string;
  endpoint: string;
  transport: 'streamable-http' | 'stdio' | 'sse';
  auth_type: 'none' | 'oauth2' | 'api_key' | 'bearer_token' | 'managed_identity';
  auth_fields?: string[];
  oauth_scopes?: string[];
  capabilities?: string[];
  tool_count?: number;
  resource_count?: number;
  icon_url?: string | null;
  allowed_agents?: string[];
  enabled?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface MCPUserConnectionDetails {
  status: 'active' | 'pending_auth' | 'expired' | 'revoked' | 'error';
  connected_at?: string;
  last_used_at?: string;
  token_expires_at?: string;
  last_error?: string | null;
  secret_ref?: string;
}

export interface UserMCPConnection {
  server: MCPServerEntry;
  connection: MCPUserConnectionDetails | null;
  is_connected: boolean;
  needs_auth: boolean;
}

export interface MCPConnectResponse {
  status: string;
  needs_auth: boolean;
  oauth_url?: string;
  connection?: MCPUserConnectionDetails;
}

export interface MCPActivateResponse {
  connection: MCPUserConnectionDetails;
  activated: boolean;
}

export interface MCPServerListResponse {
  servers: MCPServerEntry[];
  total: number;
}

export interface MCPUserConnectionsResponse {
  connections: UserMCPConnection[];
  user_id: string;
}
