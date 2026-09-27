"""Airtable OAuth service."""

import secrets
import hashlib
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx
from loguru import logger

from src.config import settings
from src.provider.oauth.models import (
    OAuthConnection,
    OAuthConnectionCreate,
    OAuthConnectionUpdate,
)
from src.provider.oauth.repository import OAuthRepository

# Global storage for code_verifiers (in production, use Redis or database)
_CODE_VERIFIERS = {}


class AirtableOAuthService:
    """Service for handling Airtable OAuth flow."""

    AIRTABLE_AUTHORIZE_URL = "https://airtable.com/oauth2/v1/authorize"
    AIRTABLE_TOKEN_URL = "https://airtable.com/oauth2/v1/token"
    AIRTABLE_API_BASE = "https://api.airtable.com/v0"
    DEFAULT_SCOPES = ["data.records:read", "schema.bases:read"]

    def __init__(self):
        self.repository = OAuthRepository()
        self.client = httpx.AsyncClient()

    def _generate_code_verifier(self) -> str:
        """Generate a code verifier for PKCE."""
        return secrets.token_urlsafe(32)

    def _generate_code_challenge(self, verifier: str) -> str:
        """Generate a code challenge from a code verifier."""
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    async def get_authorization_url(
        self, state: Optional[str] = None
    ) -> Tuple[str, str]:
        """Generate Airtable OAuth authorization URL."""
        if not state:
            state = secrets.token_urlsafe(32)

        # Generate PKCE code verifier and challenge
        code_verifier = self._generate_code_verifier()
        code_challenge = self._generate_code_challenge(code_verifier)

        # Store code_verifier in global storage for later use in token exchange
        _CODE_VERIFIERS[state] = code_verifier

        params = {
            "client_id": settings.AIRTABLE_CLIENT_ID,
            "redirect_uri": settings.AIRTABLE_REDIRECT_URI,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Airtable scopes format
            "scope": " ".join(self.DEFAULT_SCOPES),
        }

        authorization_url = f"{self.AIRTABLE_AUTHORIZE_URL}?{urlencode(params)}"
        return authorization_url, state

    async def exchange_code_for_token(
        self, code: str, state: Optional[str] = None
    ) -> dict:
        """Exchange authorization code for access token."""
        # Get code_verifier from global storage
        code_verifier = _CODE_VERIFIERS.get(state) if state else None

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.AIRTABLE_REDIRECT_URI,
            "client_id": settings.AIRTABLE_CLIENT_ID,
        }

        # Add code_verifier if available (PKCE)
        if code_verifier:
            payload["code_verifier"] = code_verifier
            # Clean up stored verifier
            if state:
                _CODE_VERIFIERS.pop(state, None)

        # Airtable requires Basic Auth with client_id:client_secret
        credentials = f"{settings.AIRTABLE_CLIENT_ID}:{settings.AIRTABLE_CLIENT_SECRET}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        response = await self.client.post(
            self.AIRTABLE_TOKEN_URL,
            data=payload,
            headers={
                "Authorization": f"Basic {encoded_credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response.raise_for_status()
        return response.json()

    async def fetch_user_info(self, access_token: str) -> dict:
        """Fetch Airtable user/workspace info."""
        # Airtable's whoami endpoint
        response = await self.client.get(
            f"{self.AIRTABLE_API_BASE}/meta/whoami",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()

    async def handle_callback(
        self, user_id: str, code: str, state: Optional[str] = None
    ) -> tuple[bool, str, Optional[dict]]:
        """Handle OAuth callback and store connection."""
        try:
            token_data = await self.exchange_code_for_token(code, state)
            access_token = token_data.get("access_token")

            if not access_token:
                return False, "Airtable did not return an access token", None

            # Try to get user info, but don't fail if unavailable
            user_info = {}
            try:
                user_info = await self.fetch_user_info(access_token)
            except Exception as e:
                logger.warning(f"Failed to fetch Airtable user info: {e}")

            expires_at = None
            if token_data.get("expires_in"):
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=int(token_data["expires_in"])
                )

            metadata = {
                "scope": token_data.get("scope"),
                "user": user_info,
            }

            # Extract workspace/user identifiers
            workspace_id = user_info.get("id") if user_info else None
            workspace_name = (
                user_info.get("email") or user_info.get("name") or "Airtable"
            )

            connection_create = OAuthConnectionCreate(
                user_id=user_id,
                provider="airtable",
                access_token=access_token,
                refresh_token=token_data.get("refresh_token"),
                token_type=token_data.get("token_type", "Bearer"),
                expires_at=expires_at,
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                metadata=metadata,
            )

            connection = await self.repository.create(connection_create)

            return (
                True,
                "Successfully connected to Airtable",
                {
                    "workspace": workspace_name,
                    "connection_id": connection.id,
                },
            )
        except httpx.HTTPStatusError as err:
            error_message = (
                f"Airtable token exchange failed: {err.response.status_code}"
            )
            try:
                error_data = err.response.json()
                error_message = error_data.get("error_description") or error_data.get(
                    "error", error_message
                )
            except Exception:
                pass
            return False, error_message, None
        except Exception as exc:
            return False, f"Failed to handle Airtable callback: {exc!s}", None

    async def get_connection(self, user_id: str) -> Optional[OAuthConnection]:
        """Get Airtable connection for a user."""
        return await self.repository.get_by_user_and_provider(user_id, "airtable")

    async def disconnect(self, user_id: str) -> bool:
        """Disconnect Airtable for user."""
        return await self.repository.delete_by_user_and_provider(user_id, "airtable")

    async def is_token_expired(self, user_id: str) -> bool:
        """Check if stored token is expired."""
        connection = await self.get_connection(user_id)
        if not connection or not connection.expires_at:
            return False
        return datetime.now(timezone.utc) > connection.expires_at

    async def refresh_token_if_needed(self, user_id: str) -> Optional[OAuthConnection]:
        """Refresh Airtable access token when expired."""
        if not await self.is_token_expired(user_id):
            return await self.get_connection(user_id)

        connection = await self.get_connection(user_id)
        if not connection or not connection.refresh_token:
            return connection

        credentials = f"{settings.AIRTABLE_CLIENT_ID}:{settings.AIRTABLE_CLIENT_SECRET}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": connection.refresh_token,
            "client_id": settings.AIRTABLE_CLIENT_ID,
        }

        try:
            response = await self.client.post(
                self.AIRTABLE_TOKEN_URL,
                data=payload,
                headers={
                    "Authorization": f"Basic {encoded_credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
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
