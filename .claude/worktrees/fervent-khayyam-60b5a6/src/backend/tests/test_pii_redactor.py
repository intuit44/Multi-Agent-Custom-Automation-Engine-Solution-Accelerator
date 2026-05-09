"""Tests for PII Redactor module."""

from utils.pii_redactor import (
    PIIRedactor,
    PIIContext,
    PIIType,
    get_pii_context,
    clear_pii_context,
)


class TestPIIRedactor:
    """Tests for the PIIRedactor class."""

    def test_redact_email(self):
        """Test that emails are properly redacted."""
        redactor = PIIRedactor()
        result = redactor.redact("Send email to john@example.com")

        assert "john@example.com" not in result.redacted_text
        assert "{{EMAIL_1}}" in result.redacted_text
        assert result.token_map["{{EMAIL_1}}"] == "john@example.com"
        assert len(result.tokens) == 1
        assert result.tokens[0].pii_type == PIIType.EMAIL

    def test_redact_multiple_emails(self):
        """Test that multiple emails are properly redacted."""
        redactor = PIIRedactor()
        result = redactor.redact("Send to alice@test.com and bob@test.com")

        assert "alice@test.com" not in result.redacted_text
        assert "bob@test.com" not in result.redacted_text
        assert "{{EMAIL_1}}" in result.redacted_text
        assert "{{EMAIL_2}}" in result.redacted_text
        assert len(result.tokens) == 2

    def test_redact_phone_number(self):
        """Test that phone numbers are properly redacted."""
        redactor = PIIRedactor()
        result = redactor.redact("Call me at 555-123-4567")

        assert "555-123-4567" not in result.redacted_text
        assert "{{PHONE_1}}" in result.redacted_text
        assert result.token_map["{{PHONE_1}}"] == "555-123-4567"

    def test_redact_phone_with_area_code(self):
        """Test phone with parentheses area code."""
        redactor = PIIRedactor()
        result = redactor.redact("Contact: (555) 123-4567")

        assert "(555) 123-4567" not in result.redacted_text
        assert "{{PHONE_1}}" in result.redacted_text

    def test_redact_mixed_pii(self):
        """Test redacting multiple types of PII."""
        redactor = PIIRedactor()
        result = redactor.redact(
            "Send welcome email to john@example.com, phone 555-123-4567"
        )

        assert "john@example.com" not in result.redacted_text
        assert "555-123-4567" not in result.redacted_text
        assert "{{EMAIL_1}}" in result.redacted_text
        assert "{{PHONE_1}}" in result.redacted_text
        assert len(result.tokens) == 2

    def test_redact_empty_string(self):
        """Test that empty string returns empty result."""
        redactor = PIIRedactor()
        result = redactor.redact("")

        assert result.redacted_text == ""
        assert len(result.tokens) == 0
        assert len(result.token_map) == 0

    def test_redact_no_pii(self):
        """Test string without PII is unchanged."""
        redactor = PIIRedactor()
        text = "This is a normal message without any PII"
        result = redactor.redact(text)

        assert result.redacted_text == text
        assert len(result.tokens) == 0

    def test_rehydrate(self):
        """Test that rehydration restores original values."""
        redactor = PIIRedactor()
        original = "Send email to john@example.com"
        result = redactor.redact(original)

        # Rehydrate should restore the original
        restored = result.rehydrate(result.redacted_text)
        assert restored == original

    def test_rehydrate_in_different_text(self):
        """Test rehydrating tokens in generated text."""
        redactor = PIIRedactor()
        result = redactor.redact("Email john@example.com about the meeting")

        # Simulate model-generated text containing the token
        generated = "I will send an email to {{EMAIL_1}} regarding the meeting."
        restored = result.rehydrate(generated)

        assert (
            restored
            == "I will send an email to john@example.com regarding the meeting."
        )

    def test_is_token(self):
        """Test token detection."""
        redactor = PIIRedactor()

        assert redactor.is_token("{{EMAIL_1}}")
        assert redactor.is_token("{{PHONE_2}}")
        assert not redactor.is_token("email")
        assert not redactor.is_token("{{invalid}}")

    def test_extract_tokens(self):
        """Test extracting tokens from text."""
        redactor = PIIRedactor()
        text = "Send to {{EMAIL_1}} and call {{PHONE_1}}"

        tokens = redactor.extract_tokens(text)
        assert "{{EMAIL_1}}" in tokens
        assert "{{PHONE_1}}" in tokens
        assert len(tokens) == 2


