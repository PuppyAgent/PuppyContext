"""Unit tests for the PUP-3 folder-upload policy.

Covers the pure policy module
(``src.platform.upload.policy``); end-to-end HTTP rejection from
``/upload/init`` and ``/ap-fs/upload`` lives in the router test files
(``test_upload_init_policy.py``) which exercise the wiring.
"""

from __future__ import annotations

import pytest

from src.platform.upload.policy import (
    DEFAULT_BLOCKLIST_SEGMENTS,
    PER_BATCH_MAX_BYTES,
    PER_BATCH_MAX_FILES,
    PER_FILE_MAX_BYTES,
    PolicyOptions,
    evaluate_batch_limits,
    evaluate_file,
    is_blocked_segment,
    is_dotfile_path,
    matches_ignore_rules,
    parse_ignore_text,
    path_has_blocked_segment,
)


class TestBlocklist:
    def test_canonical_blocked_segments(self) -> None:
        for name in [".git", "node_modules", ".DS_Store", "__pycache__", "dist", "build", ".venv"]:
            assert is_blocked_segment(name), name

    def test_non_blocked_lookalikes(self) -> None:
        for name in [".gitignore", ".gitkeep", "git", "nodemodules", "build.gradle"]:
            assert not is_blocked_segment(name), name

    def test_path_walk_finds_first_blocked_segment(self) -> None:
        assert path_has_blocked_segment("repo/.git/config") == (True, ".git")
        assert path_has_blocked_segment("foo/node_modules/lodash/index.js") == (True, "node_modules")

    def test_path_walk_ignores_substring_matches(self) -> None:
        assert path_has_blocked_segment("my.gitignore") == (False, None)
        assert path_has_blocked_segment("src/foo.py") == (False, None)

    def test_empty_segments_skipped(self) -> None:
        # Double slash is normalized away — empty segment doesn't match.
        assert path_has_blocked_segment("foo//bar") == (False, None)


class TestDotfile:
    def test_any_segment_starting_with_dot(self) -> None:
        assert is_dotfile_path(".env")
        assert is_dotfile_path("foo/.bashrc")
        assert is_dotfile_path("./local/.config")
        assert not is_dotfile_path("src/foo.py")


class TestIgnoreParser:
    def test_skips_comments_and_blanks(self) -> None:
        rules = parse_ignore_text("# comment\n\n  \nfoo\n")
        assert len(rules) == 1
        assert rules[0].pattern == "foo"

    def test_negation(self) -> None:
        rules = parse_ignore_text("*.log\n!important.log\n")
        assert rules[0].negate is False
        assert rules[1].negate is True

    def test_dir_only(self) -> None:
        rules = parse_ignore_text("dist/\nbuild\n")
        assert rules[0].dir_only is True
        assert rules[1].dir_only is False

    def test_match_ignored(self) -> None:
        rules = parse_ignore_text("*.log\n")
        assert matches_ignore_rules("app.log", rules)
        assert not matches_ignore_rules("app.py", rules)

    def test_negation_reincludes(self) -> None:
        rules = parse_ignore_text("*.log\n!important.log\n")
        assert matches_ignore_rules("app.log", rules)
        assert not matches_ignore_rules("important.log", rules)

    def test_directory_rule_matches_children(self) -> None:
        rules = parse_ignore_text("dist/\n")
        assert matches_ignore_rules("dist/index.js", rules)
        # Plain file at root named ``dist`` is not a directory match.
        assert not matches_ignore_rules("dist", rules, is_dir=False)


