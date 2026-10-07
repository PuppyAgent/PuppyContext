"""
Supabase client.

Provides a singleton Supabase client to avoid duplicate connections.
"""

import os
from typing import Optional

import httpx
from supabase import Client, create_client
from supabase.client import ClientOptions

from src.infra.supabase.instrumentation import instrument_httpx_client


class SupabaseClient:
    """Supabase client singleton class"""

    _instance: Optional["SupabaseClient"] = None
    _client: Client | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize Supabase client (only creates connection on first call)"""
        if self._client is None:
            url: str = os.environ.get("SUPABASE_URL", "")
            key: str = os.environ.get("SUPABASE_KEY", "")

            if not url or not key:
                raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set")

            # By default, do not trust environment proxy variables (HTTP_PROXY/HTTPS_PROXY/ALL_PROXY)
            # to avoid proxy-induced Supabase (PostgREST) TLS handshake errors, e.g.:
            #   [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol
            # If Supabase should use the environment proxy, set:
            #   SUPABASE_TRUST_ENV_PROXY=true
            trust_env_proxy = os.environ.get(
                "SUPABASE_TRUST_ENV_PROXY", ""
            ).strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
                "on",
            }

            self._client = create_client(
                url,
                key,
                options=ClientOptions(
                    # Version-engine pushes can upsert hundreds of object
                    # location rows after S3 upload. Ten seconds is too tight
                    # for large Git remotes on staging/local networks.
                    postgrest_client_timeout=60,
                    storage_client_timeout=30,
                    schema="public",
                    httpx_client=instrument_httpx_client(httpx.Client(trust_env=trust_env_proxy)),
                ),
            )

    @property
    def client(self) -> Client:
        """Get Supabase client instance"""
        if self._client is None:
            self.__init__()
        assert self._client is not None
        return self._client

    def get_client(self) -> Client:
        """Get Supabase client instance (method form)"""
        return self.client
