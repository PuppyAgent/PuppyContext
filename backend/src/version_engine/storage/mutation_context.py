"""Transport-independent provenance for native storage-index mutations.

Contexts propagate through async/thread bridges, but are NOT authorization or
leases by themselves. PostgreSQL revalidates the pin/epoch or non-expiring GC
token when the actual mutation executes, including delayed requests.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class PublicationStorageContext:
    project_id: str
    actor: str
    pin_id: str


@dataclass(frozen=True)
class CollectionStorageContext:
    project_id: str
    token: str


publication_context: ContextVar[PublicationStorageContext | None] = ContextVar(
    "native_publication_storage", default=None,
)
collection_context: ContextVar[CollectionStorageContext | None] = ContextVar(
    "native_collection_storage", default=None,
)


@contextmanager
def publication_storage(project_id: str, actor: str, pin_id: str):
    token = publication_context.set(PublicationStorageContext(project_id, actor, pin_id))
    try:
        yield
    finally:
        publication_context.reset(token)


@contextmanager
def collection_storage(project_id: str, gc_token: str):
    token = collection_context.set(CollectionStorageContext(project_id, gc_token))
    try:
        yield
    finally:
        collection_context.reset(token)
