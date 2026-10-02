import pytest

from tests.repository_hosting.harness.postgres import Postgres


@pytest.fixture
def pg_project():
    """Isolated synthetic tenant; its owning disposable cluster performs cleanup."""
    pg = Postgres()
    return pg, pg.create_project()
