"""
Gmail Provider - Process Gmail imports via Google Gmail API.

Architecture:
- All emails are stored as a single JSON file in the version tree
- Agent can use jq to query the JSON structure
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup

import base64
import hashlib
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx
from src.provider._base import (
    BaseProvider,
    ProviderSpec,
    Capability,
    AuthRequirement,
    TriggerMode,
    FetchResult,
    Credentials,
    ConfigField,
    SourceResource,
)
from src.provider.oauth.gmail_service import GmailOAuthService
from src.infra.s3.service import S3Service
from src.utils.logger import log_error


class GmailProvider(BaseProvider):
    """Provider for Gmail imports using Gmail API.

    Stores all emails in a single JSONB node for efficient querying with jq.
    """

    GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    GMAIL_MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}"

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="gmail",
            display_name="Gmail",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            default_trigger=TriggerMode.POLL,
            default_node_type="json",
            auth=AuthRequirement.OAUTH,
            oauth_type="gmail",
            oauth_ui_type="gmail",
            supported_sync_modes=("manual", "scheduled"),
            default_sync_mode="manual",
            creation_mode="direct",
            description="Sync emails to JSON",
            accept_types=("folder",),
            icon_url="https://www.gstatic.com/images/branding/product/1x/gmail_2020q4_32dp.png",
            config_fields=(
                ConfigField(
                    key="query",
                    label="Email filter",
                    type="select",
                    default="",
                    options=[
                        {"value": "", "label": "All emails"},
                        {"value": "in:inbox", "label": "Inbox"},
                        {"value": "is:unread", "label": "Unread"},
                        {"value": "is:starred", "label": "Starred"},
                        {"value": "in:sent", "label": "Sent"},
                        {"value": "is:important", "label": "Important"},
                        {"value": "in:drafts", "label": "Drafts"},
                    ],
                ),
                ConfigField(
                    key="max_results",
                    label="Max emails",
                    type="number",
                    required=True,
                    default=50,
                    hint="Maximum number of emails to import",
                ),
            ),
        )

    def __init__(
        self,
        gmail_service: GmailOAuthService,
        s3_service: S3Service,
        node_service: Any = None,
    ):
        self.node_service = node_service
        self.gmail_service = gmail_service
        self.s3_service = s3_service
        self.client = httpx.AsyncClient(timeout=30.0)

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        """Fetch Gmail emails using the unified fetch interface."""
        source = config.get("source") or {}
        options = config.get("options") or {}
        user_email = credentials.metadata.get("user", {}).get("email", "Gmail")
        max_results = options.get("max_results", 50)
        if isinstance(max_results, str):
            max_results = int(max_results)
        query = options.get("query", "")

        email_ids = await self._fetch_email_ids(
            access_token=credentials.access_token,
            max_results=max_results,
            query=query,
        )

        emails = []
        for email_id in email_ids:
            try:
                email_data = await self._fetch_email_details(
                    credentials.access_token, email_id,
                )
                if email_data:
                    emails.append(email_data)
            except Exception as e:
                log_error(f"[Gmail fetch] Failed to fetch email {email_id}: {e}")
                continue

        content = {
            "account": user_email,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "email_count": len(emails),
            "query": query,
            "emails": emails,
        }

        content_hash = hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]

        return FetchResult(
            content=content,
            content_hash=content_hash,
            node_type="json",
            node_name=source.get("resource_name") or f"Gmail - {user_email}",
            summary=f"Fetched {len(emails)} emails from {user_email}",
        )

    async def list_source_resources(
        self,
        credentials: Credentials,
        *,
        query: str = "",
        cursor: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> tuple[list[SourceResource], Optional[str]]:
        user_email = credentials.metadata.get("user", {}).get("email", "")
        return [
            SourceResource(
                id="me",
                type="mailbox",
                name="Gmail",
                subtitle=user_email or None,
                icon="gmail",
                metadata={"account_label": user_email} if user_email else {},
            )
        ], None

    async def _fetch_email_ids(
        self,
        access_token: str,
        max_results: int = 50,
        query: str = "",
    ) -> list[str]:
        """Fetch list of email IDs from Gmail."""
        params = {
            "maxResults": min(max_results, 100),
        }
        if query:
            params["q"] = query

        response = await self.client.get(
            self.GMAIL_MESSAGES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        response.raise_for_status()
        data = response.json()
        messages = data.get("messages", [])
        return [m["id"] for m in messages]

    async def _fetch_email_details(
        self,
        access_token: str,
        email_id: str,
    ) -> Optional[dict]:
        """Fetch full email details and return structured data."""
        response = await self.client.get(
            self.GMAIL_MESSAGE_URL.format(id=email_id),
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "full"},
        )
        response.raise_for_status()
        email_data = response.json()

        # Extract headers
        headers = {
            h["name"].lower(): h["value"]
            for h in email_data.get("payload", {}).get("headers", [])
        }

        subject = headers.get("subject", "(No Subject)")
        from_addr = headers.get("from", "Unknown")
        to_addr = headers.get("to", "")
        date_str = headers.get("date", "")

        # Parse date
        try:
            email_date = parsedate_to_datetime(date_str) if date_str else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            email_date = datetime.now(timezone.utc)

        # Extract body
        body = self._extract_body(email_data.get("payload", {}))

        # Return structured email data (for JSONB storage)
        return {
            "id": email_id,
            "thread_id": email_data.get("threadId"),
            "subject": subject,
            "from": from_addr,
            "to": to_addr,
            "date": email_date.isoformat(),
            "labels": email_data.get("labelIds", []),
            "snippet": email_data.get("snippet", ""),
            "body": body,
            "url": f"https://mail.google.com/mail/u/0/#inbox/{email_id}",
        }

    def _extract_body(self, payload: dict) -> str:
        """Extract email body from payload, handling multipart messages."""
        body = ""

        # Check for body in the payload directly
        if "body" in payload and payload["body"].get("data"):
            body = self._decode_body(payload["body"]["data"])
            return body

        # Handle multipart
        parts = payload.get("parts", [])
        for part in parts:
            mime_type = part.get("mimeType", "")

            if mime_type == "text/plain":
                if "body" in part and part["body"].get("data"):
                    body = self._decode_body(part["body"]["data"])
                    break
            elif mime_type == "text/html":
                if "body" in part and part["body"].get("data"):
                    if not body:
                        body = self._decode_body(part["body"]["data"])
            elif "parts" in part:
                nested_body = self._extract_body(part)
                if nested_body:
                    body = nested_body

        return body

    def _decode_body(self, data: str) -> str:
        """Decode base64url encoded body."""
        try:
            decoded = base64.urlsafe_b64decode(data + "==")
            return decoded.decode("utf-8", errors="replace")
        except Exception:
            return ""

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup
    oauth_svc = GmailOAuthService()
    return ProviderSetup(
        adapter=GmailProvider(
            gmail_service=oauth_svc,
            s3_service=deps.s3_service,
            node_service=deps.node_service,
        ),
        oauth_bindings={"gmail": oauth_svc},
    )
