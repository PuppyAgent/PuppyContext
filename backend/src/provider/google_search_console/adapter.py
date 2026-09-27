"""
Google Search Console Provider — pulls search performance data via GSC API.

Auth: OAuth (Google). Uses the Search Analytics endpoint to fetch
clicks, impressions, CTR, and position data for a given site.

API docs: https://developers.google.com/webmaster-tools/v1/searchanalytics
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup

import hashlib
import json
from datetime import datetime, timedelta

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

GSC_API = "https://www.googleapis.com/webmasters/v3"


class GoogleSearchConsoleProvider(BaseProvider):

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="google_search_console",
            display_name="Google Search Console",
            capabilities=Capability.PULL | Capability.INCREMENTAL,
            supported_directions=["inbound"],
            default_trigger=TriggerMode.POLL,
            default_node_type="json",
            auth=AuthRequirement.OAUTH,
            oauth_type="google_search_console",
            oauth_ui_type="google_search_console",
            supported_sync_modes=("manual", "scheduled"),
            default_sync_mode="scheduled",
            creation_mode="direct",
            description="Sync search performance data",
            accept_types=("folder",),
            config_fields=(
                ConfigField(
                    key="date_range",
                    label="Date range",
                    type="select",
                    default="7d",
                    options=[
                        {"value": "7d", "label": "Last 7 days"},
                        {"value": "28d", "label": "Last 28 days"},
                        {"value": "90d", "label": "Last 3 months"},
                    ],
                ),
                ConfigField(
                    key="dimensions",
                    label="Dimensions",
                    type="select",
                    default="query",
                    options=[
                        {"value": "query", "label": "Queries"},
                        {"value": "page", "label": "Pages"},
                        {"value": "query,page", "label": "Queries + Pages"},
                        {"value": "country", "label": "Countries"},
                    ],
                ),
                ConfigField(
                    key="row_limit",
                    label="Max rows",
                    type="number",
                    default=500,
                ),
            ),
            icon="📊",
        )

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        source = config.get("source") or {}
        options = config.get("options") or {}
        site_url = source.get("resource_id", "")
        if not site_url:
            raise ValueError("source.resource_id is required for Google Search Console")

        date_range = options.get("date_range", "7d")
        dimensions = options.get("dimensions", "query")
        row_limit = min(int(options.get("row_limit", 500)), 25000)

        days_map = {"7d": 7, "28d": 28, "90d": 90}
        days = days_map.get(date_range, 7)
        end_date = datetime.utcnow().date() - timedelta(days=3)
        start_date = end_date - timedelta(days=days)

        access_token = credentials.access_token
        if not access_token:
            raise ValueError("OAuth access token is required for Google Search Console")

        headers = {"Authorization": f"Bearer {access_token}"}
        body = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "dimensions": [d.strip() for d in dimensions.split(",")],
            "rowLimit": row_limit,
        }

        encoded_site = site_url.replace(":", "%3A").replace("/", "%2F")

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.post(
                f"{GSC_API}/sites/{encoded_site}/searchAnalytics/query",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("rows", [])
        dim_names = [d.strip() for d in dimensions.split(",")]

        records = []
        for row in rows:
            keys = row.get("keys", [])
            record: dict = {}
            for i, dim in enumerate(dim_names):
                record[dim] = keys[i] if i < len(keys) else ""
            record["clicks"] = row.get("clicks", 0)
            record["impressions"] = row.get("impressions", 0)
            record["ctr"] = round(row.get("ctr", 0), 4)
            record["position"] = round(row.get("position", 0), 1)
            records.append(record)

        content_hash = hashlib.sha256(
            json.dumps(records, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

        return FetchResult(
            content=records,
            content_hash=content_hash,
            node_type="json",
            node_name=f"GSC — {site_url} ({date_range})",
            summary=f"Fetched {len(records)} rows from Search Console ({start_date} to {end_date})",
        )

    async def list_source_resources(
        self,
        credentials: Credentials,
        *,
        query: str = "",
        cursor: str | None = None,
        resource_type: str | None = None,
    ) -> tuple[list[SourceResource], str | None]:
        headers = {"Authorization": f"Bearer {credentials.access_token}"}
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            response = await client.get(f"{GSC_API}/sites")
            response.raise_for_status()
            payload = response.json()

        needle = query.strip().lower()
        resources = []
        for item in payload.get("siteEntry", []):
            site_url = item.get("siteUrl", "")
            if needle and needle not in site_url.lower():
                continue
            resources.append(
                SourceResource(
                    id=site_url,
                    type="search_property",
                    name=site_url,
                    url=None if site_url.startswith("sc-domain:") else site_url,
                    subtitle=item.get("permissionLevel"),
                    icon="google_search_console",
                    metadata={"permission_level": item.get("permissionLevel")},
                )
            )
        return resources, None


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup
    from src.provider.oauth.google_search_console_service import (
        GoogleSearchConsoleOAuthService,
    )

    oauth_svc = GoogleSearchConsoleOAuthService()
    return ProviderSetup(
        adapter=GoogleSearchConsoleProvider(),
        oauth_bindings={"google_search_console": oauth_svc},
    )