class TestEvaluateFile:
    def test_default_options_block_blocklist(self) -> None:
        d = evaluate_file("repo/.git/config", 100, [], PolicyOptions())
        assert not d.accept
        assert d.reason == "blocklist:.git"

    def test_default_options_block_hidden(self) -> None:
        d = evaluate_file(".env", 100, [], PolicyOptions())
        assert not d.accept
        assert d.reason == "hidden"

    def test_default_options_accept_normal_file(self) -> None:
        d = evaluate_file("src/foo.py", 100, [], PolicyOptions())
        assert d.accept

    def test_per_file_size_cap(self) -> None:
        d = evaluate_file("src/big.bin", PER_FILE_MAX_BYTES + 1, [], PolicyOptions())
        assert not d.accept
        assert d.reason == "too_large"

    def test_include_blocklist_override(self) -> None:
        d = evaluate_file(
            "repo/.git/config", 100, [],
            PolicyOptions(include_blocklist=True),
        )
        # Blocklist allowed, but ``.git`` segments are also dotfiles —
        # so the dotfile rule blocks it next. User must opt-in to both.
        assert not d.accept
        assert d.reason == "hidden"

    def test_include_blocklist_and_hidden_overrides(self) -> None:
        d = evaluate_file(
            "repo/.git/config", 100, [],
            PolicyOptions(include_blocklist=True, include_hidden=True),
        )
        assert d.accept

    def test_gitignore_layer(self) -> None:
        rules = parse_ignore_text("*.log\n")
        d = evaluate_file("foo.log", 100, rules, PolicyOptions())
        assert not d.accept
        assert d.reason == "gitignore"

    def test_include_ignored_override(self) -> None:
        rules = parse_ignore_text("*.log\n")
        d = evaluate_file(
            "foo.log", 100, rules,
            PolicyOptions(include_ignored=True),
        )
        assert d.accept

    def test_stage_priority_blocklist_before_gitignore_before_hidden(self) -> None:
        """When multiple stages would reject, the first reject wins
        and reports a specific reason."""
        # .git is both blocklisted and a dotfile; blocklist wins.
        d = evaluate_file(".git/HEAD", 100, [], PolicyOptions())
        assert d.reason == "blocklist:.git"


class TestThresholds:
    def test_constants_match_product_doc(self) -> None:
        # If you change these, also update:
        #   - frontend/lib/uploadPolicy.ts
        #   - cli/src/commands/fs/lib/upload-policy.js
        #   - docs/proposals/PUP-3-folder-upload-policy.md (§2.5)
        assert PER_FILE_MAX_BYTES == 100 * 1024 * 1024
        assert PER_BATCH_MAX_FILES == 5000
        assert PER_BATCH_MAX_BYTES == 1024 * 1024 * 1024

    def test_batch_limit_allows_exact_caps(self) -> None:
        assert evaluate_batch_limits([PER_BATCH_MAX_BYTES]) == []
        assert evaluate_batch_limits([0] * PER_BATCH_MAX_FILES) == []

    def test_batch_limit_rejects_too_many_accepted_files(self) -> None:
        violations = evaluate_batch_limits([0] * (PER_BATCH_MAX_FILES + 1))

        assert [(v.kind, v.actual, v.limit) for v in violations] == [
            ("file_count", PER_BATCH_MAX_FILES + 1, PER_BATCH_MAX_FILES),
        ]

    def test_batch_limit_rejects_too_many_accepted_bytes(self) -> None:
        violations = evaluate_batch_limits([PER_BATCH_MAX_BYTES + 1])

        assert [(v.kind, v.actual, v.limit) for v in violations] == [
            ("total_bytes", PER_BATCH_MAX_BYTES + 1, PER_BATCH_MAX_BYTES),
        ]


class TestParity:
    """Catches drift between the backend module and the documented
    blocklist."""

    def test_blocklist_includes_required_minimum(self) -> None:
        required = {
            ".git", ".svn", ".hg",
            ".DS_Store", "Thumbs.db",
            "node_modules", "__pycache__", ".venv",
            ".next", "dist", "build",
        }
        missing = required - DEFAULT_BLOCKLIST_SEGMENTS
        assert not missing, f"blocklist missing required segments: {missing}"


