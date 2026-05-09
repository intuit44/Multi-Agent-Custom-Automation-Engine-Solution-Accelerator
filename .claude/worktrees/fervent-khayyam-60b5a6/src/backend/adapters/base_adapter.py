"""Base adapter interface for all external service integrations."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from pydantic import BaseModel

from credential_resolver import credential_resolver
from tool_registry import ToolRegistry


class ToolExecutionResult(BaseModel):
    """Standardized result from tool execution."""

    success: bool
    result: Any = None
    error: Optional[str] = None
    credentials_required: Optional[Dict[str, Any]] = None
    execution_time_ms: int = 0
    provider_id: str
    tool_name: str
    metadata: Dict[str, Any] = {}


class BaseAdapter(ABC):
    """Base adapter all providers should extend.

    Responsibilities:
    - Resolve provider credentials at runtime from Key Vault.
    - Return structured `credentials_required` payload when missing.
    - Execute provider action with consistent result envelope and timing.
    - Emit a minimal audit event payload for observability.
    """

    def __init__(
        self,
        project_id: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.project_id = project_id
        self.session_id = session_id
        self.user_id = user_id
        self.credential_resolver = credential_resolver
        self.provider_id = self._get_provider_id()

    @abstractmethod
    def _get_provider_id(self) -> str:
        """Return provider ID (e.g., 'firestore', 'salesforce')."""
        raise NotImplementedError

    async def _get_credentials(self) -> Optional[Dict[str, str]]:
        """Resolve credentials from Key Vault."""
        return await self.credential_resolver.resolve_credentials(
            project_id=self.project_id,
            provider_id=self.provider_id,
        )

    def _build_credentials_required(self, tool_id: str) -> Dict[str, Any]:
        """Build a structured credentials_required response for the UI."""
        provider = ToolRegistry.get_provider(self.provider_id)
        required_fields = []
        if provider:
            required_fields = [
                {
                    "name": field.name,
                    "display_name": field.display_name,
                    "type": field.type.value,
                    "required": field.required,
                    "description": field.description,
                    "placeholder": field.placeholder,
                    "sensitive": field.sensitive,
                }
                for field in provider.credential_fields
            ]

        return {
            "error_type": "credentials_required",
            "provider_id": self.provider_id,
            "tool_id": tool_id,
            "required_fields": required_fields,
            "onboarding_url": "/api/tools/connect",
            "message": f"Credentials are required for provider '{self.provider_id}'.",
        }

    async def execute(
        self,
        tool_name: str,
        params: Dict[str, Any],
        *,
        tool_id: Optional[str] = None,
    ) -> ToolExecutionResult:
        """Standard execution wrapper for provider operations."""
        started = time.perf_counter()
        audit_meta: Dict[str, Any] = {
            "provider_id": self.provider_id,
            "tool_name": tool_name,
        }

        try:
            credentials = await self._get_credentials()
            if not credentials:
                payload = self._build_credentials_required(tool_id or tool_name)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                return ToolExecutionResult(
                    success=False,
                    provider_id=self.provider_id,
                    tool_name=tool_name,
                    credentials_required=payload,
                    error=payload["message"],
                    execution_time_ms=elapsed_ms,
                    metadata={**audit_meta, "reason": "missing_credentials"},
                )

            result = await self._execute_with_credentials(
                tool_name=tool_name,
                params=params,
                credentials=credentials,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return ToolExecutionResult(
                success=True,
                provider_id=self.provider_id,
                tool_name=tool_name,
                result=result,
                execution_time_ms=elapsed_ms,
                metadata=audit_meta,
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return ToolExecutionResult(
                success=False,
                provider_id=self.provider_id,
                tool_name=tool_name,
                error=str(exc),
                execution_time_ms=elapsed_ms,
                metadata={**audit_meta, "exception": exc.__class__.__name__},
            )

    @abstractmethod
    async def _execute_with_credentials(
        self,
        tool_name: str,
        params: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> Any:
        """Provider-specific implementation using SDK/API + credentials."""
        raise NotImplementedError

    @staticmethod
    def to_json(result: ToolExecutionResult) -> str:
        """Serialize execution result for uniform adapter responses."""
        return json.dumps(result.model_dump(), ensure_ascii=True)

    @staticmethod
    def audit_payload(result: ToolExecutionResult) -> Dict[str, Any]:
        """Generate normalized audit payload for Cosmos/event bus writes."""
        payload = {
            "event_type": "tool_call",
            "provider_id": result.provider_id,
            "tool_name": result.tool_name,
            "success": result.success,
            "execution_time_ms": result.execution_time_ms,
            "error": result.error,
        }
        if result.credentials_required:
            payload["credentials_required"] = True
        return payload
