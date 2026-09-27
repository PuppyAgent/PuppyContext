"""Load and checksum immutable data migration artifacts."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError

from .errors import ManifestError
from .models import DataMigrationManifest
from .schema_history import data_migration_directories, load_baseline

TRANSACTION_CONTROL_RE = re.compile(
    r"^\s*(?:"
    r"BEGIN(?:[ \t]+[^;\r\n]+)?"
    r"|START[ \t]+TRANSACTION(?:[ \t]+[^;\r\n]+)?"
    r"|COMMIT(?:\s+(?:TRANSACTION|WORK))?(?:\s+AND\s+(?:NO\s+)?CHAIN)?"
    r"|ROLLBACK(?:\s+(?:TRANSACTION|WORK))?(?:\s+AND\s+(?:NO\s+)?CHAIN)?"
    r")\s*;\s*(?:--.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
PSQL_META_RE = re.compile(r"^\s*\\", re.MULTILINE)
COPY_PROGRAM_RE = re.compile(r"\bCOPY\b[^;]*\bPROGRAM\b", re.IGNORECASE | re.DOTALL)
APPLICATION_IMPORT_ROOTS = frozenset({"src"})


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous YAML instead of silently taking the last key."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def discover_repository_root(start: Path | None = None) -> Path:
    """Find the monorepo root without depending on the caller's cwd."""

    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "supabase" / "config.toml").is_file() and (
            candidate / "backend" / "pyproject.toml"
        ).is_file():
            return candidate
    raise ManifestError("could not locate repository root")


@dataclass(frozen=True, slots=True)
class DataMigrationArtifact:
    directory: Path
    manifest_path: Path
    manifest: DataMigrationManifest
    entrypoint_path: Path
    verify_path: Path
    checksum: str


