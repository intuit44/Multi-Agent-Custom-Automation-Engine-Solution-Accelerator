/**
 * Script para registrar GitHub Copilot MCP en el catálogo
 *
 * Ejecutar desde la consola del navegador o como función auxiliar:
 *
 * import { registerGitHubMCP } from './utils/registerMCP';
 * await registerGitHubMCP();
 */

import { apiService } from '../api/apiService';

export async function registerGitHubMCP() {
  try {
    const result = await apiService.registerMcpServer({
      server_name: "github",
      display_name: "GitHub Copilot MCP",
      description: "Accede a repositorios, issues y pull requests de GitHub. Crea issues, gestiona PRs y analiza código.",
      endpoint: "https://api.githubcopilot.com/mcp",
      transport: "streamable-http",
      auth_type: "oauth2",
      auth_fields: ["access_token"],
      oauth_scopes: ["repo", "read:user", "write:issues"],
      capabilities: ["tools", "resources"],
      icon_url: null,
      allowed_agents: [], // Empty = available to all agents
      enabled: true,
    });

    console.log('✅ GitHub MCP registered successfully:', result);
    return result;
  } catch (error) {
    console.error('❌ Error registering GitHub MCP:', error);
    throw error;
  }
}

export async function registerExampleMCPServers() {
  const servers = [
    {
      server_name: "github",
      display_name: "GitHub Copilot MCP",
      description: "Accede a repositorios, issues y pull requests de GitHub",
      endpoint: "https://api.githubcopilot.com/mcp",
      transport: "streamable-http" as const,
      auth_type: "oauth2" as const,
      auth_fields: ["access_token"],
      oauth_scopes: ["repo", "read:user"],
      capabilities: ["tools", "resources"],
      icon_url: null,
      enabled: true,
    },
    {
      server_name: "gmail",
      display_name: "Gmail API",
      description: "Lee y envía correos electrónicos desde tu cuenta de Gmail",
      endpoint: "https://gmail.googleapis.com/mcp",
      transport: "streamable-http" as const,
      auth_type: "oauth2" as const,
      auth_fields: ["access_token"],
      oauth_scopes: ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"],
      capabilities: ["tools"],
      icon_url: null,
      enabled: true,
    },
    {
      server_name: "google-calendar",
      display_name: "Google Calendar",
      description: "Gestiona eventos y reuniones en tu calendario de Google",
      endpoint: "https://calendar.googleapis.com/mcp",
      transport: "streamable-http" as const,
      auth_type: "oauth2" as const,
      auth_fields: ["access_token"],
      oauth_scopes: ["https://www.googleapis.com/auth/calendar"],
      capabilities: ["tools"],
      icon_url: null,
      enabled: true,
    },
    {
      server_name: "macae-local",
      display_name: "MACAE Local MCP",
      description: "Servidor MCP local sin autenticación para pruebas",
      endpoint: "http://localhost:8000/mcp",
      transport: "streamable-http" as const,
      auth_type: "none" as const,
      auth_fields: [],
      oauth_scopes: [],
      capabilities: ["tools", "resources", "prompts"],
      icon_url: null,
      enabled: true,
    },
  ];

  const results = [];
  for (const server of servers) {
    try {
      const result = await apiService.registerMcpServer(server);
      console.log(`✅ Registered: ${server.display_name}`);
      results.push({ server: server.server_name, success: true, result });
    } catch (error) {
      console.error(`❌ Failed to register ${server.display_name}:`, error);
      results.push({ server: server.server_name, success: false, error });
    }
  }

  return results;
}

// Para ejecutar desde la consola del navegador:
// (window as any).registerGitHubMCP = registerGitHubMCP;
// (window as any).registerExampleMCPServers = registerExampleMCPServers;
