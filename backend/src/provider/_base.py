"""
BaseProvider — Unified base class for all sync connectors.

Three-layer architecture:
  Trigger Layer  (when)  →  manual / scheduler / webhook / realtime (client-side)
  Provider Layer (what) →  connector.fetch(config, credentials) → FetchResult
  Write Layer    (how)   →  SynchronizeVersionWritePort → Version Engine commands

Provider only has ONE core method: fetch().
It does NOT know who triggered it or how data is stored.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Flag, auto, Enum
from typing import Any, List, Optional, TYPE_CHECKING


# ============================================================
# Capability flags
# ============================================================

class Capability(Flag):
    PULL        = auto()
    PUSH        = auto()
    INCREMENTAL = auto()
    REALTIME    = auto()
    BOOTSTRAP   = auto()


class AuthRequirement(str, Enum):
    NONE       = "none"
    OAUTH      = "oauth"
    OPTIONAL_OAUTH = "optional_oauth"
    API_KEY    = "api_key"
    ACCESS_KEY = "access_key"


# ============================================================
# Credentials (passed to fetch by SynchronizeEngine)
# ============================================================

@dataclass
class Credentials:
    """OAuth / API credentials resolved by SynchronizeEngine, passed to fetch()."""
    access_token: str = ""
    metadata: dict = field(default_factory=dict)


# ============================================================
# FetchResult (returned by fetch)
# ============================================================

@dataclass
class FetchResult:
    """
    Returned by connector.fetch() — the data pulled from an external source.

    SynchronizeEngine uses content_hash to decide whether to write (compare with
    sync.remote_hash). If `files` is provided, the engine writes the
    returned path->bytes map through the Integration Version write port at the
    sync mount point. Otherwise it writes `content` as the connector's single output
    file. Connectors stay storage-agnostic in both cases.
    """
    content: Any
    content_hash: str
    node_type: str = "json"
    node_name: Optional[str] = None
    summary: Optional[str] = None
    files: Optional[dict[str, bytes]] = None


@dataclass
class SourceResource:
    """Selectable external resource returned by provider resource pickers."""
    id: str
    type: str
    name: str
    url: Optional[str] = None
    subtitle: Optional[str] = None
    icon: Optional[str] = None
    authorized: bool = True
    metadata: dict = field(default_factory=dict)


# ============================================================
# PushResult (returned by push)
# ============================================================

@dataclass
class PushResult:
    """Returned by connector.push() — result of pushing data to external source."""
    success: bool = True
    remote_hash: str = ""
    summary: str = ""
    error: str = ""


# ============================================================
# Config field descriptor (for dynamic UI generation)
# ============================================================

@dataclass(frozen=True)
class ConfigField:
    """Describes one user-configurable option for a connector."""
    key: str
    label: str
    type: str = "text"              # text | select | number | url
    required: bool = False
    default: Any = None
    options: Optional[List[dict]] = None   # for type=select: [{"value": "...", "label": "..."}]
    placeholder: Optional[str] = None
    hint: Optional[str] = None


# ============================================================
# Provider specification
# ============================================================

@dataclass(frozen=True)
class ProviderSpec:
    """
    Static descriptor of a connector — declared once.

    SynchronizeEngine reads this to decide what operations are valid,
    what auth to check, and how to wire triggers.
    Registry exposes this via GET /integrations/connectors for frontend.
    """
    provider: str
    display_name: str
    capabilities: Capability
    supported_directions: list[str]
    default_node_type: str = "json"
    auth: AuthRequirement = AuthRequirement.NONE
    oauth_type: Optional[str] = None
    oauth_ui_type: Optional[str] = None
    config_schema: Optional[dict] = None

    # Dynamic UI and registry fields
    creation_mode: str = "direct"  # direct | bootstrap
    config_fields: tuple[ConfigField, ...] = ()
    icon: Optional[str] = None
    icon_url: Optional[str] = None
    description: Optional[str] = None
    accept_types: tuple[str, ...] = ("folder",)
    ui_visible: bool = True


# ============================================================
# Base connector
# ============================================================

class BaseProvider(ABC):
    """
    Unified base class for all external-system connectors.

    Subclasses MUST implement:
      - spec()   — static capability declaration
      - fetch()  — core data retrieval method

    Subclasses MAY override:
      - push()         — for bidirectional sync
      - list_resources() / setup_trigger() / teardown_trigger()
    """

    @abstractmethod
    def spec(self) -> ProviderSpec:
        """Return the static capability descriptor for this connector."""

    @abstractmethod
    async def fetch(
        self,
        config: dict,
        credentials: Credentials,
    ) -> FetchResult:
        """
        Pull data from the external source. This is the ONLY method
        a connector must implement for data retrieval.

        Args:
            config:      Provider-specific config (labels, max_results, etc.)
            credentials: OAuth token + metadata, resolved by SynchronizeEngine.

        Returns:
            FetchResult with content, content_hash, and node metadata.

        The connector does NOT:
          - Know who triggered the fetch (manual / scheduler / webhook)
          - Know how data is stored (no node_service / collab_service)
          - Manage OAuth token refresh (SynchronizeEngine handles that)
        """

    async def pull(self, source: "SourceInput") -> "FetchResult":
        """Pull latest data from external source.

        Default raises
        NotImplementedError — connectors that support pull should
        override this, or SynchronizeEngine.execute() should be used instead
        (which calls fetch() with proper credentials).
        """
        raise NotImplementedError(
            f"{self.spec().provider} does not implement pull(); "
            f"use SynchronizeEngine.execute() instead"
        )

    async def push(
        self, source: "SourceInput", content: Any, node_type: str,
    ) -> PushResult:
        """Push data to external source. Override for bidirectional connectors."""
        raise NotImplementedError(
            f"{self.spec().provider} does not support push"
        )

    async def list_resources(self, source: "SourceInput") -> List["ResourceInfo"]:
        return []

    async def list_source_resources(
        self,
        credentials: Credentials,
        *,
        query: str = "",
        cursor: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> tuple[list[SourceResource], Optional[str]]:
        return [], None

    async def setup_trigger(self, source: "SourceInput") -> Optional[Any]:
        return None

    async def teardown_trigger(self, source: "SourceInput") -> None:
        pass


# ============================================================
# Auto-discovery support
# ============================================================

@dataclass
class ProviderDeps:
    """Shared dependencies injected into connector setup() functions."""
    s3_service: Any
    node_service: Any = None


@dataclass
class ProviderSetup:
    """Returned by each connector module's setup() function."""
    adapter: "BaseProvider"
    oauth_bindings: dict[str, Any] = field(default_factory=dict)


if TYPE_CHECKING:
    from src.provider.schemas import SourceInput, ResourceInfo
