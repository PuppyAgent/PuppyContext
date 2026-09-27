"""M-2 — OAuth callback must validate the server-issued state nonce.

The vulnerability: callbacks accepted any (code, user) pair, with no
proof that the redirect was initiated by this server. An attacker could
trick a logged-in victim into linking the attacker's third-party OAuth
identity to the victim's account.

Fix: every authorize call mints a state row in oauth_states; callback
consumes it atomically (validate provider + expiry, recover issuing user,
then delete).

These tests exercise OAuthStateRepository directly with a mocked
Supabase client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.provider.oauth.state_repository import OAuthStateRepository


def _make_repo(stored_row: dict | None = None):
    """Build a repo whose Supabase client returns stored_row from select(),
    and tracks insert / delete calls.

    All calls to client.table("oauth_states") return the SAME `table_mock`
    instance so the test can inspect insert/select/delete history through
    one place.
    """
    repo = OAuthStateRepository.__new__(OAuthStateRepository)
    client = MagicMock()
    repo._client = client  # type: ignore[attr-defined]
    repo.TABLE = "oauth_states"

    table_mock = MagicMock()

    insert_chain = MagicMock()
    delete_chain = MagicMock()
    select_chain = MagicMock()

    table_mock.insert.return_value = insert_chain
    insert_chain.execute.return_value = MagicMock(data=[{}])

    table_mock.delete.return_value = delete_chain
    delete_chain.eq.return_value = delete_chain
    delete_chain.execute.return_value = MagicMock(data=[{}])

    table_mock.select.return_value = select_chain
    select_chain.eq.return_value = select_chain
    select_chain.limit.return_value = select_chain
    select_chain.execute.return_value = MagicMock(
        data=[stored_row] if stored_row else []
    )

    client.table.return_value = table_mock
    return repo, table_mock, insert_chain, delete_chain


def _future():
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


def _past():
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


# ── issue ──────────────────────────────────────────────────────────────


def test_issue_inserts_random_state():
    repo, table_mock, insert_chain, _ = _make_repo()
    state = repo.issue(user_id="u-1", provider="github")

    assert state and len(state) >= 30  # secrets.token_urlsafe(32) ⇒ long
    insert_chain.execute.assert_called_once()
    inserted = table_mock.insert.call_args.args[0]
    assert inserted["state"] == state
    assert inserted["user_id"] == "u-1"
    assert inserted["provider"] == "github"


def test_issue_distinct_states_each_call():
    repo, *_ = _make_repo()
    a = repo.issue("u-1", "github")
    b = repo.issue("u-1", "github")
    assert a != b


# ── consume ────────────────────────────────────────────────────────────


def test_consume_valid_state_returns_true_and_deletes():
    row = {
        "user_id": "u-1", "provider": "github", "expires_at": _future(),
    }
    repo, _, _, delete_chain = _make_repo(stored_row=row)

    ok = repo.consume(state="abc", user_id="u-1", provider="github")
    assert ok is True
    # Single-use: the row must be deleted on success.
    delete_chain.execute.assert_called()


def test_consume_for_provider_returns_issuing_user_and_deletes():
    row = {
        "user_id": "u-1", "provider": "github", "expires_at": _future(),
    }
    repo, _, _, delete_chain = _make_repo(stored_row=row)

    user_id = repo.consume_for_provider(state="abc", provider="github")

    assert user_id == "u-1"
    delete_chain.execute.assert_called()


def test_consume_unknown_state_returns_false():
    repo, *_ = _make_repo(stored_row=None)
    assert repo.consume("does-not-exist", "u-1", "github") is False


def test_consume_blocks_user_mismatch():
    """Attacker tries to redeem a state issued to a different user."""
    row = {
        "user_id": "victim", "provider": "github", "expires_at": _future(),
    }
    repo, *_ = _make_repo(stored_row=row)
    assert repo.consume("abc", user_id="attacker", provider="github") is False


def test_consume_blocks_provider_mismatch():
    """State issued for one provider cannot be replayed for another."""
    row = {
        "user_id": "u-1", "provider": "github", "expires_at": _future(),
    }
    repo, *_ = _make_repo(stored_row=row)
    assert repo.consume("abc", user_id="u-1", provider="notion") is False


def test_consume_for_provider_blocks_provider_mismatch():
    row = {
        "user_id": "u-1", "provider": "github", "expires_at": _future(),
    }
    repo, *_ = _make_repo(stored_row=row)
    assert repo.consume_for_provider("abc", provider="notion") is None


def test_consume_blocks_expired_state():
    row = {
        "user_id": "u-1", "provider": "github", "expires_at": _past(),
    }
    repo, *_ = _make_repo(stored_row=row)
    assert repo.consume("abc", user_id="u-1", provider="github") is False


def test_consume_empty_state_returns_false():
    repo, *_ = _make_repo()
    assert repo.consume("", "u-1", "github") is False


def test_consume_replay_protection():
    """The fix's core property: the same state cannot be used twice.
    First consume succeeds, second returns False because the row is gone.
    """
    row = {
        "user_id": "u-1", "provider": "github", "expires_at": _future(),
    }
    repo = OAuthStateRepository.__new__(OAuthStateRepository)
    repo._client = MagicMock()  # type: ignore[attr-defined]
    repo.TABLE = "oauth_states"

    # Track state in a tiny in-memory store
    store: dict[str, dict] = {"abc": row}

    def fake_table(_name):
        m = MagicMock()
        # SELECT
        m.select.return_value.eq.return_value.limit.return_value.execute = (
            lambda: MagicMock(data=[store["abc"]] if "abc" in store else [])
        )
        # DELETE (mutates the in-memory store)
        def _delete():
            chain = MagicMock()
            def _eq(_col, val):
                if val == "abc":
                    store.pop("abc", None)
                return chain
            chain.eq.side_effect = _eq
            chain.execute.return_value = MagicMock(data=[{}])
            return chain
        m.delete.side_effect = _delete
        return m

    repo._client.table.side_effect = fake_table  # type: ignore[attr-defined]

    # First consume: ok
    assert repo.consume("abc", "u-1", "github") is True
    # Second consume of same state: must fail (row deleted)
    assert repo.consume("abc", "u-1", "github") is False
