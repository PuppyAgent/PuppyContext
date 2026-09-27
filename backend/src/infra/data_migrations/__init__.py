"""Portable, manifest-driven production data migrations."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .catalog import DataMigrationArtifact, DataMigrationCatalog
    from .runner import DataMigrationRunner

__all__ = ["DataMigrationArtifact", "DataMigrationCatalog", "DataMigrationRunner"]


def __getattr__(name: str):
    # Schema-history admission is standard-library-only. Importing it must not
    # initialize the application/data-job runtime or require its dependencies.
    if name in {"DataMigrationArtifact", "DataMigrationCatalog"}:
        from . import catalog

        return getattr(catalog, name)
    if name == "DataMigrationRunner":
        from .runner import DataMigrationRunner

        return DataMigrationRunner
    raise AttributeError(name)
