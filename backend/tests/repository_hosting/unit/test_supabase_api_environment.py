"""HTTP tests must never authenticate against an ambient developer/hosted project."""

import pytest

pytestmark = pytest.mark.hosting_component


def environment():
    return {
        "HOSTING_TEST_SUPABASE": "1", "HOSTING_TEST_STACK": "puppy-baseline-abcdefgh",
        "SUPABASE_URL": "http://127.0.0.1:12345",
        "HOSTING_TEST_ANON_KEY": "fixture-anon", "SUPABASE_SERVICE_ROLE_KEY": "fixture-service",
    }


@pytest.mark.parametrize("override", [
    {"HOSTING_TEST_SUPABASE": "0"}, {"HOSTING_TEST_STACK": "native-pg-test"},
    {"SUPABASE_URL": "https://production.supabase.co"},
    {"SUPABASE_URL": "http://127.0.0.1:12345/proxy"},
    {"SUPABASE_URL": "http://secret@127.0.0.1:12345"},
    {"SUPABASE_URL": "http://127.0.0.1:12345?token=secret"},
    {"SUPABASE_URL": "http://127.0.0.1:not-a-port"},
])
def test_supabase_api_environment_rejects_nonowned_targets_before_client_creation(monkeypatch, override):
    from tests.repository_hosting.harness import supabase_api

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP client must not be constructed for unowned targets")

    monkeypatch.setattr(supabase_api.httpx, "Client", forbidden)
    with pytest.raises(RuntimeError, match="owned loopback Supabase"):
        supabase_api.SupabaseAPI(environment() | override)


def test_supabase_api_transport_does_not_proxy_loopback_or_follow_redirects(monkeypatch):
    from tests.repository_hosting.harness import supabase_api

    calls = []
    monkeypatch.setattr(supabase_api.httpx, "Client", lambda **kw: calls.append(kw))
    supabase_api.SupabaseAPI(environment())
    assert calls == [{"base_url": "http://127.0.0.1:12345", "timeout": 15,
                      "trust_env": False, "follow_redirects": False}]
