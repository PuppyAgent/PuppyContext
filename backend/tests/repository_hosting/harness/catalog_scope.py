"""Explicit boundary of the old scenario inventory executable by this runner.

Excluded rows are not coverage and are never counted as passing tests. Their
recipes require a different entrypoint or describe obsolete scope ownership.
Keep the original inventory unchanged so those differences remain reviewable.
"""

import pytest

EXCLUDED = {
    "C10": "root/scope overlap requires simultaneous scoped snapshots",
    "D01": "parent ownership recipe predates canonical-root writes",
    "D03": "parent ownership recipe predates canonical-root writes",
    "D04": "requires asserting old automatic scope routing, not only final bytes",
    "D06": "expects independent child tree surviving canonical-root deletion",
    "D07": "requires post-commit scope-move hooks; this harness disables projections",
    "D09": "expects three commits for one canonical-root transaction",
    "D10": "parent ownership recipe predates canonical-root writes",
    "D11": "needs a separately seeded stale scoped commit and intervening writes",
    "D12": "requires HTTP grant enforcement; tested by Git transport read-only contract",
    "D13": "needs a scope deletion interleaved inside the PG publish transaction",
    "D14": "needs a scope move interleaved inside the PG publish transaction",
    "E08": "asks product explicit-base write to rebase; current API defines it as a precondition",
    "F04": "parent ownership recipe predates canonical-root writes",
    "F05": "requires configured policy injection; selection tested independently",
    "F06": "requires configured policy injection; selection tested independently",
    "F07": "expects manual conflict on first nonconflicting writer; cannot be an acceptance oracle",
}

GAPS = {
    "A04": "catalog expects conflict markers for scalar JSON conflict; current policy keeps incoming JSON",
    "B11": "CAS retry deletion currently removes concurrently modified content",
    "C01": "CAS retry deletion currently removes concurrently modified content",
}


def runnable_cases(cases):
    return [
        pytest.param(case, marks=pytest.mark.hosting_gap(GAPS[case.id]))
        if case.id in GAPS
        else case
        for case in cases
        if case.id not in EXCLUDED
    ]
