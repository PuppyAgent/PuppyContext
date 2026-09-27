"""Google Calendar OAuth service."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import httpx

from src.config import settings
from src.provider.oauth.models import (
    OAuthConnection,
    OAuthConnectionCreate,
    OAuthConnectionUpdate,
)
from src.provider.oauth.repository import OAuthRepository


class GoogleCalendarOAuthService:
    """Service for handling Google Calendar OAuth flow."""

    GOOGLE_OAUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    DEFAULT_SCOPES = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
    ]

    def __init__(self):
        self.repository = OAuthRepository()
        self.client = httpx.AsyncClient()

    async def get_authorization_url(
        self, state: Optional[str] = None
    ) -> Tuple[str, str]:
        """Generate Google OAuth authorization URL for Calendar."""
        if not state:
            state = secrets.token_urlsafe(32)

        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_CALENDAR_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(self.DEFAULT_SCOPES),
            "state": state,
            "access_type": "offline",
            "prompt": "consent select_account",  # Force consent + account selector
        }

        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        authorization_url = f"{self.GOOGLE_OAUTH_URL}?{query_string}"
        return authorization_url, state

    async def exchange_code_for_token(self, code: str) -> dict:
        """Exchange authorization code for access token."""
        payload = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GOOGLE_CALENDAR_REDIRECT_URI,
            "grant_type": "authorization_code",
        }

        response = await self.client.post(
            self.GOOGLE_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()

    async def fetch_user_info(self, access_token: str) -> dict:
        """Fetch Google user info using access token."""
        response = await self.client.get(
            self.GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()

    async def handle_callback(
        self, user_id: str, code: str
    ) -> tuple[bool, str, Optional[dict]]:
        """Handle OAuth callback and store connection."""
        try:
            token_data = await self.exchange_code_for_token(code)
            access_token = token_data.get("access_token")

            if not access_token:
                return False, "Google did not return an access token", None

            user_info = await self.fetch_user_info(access_token)

            expires_at = None
            if token_data.get("expires_in"):
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=int(token_data["expires_in"])
                )

            metadata = {
                "scope": token_data.get("scope"),
                "user": {
                    "email": user_info.get("email"),
                    "id": user_info.get("id"),
                    "name": user_info.get("name"),
                    "picture": user_info.get("picture"),
                    "verified_email": user_info.get("verified_email"),
                },
            }

            connection_create = OAuthConnectionCreate(
                user_id=user_id,
                provider="google-calendar",
                access_token=access_token,
                refresh_token=token_data.get("refresh_token"),
                token_type=token_data.get("token_type", "Bearer"),
                expires_at=expires_at,
                workspace_id=user_info.get("id"),
                workspace_name=user_info.get("email"),
                metadata=metadata,
            )

            connection = await self.repository.create(connection_create)

            return (
                True,
                "Successfully connected to Google Calendar",
                {
                    "email": user_info.get("email"),
                    "connection_id": connection.id,
                },
            )
        except httpx.HTTPStatusError as err:
            error_message = f"Google token exchange failed: {err.response.status_code}"
            try:
                error_data = err.response.json()
                error_message = error_data.get("error_description", error_message)
            except Exception:
                pass
            return False, error_message, None
        except Exception as exc:
            return False, f"Failed to handle Google Calendar callback: {exc!s}", None

    async def get_connection(self, user_id: str) -> Optional[OAuthConnection]:
        """Get Google Calendar connection for a user."""
        return await self.repository.get_by_user_and_provider(user_id, "google-calendar")

    async def disconnect(self, user_id: str) -> bool:
        """Disconnect Google Calendar for user."""
        return await self.repository.delete_by_user_and_provider(user_id, "google-calendar")

    async def is_token_expired(self, user_id: str) -> bool:
        """Check if stored token is expired."""
        connection = await self.get_connection(user_id)
        if not connection:
            return False
        if not connection.expires_at:
            return bool(connection.refresh_token)
        return datetime.now(timezone.utc) > connection.expires_at

    async def refresh_token_if_needed(self, user_id: str) -> Optional[OAuthConnection]:
        """Refresh Google Calendar access token when expired."""
        if not await self.is_token_expired(user_id):
            return await self.get_connection(user_id)

        connection = await self.get_connection(user_id)
        if not connection or not connection.refresh_token:
            return connection

        payload = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": connection.refresh_token,
        }

        try:
            response = await self.client.post(
                self.GOOGLE_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            token_data = response.json()

            expires_at = None
            if token_data.get("expires_in"):
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=int(token_data["expires_in"])
                )

            update_data = OAuthConnectionUpdate(
                access_token=token_data.get("access_token"),
                refresh_token=token_data.get("refresh_token") or connection.refresh_token,
                expires_at=expires_at,
            )

            return await self.repository.update(connection.id, update_data)
        except httpx.HTTPStatusError:
            return None

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

