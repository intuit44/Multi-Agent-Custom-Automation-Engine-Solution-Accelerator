"""
MCP Inspector Bridge - Backend service for Inspector status and management.

Provides REST API endpoints for:
- Inspector proxy status checking
- Connected external servers listing (via Inspector proxy)
- Inspector UI URL generation

This service complements the InspectorService in the MCP server by providing
backend-level management and status endpoints accessible to the frontend.
"""

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class MCPInspectorBridge:
    """
    Bridge between MACAE backend and MCP Inspector proxy.

    Checks Inspector proxy health and provides status info
    to the frontend for the Inspector link component.
    """

    def __init__(
        self,
        inspector_proxy_url: str = "http://localhost:16277",
        inspector_ui_url: str = "http://localhost:16274",
    ):
        self.proxy_url = inspector_proxy_url.rstrip("/")
        self.ui_url = inspector_ui_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=5.0)

    async def get_status(self) -> Dict[str, Any]:
        """Check if the MCP Inspector proxy is running and return status + UI link."""
        try:
            response = await self.client.get(f"{self.proxy_url}/health")
            health = response.json() if response.status_code == 200 else {}
            auth_token = self._read_session_token()
            return {
                "running": response.status_code == 200,
                "proxy_url": self.proxy_url,
                "ui_url": self.ui_url,
                "health": health,
                "ui_link": self._build_ui_link(auth_token=auth_token),
                "auth_token": auth_token,
            }
        except (httpx.ConnectError, httpx.TimeoutException):
            return {
                "running": False,
                "proxy_url": self.proxy_url,
                "ui_url": self.ui_url,
                "health": {},
                "ui_link": self._build_ui_link(),
                "message": "Inspector not running. Start with: ./scripts/start_inspector.sh",
            }
        except Exception as e:
            logger.error(f"Error checking Inspector status: {e}")
            return {
                "running": False,
                "error": str(e),
                "ui_link": self._build_ui_link(),
            }

    def _read_session_token(self) -> Optional[str]:
        """Read session token from Inspector log file (background mode)."""
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "..", "..", ".inspector.log"
        )
        try:
            log_path = os.path.normpath(log_path)
            if os.path.exists(log_path):
                with open(log_path, "r") as f:
                    for line in f:
                        if "MCP_PROXY_AUTH_TOKEN=" in line:
                            return line.split("MCP_PROXY_AUTH_TOKEN=")[-1].strip()
        except Exception:
            pass
        return None

    def _build_ui_link(
        self,
        transport: Optional[str] = None,
        server_url: Optional[str] = None,
        auth_token: Optional[str] = None,
    ) -> str:
        base = self.ui_url
        params = []

        if not transport and not server_url:
            params.append("transportType=streamable-http")
            params.append("url=http%3A%2F%2Flocalhost%3A9000%2Fmcp")
        else:
            if transport:
                params.append(f"transportType={transport}")
            if server_url:
                from urllib.parse import quote
                params.append(f"url={quote(server_url, safe='')}")

        if auth_token:
            params.append(f"MCP_PROXY_AUTH_TOKEN={auth_token}")

        return f"{base}/?{'&'.join(params)}" if params else base

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


# Global singleton
_inspector_bridge: Optional[MCPInspectorBridge] = None


def get_inspector_bridge() -> MCPInspectorBridge:
    """Get or create the global Inspector Bridge instance."""
    global _inspector_bridge

    if _inspector_bridge is None:
        proxy_url = os.environ.get("MCP_INSPECTOR_PROXY_URL", "http://localhost:16277")
        ui_url = os.environ.get("MCP_INSPECTOR_UI_URL", "http://localhost:16274")
        _inspector_bridge = MCPInspectorBridge(
            inspector_proxy_url=proxy_url,
            inspector_ui_url=ui_url,
        )

    return _inspector_bridge