class DataMigrationCatalog:
    """Repository-owned collection of immutable migration artifacts."""

    def __init__(self, repository_root: Path | None = None) -> None:
        self.repository_root = discover_repository_root(repository_root)
        self.root = self.repository_root / "supabase" / "data_migrations"

    def load_all(self) -> list[DataMigrationArtifact]:
        if not self.root.is_dir():
            raise ManifestError(f"data migration directory does not exist: {self.root}")
        directories = data_migration_directories(self.repository_root)
        for directory in directories:
            symlinked_directories = [
                path.name for path in sorted(directory.iterdir()) if path.is_symlink()
            ]
            if symlinked_directories:
                raise ManifestError(
                    "data migration entries cannot be symlinks: " + ", ".join(symlinked_directories)
                )
            missing_manifests = [
                path.name
                for path in sorted(directory.iterdir())
                if path.is_dir()
                and not path.name.startswith(".")
                and not (path / "manifest.yml").is_file()
            ]
            if missing_manifests:
                raise ManifestError(
                    "data migration directories are missing manifest.yml: "
                    + ", ".join(missing_manifests)
                )
        artifacts: list[DataMigrationArtifact] = []
        seen: set[str] = set()
        baseline = load_baseline(self.repository_root)
        for manifest_path in sorted(
            path for directory in directories for path in directory.glob("*/manifest.yml")
        ):
            artifact = self._load(manifest_path)
            if artifact.manifest.id in seen:
                raise ManifestError(f"duplicate migration id: {artifact.manifest.id}")
            seen.add(artifact.manifest.id)
            if baseline and artifact.manifest.id in baseline["source_data_migrations"]:
                expected = baseline["source_data_migrations"][artifact.manifest.id][
                    "artifact_checksum"
                ]
                if artifact.checksum != expected:
                    raise ManifestError(
                        f"archived data artifact checksum changed: {artifact.manifest.id}"
                    )
            artifacts.append(artifact)
        return artifacts

    def get(self, migration_id: str) -> DataMigrationArtifact:
        matches = [item for item in self.load_all() if item.manifest.id == migration_id]
        if not matches:
            raise ManifestError(f"unknown data migration: {migration_id}")
        return matches[0]

    def _load(self, manifest_path: Path) -> DataMigrationArtifact:
        if manifest_path.is_symlink():
            raise ManifestError(f"migration manifest cannot be a symlink: {manifest_path}")
        try:
            raw = yaml.load(
                manifest_path.read_text(encoding="utf-8"),
                Loader=_UniqueKeyLoader,
            )
        except (OSError, yaml.YAMLError) as error:
            raise ManifestError(f"cannot read {manifest_path}: {error}") from error
        if not isinstance(raw, dict):
            raise ManifestError(f"manifest must be a YAML mapping: {manifest_path}")
        try:
            manifest = DataMigrationManifest.model_validate(raw)
        except ValidationError as error:
            raise ManifestError(f"invalid manifest {manifest_path}: {error}") from error
        if manifest_path.parent.name != manifest.id:
            raise ManifestError(
                f"manifest id {manifest.id} must match directory {manifest_path.parent.name}"
            )

        entrypoint_path = manifest_path.parent / manifest.entrypoint
        verify_path = manifest_path.parent / manifest.verify
        for path in (entrypoint_path, verify_path):
            if not path.is_file():
                raise ManifestError(f"migration artifact is missing required file: {path}")
            if path.is_symlink():
                raise ManifestError(f"migration artifact cannot be a symlink: {path}")

        for path in (
            (entrypoint_path, verify_path) if manifest.kind.value == "sql" else (verify_path,)
        ):
            sql = path.read_text(encoding="utf-8")
            if TRANSACTION_CONTROL_RE.search(sql):
                raise ManifestError(
                    f"runner owns the SQL transaction; remove transaction control from {path}"
                )
            if PSQL_META_RE.search(sql):
                raise ManifestError(f"psql meta-commands are forbidden in {path}")
            if COPY_PROGRAM_RE.search(sql):
                raise ManifestError(f"COPY PROGRAM is forbidden in {path}")

        if manifest.kind.value == "python":
            self._validate_python_entrypoint(entrypoint_path)

        canonical_manifest = json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256()
        for relative_name, content in (
            ("manifest", canonical_manifest),
            (manifest.entrypoint, _canonical_text_bytes(entrypoint_path)),
            (manifest.verify, _canonical_text_bytes(verify_path)),
        ):
            digest.update(relative_name.encode())
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")

        return DataMigrationArtifact(
            directory=manifest_path.parent,
            manifest_path=manifest_path,
            manifest=manifest,
            entrypoint_path=entrypoint_path,
            verify_path=verify_path,
            checksum=digest.hexdigest(),
        )

    @staticmethod
    def _validate_python_entrypoint(entrypoint_path: Path) -> None:
        """Keep released Python jobs independent of mutable application code."""

        extra_python = sorted(
            path.name for path in entrypoint_path.parent.glob("*.py") if path != entrypoint_path
        )
        if extra_python:
            raise ManifestError(
                f"Python data migration must be a single self-contained file; "
                f"unexpected: {', '.join(extra_python)}"
            )
        try:
            tree = ast.parse(entrypoint_path.read_text(encoding="utf-8"), entrypoint_path.name)
        except (OSError, SyntaxError) as error:
            raise ManifestError(
                f"cannot parse Python data migration {entrypoint_path}: {error}"
            ) from error
        for node in ast.walk(tree):
            roots: set[str] = set()
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    raise ManifestError(
                        f"Python data migration cannot use relative imports: {entrypoint_path}"
                    )
                if node.module:
                    roots = {node.module.split(".", 1)[0]}
            forbidden = roots & APPLICATION_IMPORT_ROOTS
            if forbidden:
                raise ManifestError(
                    f"Python data migration cannot import mutable application package "
                    f"{sorted(forbidden)[0]}: {entrypoint_path}"
                )


def _canonical_text_bytes(path: Path) -> bytes:
    """Make immutable artifact checksums independent of Git checkout EOLs."""

    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
