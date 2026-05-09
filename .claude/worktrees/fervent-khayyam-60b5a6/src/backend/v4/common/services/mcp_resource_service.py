"""
MCP Resource Service - Handles MCP Protocol 2025-11-25 resource operations.

Provides:
- resources/list - List available UI resources via JSON-RPC
- resources/read - Read resource content (widgets, templates) via JSON-RPC
- resources/templates/list - List resource templates with parameters via JSON-RPC

Uses JSON-RPC 2.0 over HTTP to /mcp endpoint.
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class MCPResourceService:
    """Service for reading MCP UI Resources from MCP server via JSON-RPC."""

    def __init__(self, mcp_server_url: str = "http://localhost:9000/mcp"):
        """
        Initialize MCP Resource Service.

        Args:
            mcp_server_url: Base URL for MCP server JSON-RPC endpoint
        """
        self.mcp_server_url = mcp_server_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0)
        self.session_id: Optional[str] = None
        self._initialized: bool = False

    async def _ensure_initialized(self) -> None:
        """Ensure MCP session is initialized with proper handshake."""
        if self._initialized:
            return

        try:
            # Step 1: Send initialize request
            init_result = await self._call_jsonrpc_raw(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {
                        "name": "MACAE-Backend",
                        "version": "5.2.0",
                    },
                },
            )

            logger.info(
                f"MCP initialized: {init_result.get('serverInfo', {}).get('name')}"
            )

            # Step 2: Send initialized notification (no ID, no response expected)
            # MCP protocol expects the notifications/initialized method name.
            await self._send_notification("notifications/initialized")

            self._initialized = True

        except Exception as e:
            logger.error(f"MCP initialization failed: {e}")
            raise

    async def _call_jsonrpc(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Call JSON-RPC with automatic initialization."""
        # Initialize session if not already done
        if method not in ["initialize"]:
            await self._ensure_initialized()

        return await self._call_jsonrpc_raw(method, params)

    async def _send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Send JSON-RPC 2.0 notification (no id, no response expected).

        Used for: initialized, notifications/cancelled, etc.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        # Note: No "id" field for notifications

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        try:
            async with self.client.stream(
                "POST",
                self.mcp_server_url,
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()

                # Extract session ID from response if present
                if "mcp-session-id" in response.headers:
                    new_session_id = response.headers["mcp-session-id"]
                    if new_session_id != self.session_id:
                        self.session_id = new_session_id

                logger.debug(f"Sent notification: {method}")

        except Exception as e:
            logger.warning(f"Failed to send notification {method}: {e}")
            # Don't raise - notifications are fire-and-forget

    async def _call_jsonrpc_raw(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Call MCP server using JSON-RPC 2.0 protocol over streamable-http (SSE).

        Args:
            method: JSON-RPC method name (e.g., "resources/read")
            params: Optional parameters dictionary

        Returns:
            JSON-RPC result

        Raises:
            Exception: If JSON-RPC error occurs
        """
        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        # Build headers for streamable-http transport (SSE)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        # Include session ID if established
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        try:
            # Use streaming to handle SSE response chunks
            async with self.client.stream(
                "POST",
                self.mcp_server_url,
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()

                # Extract session ID from response if present
                if "mcp-session-id" in response.headers:
                    new_session_id = response.headers["mcp-session-id"]
                    if new_session_id != self.session_id:
                        self.session_id = new_session_id
                        logger.debug(f"MCP session established: {self.session_id}")

                # Read response body and parse based on content-type
                body = await response.aread()
                result = self._parse_jsonrpc_response_bytes(body, response.headers.get("content-type", ""))

                # Check for JSON-RPC error
                if "error" in result:
                    error = result["error"]
                    raise Exception(
                        f"JSON-RPC error {error.get('code')}: {error.get('message')}"
                    )

                return result.get("result", {})

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error calling {method}: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Error calling JSON-RPC method {method}: {e}")
            raise

    def _parse_jsonrpc_response_bytes(
        self, body: bytes, content_type: str
    ) -> Dict[str, Any]:
        """
        Parse JSON-RPC response handling both application/json and text/event-stream.
        """
        ct = content_type.lower()
        text = body.decode("utf-8")

        if "application/json" in ct:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

        # SSE or fallback: try SSE parsing first
        try:
            return self._parse_sse_response(text)
        except ValueError:
            pass

        # Last resort: raw JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Cannot parse JSON-RPC response (content-type='{ct}')"
            ) from exc

    def _parse_sse_response(self, body: str) -> Dict[str, Any]:
        """
        Parse JSON-RPC response from SSE (text/event-stream) body.

        FastMCP streamable-http returns SSE frames like:
        event: message
        data: {...json-rpc...}

        Args:
            body: Raw SSE response body

        Returns:
            Parsed JSON-RPC response dict
        """
        for event in body.split("\n\n"):
            data_lines: List[str] = []
            for line in event.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if not data:
                continue
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and "jsonrpc" in parsed:
                    return parsed
            except json.JSONDecodeError:
                continue

        raise ValueError("No valid JSON-RPC payload found in SSE response")

    def _parse_jsonrpc_response(self, response: httpx.Response) -> Dict[str, Any]:
        """
        Parse JSON-RPC response from either application/json or text/event-stream.

        FastMCP streamable-http may return SSE frames like:
        event: message
        data: {...json-rpc...}
        """
        content_type = response.headers.get("content-type", "").lower()

        # Standard JSON response
        if "application/json" in content_type:
            return response.json()

        # SSE response (streamable-http)
        if "text/event-stream" in content_type:
            body = response.text
            for event in body.split("\n\n"):
                data_lines: List[str] = []
                for line in event.splitlines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                if not data_lines:
                    continue
                data = "\n".join(data_lines)
                if not data:
                    continue
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue

            raise ValueError("No valid JSON-RPC payload found in SSE response")

        # Fallback: try raw body as JSON
        try:
            return response.json()
        except Exception as exc:
            raise ValueError(
                f"Unsupported response content-type '{content_type}' for JSON-RPC"
            ) from exc

    async def list_resources(self) -> List[Dict[str, Any]]:
        """
        List all available MCP resources.

        Returns:
            List of resource descriptors with uri, name, description, mimeType
        """
        try:
            result = await self._call_jsonrpc("resources/list")
            resources = result.get("resources", [])

            logger.info(f"Listed {len(resources)} MCP resources")
            return resources

        except Exception as e:
            logger.error(f"Failed to list resources: {e}")
            return []

    async def list_resource_templates(self) -> List[Dict[str, Any]]:
        """
        List all parameterized resource templates (e.g., ui://product-card/{id}).

        Returns:
            List of templates with uriTemplate and parameters
        """
        try:
            result = await self._call_jsonrpc("resources/templates/list")
            templates = result.get("resourceTemplates", [])

            logger.info(f"Listed {len(templates)} resource templates")
            return templates

        except Exception as e:
            logger.error(f"Failed to list resource templates: {e}")
            return []

    async def read_resource(self, uri: str) -> Optional[Dict[str, Any]]:
        """
        Read a specific MCP resource by URI via JSON-RPC.

        Args:
            uri: Resource URI (e.g., "ui://product-card/premium-plan")

        Returns:
            Resource content with mimeType, content, and optional metadata
            Example:
            {
                "mimeType": "text/html",
                "content": "<div>Widget HTML</div>",
                "metadata": {
                    "title": "Product Card",
                    "interactive": true
                }
            }
        """
        try:
            # Call JSON-RPC method resources/read
            # MCP Protocol returns: {contents: [{uri, mimeType?, text/blob}]}
            result = await self._call_jsonrpc("resources/read", {"uri": uri})

            # Parse ReadResourceResult
            contents = result.get("contents", [])
            if not contents:
                logger.warning(f"No contents returned for resource: {uri}")
                return None

            # Get first content (typically single resource)
            first_content = contents[0]

            # Extract content based on type (TextResourceContents or BlobResourceContents)
            content_text = first_content.get("text") or first_content.get("blob", "")
            mime_type = first_content.get("mimeType", "text/plain")

            # Build response format expected by frontend
            resource_content = {
                "mimeType": mime_type,
                "content": content_text,
                "metadata": first_content.get("metadata", {}),
            }

            logger.info(f"Read resource: {uri} ({resource_content['mimeType']})")
            return resource_content

        except Exception as e:
            logger.error(f"Failed to read resource {uri}: {e}")
            return None

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


# Global singleton instance
_mcp_resource_service: Optional[MCPResourceService] = None


def get_mcp_resource_service(
    mcp_server_url: Optional[str] = None,
) -> MCPResourceService:
    """
    Get or create the global MCP Resource Service instance.

    Reads MCP_SERVER_ENDPOINT from app config (set by Azure Bicep in production),
    falls back to localhost:9000 for local development.

    Args:
        mcp_server_url: Optional MCP server URL override

    Returns:
        MCPResourceService instance
    """
    global _mcp_resource_service

    if _mcp_resource_service is None or mcp_server_url:
        # Priority: explicit param > config.MCP_SERVER_ENDPOINT > localhost default
        if not mcp_server_url:
            try:
                from common.config.app_config import config

                mcp_server_url = config.MCP_SERVER_ENDPOINT
            except Exception:
                pass
        url = mcp_server_url or "http://localhost:9000/mcp/"
        logger.info(f"Initializing MCPResourceService with URL: {url}")
        _mcp_resource_service = MCPResourceService(mcp_server_url=url)

    return _mcp_resource_service
