"""
SMTP Email Connector.

This connector provides email sending via SMTP (for GoDaddy M365, Office365, Gmail, etc.)
when Microsoft Graph API is not available in the same tenant.
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from connectors.base import BaseConnector, ConnectorConfig

logger = logging.getLogger(__name__)


class SMTPConnector(BaseConnector):
    """SMTP connector for sending emails."""

    def __init__(self, config: Optional[ConnectorConfig] = None):
        super().__init__(config)
        self._smtp_host = os.getenv("SMTP_HOST", "smtp.office365.com")
        self._smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self._smtp_user = os.getenv("SMTP_USER", "")
        self._smtp_password = os.getenv("SMTP_PASSWORD", "")
        self._from_email = os.getenv("SMTP_FROM_EMAIL", self._smtp_user)

    @property
    def service_name(self) -> str:
        return "SMTP Email"

    def is_configured(self) -> bool:
        """Check if SMTP is configured."""
        return bool(self._smtp_host and self._smtp_user and self._smtp_password)

    async def _initialize_production(self) -> bool:
        """Verify SMTP configuration."""
        if not self.is_configured():
            self.logger.warning(
                "SMTP not fully configured. Check SMTP_HOST, SMTP_USER, SMTP_PASSWORD"
            )
            return False
        return True

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        is_html: bool = False,
    ) -> dict:
        """
        Send an email via SMTP.

        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body (text or HTML)
            cc: CC recipients (comma-separated)
            is_html: Whether body is HTML

        Returns:
            dict with success status and message
        """
        if self.is_demo_mode:
            return self._demo_send_email(to, subject, body)

        if not self.is_configured():
            return {
                "success": False,
                "error": "SMTP not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD environment variables.",
            }

        try:
            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self._from_email
            msg["To"] = to
            if cc:
                msg["Cc"] = cc

            # Add body
            content_type = "html" if is_html else "plain"
            msg.attach(MIMEText(body, content_type, "utf-8"))

            # Connect and send
            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self._smtp_user, self._smtp_password)

                recipients = [to]
                if cc:
                    recipients.extend([r.strip() for r in cc.split(",")])

                server.sendmail(self._from_email, recipients, msg.as_string())

            self.logger.info(f"Email sent successfully to {to}")
            return {
                "success": True,
                "message": f"Email sent to {to}",
                "to": to,
                "subject": subject,
            }

        except smtplib.SMTPAuthenticationError as e:
            self.logger.error(f"SMTP authentication failed: {e}")
            return {
                "success": False,
                "error": f"SMTP authentication failed. Check username and password. Error: {str(e)}",
            }
        except Exception as e:
            self.logger.error(f"Failed to send email via SMTP: {e}")
            return {"success": False, "error": f"Failed to send email: {str(e)}"}

    def _demo_send_email(self, to: str, subject: str, body: str) -> dict:
        """Demo mode email response."""
        return {
            "success": True,
            "message": f"[DEMO] Email would be sent to {to}",
            "to": to,
            "subject": subject,
            "demo_mode": True,
        }


# Global instance
_smtp_connector: Optional[SMTPConnector] = None


def get_smtp_connector() -> SMTPConnector:
    """Get the global SMTP connector instance."""
    global _smtp_connector
    if _smtp_connector is None:
        _smtp_connector = SMTPConnector()
    return _smtp_connector
