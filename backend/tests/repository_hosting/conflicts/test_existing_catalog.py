import json

import pytest

from tests.conflicts.cases import CASES
from tests.repository_hosting.harness.catalog import run_case
from tests.repository_hosting.harness.catalog_scope import runnable_cases

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize(
    "case", runnable_cases(CASES), ids=lambda case: case.id + "-" + case.category
)
@pytest.mark.asyncio
async def test_existing_conflict_catalog(case, component_repo, monkeypatch, request):
    actual = await run_case(case, component_repo, monkeypatch)
    request.node.user_properties.extend(
        [
            ("case_id", case.id),
            ("outcomes", json.dumps(actual.outcomes)),
            ("errors", json.dumps(actual.errors)),
        ]
    )
    assert "error" not in actual.outcomes, actual.errors
    if case.expected.writer_outcomes:
        assert actual.outcomes == list(case.expected.writer_outcomes), (
            actual.outcomes,
            actual.errors,
        )
    assert actual.pending == case.expected.pending_conflicts
    for path, wanted in case.expected.final_state.items():
        got = actual.files.get(path)
        if path.endswith(".json") and wanted is not None and got is not None:
            try:
                expected_json = json.loads(wanted)
            except (ValueError, UnicodeError):
                assert got == wanted, path
            else:
                assert json.loads(got) == expected_json, path
        else:
            assert got == wanted, path
    assert "committed" in actual.outcomes or all(o == "rejected" for o in actual.outcomes)
    # Even cases without a declared final-state oracle must retain readable confirmed commits.
    for oid in actual.commits:
        kind, body = component_repo.repo.store.get_object(oid)
        assert kind == "commit" and b"tree " in body
