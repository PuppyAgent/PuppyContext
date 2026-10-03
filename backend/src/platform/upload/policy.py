"""Folder-upload policy — single source of truth for the backend.

See ``docs/proposals/PUP-3-folder-upload-policy.md`` for the product
contract this implements. The frontend mirror lives at
``frontend/lib/uploadPolicy.ts`` and the CLI mirror at
``cli/src/commands/fs/lib/upload-policy.js``; any constant changes here
MUST be applied in those two files as well.

The policy has three independent filtering stages applied in order:
  1. Hardcoded blocklist  — segments no user wants in their tree (.git/,
     .DS_Store, node_modules/, generated build output).
  2. .gitignore / .puppyignore at the upload root.
  3. Dotfile rule — skip-by-default, includable via explicit opt-in.

Threshold constants gate the per-file / per-batch limits.

The backend re-runs stages 1 + thresholds as defense in depth (Q8); the
frontend / CLI are the primary enforcement and surface the override UX.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass

# ── Hardcoded blocklist (Q1 minimum) ──────────────────────────────
#
# Each entry is a single path *segment* name (not a glob). The policy
# rejects a file when ANY segment of its mount_path matches one of
# these names. Conservative on purpose: this is the "no user ever
# wants to import this" set.
#
# Anything project-specific (build output a particular project doesn't
# treat as build output, etc.) belongs in .puppyignore / .gitignore, not
# here.
DEFAULT_BLOCKLIST_SEGMENTS: frozenset[str] = frozenset({
    # Version-control internals
    ".git",
    ".svn",
    ".hg",
    # OS junk
    ".DS_Store",
    "Thumbs.db",
    # Dependency / virtualenv directories
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    # Build output (common JS / TS / Rust / Python conventions)
    ".next",
    "dist",
    "build",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    "target",  # cargo / maven
})

# ── Thresholds (Q4) ────────────────────────────────────────────────
PER_FILE_MAX_BYTES: int = 100 * 1024 * 1024             # 100 MB
PER_BATCH_MAX_FILES: int = 5000
PER_BATCH_MAX_BYTES: int = 1024 * 1024 * 1024           # 1 GB
PREFLIGHT_FILE_THRESHOLD: int = 50
PREFLIGHT_BYTES_THRESHOLD: int = 100 * 1024 * 1024      # 100 MB


# ── Public helpers ─────────────────────────────────────────────────


def is_blocked_segment(segment: str) -> bool:
    """Return True iff ``segment`` matches a hardcoded blocklist name.

    Case-sensitive on Unix conventions (".DS_Store", "Thumbs.db"
    capitalized as they appear on-disk). We don't lowercase because
    real Linux filesystems are case-sensitive and a file legitimately
    named ``Dist`` (e.g. a German place name) shouldn't get blocked.
    """
    return segment in DEFAULT_BLOCKLIST_SEGMENTS


def path_has_blocked_segment(path: str) -> tuple[bool, str | None]:
    """Walk every '/' separated segment of ``path``; return (True, name)
    on the first hit, else (False, None).

    Empty segments are ignored (treat ``a//b`` as ``a/b``). The check is
    intentionally segment-based, not substring-based: a file legitimately
    named ``my.gitignore`` shouldn't trip the ``.git`` block.
    """
    for raw_seg in path.split("/"):
        seg = raw_seg.strip()
        if not seg:
            continue
        if is_blocked_segment(seg):
            return True, seg
    return False, None


def is_dotfile_path(path: str) -> bool:
    """True if ANY segment of ``path`` starts with '.'.

    Used by the dotfile-skip default (Q2). Note ``.git`` would match
    here too — but the blocklist check fires first and gives a more
    specific reason. This is only consulted for the residual dotfile
    decision.
    """
    for raw_seg in path.split("/"):
        seg = raw_seg.strip()
        if seg and seg.startswith("."):
            return True
    return False


@dataclass(frozen=True)
class IgnoreRule:
    """One parsed line of a .gitignore / .puppyignore file.

    We support a deliberate subset of the gitignore spec:
      - leading '#' = comment, skip
      - leading '!' = negation, re-include (rare; we still parse it)
      - trailing '/' = directory-only (matches that name as a dir segment)
      - '**' for zero-or-more path segments
      - '*' for any chars within a segment
      - '?' for one char
    Out of scope: anchored matching with leading '/', deeply-nested
    negation interactions, character classes. We can extend if user
    feedback demands it; the unsupported corners are rare in real
    .gitignore files for the kinds of folders Puppyone is imported with.
    """

    pattern: str
    negate: bool
    dir_only: bool

    @classmethod
    def parse_line(cls, line: str) -> "IgnoreRule | None":
        s = line.strip()
        if not s or s.startswith("#"):
            return None
        negate = False
        if s.startswith("!"):
            negate = True
            s = s[1:]
        dir_only = s.endswith("/")
        if dir_only:
            s = s[:-1]
        if not s:
            return None
        return cls(pattern=s, negate=negate, dir_only=dir_only)


def parse_ignore_text(text: str) -> list[IgnoreRule]:
    """Parse a .gitignore / .puppyignore body into rules."""
    rules: list[IgnoreRule] = []
    for line in text.splitlines():
        rule = IgnoreRule.parse_line(line)
        if rule is not None:
            rules.append(rule)
    return rules


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob (supporting ``**``) into a
    regex that matches against the full relative path.

    ``fnmatch.translate`` handles ``*`` and ``?`` but expands ``**`` as
    ``.*`` which over-matches across path separators in unintended ways.
    We do a small pre-pass for ``**`` ourselves, then hand the rest to
    ``fnmatch`` segment-by-segment.
    """
    # Anchor pattern containing '/' to start of path; non-anchored
    # patterns match any path segment (gitignore semantics).
    if "/" in pattern:
        anchored = True
    else:
        anchored = False
    # Replace '**' first so single '*' isn't expanded inside it.
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i:i + 2] == "**":
            parts.append("(?:.*/)?")
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    regex = "".join(parts)
    if anchored:
        return re.compile(f"^{regex}(?:/.*)?$")
    return re.compile(f"(?:^|.*/)({regex})(?:/.*)?$")


