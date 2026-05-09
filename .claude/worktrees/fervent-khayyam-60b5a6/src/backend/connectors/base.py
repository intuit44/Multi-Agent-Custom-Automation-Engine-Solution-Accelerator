"""
Base connector class and configuration for all service connectors.

This provides a common interface and configuration pattern for all connectors.
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ConnectorConfig:
    """Configuration for service connectors.

    This class manages connector settings from environment variables
    with fallback to default values for development/demo mode.
    """

    # Microsoft Graph API settings
    graph_client_id: str = field(
        default_factory=lambda: os.getenv("MICROSOFT_GRAPH_CLIENT_ID", "")
    )
    graph_client_secret: str = field(
        default_factory=lambda: os.getenv("MICROSOFT_GRAPH_CLIENT_SECRET", "")
    )
    graph_tenant_id: str = field(
        default_factory=lambda: os.getenv("MICROSOFT_GRAPH_TENANT_ID", "")
    )

    # Database settings (for employee records, etc.)
    hr_database_url: str = field(
        default_factory=lambda: os.getenv("HR_DATABASE_URL", "")
    )

    # Email settings
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))

    # Calendar/Scheduling settings
    calendar_api_url: str = field(
        default_factory=lambda: os.getenv("CALENDAR_API_URL", "")
    )

    # Demo mode - when True, connectors return simulated responses
    demo_mode: bool = field(
        default_factory=lambda: (
            os.getenv("CONNECTOR_DEMO_MODE", "true").lower() == "true"
        )
    )

    def is_graph_configured(self) -> bool:
        """Check if Microsoft Graph API is properly configured."""
        return all(
            [self.graph_client_id, self.graph_client_secret, self.graph_tenant_id]
        )

    def is_database_configured(self) -> bool:
        """Check if database connection is configured."""
        return bool(self.hr_database_url)

    def is_email_configured(self) -> bool:
        """Check if email (SMTP) is configured."""
        return all([self.smtp_host, self.smtp_user, self.smtp_password])


# Global configuration instance
_config: Optional[ConnectorConfig] = None


def get_connector_config() -> ConnectorConfig:
    """Get the global connector configuration."""
    global _config
    if _config is None:
        _config = ConnectorConfig()
        if _config.demo_mode:
            logger.info(
                "Connectors running in DEMO MODE - returning simulated responses"
            )
        else:
            logger.info(
                "Connectors running in PRODUCTION MODE - connecting to real services"
            )
    return _config


class BaseConnector(ABC):
    """Abstract base class for all service connectors.

    All connectors should inherit from this class and implement
    the required methods. The connector automatically falls back
    to demo mode if the service is not configured.
    """

    def __init__(self, config: Optional[ConnectorConfig] = None):
        """Initialize the connector with configuration.

        Args:
            config: Optional connector configuration. If not provided,
                   uses the global configuration.
        """
        self.config = config or get_connector_config()
        self._initialized = False
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    @abstractmethod
    def service_name(self) -> str:
        """Return the name of the service this connector integrates with."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if this connector is properly configured for production use."""
        pass

    @property
    def is_demo_mode(self) -> bool:
        """Check if connector should operate in demo mode."""
        return self.config.demo_mode or not self.is_configured()

    async def initialize(self) -> bool:
        """Initialize the connector (authenticate, establish connections, etc.).

        Returns:
            True if initialization succeeded, False otherwise.
        """
        if self._initialized:
            return True

        if self.is_demo_mode:
            self.logger.info(f"{self.service_name} connector running in DEMO mode")
            self._initialized = True
            return True

        try:
            success = await self._initialize_production()
            self._initialized = success
            if success:
                self.logger.info(
                    f"{self.service_name} connector initialized successfully"
                )
            return success
        except Exception as e:
            self.logger.error(
                f"Failed to initialize {self.service_name} connector: {e}"
            )
            return False

    @abstractmethod
    async def _initialize_production(self) -> bool:
        """Initialize production connection. Override in subclasses."""
        pass

    def _demo_response(self, operation: str, **kwargs) -> Dict[str, Any]:
        """Generate a demo response for an operation.

        Args:
            operation: Name of the operation being simulated
            **kwargs: Parameters that would be used in the real operation

        Returns:
            A simulated response dictionary
        """
        return {
            "success": True,
            "demo_mode": True,
            "service": self.service_name,
            "operation": operation,
            "parameters": kwargs,
            "message": f"[DEMO] {operation} simulated successfully",
        }
