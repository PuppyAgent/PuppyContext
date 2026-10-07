"""
Google Calendar Provider - Process Google Calendar event imports.

Architecture:
- All events are stored in a single JSON file in the version tree
- Agent can query with jq: jq '.events[] | select(.start > "2026-02-01")'
- Uses parallel requests for speed
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from typing import Any
from src.provider._base import (
    BaseProvider,
    ProviderSpec,
    Capability,
    AuthRequirement,
    FetchResult,
    Credentials,
    ConfigField,
    SourceResource,
)
from src.provider.oauth.google_calendar_service import GoogleCalendarOAuthService
from src.infra.s3.service import S3Service
from src.utils.logger import log_error


class GoogleCalendarProvider(BaseProvider):
    """Provider for Google Calendar imports - stores all events in single JSONB node."""

    CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
    CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="google_calendar",
            display_name="Google Calendar",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            default_node_type="json",
            auth=AuthRequirement.OAUTH,
            oauth_type="calendar",
            oauth_ui_type="google_calendar",
            creation_mode="direct",
            description="Sync calendar events",
            accept_types=("folder",),
            icon_url="https://www.gstatic.com/images/branding/product/1x/calendar_2020q4_32dp.png",
            config_fields=(
                ConfigField(key="days_past", label="Days of past events", type="number", default=30),
                ConfigField(key="days_future", label="Days of future events", type="number", default=30),
                ConfigField(key="max_results", label="Max events per calendar", type="number", default=100),
            ),
        )

    def __init__(
        self,
        calendar_service: GoogleCalendarOAuthService,
        s3_service: S3Service,
        node_service: Any = None,
    ):
        self.node_service = node_service
        self.calendar_service = calendar_service
        self.s3_service = s3_service
        self.client = httpx.AsyncClient(timeout=60.0)

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        """Fetch Google Calendar events using the unified fetch interface."""
        source = config.get("source") or {}
        options = config.get("options") or {}
        user_email = credentials.metadata.get("user", {}).get("email", "Google Calendar")
        access_token = credentials.access_token

        calendar_ids = options.get("calendar_ids") or (source.get("metadata") or {}).get("calendar_ids")
        if isinstance(calendar_ids, str):
            calendar_ids = [calendar_ids]
        if not calendar_ids:
            raise ValueError("source calendar selection is required")

        days_past = options.get("days_past", 30)
        days_future = options.get("days_future", 30)
        max_results = options.get("max_results", 100)

        time_min = (datetime.now(timezone.utc) - timedelta(days=days_past)).isoformat()
        time_max = (datetime.now(timezone.utc) + timedelta(days=days_future)).isoformat()

        calendars = await self._list_calendars(access_token)
        selected_ids = {str(calendar_id) for calendar_id in calendar_ids}
        calendars = [calendar for calendar in calendars if str(calendar.get("id", "")) in selected_ids]
        if not calendars:
            raise ValueError("Selected calendars are no longer available")

        calendars_info = [
            {
                "id": cal.get("id", ""),
                "name": cal.get("summary", "Unknown"),
                "primary": cal.get("primary", False),
            }
            for cal in calendars
        ]

        async def fetch_calendar_events(calendar: dict) -> list[dict]:
            calendar_name = calendar.get("summary", "Unknown")
            calendar_id = calendar.get("id", "")
            try:
                events = await self._list_events(
                    access_token=access_token,
                    calendar_id=calendar_id,
                    time_min=time_min,
                    time_max=time_max,
                    max_results=max_results,
                )
                for event in events:
                    event["calendar_name"] = calendar_name
                    event["calendar_id"] = calendar_id
                return events
            except Exception as e:
                log_error(f"[Calendar fetch] Failed to fetch events from {calendar_name}: {e}")
                return []

        results = await asyncio.gather(*[fetch_calendar_events(cal) for cal in calendars])

        all_events = []
        for events in results:
            all_events.extend(events)

        events_data = [self._format_event_data(event) for event in all_events]

        content = {
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "source": "google_calendar",
            "account": user_email,
            "time_range": {
                "from": time_min,
                "to": time_max,
                "days_past": days_past,
                "days_future": days_future,
            },
            "calendar_count": len(calendars_info),
            "calendars": calendars_info,
            "event_count": len(events_data),
            "events": events_data,
        }

        content_hash = hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]

        return FetchResult(
            content=content,
            content_hash=content_hash,
            node_type="json",
            node_name=source.get("resource_name") or f"Google Calendar - {user_email}"[:100],
            summary=f"Fetched {len(events_data)} events from {len(calendars_info)} calendars",
        )

    async def list_source_resources(
        self,
        credentials: Credentials,
        *,
        query: str = "",
        cursor: str | None = None,
        resource_type: str | None = None,
    ) -> tuple[list[SourceResource], str | None]:
        calendars = await self._list_calendars(credentials.access_token)
        needle = query.strip().lower()
        resources: list[SourceResource] = []
        for calendar in calendars:
            name = calendar.get("summary", "Untitled calendar")
            if needle and needle not in name.lower():
                continue
            calendar_id = str(calendar.get("id", ""))
            resources.append(
                SourceResource(
                    id=calendar_id,
                    type="calendar",
                    name=name,
                    url=f"https://calendar.google.com/calendar/u/0/r?cid={quote(calendar_id)}",
                    subtitle=calendar.get("description") or calendar.get("summaryOverride"),
                    icon="google_calendar",
                    metadata={
                        "primary": bool(calendar.get("primary")),
                        "time_zone": calendar.get("timeZone"),
                    },
                )
            )
        return resources, None

    async def _list_calendars(self, access_token: str) -> list[dict]:
        """List user's calendars."""
        response = await self.client.get(
            self.CALENDAR_LIST_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"minAccessRole": "reader"},
        )
        response.raise_for_status()
        return response.json().get("items", [])

    async def _list_events(
        self,
        access_token: str,
        calendar_id: str,
        time_min: str,
        time_max: str,
        max_results: int = 100,
    ) -> list[dict]:
        """List events from a specific calendar."""
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": min(max_results, 250),
            "singleEvents": "true",
            "orderBy": "startTime",
        }

        response = await self.client.get(
            self.CALENDAR_EVENTS_URL.format(calendar_id=calendar_id),
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        response.raise_for_status()
        return response.json().get("items", [])

    def _format_event_data(self, event: dict) -> dict:
        """Format event data for JSONB storage."""
        start = event.get("start", {})
        end = event.get("end", {})
        attendees = event.get("attendees", [])
        organizer = event.get("organizer", {})

        return {
            "id": event.get("id", ""),
            "summary": event.get("summary", "Untitled Event"),
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "all_day": "date" in start and "dateTime" not in start,
            "calendar": event.get("calendar_name", ""),
            "calendar_id": event.get("calendar_id", ""),
            "organizer": organizer.get("email", ""),
            "attendees": [a.get("email", "") for a in attendees if a.get("email")][:20],
            "attendee_count": len(attendees),
            "status": event.get("status", ""),
            "html_link": event.get("htmlLink", ""),
            "created": event.get("created", ""),
            "updated": event.get("updated", ""),
        }

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup
    from src.provider.oauth.google_calendar_service import GoogleCalendarOAuthService
    oauth_svc = GoogleCalendarOAuthService()
    return ProviderSetup(
        adapter=GoogleCalendarProvider(
            calendar_service=oauth_svc,
            s3_service=deps.s3_service,
            node_service=deps.node_service,
        ),
        oauth_bindings={"calendar": oauth_svc},
    )
