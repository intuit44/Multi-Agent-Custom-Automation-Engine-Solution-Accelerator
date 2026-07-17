# app_config.py
import logging
import os
from typing import Optional

from azure.ai.projects.aio import AIProjectClient
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.identity.aio import (
    DefaultAzureCredential as DefaultAzureCredentialAsync,
)
from azure.identity.aio import (
    ManagedIdentityCredential as ManagedIdentityCredentialAsync,
)
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class StaticTokenCredential:
    """Async credential that returns a static access token for OBO scenarios.

    Extracts actual expiry from JWT to avoid sending expired tokens.
    Used when the user's EasyAuth/MSAL token is available for OBO flow.
    """

    def __init__(self, access_token: str):
        import base64 as _b64
        import json as _json
        from datetime import datetime as _dt

        self._access_token = access_token
        try:
            payload = access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            decoded = _json.loads(_b64.b64decode(payload))
            self._expires_on = decoded.get("exp", int(_dt.now().timestamp()) + 3600)
        except Exception:
            self._expires_on = int(_dt.now().timestamp()) + 3600

    async def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken as AT

        return AT(self._access_token, self._expires_on)

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type=None, exc_value=None, traceback=None):
        await self.close()


class AppConfig:
    """Application configuration class that loads settings from environment variables."""

    def __init__(self):
        """Initialize the application configuration with environment variables."""
        self.logger = logging.getLogger(__name__)
        # Azure authentication settings
        self.AZURE_TENANT_ID = self._get_optional("AZURE_TENANT_ID")
        self.AZURE_CLIENT_ID = self._get_optional("AZURE_CLIENT_ID")
        self.AZURE_CLIENT_SECRET = self._get_optional("AZURE_CLIENT_SECRET")
        # On-Behalf-Of (OBO) for data-plane user delegation. Uses a DEDICATED
        # confidential-client app registration — NOT AZURE_CLIENT_ID, which in this
        # app is the Managed Identity client_id used for MI auth. When ENABLE_OBO is
        # true the data-plane credential exchanges the user's EasyAuth assertion for
        # a per-scope delegated token; otherwise it forwards the token verbatim.
        # Requires: EasyAuth issuing a token whose audience is OBO_CLIENT_ID
        # (api://<OBO_CLIENT_ID>/user_impersonation), the OBO app registered as a
        # confidential client (OBO_CLIENT_SECRET or OBO_CLIENT_CERTIFICATE_PATH), and
        # admin-consented delegated permissions for each downstream resource.
        # Default off so deploying this code does not change behavior (safe rollout).
        self.ENABLE_OBO = self._get_bool("ENABLE_OBO")
        self.OBO_CLIENT_ID = self._get_optional("OBO_CLIENT_ID")
        self.OBO_CLIENT_SECRET = self._get_optional("OBO_CLIENT_SECRET")
        self.OBO_TENANT_ID = self._get_optional("OBO_TENANT_ID") or self.AZURE_TENANT_ID
        self.OBO_CLIENT_CERTIFICATE_PATH = self._get_optional(
            "OBO_CLIENT_CERTIFICATE_PATH"
        )

        # CosmosDB settings
        self.COSMOSDB_ENDPOINT = self._get_optional("COSMOSDB_ENDPOINT")
        self.COSMOSDB_DATABASE = self._get_optional("COSMOSDB_DATABASE")
        self.COSMOSDB_CONTAINER = self._get_optional("COSMOSDB_CONTAINER")
        self.COSMOSDB_MCP_CONNECTIONS_CONTAINER = self._get_optional(
            "COSMOSDB_MCP_CONNECTIONS_CONTAINER", "mcp_connections"
        )

        self.APPLICATIONINSIGHTS_CONNECTION_STRING = self._get_required(
            "APPLICATIONINSIGHTS_CONNECTION_STRING"
        )
        self.APP_ENV = self._get_required("APP_ENV", "prod")

        self.AZURE_COGNITIVE_SERVICES = self._get_optional(
            "AZURE_COGNITIVE_SERVICES", "https://cognitiveservices.azure.com/.default"
        )

        self.AZURE_MANAGEMENT_SCOPE = self._get_optional(
            "AZURE_MANAGEMENT_SCOPE", "https://management.azure.com/.default"
        )

        # Azure OpenAI settings
        self.AZURE_OPENAI_DEPLOYMENT_NAME = self._get_required(
            "AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o"
        )

        self.AZURE_OPENAI_RAI_DEPLOYMENT_NAME = self._get_required(
            "AZURE_OPENAI_RAI_DEPLOYMENT_NAME", "gpt-4.1"
        )
        self.AZURE_OPENAI_API_VERSION = self._get_required(
            "AZURE_OPENAI_API_VERSION", "2025-01-01-preview"
        )
        self.AZURE_OPENAI_ENDPOINT = self._get_required("AZURE_OPENAI_ENDPOINT")
        self.REASONING_MODEL_NAME = self._get_optional("REASONING_MODEL_NAME", "o3")
        # self.AZURE_BING_CONNECTION_NAME = self._get_optional(
        #     "AZURE_BING_CONNECTION_NAME"
        # )
        self.SUPPORTED_MODELS = self._get_optional("SUPPORTED_MODELS")
        # Frontend settings
        self.FRONTEND_SITE_NAME = self._get_optional(
            "FRONTEND_SITE_NAME", "http://127.0.0.1:3000"
        )

        # Azure AI settings
        self.AZURE_AI_SUBSCRIPTION_ID = self._get_required("AZURE_AI_SUBSCRIPTION_ID")
        self.AZURE_AI_RESOURCE_GROUP = self._get_required("AZURE_AI_RESOURCE_GROUP")
        self.AZURE_AI_PROJECT_NAME = self._get_required("AZURE_AI_PROJECT_NAME")
        self.AZURE_AI_AGENT_ENDPOINT = self._get_required("AZURE_AI_AGENT_ENDPOINT")
        self.AZURE_AI_PROJECT_ENDPOINT = self._get_optional("AZURE_AI_PROJECT_ENDPOINT")

        # Deployed Foundry Hosted Agent that answers all chat (ReAct + Toolbox,
        # server-side tools). The backend references it by name via
        # AzureAIClient(use_latest_version=True) — it never republishes it.
        self.CHAT_ORCHESTRATOR_AGENT_NAME = self._get_optional(
            "CHAT_ORCHESTRATOR_AGENT_NAME", "my-agent-vzq3de"
        )
        # Chat is driven by the model's Responses API directly (account
        # /openai endpoint) with the Toolbox attached as an MCP tool + a native
        # code interpreter, instead of the deployed Hosted Agent — the hosted
        # runtime silently drops code_interpreter items over the wire, so
        # generated files can never be surfaced/downloaded. The direct call
        # reproduces the Toolbox (tool search) AND exposes container_id/file_id
        # for downloads. CHAT_ORCHESTRATOR_MODEL is the model deployment the
        # Hosted Agent used (o4-mini); CHAT_TOOLBOX_NAME is its Toolbox name.
        self.CHAT_ORCHESTRATOR_MODEL = self._get_optional(
            "CHAT_ORCHESTRATOR_MODEL", "o4-mini"
        )
        self.CHAT_TOOLBOX_NAME = self._get_optional("CHAT_TOOLBOX_NAME", "Toolbox")
        # Model Router front-door: chat is entered through the deployed Model
        # Router (chat/completions), which routes to the best model per request
        # and signals — via a function tool — when a task needs the Responses
        # execution layer (code interpreter). The router itself cannot carry
        # code_interpreter/mcp and is not usable on Responses/as an agent.
        self.CHAT_ROUTER_MODEL = self._get_optional("CHAT_ROUTER_MODEL", "model-router")
        self.CHAT_ROUTER_API_VERSION = self._get_optional(
            "CHAT_ROUTER_API_VERSION", "2025-01-01-preview"
        )

        # Azure Search settings
        self.AZURE_SEARCH_ENDPOINT = self._get_optional("AZURE_AI_SEARCH_ENDPOINT")

        # Logging settings
        self.AZURE_BASIC_LOGGING_LEVEL = self._get_optional(
            "AZURE_BASIC_LOGGING_LEVEL", "INFO"
        )
        self.AZURE_PACKAGE_LOGGING_LEVEL = self._get_optional(
            "AZURE_PACKAGE_LOGGING_LEVEL", "WARNING"
        )
        self.AZURE_LOGGING_PACKAGES = self._get_optional("AZURE_LOGGING_PACKAGES")

        # Optional MCP server endpoint (for local MCP server or remote)
        # Example: http://127.0.0.1:8000/mcp
        self.MCP_SERVER_ENDPOINT = self._get_optional("MCP_SERVER_ENDPOINT")
        # PUBLIC ca-mcp (MacaeMcpServer) endpoint reachable by the Azure model
        # service when the Responses API attaches ca-mcp DIRECTLY (needed so the
        # identity header survives — the Foundry Toolbox proxy strips it). In prod
        # this is the ca-mcp container's public ingress; in dev MCP_SERVER_ENDPOINT
        # is usually localhost (unreachable by Azure), so point this at the deployed
        # ca-mcp URL. Falls back to MCP_SERVER_ENDPOINT when unset.
        self.MACAE_MCP_PUBLIC_ENDPOINT = (
            self._get_optional("MACAE_MCP_PUBLIC_ENDPOINT")
            or self.MCP_SERVER_ENDPOINT
        )
        # Foundry MCP Server (preview) — a NATIVE Foundry MCP (agents, models,
        # evaluations, datasets, prompts, sessions, connections). Attached DIRECTLY
        # by the backend as a capability (run_foundry_mcp), NOT registered by the
        # user. It is Entra-OAuth protected; the token must carry the scope from its
        # OAuth metadata (verified live): resource https://mcp.ai.azure.com, scope
        # https://mcp.ai.azure.com/Foundry.Mcp.Tools. A ".default" token for that
        # resource works (server accepts bearer via header).
        self.FOUNDRY_MCP_ENDPOINT = self._get_optional(
            "FOUNDRY_MCP_ENDPOINT", "https://mcp.ai.azure.com"
        )
        self.FOUNDRY_MCP_SCOPE = self._get_optional(
            "FOUNDRY_MCP_SCOPE", "https://mcp.ai.azure.com/.default"
        )
        self.MCP_SERVER_NAME = self._get_optional(
            "MCP_SERVER_NAME", "MCPGreetingServer"
        )
        self.MCP_SERVER_DESCRIPTION = self._get_optional(
            "MCP_SERVER_DESCRIPTION", "MCP server with greeting and planning tools"
        )
        self.TENANT_ID = self._get_optional("AZURE_TENANT_ID")
        self.CLIENT_ID = self._get_optional("AZURE_CLIENT_ID")
        self.AZURE_AI_SEARCH_CONNECTION_NAME = self._get_optional(
            "AZURE_AI_SEARCH_CONNECTION_NAME"
        )
        self.AZURE_AI_SEARCH_ENDPOINT = self._get_optional("AZURE_AI_SEARCH_ENDPOINT")
        self.AZURE_AI_SEARCH_API_KEY = self._get_optional("AZURE_AI_SEARCH_API_KEY")
        self.AZURE_OPENAI_EMBEDDING_DEPLOYMENT = self._get_optional(
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
        )
        # self.BING_CONNECTION_NAME = self._get_optional("BING_CONNECTION_NAME")

        test_team_json = self._get_optional("TEST_TEAM_JSON")

        self.AGENT_TEAM_FILE = f"../../data/agent_teams/{test_team_json}.json"

        # Cached clients and resources
        self._azure_credentials = None
        self._cosmos_client = None
        self._cosmos_database = None
        self._ai_project_client = None
        # Process-scoped async Managed Identity credential. Shared (borrowed) by
        # agents that don't carry a user token. It is owned by the process and
        # closed once at app shutdown (see aclose_shared_resources) — NEVER by an
        # individual agent's close(), so reusing/closing an agent can't tear down
        # the transport another in-flight task is still using.
        self._ai_async_credential = None

        self._agents = {}

    def get_azure_credential(self, client_id=None):
        """
        Returns an Azure credential based on the application environment.

        If the environment is 'dev', it uses DefaultAzureCredential with exclude_environment_credential=True
        to avoid EnvironmentCredential exceptions in Application Insights traces.
        Otherwise, it uses ManagedIdentityCredential.

        Args:
            client_id (str, optional): The client ID for the Managed Identity Credential.

        Returns:
            Credential object: Either DefaultAzureCredential or ManagedIdentityCredential.
        """
        if self.APP_ENV == "dev":
            return DefaultAzureCredential(
                exclude_environment_credential=True
            )  # CodeQL [SM05139]: DefaultAzureCredential is safe here
        else:
            return ManagedIdentityCredential(client_id=client_id)

    def get_azure_credential_async(self, client_id=None):
        """
        Returns an async Azure credential based on the application environment.

        If the environment is 'dev', it uses DefaultAzureCredential (async) with exclude_environment_credential=True
        to avoid EnvironmentCredential exceptions in Application Insights traces.
        Otherwise, it uses ManagedIdentityCredential (async).

        Args:
            client_id (str, optional): The client ID for the Managed Identity Credential.

        Returns:
            Async Credential object: Either DefaultAzureCredentialAsync or ManagedIdentityCredentialAsync.
        """
        if self.APP_ENV == "dev":
            return DefaultAzureCredentialAsync(exclude_environment_credential=True)
        else:
            return ManagedIdentityCredentialAsync(client_id=client_id)

    def get_azure_credentials(self):
        """Retrieve Azure credentials, either from environment variables or managed identity."""
        if self._azure_credentials is None:
            self._azure_credentials = self.get_azure_credential(self.AZURE_CLIENT_ID)
        return self._azure_credentials

    def get_shared_async_credential(self):
        """Return the process-scoped async Managed Identity credential.

        This single instance is reused (borrowed) by every agent that does not
        carry an end-user token. It is created lazily and closed exactly once at
        application shutdown via ``aclose_shared_resources`` — agents must NOT
        enter it into their own AsyncExitStack nor call ``close`` on it, otherwise
        closing/reusing one agent would tear down the aiohttp transport that other
        in-flight tasks (e.g. a background orchestration run) still depend on.
        """
        if self._ai_async_credential is None:
            self._ai_async_credential = self.get_azure_credential_async(
                self.AZURE_CLIENT_ID
            )
        return self._ai_async_credential

    async def aclose_shared_resources(self) -> None:
        """Close process-scoped async resources. Call once at app shutdown."""
        client = self._ai_project_client
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception as exc:  # pragma: no cover - best-effort shutdown
                logging.warning("Error closing shared AIProjectClient: %s", exc)
        self._ai_project_client = None

        cred = self._ai_async_credential
        if cred is not None and hasattr(cred, "close"):
            try:
                await cred.close()
            except Exception as exc:  # pragma: no cover - best-effort shutdown
                logging.warning("Error closing shared async credential: %s", exc)
        self._ai_async_credential = None

    async def get_access_token(self) -> str:
        """Get Azure access token for API calls."""
        try:
            credential = self.get_azure_credentials()
            token = credential.get_token(self.AZURE_COGNITIVE_SERVICES)
            return token.token
        except Exception as e:
            self.logger.error(f"Failed to get access token: {e}")
            raise

    def _get_required(self, name: str, default: Optional[str] = None) -> str:
        """Get a required configuration value from environment variables.

        Args:
            name: The name of the environment variable
            default: Optional default value if not found

        Returns:
            The value of the environment variable or default if provided

        Raises:
            ValueError: If the environment variable is not found and no default is provided
        """
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            logging.warning(
                "Environment variable %s not found, using default value", name
            )
            return default
        raise ValueError(
            f"Environment variable {name} not found and no default provided"
        )

    def _get_optional(self, name: str, default: str = "") -> str:
        """Get an optional configuration value from environment variables.

        Args:
            name: The name of the environment variable
            default: Default value if not found (default: "")

        Returns:
            The value of the environment variable or the default value
        """
        if name in os.environ:
            return os.environ[name]
        return default

    def _get_bool(self, name: str) -> bool:
        """Get a boolean configuration value from environment variables.

        Args:
            name: The name of the environment variable

        Returns:
            True if the environment variable exists and is set to 'true' or '1', False otherwise
        """
        return name in os.environ and os.environ[name].lower() in ["true", "1"]

    def get_cosmos_database_client(self):
        """Get a Cosmos DB client for the configured database.

        Returns:
            A Cosmos DB database client
        """
        try:
            if self._cosmos_client is None:
                self._cosmos_client = CosmosClient(
                    self.COSMOSDB_ENDPOINT,
                    credential=self.get_azure_credential(self.AZURE_CLIENT_ID),
                )

            if self._cosmos_database is None:
                self._cosmos_database = self._cosmos_client.get_database_client(
                    self.COSMOSDB_DATABASE
                )

            return self._cosmos_database
        except Exception as exc:
            logging.error(
                "Failed to create CosmosDB client: %s. CosmosDB is required for this application.",
                exc,
            )
            raise

    def _build_obo_credential(self, user_assertion: str):
        """Build a real On-Behalf-Of credential for the signed-in user.

        Exchanges ``user_assertion`` (the user's access token) for a per-scope
        delegated token each time the SDK requests one. Unlike forwarding the raw
        token, this yields a correctly-scoped token for whatever downstream the
        SDK targets, which is what lets Foundry's ARA perform its own OBO exchange
        to user-delegated tool connections (e.g. agent365/WorkIQ).

        Requirements (provisioned outside this code):
          - ``user_assertion`` audience must be the OBO app: set EasyAuth
            loginParameters scope to ``api://<OBO_CLIENT_ID>/user_impersonation``
            (plus ``offline_access``).
          - OBO_CLIENT_ID must be a confidential client with either
            OBO_CLIENT_SECRET or OBO_CLIENT_CERTIFICATE_PATH, and have
            admin-consented delegated permissions for each downstream resource.
            NOTE: this is the auth app registration, NOT AZURE_CLIENT_ID (which in
            this app is the Managed Identity client_id).
        """
        from azure.identity.aio import OnBehalfOfCredential

        if not (self.OBO_TENANT_ID and self.OBO_CLIENT_ID):
            raise RuntimeError(
                "OBO requires OBO_CLIENT_ID (the auth app registration) and a "
                "tenant id (OBO_TENANT_ID or AZURE_TENANT_ID) to be set"
            )

        if self.OBO_CLIENT_SECRET:
            return OnBehalfOfCredential(
                tenant_id=self.OBO_TENANT_ID,
                client_id=self.OBO_CLIENT_ID,
                client_secret=self.OBO_CLIENT_SECRET,
                user_assertion=user_assertion,
            )

        if self.OBO_CLIENT_CERTIFICATE_PATH:
            with open(self.OBO_CLIENT_CERTIFICATE_PATH, "rb") as cert_file:
                cert_bytes = cert_file.read()
            return OnBehalfOfCredential(
                tenant_id=self.OBO_TENANT_ID,
                client_id=self.OBO_CLIENT_ID,
                client_certificate=cert_bytes,
                user_assertion=user_assertion,
            )

        raise RuntimeError(
            "OBO enabled but no confidential-client credential configured: set "
            "OBO_CLIENT_SECRET or OBO_CLIENT_CERTIFICATE_PATH for app "
            f"{self.OBO_CLIENT_ID}"
        )

    def build_user_credential(self, user_assertion: str):
        """Return the async credential representing the end user for data-plane
        (inference) calls.

        With ENABLE_OBO and a confidential-client credential configured, returns a
        real OnBehalfOfCredential. Otherwise falls back to the legacy passthrough
        (StaticTokenCredential), preserving current behavior until OBO is fully
        provisioned. Any OBO build error degrades to passthrough so the chat path
        stays up.
        """
        if self.ENABLE_OBO:
            try:
                return self._build_obo_credential(user_assertion)
            except Exception as exc:
                logging.warning(
                    "ENABLE_OBO set but OBO credential unavailable (%s); "
                    "falling back to token passthrough.",
                    exc,
                )
        return StaticTokenCredential(user_assertion)

    def get_ai_project_client(self, user_access_token: Optional[str] = None):
        """Create and return an AIProjectClient for Azure AI Foundry (management plane).

        Always authenticates with Managed Identity. Management-plane operations
        (listing/creating agent versions) don't need the user's identity; the
        user-delegated (OBO) credential is applied only to the data-plane inference
        client — see ``build_user_credential`` and the agent lifecycle.

        Args:
            user_access_token: Ignored. Retained for call-site compatibility;
                user-delegated auth now happens at the data-plane credential.

        Returns:
            An AIProjectClient instance
        """
        # Managed Identity client (cached, shared across users)
        if self._ai_project_client is not None:
            return self._ai_project_client

        try:
            credential = self.get_shared_async_credential()
            if credential is None:
                raise RuntimeError(
                    "Unable to acquire Azure credentials; ensure Managed Identity is configured"
                )

            endpoint = self.AZURE_AI_AGENT_ENDPOINT
            self._ai_project_client = AIProjectClient(
                endpoint=endpoint, credential=credential
            )

            return self._ai_project_client
        except Exception as exc:
            logging.error("Failed to create AIProjectClient: %s", exc)
            raise

    def get_user_local_browser_language(self) -> str:
        """Get the user's local browser language from environment variables.

        Returns:
            The user's local browser language or 'en-US' if not set
        """
        return self._get_optional("USER_LOCAL_BROWSER_LANGUAGE", "en-US")

    def set_user_local_browser_language(self, language: str):
        """Set the user's local browser language in environment variables.

        Args:
            language: The language code to set (e.g., 'en-US')
        """
        os.environ["USER_LOCAL_BROWSER_LANGUAGE"] = language

    # Get agent team list by user_id dictionary index
    def get_agents(self) -> dict[str, list]:
        """Get the list of agents configured in the application.

        Returns:
            A list of agent names or configurations
        """
        return self._agents


# Create a global instance of AppConfig
config = AppConfig()
