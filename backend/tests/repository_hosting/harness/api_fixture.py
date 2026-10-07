"""Owned Supabase API fixture; refuses hosted or unspecified targets."""

import os

import pytest

from tests.repository_hosting.harness.supabase_api import SupabaseAPI


@pytest.fixture(scope="module")
def api():
    owned = SupabaseAPI(os.environ)
    try:
        owned.authenticate()
        yield owned
    finally:
        owned.close()
