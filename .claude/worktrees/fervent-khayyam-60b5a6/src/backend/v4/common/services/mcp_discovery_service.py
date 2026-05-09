"""
MCP Discovery Service - Proactive widget/app discovery for MCP Protocol 2025-11-25.

Provides:
- Discovery of all available UI resources (ui:// scheme)
- Multi-tenant catalog with user/team filtering
- Cache management for performance
- Normalization of widget metadata

Architecture:
- Proactive: User sees available widgets before AI suggests them
- Complements reactive flow (_meta.ui.resourceUri from tools)
"""

import logging
import time
from typing import Any, Dict, List, Optional

from v4.common.services.mcp_resource_service import get_mcp_resource_service

logger = logging.getLogger(__name__)


class MCPDiscoveryService:
    """Service for discovering and cataloging MCP UI resources proactively."""

    def __init__(self, cache_ttl_seconds: int = 180):
        """
        Initialize MCP Discovery Service.

        Args:
            cache_ttl_seconds: Cache TTL for discovery results (default: 180s)
        """
        self.cache_ttl = cache_ttl_seconds
        self.error_cache_ttl = 30  # Short TTL for errors (30s)
        self._cache: Dict[
            str, Dict[str, Any]
        ] = {}  # {user_id: {data, timestamp, is_error}}

    def _get_cache_key(self, user_id: str, team_id: Optional[str] = None) -> str:
        """Generate cache key for user/team."""
        return f"{user_id}:{team_id or 'default'}"

    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cached discovery is still valid based on success/error TTL."""
        if cache_key not in self._cache:
            return False

        cached = self._cache[cache_key]
        age = time.time() - cached["timestamp"]

        # Use shorter TTL for error states
        ttl = self.error_cache_ttl if cached.get("is_error", False) else self.cache_ttl
        return age < ttl

    async def discover_widgets(
        self, user_id: str, team_id: Optional[str] = None, use_cache: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Discover all available UI widgets/apps for a user.

        Args:
            user_id: User identifier for multi-tenant filtering
            team_id: Optional team identifier
            use_cache: Whether to use cached results

        Returns:
            List of normalized widget descriptors:
            [
                {
                    "server_id": "macae-mcp-server",
                    "resource_uri": "ui://product-card/{id}",
                    "title": "Product Card Widget",
                    "description": "Interactive product card",
                    "icon": "🛍️",
                    "tags": ["product", "commerce"],
                    "interactive": true,
                    "mimeType": "text/html",
                    "parameters": [{"name": "id", "required": true}]
                }
            ]
        """
        cache_key = self._get_cache_key(user_id, team_id)

        # Return cached results if valid
        if use_cache and self._is_cache_valid(cache_key):
            logger.info(f"Using cached discovery for {cache_key}")
            return self._cache[cache_key]["data"]

        logger.info(f"Running fresh discovery for user {user_id}")

        try:
            # Get MCP Resource Service with validation
            mcp_service = get_mcp_resource_service()

            # Validate MCP server URL is configured
            if (
                not mcp_service.mcp_server_url
                or mcp_service.mcp_server_url == "http://localhost:9000/mcp"
            ):
                logger.warning(
                    "MCP server URL not configured or using default localhost"
                )

            # Discover static resources
            resources = await mcp_service.list_resources()
            logger.debug(f"Found {len(resources)} static resources")

            # Discover parameterized templates
            templates = await mcp_service.list_resource_templates()
            logger.debug(f"Found {len(templates)} resource templates")

            # Normalize and filter UI resources with strict validation
            catalog = []

            # Process static resources (proactive - can be shown immediately)
            for resource in resources:
                uri = resource.get("uri", "")
                mime_type = resource.get("mimeType", "")

                # Filter: Only ui:// scheme AND valid UI mimeType
                if uri.startswith("ui://") and self._is_valid_ui_mimetype(mime_type):
                    normalized = self._normalize_resource(resource, is_template=False)
                    if normalized and not self._has_parameters(
                        normalized["resource_uri"]
                    ):
                        # Static widget - add to proactive catalog
                        catalog.append(normalized)
                    else:
                        logger.debug(f"Skipped parametrized static resource: {uri}")

            # Process templates (reactive - require parameters from AI)
            for template in templates:
                uri_template = template.get("uriTemplate", "")
                mime_type = template.get("mimeType", "")

                # Filter: Only ui:// scheme AND valid UI mimeType
                if uri_template.startswith("ui://") and self._is_valid_ui_mimetype(
                    mime_type
                ):
                    normalized = self._normalize_template(template)
                    if normalized:
                        # Mark as parametrized (reactive only)
                        normalized["proactive"] = False
                        catalog.append(normalized)

            logger.info(f"Discovered {len(catalog)} UI widgets for user {user_id}")

            # Cache successful results
            self._cache[cache_key] = {
                "data": catalog,
                "timestamp": time.time(),
                "is_error": False,
            }

            return catalog

        except Exception as e:
            logger.error(f"Discovery failed for user {user_id}: {e}")

            # Cache error state with short TTL
            self._cache[cache_key] = {
                "data": [],
                "timestamp": time.time(),
                "is_error": True,
            }

            # Return stale cached data if available
            if cache_key in self._cache and self._cache[cache_key].get("data"):
                logger.warning("Returning stale cached data due to error")
                return self._cache[cache_key]["data"]
            return []

    def _normalize_resource(
        self, resource: Dict[str, Any], is_template: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Normalize a resource descriptor to catalog format.

        Args:
            resource: Raw resource from MCP server
            is_template: Whether this is a template (has parameters)

        Returns:
            Normalized descriptor or None if invalid
        """
        try:
            uri = resource.get("uri", "")
            name = resource.get("name", "")
            description = resource.get("description", "")
            mime_type = resource.get("mimeType", "text/html")

            # Extract icon from description or use default
            icon = self._extract_icon(description)

            # Extract tags from description or name
            tags = self._extract_tags(name, description)

            return {
                "server_id": "macae-mcp-server",  # TODO: Multi-server support
                "resource_uri": uri,
                "title": name or self._uri_to_title(uri),
                "description": description,
                "icon": icon,
                "tags": tags,
                "interactive": True,
                "mimeType": mime_type,
                "parameters": [],  # Static resources have no params
            }

        except Exception as e:
            logger.warning(f"Failed to normalize resource: {e}")
            return None

    def _normalize_template(self, template: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize a resource template to catalog format.

        Args:
            template: Raw template from MCP server

        Returns:
            Normalized descriptor or None if invalid
        """
        try:
            uri_template = template.get("uriTemplate", "")
            name = template.get("name", "")
            description = template.get("description", "")
            mime_type = template.get("mimeType", "text/html")

            # Extract parameters from template
            parameters = self._extract_parameters(uri_template)

            icon = self._extract_icon(description)
            tags = self._extract_tags(name, description)

            return {
                "server_id": "macae-mcp-server",
                "resource_uri": uri_template,
                "title": name or self._uri_to_title(uri_template),
                "description": description,
                "icon": icon,
                "tags": tags,
                "interactive": True,
                "mimeType": mime_type,
                "parameters": parameters,
            }

        except Exception as e:
            logger.warning(f"Failed to normalize template: {e}")
            return None

    def _extract_parameters(self, uri_template: str) -> List[Dict[str, Any]]:
        """
        Extract parameter definitions from URI template.

        Example: "ui://product-card/{id}" -> [{"name": "id", "required": true}]
        """
        import re

        params = []
        matches = re.findall(r"\{(\w+)\}", uri_template)
        for param_name in matches:
            params.append(
                {
                    "name": param_name,
                    "required": True,  # All URI params are required
                }
            )
        return params

    def _extract_icon(self, text: str) -> str:
        """Extract emoji icon from text or return default."""
        import re

        # Look for emoji at start of description
        emoji_match = re.match(r"^([^\w\s]{1,2})\s+", text)
        if emoji_match:
            return emoji_match.group(1)
        # Default icons by keyword
        if "product" in text.lower():
            return "🛍️"
        if "comparison" in text.lower():
            return "📊"
        if "card" in text.lower():
            return "🎴"
        return "🔧"

    def _extract_tags(self, name: str, description: str) -> List[str]:
        """Extract tags from name and description."""
        tags = set()
        text = f"{name} {description}".lower()

        # Common tag patterns
        tag_keywords = {
            "product": ["product", "item", "catalog"],
            "commerce": ["shop", "store", "purchase", "buy"],
            "comparison": ["compare", "vs", "versus"],
            "chart": ["chart", "graph", "visualization"],
            "data": ["data", "table", "list"],
            "interactive": ["interactive", "widget", "component"],
        }

        for tag, keywords in tag_keywords.items():
            if any(keyword in text for keyword in keywords):
                tags.add(tag)

        return sorted(list(tags))

    def _uri_to_title(self, uri: str) -> str:
        """Convert URI to human-readable title."""
        # ui://product-card/123 -> Product Card
        # ui://comparison -> Comparison
        import re

        path = uri.replace("ui://", "").split("/")[0]
        path = re.sub(r"[{}\[\]]", "", path)  # Remove template markers
        return path.replace("-", " ").replace("_", " ").title()

    def _is_valid_ui_mimetype(self, mime_type: str) -> bool:
        """
        Validate that mimeType is suitable for UI rendering.

        Args:
            mime_type: MIME type from resource

        Returns:
            True if valid for UI widgets
        """
        if not mime_type:
            return False

        valid_types = [
            "text/html",
            "application/json",
            "application/vnd.mcp.ui+json",
            "text/plain",  # Simple text widgets
        ]

        # Strip MIME parameters (e.g., ;profile=mcp-app, ;charset=utf-8)
        base_mime = mime_type.split(";")[0].strip().lower()

        return base_mime in valid_types

    def _has_parameters(self, uri: str) -> bool:
        """
        Check if URI has parameter placeholders.

        Args:
            uri: Resource URI

        Returns:
            True if URI contains {param} placeholders
        """
        import re

        return bool(re.search(r"\{\w+\}", uri))

    def invalidate_cache(self, user_id: str, team_id: Optional[str] = None):
        """Invalidate cached discovery for a user/team."""
        cache_key = self._get_cache_key(user_id, team_id)
        if cache_key in self._cache:
            del self._cache[cache_key]
            logger.info(f"Invalidated discovery cache for {cache_key}")


# Global singleton instance
_mcp_discovery_service: Optional[MCPDiscoveryService] = None


def get_mcp_discovery_service(cache_ttl: int = 180) -> MCPDiscoveryService:
    """
    Get or create the global MCP Discovery Service instance.

    Args:
        cache_ttl: Cache TTL in seconds (default: 180s = 3 minutes)

    Returns:
        MCPDiscoveryService instance
    """
    global _mcp_discovery_service

    if _mcp_discovery_service is None:
        _mcp_discovery_service = MCPDiscoveryService(cache_ttl_seconds=cache_ttl)

    return _mcp_discovery_service
