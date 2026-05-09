"""
PII Redactor - Extracts and tokenizes Personally Identifiable Information.

This module provides functionality to:
1. Detect PII (emails, phone numbers, names, etc.) in user input
2. Replace PII with safe tokens before sending to LLM
3. Store the mapping for later re-hydration when executing actions

This prevents content filter issues and improves security by not exposing
real PII to the model.
"""

import re
import logging
from typing import Dict, List, Tuple
from dataclasses import dataclass, field
from enum import Enum


class PIIType(Enum):
    """Types of PII that can be detected and redacted."""

    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"
    # Names are harder to detect reliably, handled separately


@dataclass
class PIIToken:
    """Represents a redacted PII value."""

    token: str
    original_value: str
    pii_type: PIIType
    position: Tuple[int, int]  # start, end in original text


@dataclass
class RedactionResult:
    """Result of PII redaction."""

    redacted_text: str
    tokens: List[PIIToken] = field(default_factory=list)
    token_map: Dict[str, str] = field(default_factory=dict)  # token -> original

    def rehydrate(self, text: str) -> str:
        """Replace tokens with original values in the given text."""
        result = text
        for token, original in self.token_map.items():
            result = result.replace(token, original)
        return result


class PIIRedactor:
    """
    Detects and redacts PII from text, replacing with safe tokens.

    Usage:
        redactor = PIIRedactor()
        result = redactor.redact("Send email to john@example.com")
        # result.redacted_text = "Send email to {{EMAIL_1}}"
        # result.token_map = {"{{EMAIL_1}}": "john@example.com"}

        # Later, when executing:
        actual_email = result.rehydrate("Sending to {{EMAIL_1}}")
        # actual_email = "Sending to john@example.com"
    """

    # Regex patterns for PII detection
    PATTERNS = {
        PIIType.EMAIL: re.compile(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", re.IGNORECASE
        ),
        PIIType.PHONE: re.compile(
            r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b"
        ),
        PIIType.SSN: re.compile(r"\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b"),
        PIIType.CREDIT_CARD: re.compile(r"\b(?:\d{4}[-.\s]?){3}\d{4}\b"),
        PIIType.IP_ADDRESS: re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    }

    # Token format for each PII type
    TOKEN_FORMATS = {
        PIIType.EMAIL: "{{{{EMAIL_{0}}}}}",
        PIIType.PHONE: "{{{{PHONE_{0}}}}}",
        PIIType.SSN: "{{{{SSN_{0}}}}}",
        PIIType.CREDIT_CARD: "{{{{CARD_{0}}}}}",
        PIIType.IP_ADDRESS: "{{{{IP_{0}}}}}",
    }

    def __init__(self):
        self._counters: Dict[PIIType, int] = {t: 0 for t in PIIType}

    def _get_next_token(self, pii_type: PIIType) -> str:
        """Generate the next token for a given PII type."""
        self._counters[pii_type] += 1
        return self.TOKEN_FORMATS[pii_type].format(self._counters[pii_type])

    def redact(self, text: str) -> RedactionResult:
        """
        Detect and redact PII from text.

        Args:
            text: The input text potentially containing PII

        Returns:
            RedactionResult with redacted text and token mappings
        """
        if not text:
            return RedactionResult(redacted_text=text)

        # Reset counters for each redaction
        self._counters = {t: 0 for t in PIIType}

        tokens: List[PIIToken] = []
        token_map: Dict[str, str] = {}

        # Find all PII matches with their positions
        all_matches: List[Tuple[int, int, str, PIIType]] = []

        for pii_type, pattern in self.PATTERNS.items():
            for match in pattern.finditer(text):
                all_matches.append(
                    (match.start(), match.end(), match.group(), pii_type)
                )

        # Sort by position (start) to process in order
        all_matches.sort(key=lambda x: x[0])

        # Build redacted text
        redacted_parts: List[str] = []
        last_end = 0

        for start, end, value, pii_type in all_matches:
            # Add text before this match
            redacted_parts.append(text[last_end:start])

            # Generate token and add mapping
            token = self._get_next_token(pii_type)
            redacted_parts.append(token)

            tokens.append(
                PIIToken(
                    token=token,
                    original_value=value,
                    pii_type=pii_type,
                    position=(start, end),
                )
            )
            token_map[token] = value

            last_end = end

        # Add remaining text
        redacted_parts.append(text[last_end:])

        redacted_text = "".join(redacted_parts)

        if tokens:
            logging.info(
                f"PIIRedactor: Redacted {len(tokens)} PII items: {[t.pii_type.value for t in tokens]}"
            )

        return RedactionResult(
            redacted_text=redacted_text, tokens=tokens, token_map=token_map
        )

    def is_token(self, text: str) -> bool:
        """Check if text looks like a PII token."""
        return bool(re.match(r"\{\{[A-Z]+_\d+\}\}", text))

    def extract_tokens(self, text: str) -> List[str]:
        """Extract all PII tokens from text."""
        return re.findall(r"\{\{[A-Z]+_\d+\}\}", text)


class PIIContext:
    """
    Manages PII context across a session/conversation.

    Stores token mappings so they can be rehydrated when actions are executed.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.redactor = PIIRedactor()
        self._token_map: Dict[str, str] = {}

    def redact(self, text: str) -> str:
        """Redact PII and store mappings. Returns redacted text."""
        result = self.redactor.redact(text)
        self._token_map.update(result.token_map)
        return result.redacted_text

    def rehydrate(self, text: str) -> str:
        """Replace all known tokens with their original values."""
        result = text
        for token, original in self._token_map.items():
            result = result.replace(token, original)
        return result

    def get_token_map(self) -> Dict[str, str]:
        """Get a copy of the current token map."""
        return self._token_map.copy()

    def add_mapping(self, token: str, value: str) -> None:
        """Manually add a token mapping."""
        self._token_map[token] = value

    def clear(self) -> None:
        """Clear all stored mappings."""
        self._token_map.clear()


# Global registry for session PII contexts
_pii_contexts: Dict[str, PIIContext] = {}


def get_pii_context(session_id: str) -> PIIContext:
    """Get or create a PII context for a session."""
    if session_id not in _pii_contexts:
        _pii_contexts[session_id] = PIIContext(session_id)
    return _pii_contexts[session_id]


def clear_pii_context(session_id: str) -> None:
    """Clear and remove a PII context for a session."""
    if session_id in _pii_contexts:
        _pii_contexts[session_id].clear()
        del _pii_contexts[session_id]
