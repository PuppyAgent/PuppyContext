"""Notion OAuth service."""

import secrets
from datetime import datetime, timedelta
from typing import Optional
import httpx

from src.config import settings
from src.provider.oauth.models import OAuthConnection, OAuthConnectionCreate
from src.provider.oauth.repository import OAuthRepository


class NotionOAuthService:
    """Service for handling Notion OAuth flow."""

    NOTION_OAUTH_URL = "https://api.notion.com/v1/oauth/authorize"
    NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"

    def __init__(self):
        self.repository = OAuthRepository()
        self.client = httpx.AsyncClient()

    async def get_authorization_url(
        self, state: Optional[str] = None
    ) -> tuple[str, str]:
        """Generate Notion OAuth authorization URL.

        Returns:
            Tuple of (authorization_url, state)
        """
        if not state:
            state = secrets.token_urlsafe(32)

        params = {
            "client_id": settings.NOTION_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.NOTION_REDIRECT_URI,
            "owner": "user",
            "state": state,
        }

        # Build query string
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        authorization_url = f"{self.NOTION_OAUTH_URL}?{query_string}"

        return authorization_url, state

    async def exchange_code_for_token(self, code: str) -> dict:
        """Exchange authorization code for access token."""
        import base64

        # Notion requires Basic Auth with client_id:client_secret
        credentials = f"{settings.NOTION_CLIENT_ID}:{settings.NOTION_CLIENT_SECRET}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.NOTION_REDIRECT_URI,
        }

        response = await self.client.post(
            self.NOTION_TOKEN_URL,
            data=data,
            headers={
                "Authorization": f"Basic {encoded_credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response.raise_for_status()

        return response.json()

    async def handle_callback(
        self, user_id: str, code: str
    ) -> tuple[bool, str, Optional[dict]]:
        """Handle OAuth callback and store connection.

        Returns:
            Tuple of (success, message, connection_info)
        """
        try:
            token_data = await self.exchange_code_for_token(code)

            # Extract workspace information from the token data
            workspace_name = None
            workspace_id = None
            bot_id = None

            # Notion typically returns workspace info in the token response
            if "workspace_name" in token_data:
                workspace_name = token_data["workspace_name"]
            if "workspace_id" in token_data:
                workspace_id = token_data["workspace_id"]
            if "bot_id" in token_data:
                bot_id = token_data["bot_id"]
            elif "owner" in token_data and "id" in token_data["owner"]:
                bot_id = token_data["owner"]["id"]

            # Calculate expiration time if provided
            expires_at = None
            if "expires_in" in token_data:
                expires_at = datetime.utcnow() + timedelta(
                    seconds=token_data["expires_in"]
                )

            # Create connection record
            # Store additional token data in metadata
            metadata = {}
            if "duplicated_template_id" in token_data:
                metadata["duplicated_template_id"] = token_data[
                    "duplicated_template_id"
                ]
            if "owner" in token_data:
                metadata["owner"] = token_data["owner"]

            connection_create = OAuthConnectionCreate(
                user_id=user_id,
                provider="notion",
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                token_type=token_data.get("token_type", "bearer"),
                expires_at=expires_at,
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                bot_id=bot_id,
                metadata=metadata,
            )

            connection = await self.repository.create(connection_create)

            return (
                True,
                "Successfully connected to Notion",
                {"workspace_name": workspace_name, "connection_id": connection.id},
            )

        except httpx.HTTPStatusError as e:
            error_message = (
                f"Failed to exchange code for token: {e.response.status_code}"
            )
            if e.response.text:
                try:
                    error_data = e.response.json()
                    error_message = error_data.get("error_description", error_message)
                except Exception:
                    pass
            return False, error_message, None

        except Exception as e:
            return False, f"Failed to handle Notion callback: {e!s}", None

    async def get_connection(self, user_id: str) -> Optional[OAuthConnection]:
        """Get Notion connection for user."""
        return await self.repository.get_by_user_and_provider(user_id, "notion")

    async def disconnect(self, user_id: str) -> bool:
        """Disconnect Notion integration for user."""
        return await self.repository.delete_by_user_and_provider(user_id, "notion")

    async def is_token_expired(self, user_id: str) -> bool:
        """Check if the stored token is expired."""
        connection = await self.get_connection(user_id)
        if not connection or not connection.expires_at:
            return False  # No expiration date means token doesn't expire

        return datetime.utcnow() > connection.expires_at

    async def refresh_token_if_needed(self, user_id: str) -> Optional[OAuthConnection]:
        """Refresh token if it's expired."""
        if not await self.is_token_expired(user_id):
            return await self.get_connection(user_id)

        connection = await self.get_connection(user_id)
        if not connection or not connection.refresh_token:
            return None

        try:
            import base64

            # Notion requires Basic Auth with client_id:client_secret
            credentials = f"{settings.NOTION_CLIENT_ID}:{settings.NOTION_CLIENT_SECRET}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()

            data = {
                "grant_type": "refresh_token",
                "refresh_token": connection.refresh_token,
            }

            response = await self.client.post(
                self.NOTION_TOKEN_URL,
                data=data,
                headers={
                    "Authorization": f"Basic {encoded_credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response.raise_for_status()

            token_data = response.json()

            # Update connection with new token
            update_data = {
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get(
                    "refresh_token", connection.refresh_token
                ),
                "expires_at": datetime.utcnow()
                + timedelta(seconds=token_data["expires_in"])
                if "expires_in" in token_data
                else None,
            }

            return await self.repository.update(connection.id, update_data)

        except httpx.HTTPStatusError:
            # Failed to refresh token, connection might be invalid
            return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
