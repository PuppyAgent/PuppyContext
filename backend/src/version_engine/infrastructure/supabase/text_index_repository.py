"""Text-index repository — reads + writes the ``version_text_index``
table that powers ``/ap-fs/grep-indexed`` and ``/ap-fs/search``.

Schema is defined in
``supabase/archive/before_b1/migrations/20260526000000_version_text_index.sql``;
the design rationale lives in
``docs/proposals/PUP-cloud-grep.md``.

The query surface is intentionally narrow:

    - ``query_indexed_grep`` — substring / regex / word match with
      ``pg_trgm`` + ``tsvector`` candidate selection.
    - ``get_freshness`` — derives ``indexed`` / ``stale`` / ``missing``
      for a given (project, scope) tuple.
    - ``upsert_chunks`` — used by the outbox indexer worker; idempotent
      via the natural key ``(project_id, content_hash, chunk_idx)``.

Why we don't expose generic CRUD: callers go through these three
entry points so the GIN-friendly query shape stays in one place and
the upsert path can't accidentally bypass the natural-key dedupe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from src.infra.supabase.client import SupabaseClient
from src.utils.logger import log_error


_TABLE = "version_text_index"
_STATE_TABLE = "version_text_index_state"


def _escape_like(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards so a literal ``%`` / ``_`` in the
    user-supplied value isn't treated as a wildcard. Backslash first so
    we don't double-escape our own escapes."""
    return (
        value.replace("\\", "\\\\")
        .replace("%", r"\%")
        .replace("_", r"\_")
    )


