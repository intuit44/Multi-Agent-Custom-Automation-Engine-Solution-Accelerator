"""Retry utilities for handling transient Azure/OpenAI errors with exponential backoff."""

import asyncio
import functools
import logging
from typing import Any, Callable, List, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def async_retry(
    max_retries: int = 3,
    initial_delay: float = 0.5,
    max_delay: float = 10.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: List[Type[Exception]] | None = None,
):
    """
    Decorator for async functions to retry on transient errors with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 0.5)
        max_delay: Maximum delay between retries (default: 10.0)
        backoff_factor: Multiplier for delay after each retry (default: 2.0)
        retryable_exceptions: List of exception types to retry on. If None, retries on:
                            - ConnectionError, TimeoutError, OSError (network issues)
                            - Azure/OpenAI transient: 429, 500, 502, 503, 504
    """
    if retryable_exceptions is None:
        retryable_exceptions = [
            ConnectionError,
            TimeoutError,
            OSError,
            asyncio.TimeoutError,
        ]

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    # Check if exception is retryable
                    is_retryable = isinstance(e, tuple(retryable_exceptions))

                    # Also check for HTTP status code errors (429, 5xx)
                    if not is_retryable and hasattr(e, "status_code"):
                        status = getattr(e, "status_code", None)
                        is_retryable = status in [429, 500, 502, 503, 504]

                    if not is_retryable or attempt >= max_retries:
                        logger.error(
                            f"❌ {func.__name__} failed permanently "
                            f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                        )
                        raise

                    logger.warning(
                        f"⚠️ {func.__name__} attempt {attempt + 1}/{max_retries + 1} "
                        f"failed: {type(e).__name__}. Retrying in {delay:.2f}s..."
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * backoff_factor, max_delay)

            # Should not reach here, but just in case
            raise last_exception or Exception(
                f"{func.__name__} failed after {max_retries + 1} attempts"
            )

        return wrapper

    return decorator