def matches_ignore_rules(
    path: str,
    rules: list[IgnoreRule],
    *,
    is_dir: bool = False,
) -> bool:
    """Apply ``rules`` to ``path`` in order; later matches win
    (gitignore semantics: a later '!pattern' un-ignores).

    Returns True if the path is ignored.
    """
    ignored = False
    for rule in rules:
        if rule.dir_only and not is_dir:
            # Check if any ancestor segment matches; if so, this file
            # under that directory is also ignored. Simplified: just
            # test whether the directory name appears as a segment.
            segs = path.split("/")
            # Drop the leaf — directory rule matches the parent.
            for seg in segs[:-1]:
                if fnmatch.fnmatchcase(seg, rule.pattern):
                    ignored = not rule.negate
                    break
            continue
        regex = _glob_to_regex(rule.pattern)
        if regex.match(path):
            ignored = not rule.negate
    return ignored


# ── Per-file / per-batch policy entry point ───────────────────────


@dataclass(frozen=True)
class PolicyOptions:
    """Per-upload overrides toggled by the caller (UI / CLI flag)."""

    include_blocklist: bool = False
    include_ignored: bool = False
    include_hidden: bool = False
    extra_rules: tuple[IgnoreRule, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of evaluating one file against the policy."""

    accept: bool
    reason: str  # "ok", "blocklist:.git", "gitignore", "hidden", "too_large"


@dataclass(frozen=True)
class BatchLimitViolation:
    """Hard batch cap violation, evaluated after upload filtering."""

    kind: str  # "file_count" | "total_bytes"
    actual: int
    limit: int


def evaluate_file(
    relative_path: str,
    size_bytes: int,
    rules: list[IgnoreRule],
    options: PolicyOptions,
) -> PolicyDecision:
    """Single-file decision used by the backend and mirror-tested
    against the frontend / CLI implementations.

    Stages run in order; the first reject wins so the reason is
    actionable. Per-file size cap is a hard reject even when other
    overrides are on (caller can split into smaller batches).
    """
    if size_bytes > PER_FILE_MAX_BYTES:
        return PolicyDecision(accept=False, reason="too_large")

    blocked, seg = path_has_blocked_segment(relative_path)
    if blocked and not options.include_blocklist:
        return PolicyDecision(accept=False, reason=f"blocklist:{seg}")

    if rules or options.extra_rules:
        combined = list(rules) + list(options.extra_rules)
        if matches_ignore_rules(relative_path, combined) and not options.include_ignored:
            return PolicyDecision(accept=False, reason="gitignore")

    if is_dotfile_path(relative_path) and not options.include_hidden:
        # If the dotfile was a blocked segment we'd have caught it
        # above; this catches "real" dotfiles like .env / .DS_Store
        # (.DS_Store is also blocklisted so it never reaches here).
        return PolicyDecision(accept=False, reason="hidden")

    return PolicyDecision(accept=True, reason="ok")


def evaluate_batch_limits(
    file_sizes: Iterable[int],
    *,
    max_files: int | None = None,
    max_total_bytes: int | None = None,
) -> list[BatchLimitViolation]:
    """Evaluate per-batch hard caps on the files that will upload.

    Callers should pass the post-policy accepted file sizes. The backend
    route passes the request files directly because the web / CLI clients
    already strip ignored and blocked files before calling ``/upload/init``;
    direct API callers get the same protection on what they attempted to
    upload.
    """
    count = 0
    total_bytes = 0
    for size in file_sizes:
        count += 1
        total_bytes += size

    violations: list[BatchLimitViolation] = []
    file_limit = PER_BATCH_MAX_FILES if max_files is None else max_files
    total_limit = PER_BATCH_MAX_BYTES if max_total_bytes is None else max_total_bytes

    if count > file_limit:
        violations.append(BatchLimitViolation(
            kind="file_count",
            actual=count,
            limit=file_limit,
        ))
    if total_bytes > total_limit:
        violations.append(BatchLimitViolation(
            kind="total_bytes",
            actual=total_bytes,
            limit=total_limit,
        ))
    return violations