def _pgrst_or_quote(value: str) -> str:
    """Wrap a value for safe interpolation into a PostgREST ``or=``
    mini-language clause.

    The ``or=`` grammar splits conditions on commas and treats ``.``,
    ``(``, ``)`` as syntax, so any value carrying those (a path with a
    dot or comma) would corrupt the filter. Double-quoting tells the
    parser to treat the contents as a literal; embedded double quotes
    are backslash-escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class IndexHit:
    """One matched line in the indexed grep response.

    The ``chunk_text`` is the entire indexed chunk (~4 KB), not just
    the matched line — the caller (HTTP layer) does the final regex
    against this chunk to recover line offsets + context windows.
    Doing the per-line work in the application layer keeps the SQL
    GIN-friendly: the index gives us "this chunk contains your
    pattern," and Python does the precise cut.
    """
    file_path: str
    content_hash: str
    chunk_idx: int
    line_start: int
    chunk_text: str


@dataclass
class FreshnessSnapshot:
    """Per-(project, scope) freshness watermark.

    ``status`` is the value the API returns to the CLI so it knows
    whether the indexed channel is authoritative or needs the
    legacy S3 fallback.
    """
    status: str  # "indexed" | "stale" | "missing"
    indexed_commit_id: str
    head_commit_id: str
    commits_behind: int  # 0 if indexed; -1 if missing (unknown).


class TextIndexRepository:
    """Thin Supabase wrapper for ``version_text_index``."""

    def __init__(self, client: SupabaseClient | None = None) -> None:
        self._client = (client or SupabaseClient()).client

    # ────────────────────────────────────────────────────────────────
    # Read side
    # ────────────────────────────────────────────────────────────────

    def query_indexed_grep(
        self,
        *,
        project_id: str,
        scope_path: str,
        pattern: str,
        regex: bool,
        ignore_case: bool,
        candidate_limit: int = 2000,
    ) -> list[IndexHit]:
        """Pull candidate chunks that might match ``pattern``.

        ``pg_trgm`` is the load-bearing operator here:

            - ``regex=True``         → ``text ~ pattern``   (regex)
            - ``ignore_case=True``   → ``text ILIKE %p%``   (substring, CI)
            - default                → ``text LIKE %p%``    (substring, CS)

        Postgres pushes a trigram filter down for all three, so a
        five-character pattern hits the GIN index in ~ms even on
        billions of rows. We DELIBERATELY pull a small candidate set
        and re-filter in Python — sending the precise per-line cut
        back to the caller from SQL would tie us to RPC functions we
        don't need yet.

        We also intentionally don't WHERE on ``tsv @@ to_tsquery(...)``
        for the substring path: trigram alone is strict enough, and
        falling back to tsvector for word-boundary semantics would
        miss substring hits inside words ("foo" inside "foobar").
        """
        # PostgREST quirk: bound parameters in ``filter()`` get
        # interpreted as PostgREST mini-language, so wildcards in the
        # value need URL-style escaping. Server-side substring search
        # is the hot path; encode ``%`` as ``\%`` so a literal "%"
        # the user types stays literal.
        like_value = "%" + _escape_like(pattern) + "%"
        query = (
            self._client.table(_TABLE)
            .select("file_path, content_hash, chunk_idx, line_start, text")
            .eq("project_id", project_id)
        )
        if scope_path:
            # AP scope is bounded by FILE PATH, not by the row's
            # ``scope_path`` column — the indexer writes every chunk
            # with ``scope_path=""`` (the canonical project root)
            # because chunks are global within the project. The READ
            # side narrows by the file's location: an AP scoped to
            # ``notes/`` matches both ``notes/x.md`` and
            # ``notes/sub/y.md``. Two predicates (exact + prefix)
            # keep the planner from doing a sequential scan when the
            # scope is the project root.
            #
            # The value is interpolated into the PostgREST ``or=``
            # mini-language, which splits conditions on commas and
            # treats ``.``/``(``/``)`` as syntax. A scope_path with any
            # of those (or a LIKE wildcard ``%``/``_``) would corrupt
            # the filter, so: (a) escape LIKE wildcards for the prefix
            # predicate, (b) wrap both values in double quotes so the
            # mini-language parser treats them as literals.
            exact_val = _pgrst_or_quote(scope_path)
            prefix_val = _pgrst_or_quote(_escape_like(scope_path) + "/*")
            query = query.or_(
                f"file_path.eq.{exact_val},"
                f"file_path.like.{prefix_val}"
            )

        if regex:
            # PostgREST exposes ``~`` (case-sensitive POSIX regex) as
            # ``match`` and ``~*`` (CI POSIX regex) as ``imatch``.
            op = "imatch" if ignore_case else "match"
            query = query.filter("text", op, pattern)
        else:
            op = "ilike" if ignore_case else "like"
            query = query.filter("text", op, like_value)

        query = query.limit(max(1, min(int(candidate_limit), 5000)))

        try:
            resp = query.execute()
        except Exception as exc:
            log_error(f"[TextIndex] candidate query failed: {exc}")
            return []
        rows = resp.data or []
        return [
            IndexHit(
                file_path=row["file_path"],
                content_hash=row["content_hash"],
                chunk_idx=row["chunk_idx"],
                line_start=row["line_start"],
                chunk_text=row["text"],
            )
            for row in rows
        ]

    def get_freshness(
        self,
        *,
        project_id: str,
        scope_path: str,  # noqa: ARG002 — kept for callsite stability
        head_commit_id: str,
        rows_estimate: int | None = None,
    ) -> FreshnessSnapshot:
        """Resolve ``indexed`` / ``stale`` / ``missing`` for the project.

        The freshness watermark is project-scoped, not AP-scoped: the
        indexer fires once per commit at the project root and stamps a
        single ``(project_id, scope_path='')`` row. Per-AP watermarks
        would be wrong here because the same content_hash can appear
        under multiple AP scopes and we want all of them to see the
        same "is the index up to date" answer.

        ``scope_path`` is kept in the signature so future per-scope
        bootstrap paths (e.g. the admin reindex endpoint) can opt in
        without churning every caller; today it's ignored.

        Cheap; one row lookup. ``rows_estimate`` is informational only —
        when the caller already counted candidate hits it can be passed
        to short-circuit the "is any row present at all" check.
        """
        resp = (
            self._client.table(_STATE_TABLE)
            .select("indexed_commit_id")
            .eq("project_id", project_id)
            .eq("scope_path", "")
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            # No state row → indexer has never run for this scope.
            # If the caller saw 0 candidate rows that's consistent
            # with ``missing``; if it saw rows, those rows came from
            # the indexer running before the state table existed
            # (bootstrap migration). Treat as ``stale`` in that case
            # so the CLI knows the result is best-effort.
            if rows_estimate and rows_estimate > 0:
                return FreshnessSnapshot(
                    status="stale",
                    indexed_commit_id="",
                    head_commit_id=head_commit_id,
                    commits_behind=-1,
                )
            return FreshnessSnapshot(
                status="missing",
                indexed_commit_id="",
                head_commit_id=head_commit_id,
                commits_behind=-1,
            )
        indexed = rows[0].get("indexed_commit_id") or ""
        if indexed == head_commit_id:
            return FreshnessSnapshot(
                status="indexed",
                indexed_commit_id=indexed,
                head_commit_id=head_commit_id,
                commits_behind=0,
            )
        # We don't compute a real ``commits_behind`` here — that would
        # require walking the commit log. ``-1`` signals "behind by
        # an unknown amount"; the CLI surfaces that as "stale".
        return FreshnessSnapshot(
            status="stale",
            indexed_commit_id=indexed,
            head_commit_id=head_commit_id,
            commits_behind=-1,
        )

    # ────────────────────────────────────────────────────────────────
    # Write side (used by the outbox indexer)
    # ────────────────────────────────────────────────────────────────

    def upsert_chunks(
        self,
        *,
        project_id: str,
        scope_path: str,
        file_path: str,
        content_hash: str,
        chunks: Iterable[tuple[int, int, str]],  # (chunk_idx, line_start, text)
    ) -> int:
        """Idempotent upsert keyed by ``(project_id, content_hash, chunk_idx)``.

        Returns the number of rows the call wrote (best-effort —
        Supabase doesn't always echo affected counts).
        """
        rows: list[dict] = []
        for chunk_idx, line_start, text in chunks:
            rows.append({
                "project_id": project_id,
                "scope_path": scope_path or "",
                "file_path": file_path,
                "content_hash": content_hash,
                "chunk_idx": int(chunk_idx),
                "line_start": int(line_start),
                "text": text,
            })
        if not rows:
            return 0
        try:
            resp = (
                self._client.table(_TABLE)
                .upsert(rows, on_conflict="project_id,content_hash,chunk_idx")
                .execute()
            )
        except Exception as exc:
            log_error(f"[TextIndex] upsert_chunks failed: {exc}")
            return 0
        return len(resp.data or rows)

    def delete_file(
        self,
        *,
        project_id: str,
        file_path: str,
        include_subtree: bool = False,
    ) -> int:
        """Remove index rows attributed to ``file_path``.

        Called by the commit-delta indexer in two cases (GAP-6):

          - **delete** — a file (or, with ``include_subtree``, a whole
            directory subtree) was removed in the commit, so its rows
            must be purged or ``grep`` keeps returning the dead path.
          - **modify** — a file's content changed; we clear its old
            rows before re-indexing the new content so stale chunks
            from the previous version aren't searchable, and so the
            index stays bounded to the live working tree rather than
            growing one row-set per historical content version.

        ``include_subtree`` also removes rows whose ``file_path`` lives
        under ``file_path/`` — used for directory deletes where the
        change list carries the directory path, not each child.

        Note: rows are content-addressed by ``content_hash`` with
        ``file_path`` recording the last writer. In the rare case two
        distinct paths share identical bytes, deleting one path can
        drop the shared row and make the other temporarily unsearchable
        until its next commit re-indexes it. That trade-off is inherent
        to the "indexed exactly once" design in the table migration.

        Returns a best-effort deleted row count.
        """
        try:
            query = (
                self._client.table(_TABLE)
                .delete()
                .eq("project_id", project_id)
            )
            if include_subtree:
                exact_val = _pgrst_or_quote(file_path)
                prefix_val = _pgrst_or_quote(_escape_like(file_path) + "/*")
                query = query.or_(
                    f"file_path.eq.{exact_val},"
                    f"file_path.like.{prefix_val}"
                )
            else:
                query = query.eq("file_path", file_path)
            resp = query.execute()
        except Exception as exc:
            log_error(f"[TextIndex] delete_file({file_path}) failed: {exc}")
            return 0
        return len(resp.data or [])

    def set_scope_freshness(
        self,
        *,
        project_id: str,
        scope_path: str,
        indexed_commit_id: str,
    ) -> None:
        """Bump the per-scope freshness watermark.

        Called by the indexer once a commit's deltas have all been
        upserted. Idempotent.
        """
        try:
            (
                self._client.table(_STATE_TABLE)
                .upsert(
                    {
                        "project_id": project_id,
                        "scope_path": scope_path or "",
                        "indexed_commit_id": indexed_commit_id,
                    },
                    on_conflict="project_id,scope_path",
                )
                .execute()
            )
        except Exception as exc:
            log_error(f"[TextIndex] set_scope_freshness failed: {exc}")


# ────────────────────────────────────────────────────────────────────
# Helpers shared by the HTTP layer
# ────────────────────────────────────────────────────────────────────


def cut_chunk_to_hits(
    *,
    chunk_text: str,
    line_start: int,
    matcher: "re.Pattern[str]",
    invert: bool = False,
    only_matching: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    per_file_remaining: int | None = None,
) -> list[dict]:
    """Re-run the precise matcher against one chunk and produce hit
    dicts the API caller can return verbatim.

    Pulled out so the SQL layer never has to know about line-by-line
    formatting. The chunk-text shape is exactly what
    ``IndexHit.chunk_text`` contains.
    """
    lines = chunk_text.splitlines()
    hits: list[dict] = []
    for offset, line in enumerate(lines):
        m = matcher.search(line)
        matched = (m is not None)
        if invert:
            matched = not matched
        if not matched:
            continue
        line_no = line_start + offset
        out_line = line if not only_matching or invert else (m.group(0) if m else line)
        before = lines[max(0, offset - before_context):offset] if before_context else []
        after = lines[offset + 1:offset + 1 + after_context] if after_context else []
        hits.append({
            "line": line_no,
            "col": (m.start() + 1) if (m and not invert) else 0,
            "match": out_line,
            "context_before": before,
            "context_after": after,
        })
        if per_file_remaining is not None and len(hits) >= per_file_remaining:
            break
    return hits
