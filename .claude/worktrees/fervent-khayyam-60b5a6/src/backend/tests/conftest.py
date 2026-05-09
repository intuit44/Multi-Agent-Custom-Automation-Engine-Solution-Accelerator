"""
Pytest configuration and fixtures for backend tests.

This module provides:
- Default mock environment variables for unit tests
- Real Azure fixtures for integration tests (when credentials available)
- Async fixtures using pytest_asyncio

Test markers:
- @pytest.mark.unit: Fast tests with mocks, no external services (default)
- @pytest.mark.integration: Tests requiring real Azure services
- @pytest.mark.e2e: End-to-end tests requiring full environment
"""

import os
import uuid
import logging

import pytest
import pytest_asyncio

# Set default environment variables for testing to prevent import-time failures
# in app_config.py when its global `config = AppConfig()` is instantiated during
# test collection. These values are overridden by individual test fixtures as needed.
os.environ.setdefault(
    "AZURE_OPENAI_ENDPOINT", "https://mock-openai-endpoint.azure.com/"
)
os.environ.setdefault("AZURE_AI_SUBSCRIPTION_ID", "mock-subscription-id")
os.environ.setdefault("AZURE_AI_RESOURCE_GROUP", "mock-resource-group")
os.environ.setdefault("AZURE_AI_PROJECT_NAME", "mock-project-name")
os.environ.setdefault(
    "AZURE_AI_AGENT_ENDPOINT", "https://mock-agent-endpoint.azure.com/"
)

logger = logging.getLogger(__name__)


# =============================================================================
# Environment Detection Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def azure_credentials_available() -> bool:
    """Check if real Azure credentials are available for integration tests."""
    required_vars = [
        "AZURE_AI_AGENT_ENDPOINT",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
    ]
    # Check that values are real (not mock)
    available = all(
        os.getenv(var) and "mock" not in os.getenv(var, "").lower()
        for var in required_vars
    )
    if available:
        logger.info("Azure credentials detected - integration tests can run")
    else:
        logger.info("No real Azure credentials - integration tests will be skipped")
    return available


@pytest.fixture(scope="session")
def cosmos_credentials_available() -> bool:
    """Check if real Cosmos DB credentials are available."""
    endpoint = os.getenv("COSMOSDB_ENDPOINT", "")
    return bool(endpoint) and "mock" not in endpoint.lower()


# =============================================================================
# Azure AI Project Client Fixtures (for integration tests)
# =============================================================================


@pytest_asyncio.fixture(scope="module")
async def real_ai_client(azure_credentials_available):
    """
    Real Azure AI Project client for integration tests.

    Uses azure.ai.projects.aio.AIProjectClient for async operations.
    Skips tests if credentials are not available.
    """
    if not azure_credentials_available:
        pytest.skip("Azure credentials not available for integration tests")

    try:
        from azure.identity.aio import DefaultAzureCredential
        from azure.ai.projects.aio import AIProjectClient

        endpoint = os.getenv("AZURE_AI_AGENT_ENDPOINT")
        if not endpoint:
            pytest.skip("AZURE_AI_AGENT_ENDPOINT not set")

        credential = DefaultAzureCredential()
        client = AIProjectClient(endpoint=endpoint, credential=credential)

        logger.info(f"Created AIProjectClient for endpoint: {endpoint}")
        yield client

        # Cleanup
        await client.close()
        await credential.close()
        logger.info("Closed AIProjectClient")

    except ImportError as e:
        pytest.skip(f"Azure AI SDK not installed: {e}")
    except Exception as e:
        pytest.skip(f"Failed to create AI client: {e}")


@pytest_asyncio.fixture
async def test_agent_definition(real_ai_client):
    """
    Create a temporary test agent in Azure AI for integration testing.

    The agent is automatically deleted after the test.
    """
    test_name = f"TestAgent_{uuid.uuid4().hex[:8]}"
    definition = None

    try:
        # Create a simple test agent
        definition = await real_ai_client.agents.create_agent(
            model=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o"),
            name=test_name,
            instructions="You are a test agent. Always respond with 'TEST OK'.",
        )
        logger.info(f"Created test agent: {definition.id}")
        yield definition

    finally:
        # Cleanup: delete the test agent
        if definition and hasattr(definition, "id"):
            try:
                await real_ai_client.agents.delete_agent(definition.id)
                logger.info(f"Deleted test agent: {definition.id}")
            except Exception as e:
                logger.warning(f"Failed to delete test agent {definition.id}: {e}")


# =============================================================================
# Cosmos DB Fixtures (for integration tests)
# =============================================================================


@pytest_asyncio.fixture
async def real_cosmos_context(cosmos_credentials_available):
    """
    Real Cosmos DB context for integration tests.

    Creates a test session and cleans up afterward.
    """
    if not cosmos_credentials_available:
        pytest.skip("Cosmos DB credentials not available")

    try:
        from context.cosmos_memory_kernel import CosmosMemoryContext

        session_id = f"test-{uuid.uuid4().hex[:8]}"
        user_id = "integration-test-user"

        context = CosmosMemoryContext(
            session_id=session_id,
            user_id=user_id,
        )

        logger.info(f"Created Cosmos context for session: {session_id}")
        yield context

        # Cleanup would go here if needed

    except Exception as e:
        pytest.skip(f"Failed to create Cosmos context: {e}")


# =============================================================================
# Mock Fixtures (for unit tests)
# =============================================================================


@pytest.fixture
def mock_memory_store():
    """Mock memory store for unit tests."""
    from unittest.mock import AsyncMock, MagicMock

    store = MagicMock()
    store.add_item = AsyncMock()
    store.add_plan = AsyncMock()
    store.add_step = AsyncMock()
    store.get_plan_by_session = AsyncMock(return_value=None)
    store.get_steps_by_plan = AsyncMock(return_value=[])
    store.update_item = AsyncMock()
    store.get_data_by_type_and_session_id = AsyncMock(return_value=[])
    return store


@pytest.fixture
def mock_ai_client():
    """Mock AI Project client for unit tests."""
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.agents = MagicMock()
    client.agents.create_agent = AsyncMock()
    client.agents.delete_agent = AsyncMock()
    client.agents.create_thread = AsyncMock()
    client.agents.delete_thread = AsyncMock()
    client.close = AsyncMock()
    return client


# =============================================================================
# Test Session Fixtures
# =============================================================================


@pytest.fixture
def test_session_id() -> str:
    """Generate a unique session ID for each test."""
    return f"test-session-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def test_user_id() -> str:
    """Standard test user ID."""
    return "test-user-001"