class TestCrossLanguageParity:
    """Real cross-language parity check (replaces the comment that
    *claimed* a CI string comparison existed but never did).

    The upload policy is duplicated by hand in three files:
      - backend/src/platform/upload/policy.py   (source of truth)
      - frontend/lib/uploadPolicy.ts
      - cli/src/commands/fs/lib/upload-policy.js

    This test parses all three and asserts the blocklist segments and
    the five thresholds are byte-for-byte identical. It runs as part of
    pytest, so the parity guarantee is real and CI-enforced — change one
    file without the others and this fails.
    """

    import re as _re
    from pathlib import Path as _Path

    _REPO_ROOT = _Path(__file__).resolve().parents[3]
    _PY = _REPO_ROOT / "backend/src/platform/upload/policy.py"
    _TS = _REPO_ROOT / "frontend/lib/uploadPolicy.ts"
    _JS = _REPO_ROOT / "cli/src/commands/fs/lib/upload-policy.js"

    _THRESHOLD_NAMES = (
        "PER_FILE_MAX_BYTES",
        "PER_BATCH_MAX_FILES",
        "PER_BATCH_MAX_BYTES",
        "PREFLIGHT_FILE_THRESHOLD",
        "PREFLIGHT_BYTES_THRESHOLD",
    )

    @classmethod
    def _eval_int_expr(cls, expr: str) -> int:
        # Only digits, ``*`` and whitespace are allowed — a constrained
        # arithmetic eval, never arbitrary code.
        cleaned = expr.strip().rstrip(";").strip()
        if not cls._re.fullmatch(r"[0-9*\s]+", cleaned):
            raise ValueError(f"unexpected threshold expression: {expr!r}")
        value = 1
        for part in cleaned.split("*"):
            value *= int(part.strip())
        return value

    @classmethod
    def _extract_thresholds(cls, text: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for name in cls._THRESHOLD_NAMES:
            # Matches: ``NAME: int = 100 * 1024 * 1024`` (py) and
            # ``export const NAME = 100 * 1024 * 1024;`` (ts/js).
            m = cls._re.search(
                rf"{name}\s*(?::\s*int)?\s*=\s*([0-9*\s]+?)\s*(?:;|#|//|$)",
                text,
                cls._re.MULTILINE,
            )
            assert m, f"threshold {name} not found"
            out[name] = cls._eval_int_expr(m.group(1))
        return out

    @classmethod
    def _extract_blocklist(cls, text: str) -> set[str]:
        # Find the constructor call after the declaration —
        # ``frozenset(...)`` (py) or ``new Set(...)`` (ts/js) — and
        # paren-depth-match to its close. The contents are only string
        # literals + commas + comments (no nested parens), so depth
        # matching on ``(`` / ``)`` cleanly bounds the block. A plain
        # regex trips on the ``[`` in the ``frozenset[str]`` annotation.
        decl = text.index("DEFAULT_BLOCKLIST_SEGMENTS")
        open_paren = text.index("(", decl)
        depth = 0
        end = open_paren
        for i in range(open_paren, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = text[open_paren + 1:end]
        # Strip comment lines so commented-out entries don't count.
        lines = [
            ln for ln in body.splitlines()
            if not ln.strip().startswith(("#", "//"))
        ]
        return set(cls._re.findall(r"""['"]([^'"]+)['"]""", "\n".join(lines)))

    def test_thresholds_identical_across_languages(self) -> None:
        py = self._extract_thresholds(self._PY.read_text(encoding="utf-8"))
        ts = self._extract_thresholds(self._TS.read_text(encoding="utf-8"))
        js = self._extract_thresholds(self._JS.read_text(encoding="utf-8"))
        assert py == ts, f"py vs ts threshold drift: {py} != {ts}"
        assert py == js, f"py vs js threshold drift: {py} != {js}"
        # And they match the imported Python constants (catches a parser
        # that silently reads stale text).
        assert py["PER_FILE_MAX_BYTES"] == PER_FILE_MAX_BYTES
        assert py["PER_BATCH_MAX_FILES"] == PER_BATCH_MAX_FILES
        assert py["PER_BATCH_MAX_BYTES"] == PER_BATCH_MAX_BYTES

    def test_blocklist_identical_across_languages(self) -> None:
        py = self._extract_blocklist(self._PY.read_text(encoding="utf-8"))
        ts = self._extract_blocklist(self._TS.read_text(encoding="utf-8"))
        js = self._extract_blocklist(self._JS.read_text(encoding="utf-8"))
        assert py == ts, f"py vs ts blocklist drift: {py ^ ts}"
        assert py == js, f"py vs js blocklist drift: {py ^ js}"
        assert py == set(DEFAULT_BLOCKLIST_SEGMENTS)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
