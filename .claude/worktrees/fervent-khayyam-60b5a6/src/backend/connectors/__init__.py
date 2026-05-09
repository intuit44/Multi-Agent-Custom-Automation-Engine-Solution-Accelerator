"""
Connectors module for Multi-Agent Custom Automation Engine.

This module provides service connectors that tools can use to interact with external
services like Microsoft Graph API, databases, email services, etc.

Architecture:
- Each connector implements a specific service integration
- Connectors are designed to be mockable for testing
- Tools in kernel_tools/ use these connectors for real functionality

Example usage in a tool:
    from connectors import get_graph_connector, get_database_connector

    graph = get_graph_connector()
    await graph.send_email(to="user@company.com", subject="Welcome", body="...")
"""

from connectors.base import BaseConnector, ConnectorConfig
from connectors.graph_connector import GraphConnector, get_graph_connector
from connectors.database_connector import DatabaseConnector, get_database_connector
from connectors.calendar_connector import CalendarConnector, get_calendar_connector

__all__ = [
    "BaseConnector",
    "ConnectorConfig",
    "GraphConnector",
    "get_graph_connector",
    "DatabaseConnector",
    "get_database_connector",
    "CalendarConnector",
    "get_calendar_connector",
]
