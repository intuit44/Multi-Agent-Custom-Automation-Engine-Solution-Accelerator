"""
Calendar Connector for scheduling operations.

This connector provides calendar and scheduling functionality.
It can integrate with:
- Microsoft Graph Calendar API (via GraphConnector)
- Google Calendar API
- Other calendar systems

For simplicity, this wraps common calendar operations.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import uuid

from connectors.base import BaseConnector, ConnectorConfig

logger = logging.getLogger(__name__)


class CalendarConnector(BaseConnector):
    """Calendar connector for scheduling operations."""

    def __init__(self, config: Optional[ConnectorConfig] = None):
        super().__init__(config)
        # In-memory calendar for demo mode
        self._events: Dict[str, Dict[str, Any]] = {}

    @property
    def service_name(self) -> str:
        return "Calendar Service"

    def is_configured(self) -> bool:
        # Calendar is configured if we have Graph API or dedicated calendar API
        return self.config.is_graph_configured() or bool(self.config.calendar_api_url)

    async def _initialize_production(self) -> bool:
        """Initialize production calendar service."""
        # Would initialize Graph client or other calendar API
        return True

    async def schedule_event(
        self,
        title: str,
        start_time: datetime,
        duration_minutes: int = 60,
        attendees: Optional[List[str]] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        event_type: str = "meeting",
    ) -> Dict[str, Any]:
        """Schedule a calendar event.

        Args:
            title: Event title
            start_time: Start datetime
            duration_minutes: Duration in minutes
            attendees: List of attendee emails
            location: Event location
            description: Event description
            event_type: Type of event (meeting, orientation, review, etc.)

        Returns:
            Event details with ID and confirmation
        """
        await self.initialize()

        event_id = f"evt_{uuid.uuid4().hex[:8]}"
        end_time = start_time + timedelta(minutes=duration_minutes)

        event = {
            "id": event_id,
            "title": title,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_minutes": duration_minutes,
            "attendees": attendees or [],
            "location": location or "To be determined",
            "description": description,
            "event_type": event_type,
            "status": "scheduled",
            "created_at": datetime.utcnow().isoformat(),
        }

        self._events[event_id] = event

        return {
            "success": True,
            "demo_mode": self.is_demo_mode,
            "event": event,
            "message": f"Event '{title}' scheduled for {start_time.strftime('%B %d, %Y at %I:%M %p')}",
        }

    async def schedule_orientation(
        self, employee_name: str, date: datetime, hr_contact: str = "hr@company.com"
    ) -> Dict[str, Any]:
        """Schedule an orientation session.

        Args:
            employee_name: New employee name
            date: Orientation date/time
            hr_contact: HR contact email

        Returns:
            Orientation event details
        """
        return await self.schedule_event(
            title=f"New Employee Orientation - {employee_name}",
            start_time=date,
            duration_minutes=120,  # 2-hour orientation
            attendees=[hr_contact],
            location="HR Conference Room / Virtual",
            description=f"""
Welcome orientation session for {employee_name}.

Agenda:
- Company Overview & Culture (30 min)
- HR Policies & Benefits (30 min)
- IT Setup & Access (30 min)
- Q&A and Tour (30 min)

Please bring any questions you may have!
            """.strip(),
            event_type="orientation",
        )

    async def schedule_performance_review(
        self, employee_name: str, date: datetime, manager_email: Optional[str] = None
    ) -> Dict[str, Any]:
        """Schedule a performance review.

        Args:
            employee_name: Employee name
            date: Review date/time
            manager_email: Manager's email

        Returns:
            Review event details
        """
        return await self.schedule_event(
            title=f"Performance Review - {employee_name}",
            start_time=date,
            duration_minutes=60,
            attendees=[manager_email] if manager_email else [],
            location="Manager's Office / Virtual",
            description=f"Annual/quarterly performance review for {employee_name}.",
            event_type="review",
        )

    async def schedule_training(
        self,
        employee_name: str,
        program_name: str,
        date: datetime,
        duration_hours: int = 4,
    ) -> Dict[str, Any]:
        """Schedule a training session.

        Args:
            employee_name: Employee name
            program_name: Training program name
            date: Training date/time
            duration_hours: Duration in hours

        Returns:
            Training event details
        """
        return await self.schedule_event(
            title=f"Training: {program_name}",
            start_time=date,
            duration_minutes=duration_hours * 60,
            attendees=[],
            location="Training Room / Online",
            description=f"Training session for {employee_name}: {program_name}",
            event_type="training",
        )

    async def get_events(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        event_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get calendar events within a date range.

        Args:
            start_date: Start of range (defaults to today)
            end_date: End of range (defaults to 30 days from start)
            event_type: Filter by event type

        Returns:
            List of events
        """
        await self.initialize()

        start_date = start_date or datetime.utcnow()
        end_date = end_date or (start_date + timedelta(days=30))

        events = []
        for event in self._events.values():
            event_start = datetime.fromisoformat(event["start_time"])
            if start_date <= event_start <= end_date:
                if event_type is None or event["event_type"] == event_type:
                    events.append(event)

        return {
            "success": True,
            "demo_mode": self.is_demo_mode,
            "events": sorted(events, key=lambda e: e["start_time"]),
            "count": len(events),
        }


# Singleton instance
_calendar_connector: Optional[CalendarConnector] = None


def get_calendar_connector(
    config: Optional[ConnectorConfig] = None,
) -> CalendarConnector:
    """Get the singleton calendar connector instance."""
    global _calendar_connector
    if _calendar_connector is None:
        _calendar_connector = CalendarConnector(config)
    return _calendar_connector
