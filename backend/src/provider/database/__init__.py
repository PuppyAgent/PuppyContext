"""DB Provider Registry"""

from src.provider.database.base import BaseDBProvider
from src.provider.database.supabase_rest import SupabaseRestProvider

# Registry: add a new line here for each new database
PROVIDERS: dict[str, type[BaseDBProvider]] = {
    "supabase": SupabaseRestProvider,
    # "mysql": MySQLProvider,
}


def get_provider(provider_type: str) -> BaseDBProvider:
    """Get the corresponding Provider instance."""
    cls = PROVIDERS.get(provider_type)
    if not cls:
        supported = ", ".join(PROVIDERS.keys())
        raise ValueError(f"Unsupported database provider: {provider_type}. Supported: {supported}")
    return cls()
