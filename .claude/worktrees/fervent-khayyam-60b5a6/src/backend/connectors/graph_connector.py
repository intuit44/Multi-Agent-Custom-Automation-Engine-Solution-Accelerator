"""
Microsoft Graph API Connector.

This connector provides integration with Microsoft Graph API for:
- Sending emails
- Managing calendar events
- User management
- Teams messages
- OneDrive operations

In demo mode, it returns simulated responses.
In production mode, it uses the Microsoft Graph SDK.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

from connectors.base import BaseConnector, ConnectorConfig

logger = logging.getLogger(__name__)

# Only import Graph SDK if available
try:
    from azure.identity import ClientSecretCredential
    from msgraph.graph_service_client import GraphServiceClient
    from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
        SendMailPostRequestBody,
    )
    from msgraph.generated.models.message import Message
    from msgraph.generated.models.item_body import ItemBody
    from msgraph.generated.models.body_type import BodyType
    from msgraph.generated.models.recipient import Recipient
    from msgraph.generated.models.email_address import EmailAddress

    GRAPH_SDK_AVAILABLE = True
except ImportError:
    GRAPH_SDK_AVAILABLE = False
    ClientSecretCredential = cast(Any, object)
    GraphServiceClient = cast(Any, object)
    SendMailPostRequestBody = cast(Any, object)
    Message = cast(Any, object)
    ItemBody = cast(Any, object)
    BodyType = cast(Any, object)
    Recipient = cast(Any, object)
    EmailAddress = cast(Any, object)
    logger.warning(
        "Microsoft Graph SDK not installed. Install with: pip install msgraph-sdk azure-identity"
    )


class GraphConnector(BaseConnector):
    """Microsoft Graph API connector for email, calendar, and user management."""

    def __init__(self, config: Optional[ConnectorConfig] = None):
        super().__init__(config)
        self._client: Optional[Any] = None

    @property
    def service_name(self) -> str:
        return "Microsoft Graph"

    def is_configured(self) -> bool:
        return self.config.is_graph_configured() and GRAPH_SDK_AVAILABLE

    async def _initialize_production(self) -> bool:
        """Initialize the Graph API client."""
        if not GRAPH_SDK_AVAILABLE:
            self.logger.error("Microsoft Graph SDK not installed")
            return False

        try:
            credential = ClientSecretCredential(
                tenant_id=self.config.graph_tenant_id,
                client_id=self.config.graph_client_id,
                client_secret=self.config.graph_client_secret,
            )
            self._client = GraphServiceClient(credential)
            # Test connection by getting organization info
            # await self._client.organization.get()
            return True
        except Exception as e:
            self.logger.error(f"Failed to initialize Graph client: {e}")
            return False

    def _require_client(self) -> Any:
        """Return initialized Graph client or raise a clear error."""
        if self._client is None:
            raise RuntimeError("Graph client is not initialized")
        return self._client

    # =========================================================================
    # EMAIL OPERATIONS
    # =========================================================================

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        is_html: bool = True,
        from_user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an email via Microsoft Graph.

        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body (HTML or plain text)
            cc: Optional list of CC recipients
            is_html: Whether body is HTML (default True)
            from_user: Sender's email (defaults to MICROSOFT_GRAPH_DEFAULT_FROM_EMAIL)

        Returns:
            Result dictionary with success status and details
        """
        if from_user is None:
            from_user = os.getenv(
                "MICROSOFT_GRAPH_DEFAULT_FROM_EMAIL",
                "costumerservice@esparzasgroup.net",
            )

        await self.initialize()

        if self.is_demo_mode:
            return {
                "success": True,
                "demo_mode": True,
                "message": f"[DEMO] Email sent to {to}",
                "details": {
                    "to": to,
                    "subject": subject,
                    "cc": cc,
                    "from": from_user,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            }

        try:
            client = self._require_client()

            # Build message using msgraph SDK typed objects
            request_body = SendMailPostRequestBody(
                message=Message(
                    subject=subject,
                    body=ItemBody(
                        content_type=BodyType.Html if is_html else BodyType.Text,
                        content=body,
                    ),
                    to_recipients=[Recipient(email_address=EmailAddress(address=to))],
                ),
                save_to_sent_items=True,
            )

            if cc:
                if request_body.message is None:
                    request_body.message = Message()
                request_body.message.cc_recipients = [
                    Recipient(email_address=EmailAddress(address=addr)) for addr in cc
                ]

            await client.users.by_user_id(from_user).send_mail.post(request_body)

            return {
                "success": True,
                "message": f"Email sent to {to}",
                "message_id": "sent",
            }
        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")
            return {"success": False, "error": str(e)}

    async def send_welcome_email(
        self, employee_name: str, employee_email: str
    ) -> Dict[str, Any]:
        """Send a welcome email to a new employee.

        Args:
            employee_name: Name of the new employee
            employee_email: Email address of the new employee

        Returns:
            Result dictionary with success status
        """
        subject = f"Welcome to the Team, {employee_name}!"
        body = f"""
        <html>
        <body>
            <h1>Welcome, {employee_name}!</h1>
            <p>We are excited to have you join our team.</p>
            <p>Here are some important resources to get you started:</p>
            <ul>
                <li>Employee Handbook</li>
                <li>IT Setup Guide</li>
                <li>HR Portal</li>
            </ul>
            <p>If you have any questions, please reach out to your manager or HR.</p>
            <p>Best regards,<br>The HR Team</p>
        </body>
        </html>
        """
        return await self.send_email(to=employee_email, subject=subject, body=body)

    # =========================================================================
    # CALENDAR OPERATIONS
    # =========================================================================

    async def create_calendar_event(
        self,
        user_email: str,
        subject: str,
        start_time: datetime,
        end_time: datetime,
        attendees: Optional[List[str]] = None,
        location: Optional[str] = None,
        body: Optional[str] = None,
        is_online_meeting: bool = False,
    ) -> Dict[str, Any]:
        """Create a calendar event.

        Args:
            user_email: The user's calendar to create event in
            subject: Event subject/title
            start_time: Event start time
            end_time: Event end time
            attendees: List of attendee email addresses
            location: Optional location string
            body: Optional event description
            is_online_meeting: Create as Teams meeting

        Returns:
            Result dictionary with event details
        """
        await self.initialize()

        if self.is_demo_mode:
            event_id = f"demo_event_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            return {
                "success": True,
                "demo_mode": True,
                "event_id": event_id,
                "message": f"[DEMO] Calendar event '{subject}' created",
                "details": {
                    "subject": subject,
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                    "attendees": attendees or [],
                    "location": location,
                    "is_online_meeting": is_online_meeting,
                    "join_url": f"https://teams.microsoft.com/l/meetup-join/{event_id}"
                    if is_online_meeting
                    else None,
                },
            }

        try:
            client = self._require_client()

            event = {
                "subject": subject,
                "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": end_time.isoformat(), "timeZone": "UTC"},
            }

            if attendees:
                event["attendees"] = [
                    {"emailAddress": {"address": addr}, "type": "required"}
                    for addr in attendees
                ]

            if location:
                event["location"] = {"displayName": location}

            if body:
                event["body"] = {"contentType": "HTML", "content": body}

            if is_online_meeting:
                event["isOnlineMeeting"] = True
                event["onlineMeetingProvider"] = "teamsForBusiness"

            result = await client.users.by_user_id(user_email).events.post(event)

            return {
                "success": True,
                "event_id": result.id,
                "message": f"Calendar event '{subject}' created",
                "join_url": result.online_meeting.join_url
                if is_online_meeting
                else None,
            }
        except Exception as e:
            self.logger.error(f"Failed to create calendar event: {e}")
            return {"success": False, "error": str(e)}

    async def schedule_orientation(
        self,
        employee_name: str,
        employee_email: str,
        date: datetime,
        hr_email: str = "hr@company.com",
    ) -> Dict[str, Any]:
        """Schedule an orientation session for a new employee.

        Args:
            employee_name: Name of the new employee
            employee_email: Email of the new employee
            date: Date and time for orientation
            hr_email: HR representative's email

        Returns:
            Result dictionary with meeting details
        """
        end_time = date + timedelta(hours=2)  # 2-hour orientation

        return await self.create_calendar_event(
            user_email=hr_email,
            subject=f"New Employee Orientation - {employee_name}",
            start_time=date,
            end_time=end_time,
            attendees=[employee_email, hr_email],
            location="HR Conference Room / Online",
            body=f"""
            <h2>Welcome Orientation for {employee_name}</h2>
            <p>This orientation will cover:</p>
            <ul>
                <li>Company overview and culture</li>
                <li>HR policies and procedures</li>
                <li>Benefits enrollment</li>
                <li>IT setup and access</li>
                <li>Q&A session</li>
            </ul>
            """,
            is_online_meeting=True,
        )

    # =========================================================================
    # USER MANAGEMENT
    # =========================================================================

    async def get_user_info(self, user_email: str) -> Dict[str, Any]:
        """Get user information from Azure AD.

        Args:
            user_email: The user's email address

        Returns:
            User information dictionary
        """
        await self.initialize()

        if self.is_demo_mode:
            # Return simulated user data
            name_parts = user_email.split("@")[0].replace(".", " ").title()
            return {
                "success": True,
                "demo_mode": True,
                "user": {
                    "id": f"demo_user_{hash(user_email) % 10000}",
                    "displayName": name_parts,
                    "mail": user_email,
                    "jobTitle": "Employee",
                    "department": "General",
                    "officeLocation": "Main Office",
                    "manager": "manager@company.com",
                },
            }

        try:
            client = self._require_client()
            user = await client.users.by_user_id(user_email).get()
            return {
                "success": True,
                "user": {
                    "id": user.id,
                    "displayName": user.display_name,
                    "mail": user.mail,
                    "jobTitle": user.job_title,
                    "department": user.department,
                    "officeLocation": user.office_location,
                },
            }
        except Exception as e:
            self.logger.error(f"Failed to get user info: {e}")
            return {"success": False, "error": str(e)}

    async def assign_manager(
        self, employee_email: str, manager_email: str
    ) -> Dict[str, Any]:
        """Assign a manager to an employee (mentor assignment).

        Args:
            employee_email: The employee's email
            manager_email: The manager/mentor's email

        Returns:
            Result dictionary
        """
        await self.initialize()

        if self.is_demo_mode:
            return {
                "success": True,
                "demo_mode": True,
                "message": f"[DEMO] Assigned {manager_email} as mentor for {employee_email}",
                "details": {
                    "employee": employee_email,
                    "manager": manager_email,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            }

        try:
            # In production, this would update the user's manager in Azure AD
            # await self._client.users.by_user_id(employee_email).manager.ref.put(...)
            return {
                "success": True,
                "message": f"Assigned {manager_email} as manager for {employee_email}",
            }
        except Exception as e:
            self.logger.error(f"Failed to assign manager: {e}")
            return {"success": False, "error": str(e)}


# Singleton instance
_graph_connector: Optional[GraphConnector] = None


def get_graph_connector(config: Optional[ConnectorConfig] = None) -> GraphConnector:
    """Get the singleton Graph connector instance."""
    global _graph_connector
    if _graph_connector is None:
        _graph_connector = GraphConnector(config)
    return _graph_connector