class TestPIIContext:
    """Tests for the PIIContext class."""

    def test_context_redact_and_rehydrate(self):
        """Test context maintains mapping across operations."""
        ctx = PIIContext("test-session")

        redacted = ctx.redact("Contact john@example.com")
        assert "john@example.com" not in redacted

        restored = ctx.rehydrate(redacted)
        assert "john@example.com" in restored

    def test_context_accumulates_mappings(self):
        """Test that context accumulates mappings from multiple redactions."""
        ctx = PIIContext("test-session")

        ctx.redact("Email alice@test.com")
        ctx.redact("Call 555-123-4567")

        token_map = ctx.get_token_map()
        assert len(token_map) == 2
        assert "{{EMAIL_1}}" in token_map
        assert "{{PHONE_1}}" in token_map

    def test_context_rehydrate_mixed_sources(self):
        """Test rehydrating text with tokens from multiple redactions."""
        ctx = PIIContext("test-session")

        ctx.redact("User email is alice@test.com")
        ctx.redact("Phone is 555-123-4567")

        # Text containing tokens from both redactions
        text = "Send confirmation to {{EMAIL_1}}, then call {{PHONE_1}}"
        restored = ctx.rehydrate(text)

        assert "alice@test.com" in restored
        assert "555-123-4567" in restored

    def test_context_clear(self):
        """Test that clear removes all mappings."""
        ctx = PIIContext("test-session")
        ctx.redact("Email alice@test.com")

        assert len(ctx.get_token_map()) == 1

        ctx.clear()
        assert len(ctx.get_token_map()) == 0


class TestGlobalPIIContextRegistry:
    """Tests for the global PII context registry."""

    def test_get_creates_new_context(self):
        """Test that get_pii_context creates new context."""
        clear_pii_context("new-session-1")
        ctx = get_pii_context("new-session-1")

        assert ctx is not None
        assert ctx.session_id == "new-session-1"

        # Cleanup
        clear_pii_context("new-session-1")

    def test_get_returns_same_context(self):
        """Test that get_pii_context returns the same context for same session."""
        clear_pii_context("test-session-2")
        ctx1 = get_pii_context("test-session-2")
        ctx2 = get_pii_context("test-session-2")

        assert ctx1 is ctx2

        # Cleanup
        clear_pii_context("test-session-2")

    def test_clear_removes_context(self):
        """Test that clear_pii_context removes the context."""
        ctx = get_pii_context("test-session-3")
        ctx.redact("email@test.com")

        clear_pii_context("test-session-3")

        # Getting context again should create a fresh one
        new_ctx = get_pii_context("test-session-3")
        assert len(new_ctx.get_token_map()) == 0

        # Cleanup
        clear_pii_context("test-session-3")


class TestContentFilterScenario:
    """Test the specific scenario that was causing content filter issues."""

    def test_welcome_email_scenario(self):
        """Test the welcome email scenario that was triggering content filter."""
        ctx = PIIContext("sid_1773098444009_2925")

        # Original user input that was causing issues
        original = "Send welcome email to John at yellowstone413g@gmail.com"

        # Redact before sending to model
        redacted = ctx.redact(original)

        # The email should be redacted
        assert "yellowstone413g@gmail.com" not in redacted
        assert "{{EMAIL_1}}" in redacted
        assert "Send welcome email to John at {{EMAIL_1}}" == redacted

        # Simulate model response (it may echo the tokens)
        model_response_action = "Send a welcome email to John at {{EMAIL_1}}"

        # Re-hydrate for actual execution
        actual_action = ctx.rehydrate(model_response_action)
        assert (
            actual_action == "Send a welcome email to John at yellowstone413g@gmail.com"
        )

    def test_complex_pii_scenario(self):
        """Test complex scenario with multiple PII types."""
        ctx = PIIContext("complex-session")

        original = (
            "Onboard new employee Jane Doe, email jane.doe@company.com, "
            "phone (555) 987-6543, SSN 123-45-6789"
        )

        redacted = ctx.redact(original)

        # All PII should be redacted
        assert "jane.doe@company.com" not in redacted
        assert "(555) 987-6543" not in redacted
        assert "123-45-6789" not in redacted

        # Should be safe to send to model now
        assert "{{EMAIL_1}}" in redacted
        assert "{{PHONE_1}}" in redacted
        assert "{{SSN_1}}" in redacted

        # Re-hydration should restore all
        restored = ctx.rehydrate(redacted)
        assert "jane.doe@company.com" in restored
        assert "(555) 987-6543" in restored
        assert "123-45-6789" in restored
