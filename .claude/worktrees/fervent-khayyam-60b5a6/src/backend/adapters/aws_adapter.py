"""AWS adapter - proxy to existing infrastructure."""

import os
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Optional
from adapters.base_adapter import BaseAdapter, ToolExecutionResult
import time


class AWSAdapter(BaseAdapter):
    """Minimal AWS adapter - calls existing infrastructure, not recreates it."""

    def _get_provider_id(self) -> str:
        return "aws_s3"

    @staticmethod
    def _resolve_endpoint(tool_name: str) -> str:
        """Resolve API/Lambda endpoint for each AWS operation."""
        by_tool = {
            "get_signed_url": os.getenv("AWS_S3_SIGNED_URL_ENDPOINT", ""),
            "s3_upload_object": os.getenv("AWS_S3_UPLOAD_ENDPOINT", ""),
            "s3_upload_file": os.getenv("AWS_S3_UPLOAD_ENDPOINT", ""),
            "s3_delete_object": os.getenv("AWS_S3_DELETE_ENDPOINT", ""),
            "s3_list_objects": os.getenv("AWS_S3_LIST_ENDPOINT", ""),
        }

        endpoint = by_tool.get(tool_name, "")
        if endpoint:
            return endpoint

        api_base = os.getenv("AWS_S3_API_BASE_URL", "").rstrip("/")
        if not api_base:
            raise ValueError(
                f"Missing endpoint for {tool_name}. Configure AWS_S3_*_ENDPOINT or AWS_S3_API_BASE_URL."
            )

        fallback_paths = {
            "s3_upload_object": "/api/s3/upload",
            "s3_upload_file": "/api/s3/upload",
            "s3_delete_object": "/api/s3/delete",
            "s3_list_objects": "/api/s3/list",
            "get_signed_url": "/api/video/sign",
        }
        return f"{api_base}{fallback_paths.get(tool_name, '')}"

    @staticmethod
    async def _post_json(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"AWS API failed ({response.status}): {error_text}")
                body = await response.json()
                if isinstance(body, dict):
                    return body
                raise Exception("AWS API returned non-object JSON response")

    async def _execute_with_credentials(
        self,
        tool_name: str,
        params: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> Any:
        """Execute AWS operations by calling existing infrastructure."""

        if tool_name == "get_signed_url":
            return await self._get_signed_url(params, credentials)
        if tool_name in {"s3_upload_object", "s3_upload_file"}:
            return await self._upload_object(params, credentials)
        if tool_name == "s3_delete_object":
            return await self._delete_object(params, credentials)
        if tool_name == "s3_list_objects":
            return await self._list_objects(params, credentials)
        else:
            raise ValueError(f"Unknown AWS operation: {tool_name}")

    async def execute(
        self,
        tool_name: str,
        params: Dict[str, Any],
        *,
        tool_id: Optional[str] = None,
    ) -> ToolExecutionResult:
        """Execution wrapper with public-Lambda bypass for S3 operations."""
        started = time.perf_counter()
        audit_meta: Dict[str, Any] = {
            "provider_id": self.provider_id,
            "tool_name": tool_name,
        }

        try:
            # Bypass credential check for all S3 operations
            if tool_name in {
                "get_signed_url",
                "s3_upload_object",
                "s3_upload_file",
                "s3_delete_object",
                "s3_list_objects",
            }:
                result = await self._execute_with_credentials(
                    tool_name=tool_name, params=params, credentials={}
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                return ToolExecutionResult(
                    success=True,
                    provider_id=self.provider_id,
                    tool_name=tool_name,
                    result=result,
                    execution_time_ms=elapsed_ms,
                    metadata={**audit_meta, "auth_mode": "public_lambda"},
                )

            return await super().execute(
                tool_name=tool_name, params=params, tool_id=tool_id
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

    async def _get_signed_url(
        self, params: Dict[str, Any], credentials: Dict[str, str]
    ) -> Dict[str, Any]:
        """Get signed URL by calling existing Lambda firmadora."""
        from datetime import datetime, timedelta

        s3_key = params.get("s3_key")
        if not s3_key:
            raise ValueError("s3_key is required")
        api_endpoint = self._resolve_endpoint("get_signed_url")
        result = await self._post_json(api_endpoint, {"s3Key": s3_key})

        expires_in_seconds = int(result.get("expiresIn", 7200))
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in_seconds)

        cache_hit_raw = (
            result.get("cacheHit") if "cacheHit" in result else result.get("cache_hit")
        )
        if cache_hit_raw is True:
            cache_status = "cache_hit"
        elif cache_hit_raw is False:
            cache_status = "generated"
        else:
            cached_raw = result.get("cached")
            if cached_raw is True:
                cache_status = "cache_hit"
            elif cached_raw is False:
                cache_status = "generated"
            else:
                cache_status = str(result.get("cacheStatus", "unknown"))

        return {
            "url": result.get("signedUrl") or result.get("url"),
            "s3_key": s3_key,
            "expires_at": expires_at.isoformat() + "Z",
            "expires_in_seconds": expires_in_seconds,
            "cache_status": cache_status,
            "source": result.get("source", "lambda_api"),
            "endpoint_used": api_endpoint,
        }

    @staticmethod
    def _resolve_bucket(params: Dict[str, Any], credentials: Dict[str, str]) -> str:
        bucket = (
            params.get("bucket")
            or params.get("bucket_name")
            or credentials.get("bucket")
            or credentials.get("bucket_name")
        )
        if not bucket:
            raise ValueError("bucket is required for S3 object operations")
        bucket_value = str(bucket).strip()

        # Tolerate natural-language bucket inputs (e.g. spaces instead of hyphens).
        # Example: "fibroskin academic videos" -> "fibroskin-academic-videos"
        if " " in bucket_value:
            bucket_value = re.sub(r"\s+", "-", bucket_value).strip("-")

        canonical_bucket = (
            os.getenv("AWS_S3_BUCKET")
            or os.getenv("AWS_S3_BUCKET_NAME")
            or os.getenv("AWS_BUCKET_NAME")
        )
        if canonical_bucket:
            norm_given = "".join(ch for ch in bucket_value.lower() if ch.isalnum())
            norm_canonical = "".join(
                ch for ch in canonical_bucket.lower() if ch.isalnum()
            )
            similarity = SequenceMatcher(None, norm_given, norm_canonical).ratio()
            if norm_given == norm_canonical or similarity >= 0.82:
                return str(canonical_bucket)

        return bucket_value

    @staticmethod
    def _resolve_region(params: Dict[str, Any], credentials: Dict[str, str]) -> str:
        return str(
            params.get("region")
            or credentials.get("region")
            or credentials.get("aws_region")
            or "us-east-1"
        )

    async def _upload_object(
        self, params: Dict[str, Any], credentials: Dict[str, str]
    ) -> Dict[str, Any]:
        key = params.get("key") or params.get("object_key") or params.get("s3_key")
        if not key:
            raise ValueError("key is required for s3_upload_object")

        bucket = self._resolve_bucket(params, credentials)
        api_endpoint = self._resolve_endpoint("get_signed_url")

        payload: Dict[str, Any] = {
            "action": "upload",
            "bucket": bucket,
            "s3Key": str(key),
            "contentType": params.get("content_type") or "application/octet-stream",
        }
        if params.get("content") is not None:
            payload["content"] = params.get("content")
        if params.get("content_base64") is not None:
            payload["contentBase64"] = params.get("content_base64")
        if isinstance(params.get("metadata"), dict):
            payload["metadata"] = params.get("metadata")

        result = await self._post_json(api_endpoint, payload)

        return {
            "bucket": result.get("bucket", bucket),
            "key": result.get("key", str(key)),
            "status": result.get("status", "accepted"),
            "upload_url": result.get("uploadUrl") or result.get("upload_url"),
            "fields": result.get("fields"),
            "source": result.get("source", "lambda_api"),
            "endpoint_used": api_endpoint,
        }

    async def _delete_object(
        self, params: Dict[str, Any], credentials: Dict[str, str]
    ) -> Dict[str, Any]:
        key = params.get("key") or params.get("object_key") or params.get("s3_key")
        if not key:
            raise ValueError("key is required for s3_delete_object")

        bucket = self._resolve_bucket(params, credentials)
        api_endpoint = self._resolve_endpoint("get_signed_url")
        result = await self._post_json(
            api_endpoint,
            {"action": "delete", "bucket": bucket, "s3Key": str(key)},
        )
        return {
            "bucket": result.get("bucket", bucket),
            "key": result.get("key", str(key)),
            "status": result.get("status", "deleted"),
            "source": result.get("source", "lambda_api"),
            "endpoint_used": api_endpoint,
        }

    async def _list_objects(
        self, params: Dict[str, Any], credentials: Dict[str, str]
    ) -> Dict[str, Any]:
        bucket = self._resolve_bucket(params, credentials)
        prefix = str(params.get("prefix") or "")
        max_keys = int(params.get("max_keys") or 100)
        api_endpoint = self._resolve_endpoint("get_signed_url")
        result = await self._post_json(
            api_endpoint,
            {"action": "list", "bucket": bucket, "prefix": prefix, "maxKeys": max_keys},
        )
        objects = result.get("objects") or []
        return {
            "bucket": result.get("bucket", bucket),
            "prefix": result.get("prefix", prefix),
            "count": result.get("count", len(objects)),
            "objects": objects,
            "source": result.get("source", "lambda_api"),
            "endpoint_used": api_endpoint,
        }
