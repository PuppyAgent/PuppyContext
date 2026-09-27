"""GitHub OAuth service."""

import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

import httpx

from src.config import settings
from src.provider.oauth.models import OAuthConnection, OAuthConnectionCreate
from src.provider.oauth.repository import OAuthRepository


class GithubOAuthService:
    """Service for handling GitHub OAuth flow."""

    GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
    GITHUB_USER_API_URL = "https://api.github.com/user"
    DEFAULT_SCOPES = ["repo", "read:user", "read:org"]

    def __init__(self):
        self.repository = OAuthRepository()
        self.client = httpx.AsyncClient()

    async def get_authorization_url(
        self, state: Optional[str] = None
    ) -> Tuple[str, str]:
        """Generate GitHub OAuth authorization URL."""
        if not state:
            state = secrets.token_urlsafe(32)

        params = {
            "client_id": settings.GITHUB_CLIENT_ID,
            "redirect_uri": settings.GITHUB_REDIRECT_URI,
            "scope": " ".join(self.DEFAULT_SCOPES),
            "state": state,
            "allow_signup": "true",
        }

        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        authorization_url = f"{self.GITHUB_AUTHORIZE_URL}?{query_string}"
        return authorization_url, state

    async def exchange_code_for_token(self, code: str) -> dict:
        """Exchange authorization code for access token."""
        payload = {
            "client_id": settings.GITHUB_CLIENT_ID,
            "client_secret": settings.GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GITHUB_REDIRECT_URI,
        }

        response = await self.client.post(
            self.GITHUB_TOKEN_URL,
            json=payload,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()

    async def fetch_user_profile(self, access_token: str) -> dict:
        """Fetch GitHub user profile using access token."""
        response = await self.client.get(
            self.GITHUB_USER_API_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
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
                return False, "GitHub did not return an access token", None

            user_profile = await self.fetch_user_profile(access_token)

            expires_at = None
            if token_data.get("expires_in"):
                expires_at = datetime.utcnow() + timedelta(
                    seconds=int(token_data["expires_in"])
                )

            metadata = {
                "scope": token_data.get("scope"),
                "user": {
                    "login": user_profile.get("login"),
                    "id": user_profile.get("id"),
                    "name": user_profile.get("name"),
                    "avatar_url": user_profile.get("avatar_url"),
                    "html_url": user_profile.get("html_url"),
                },
            }

            connection_create = OAuthConnectionCreate(
                user_id=user_id,
                provider="github",
                access_token=access_token,
                refresh_token=token_data.get("refresh_token"),
                token_type=token_data.get("token_type", "bearer"),
                expires_at=expires_at,
                workspace_id=str(user_profile.get("id"))
                if user_profile.get("id")
                else None,
                workspace_name=user_profile.get("login") or user_profile.get("name"),
                metadata=metadata,
            )

            connection = await self.repository.create(connection_create)

            return (
                True,
                "Successfully connected to GitHub",
                {
                    "username": user_profile.get("login"),
                    "connection_id": connection.id,
                },
            )
        except httpx.HTTPStatusError as err:
            error_message = f"GitHub token exchange failed: {err.response.status_code}"
            try:
                error_data = err.response.json()
                error_message = error_data.get("error_description", error_message)
            except Exception:
                pass
            return False, error_message, None
        except Exception as exc:
            return False, f"Failed to handle GitHub callback: {exc!s}", None

    async def get_connection(self, user_id: str) -> Optional[OAuthConnection]:
        """Get GitHub connection for a user."""
        return await self.repository.get_by_user_and_provider(user_id, "github")

    async def disconnect(self, user_id: str) -> bool:
        """Disconnect GitHub for user."""
        return await self.repository.delete_by_user_and_provider(user_id, "github")

    async def is_token_expired(self, user_id: str) -> bool:
        """Check if stored token is expired."""
        connection = await self.get_connection(user_id)
        if not connection or not connection.expires_at:
            return False
        return datetime.utcnow() > connection.expires_at

    async def refresh_token_if_needed(self, user_id: str) -> Optional[OAuthConnection]:
        """Refresh GitHub access token when expired."""
        if not await self.is_token_expired(user_id):
            return await self.get_connection(user_id)

        connection = await self.get_connection(user_id)
        if not connection or not connection.refresh_token:
            return connection

        payload = {
            "client_id": settings.GITHUB_CLIENT_ID,
            "client_secret": settings.GITHUB_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": connection.refresh_token,
        }

        try:
            response = await self.client.post(
                self.GITHUB_TOKEN_URL,
                json=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            token_data = response.json()

            expires_at = None
            if token_data.get("expires_in"):
                expires_at = datetime.utcnow() + timedelta(
                    seconds=int(token_data["expires_in"])
                )

            update_data = {
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token")
                or connection.refresh_token,
                "expires_at": expires_at,
            }

            return await self.repository.update(connection.id, update_data)
        except httpx.HTTPStatusError:
            return None

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
