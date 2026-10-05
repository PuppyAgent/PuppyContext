"""Version-engine error types owned by PuppyOne."""

from __future__ import annotations


class VersionEngineError(Exception):
    """Base error for version-engine operations."""

    http_status: int = 500


class NotARepoError(VersionEngineError):
    http_status = 400


class SnapshotNotFoundError(VersionEngineError):
    http_status = 404


class ObjectNotFoundError(VersionEngineError):
    http_status = 404


class NativeObjectNotFoundError(ObjectNotFoundError):
    """Canonical native bytes are unavailable; legacy incidents cannot classify it."""

    http_status = 500


class RepositoryRefNotFoundError(KeyError, VersionEngineError):
    """A well-formed ref is absent from an admitted metadata snapshot."""

    http_status = 404


class RepositoryRefTypeError(ValueError, VersionEngineError):
    """A ref's type or repository profile cannot support this path selection."""

    http_status = 400


class PathNotFoundError(VersionEngineError):
    http_status = 404


class AuthenticationError(VersionEngineError):
    http_status = 401


class PermissionDenied(VersionEngineError):
    http_status = 403


class LockError(VersionEngineError):
    http_status = 409


class ConflictError(VersionEngineError):
    http_status = 409


class DirtyWorkdirError(VersionEngineError):
    http_status = 400


class NetworkError(VersionEngineError):
    http_status = 502


class VersionReadError(VersionEngineError):
    http_status = 502


class StorageWriteError(VersionEngineError):
    http_status = 502


class PayloadTooLargeError(VersionEngineError):
    http_status = 413


class ValidationError(VersionEngineError):
    http_status = 422


class ClientTooOldError(VersionEngineError):
    http_status = 426
