"""Linear OAuth service."""

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


class LinearOAuthService:
    """Service for handling Linear OAuth flow."""

    LINEAR_AUTHORIZE_URL = "https://linear.app/oauth/authorize"
    LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
    LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
    DEFAULT_SCOPES = ["read"]

    def __init__(self):
        self.repository = OAuthRepository()
        self.client = httpx.AsyncClient()

    async def get_authorization_url(
        self, state: Optional[str] = None
    ) -> Tuple[str, str]:
        """Generate Linear OAuth authorization URL."""
        if not state:
            state = secrets.token_urlsafe(32)

        params = {
            "client_id": settings.LINEAR_CLIENT_ID,
            "redirect_uri": settings.LINEAR_REDIRECT_URI,
            "response_type": "code",
            "scope": ",".join(self.DEFAULT_SCOPES),
            "state": state,
            "prompt": "consent",
        }

        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        authorization_url = f"{self.LINEAR_AUTHORIZE_URL}?{query_string}"
        return authorization_url, state

    async def exchange_code_for_token(self, code: str) -> dict:
        """Exchange authorization code for access token."""
        payload = {
            "client_id": settings.LINEAR_CLIENT_ID,
            "client_secret": settings.LINEAR_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.LINEAR_REDIRECT_URI,
            "grant_type": "authorization_code",
        }

        response = await self.client.post(
            self.LINEAR_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()

    async def fetch_viewer_info(self, access_token: str) -> dict:
        """Fetch Linear viewer (current user) info using GraphQL."""
        query = """
        query {
            viewer {
                id
                name
                email
                displayName
            }
        }
        """

        response = await self.client.post(
            self.LINEAR_GRAPHQL_URL,
            json={"query": query},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("viewer", {})

    async def handle_callback(
        self, user_id: str, code: str
    ) -> tuple[bool, str, Optional[dict]]:
        """Handle OAuth callback and store connection."""
        try:
            token_data = await self.exchange_code_for_token(code)
            access_token = token_data.get("access_token")

            if not access_token:
                return False, "Linear did not return an access token", None

            viewer_info = await self.fetch_viewer_info(access_token)

            expires_at = None
            if token_data.get("expires_in"):
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=int(token_data["expires_in"])
                )

            metadata = {
                "scope": token_data.get("scope"),
                "user": {
                    "id": viewer_info.get("id"),
                    "name": viewer_info.get("name"),
                    "email": viewer_info.get("email"),
                    "displayName": viewer_info.get("displayName"),
                },
            }

            connection_create = OAuthConnectionCreate(
                user_id=user_id,
                provider="linear",
                access_token=access_token,
                refresh_token=token_data.get("refresh_token"),
                token_type=token_data.get("token_type", "Bearer"),
                expires_at=expires_at,
                workspace_id=viewer_info.get("id"),
                workspace_name=viewer_info.get("displayName")
                or viewer_info.get("name")
                or viewer_info.get("email"),
                metadata=metadata,
            )

            connection = await self.repository.create(connection_create)

            return (
                True,
                "Successfully connected to Linear",
                {
                    "username": viewer_info.get("displayName")
                    or viewer_info.get("name"),
                    "connection_id": connection.id,
                },
            )
        except httpx.HTTPStatusError as err:
            error_message = f"Linear token exchange failed: {err.response.status_code}"
            try:
                error_data = err.response.json()
                error_message = error_data.get("error_description", error_message)
            except Exception:
                pass
            return False, error_message, None
        except Exception as exc:
            return False, f"Failed to handle Linear callback: {exc!s}", None

    async def get_connection(self, user_id: str) -> Optional[OAuthConnection]:
        """Get Linear connection for a user."""
        return await self.repository.get_by_user_and_provider(user_id, "linear")

    async def disconnect(self, user_id: str) -> bool:
        """Disconnect Linear for user."""
        return await self.repository.delete_by_user_and_provider(user_id, "linear")

    async def is_token_expired(self, user_id: str) -> bool:
        """Check if stored token is expired."""
        connection = await self.get_connection(user_id)
        if not connection or not connection.expires_at:
            return False
        return datetime.now(timezone.utc) > connection.expires_at

    async def refresh_token_if_needed(self, user_id: str) -> Optional[OAuthConnection]:
        """Refresh Linear access token when expired."""
        if not await self.is_token_expired(user_id):
            return await self.get_connection(user_id)

        connection = await self.get_connection(user_id)
        if not connection or not connection.refresh_token:
            return connection

        payload = {
            "client_id": settings.LINEAR_CLIENT_ID,
            "client_secret": settings.LINEAR_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": connection.refresh_token,
        }

        try:
            response = await self.client.post(
                self.LINEAR_TOKEN_URL,
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
                refresh_token=token_data.get("refresh_token")
                or connection.refresh_token,
                expires_at=expires_at,
            )

            return await self.repository.update(connection.id, update_data)
        except httpx.HTTPStatusError:
            return None

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
