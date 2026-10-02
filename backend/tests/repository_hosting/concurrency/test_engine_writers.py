import asyncio

import pytest

from src.version_engine.write_engine.tree_objects import flatten_tree_to_bytes

pytestmark = pytest.mark.hosting_component


@pytest.mark.asyncio
@pytest.mark.parametrize("writers", [2, 8, 20])
async def test_actual_overlapping_disjoint_writers_never_lose_acknowledged_files(
    component_repo, writers
):
    state = component_repo
    outcomes = await asyncio.gather(
        *[
            state.adapter.write_file(
                "test-proj",
                f"writer-{i}",
                f"payload-{i}".encode(),
                who=f"user:{i}",
                defer_projection=True,
            )
            for i in range(writers)
        ],
        return_exceptions=True,
    )
    successes = [
        (i, result) for i, result in enumerate(outcomes) if not isinstance(result, Exception)
    ]
    assert successes
    for result in outcomes:
        if isinstance(result, Exception):
            # Only an explicit bounded-CAS retry exhaustion is an allowed refusal.
            assert "CAS" in str(result) or "concurrent" in str(result).lower(), repr(result)
    files = flatten_tree_to_bytes(state.repo.store, state.repo.history.get_root_hash())
    for i, result in successes:
        assert result.status == "ok"
        assert files[f"writer-{i}"] == f"payload-{i}".encode()
        assert state.repo.store.get_object(result.commit_id)[0] == "commit"
    if writers == 2:
        assert len(successes) == 2
