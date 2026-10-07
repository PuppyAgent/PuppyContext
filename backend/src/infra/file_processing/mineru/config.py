"""
MineRU Client Configuration

Configuration for MineRU API client.
"""


from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MineRUConfig(BaseSettings):
    """Configuration for MineRU API client."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    # API Key from environment
    mineru_api_key: str | None = Field(default=None, description="MineRU API Key")

    # API endpoint
    mineru_api_base_url: str = Field(
        default="https://mineru.net/api/v4", description="MineRU API base URL"
    )

    # Polling settings
    mineru_poll_interval: int = Field(
        default=5, description="Polling interval in seconds"
    )

    mineru_max_wait_time: int = Field(
        default=600,
        description="Maximum wait time for task completion in seconds (10 minutes)",
    )

    # File size and page limits (MineRU constraints)
    mineru_max_file_size_mb: int = Field(
        default=200, description="Maximum file size in MB"
    )

    mineru_max_pages: int = Field(default=600, description="Maximum number of pages")

    # Cache directory
    mineru_cache_dir: str = Field(
        default=".mineru_cache", description="Directory for caching MineRU results"
    )


# Global config instance
mineru_config = MineRUConfig()
