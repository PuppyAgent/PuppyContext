"""
OCR Provider Factory

Factory for creating OCR provider instances based on configuration.
"""

import logging
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.infra.file_processing.ocr.base import OCRProvider, OCRProviderConfigError

logger = logging.getLogger(__name__)


class OCRConfig(BaseSettings):
    """OCR provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    # Default OCR provider to use
    ocr_provider: str = Field(
        default="mineru",
        description="OCR provider to use: 'mineru' or 'reducto'",
    )


# Global config instance
ocr_config = OCRConfig()


class OCRProviderFactory:
    """
    Factory for creating OCR provider instances.

    Usage:
        provider = OCRProviderFactory.get_provider()
        result = await provider.parse_document(url)
    """

    _providers: ClassVar[dict[str, type[OCRProvider]]] = {}

    @classmethod
    def register(cls, name: str, provider_class: type[OCRProvider]) -> None:
        """
        Register a provider class.

        Args:
            name: Provider name (e.g., 'mineru', 'reducto')
            provider_class: Provider class implementing OCRProvider
        """
        cls._providers[name.lower()] = provider_class
        logger.debug(f"Registered OCR provider: {name}")

    @classmethod
    def get_provider(cls, name: str | None = None) -> OCRProvider:
        """
        Get an OCR provider instance.

        Args:
            name: Provider name (defaults to OCR_PROVIDER env var)

        Returns:
            Configured OCRProvider instance

        Raises:
            OCRProviderConfigError: If provider is not found or not configured
        """
        provider_name = (name or ocr_config.ocr_provider).lower()

        # Lazy load providers to avoid import issues
        if not cls._providers:
            cls._register_default_providers()

        if provider_name not in cls._providers:
            available = list(cls._providers.keys())
            raise OCRProviderConfigError(
                provider=provider_name,
                message=f"Unknown OCR provider '{provider_name}'. Available: {available}",
            )

        provider_class = cls._providers[provider_name]
        logger.info(f"Creating OCR provider: {provider_name}")

        return provider_class()

    @classmethod
    def _register_default_providers(cls) -> None:
        """Register built-in providers."""
        from src.infra.file_processing.ocr.deepseek_provider import DeepSeekOCRProvider
        from src.infra.file_processing.ocr.mineru_adapter import MineRUProvider
        from src.infra.file_processing.ocr.reducto_provider import ReductoProvider

        cls.register("mineru", MineRUProvider)
        cls.register("reducto", ReductoProvider)
        cls.register("deepseek", DeepSeekOCRProvider)

def get_ocr_provider(name: str | None = None) -> OCRProvider:
    """
    Convenience function to get an OCR provider.

    Args:
        name: Provider name (optional, defaults to env config)

    Returns:
        Configured OCRProvider instance
    """
    return OCRProviderFactory.get_provider(name)

