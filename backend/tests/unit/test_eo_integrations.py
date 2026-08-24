"""
tests/unit/test_eo_integrations.py — Patch 7e-S6.

eo/integrations.py had zero test coverage before this. Two things
worth pinning directly:

  1. Token encryption. access_token/refresh_token are stored
     Fernet-encrypted (module docstring's "Token encryption" section) --
     get_credentials() must decrypt back to the exact original string,
     and a corrupted/wrong-key ciphertext must degrade to None (treated
     as "no usable credential") rather than raising into a connector's
     request path (_decrypt()'s own InvalidToken handling).
  2. refresh_if_needed()'s branching: valid-and-not-expiring returns the
     stored token untouched (no network call); expired-with-no-expiry-
     info assumes still valid (a provider that never told us an expiry);
     expired-with-a-refresh_token actually calls the provider's token
     endpoint and re-persists; expired-with-no-refresh_token or an
     outright-failed refresh both return None rather than retrying
     blindly (module docstring: "caller should treat that as 'not
     connected'").

Isolation: integrations.py does `from eo import db` (module import) and
`from eo.audit_log import write_audit` (bound name) -- same two
patch points test_eo_audit_log.py and test_eo_chat_workspace.py already
establish conventions for: db.cursor patched via a FakeCursorContext
on `integrations.db`, write_audit patched directly on
`integrations.write_audit` since patching eo.audit_log.write_audit
would not reach the bound copy this module actually calls.

INTEGRATIONS_ENCRYPTION_KEY is read once into a lazy module-level
Fernet singleton (_fernet) the first time any encrypt/decrypt call
happens -- an autouse fixture sets a real, valid test key via
monkeypatch.setenv AND resets `_fernet` to None before every test, so
no test's key choice/prior _fernet instance leaks into the next.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

import eo.integrations as integrations


_TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _reset_encryption(monkeypatch):
    monkeypatch.setenv("INTEGRATIONS_ENCRYPTION_KEY", _TEST_KEY)
    integrations._ENCRYPTION_KEY = _TEST_KEY
    integrations._fernet = None
    yield
    integrations._fernet = None


@pytest.fixture(autouse=True)
def _stub_write_audit(monkeypatch):
    monkeypatch.setattr(integrations, "write_audit", MagicMock())


class FakeCursor:
    def __init__(self, fetchone_result=None):
        self.executed = []
        self._fetchone_result = fetchone_result

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return self._fetchone_result

    def fetchall(self):
        return self._fetchone_result or []


class FakeCursorContext:
    def __init__(self, cursor, calls_log, **kwargs):
        self.cursor = cursor
        self.calls_log = calls_log
        self.kwargs = kwargs

    def __enter__(self):
        self.calls_log.append(self.kwargs)
        return self.cursor

    def __exit__(self, *exc_info):
        return False


def _install_fake_cursor(monkeypatch, cursor, calls_log=None):
    calls_log = calls_log if calls_log is not None else []
    monkeypatch.setattr(
        integrations.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


def _row(**overrides):
    base = {
        "provider": "google_calendar",
        "account_label": "work@example.com",
        "access_token": integrations._encrypt("live-access-token"),
        "refresh_token": integrations._encrypt("live-refresh-token"),
        "scope": "calendar.readonly",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# encryption round-trip
# ---------------------------------------------------------------------

def test_encrypt_then_decrypt_returns_the_original_value():
    encrypted = integrations._encrypt("super-secret-token")
    assert encrypted != "super-secret-token"
    assert integrations._decrypt(encrypted) == "super-secret-token"


def test_encrypt_none_returns_none():
    assert integrations._encrypt(None) is None


def test_decrypt_none_returns_none():
    assert integrations._decrypt(None) is None


def test_decrypt_garbage_ciphertext_returns_none_not_raise():
    assert integrations._decrypt("not-a-real-fernet-token") is None


def test_decrypt_with_a_different_key_returns_none_not_raise():
    encrypted = integrations._encrypt("secret")
    # Simulate a rotated key -- ciphertext from before rotation is now
    # unreadable, must degrade to None per module docstring.
    integrations._fernet = None
    integrations._ENCRYPTION_KEY = Fernet.generate_key().decode()
    assert integrations._decrypt(encrypted) is None


def test_missing_encryption_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("INTEGRATIONS_ENCRYPTION_KEY", raising=False)
    integrations._ENCRYPTION_KEY = None
    integrations._fernet = None
    with pytest.raises(RuntimeError):
        integrations._encrypt("x")


# ---------------------------------------------------------------------
# save_credentials / get_credentials
# ---------------------------------------------------------------------

def test_save_credentials_upserts_with_user_scoped_cursor(monkeypatch):
    fake_cursor = FakeCursor()
    calls_log = _install_fake_cursor(monkeypatch, fake_cursor)

    integrations.save_credentials(
        "user_1", "google_calendar", "access-tok",
        refresh_token="refresh-tok", expires_in=3600,
        scope="calendar.readonly", account_label="work@example.com",
    )

    assert calls_log == [{"user_id": "user_1"}]
    query, params = fake_cursor.executed[0]
    assert "insert into user_integrations" in query
    assert params[0] == "user_1"
    assert params[1] == "google_calendar"
    assert params[2] == "work@example.com"
    # access_token/refresh_token stored encrypted, never in plaintext
    assert params[3] != "access-tok"
    assert integrations._decrypt(params[3]) == "access-tok"
    assert integrations._decrypt(params[4]) == "refresh-tok"


def test_save_credentials_writes_an_audit_record(monkeypatch):
    _install_fake_cursor(monkeypatch, FakeCursor())
    integrations.save_credentials("user_1", "google_calendar", "tok")
    integrations.write_audit.assert_called_once()
    args = integrations.write_audit.call_args[0]
    assert args[0] == "user_1"
    assert args[1] == "integration.connect"
    assert args[2] == "integration"
    assert args[3] == "google_calendar"


def test_get_credentials_returns_none_when_no_row(monkeypatch):
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=None))
    assert integrations.get_credentials("user_1", "google_calendar") is None


def test_get_credentials_decrypts_tokens_back_to_plaintext(monkeypatch):
    row = _row()
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))
    result = integrations.get_credentials("user_1", "google_calendar")
    assert result["access_token"] == "live-access-token"
    assert result["refresh_token"] == "live-refresh-token"
    assert result["provider"] == "google_calendar"
    assert result["account_label"] == "work@example.com"


def test_list_connected_never_includes_tokens(monkeypatch):
    fake_cursor = FakeCursor(fetchone_result=[
        {"provider": "google_calendar", "account_label": "work@example.com",
         "expires_at": datetime.now(timezone.utc), "created_at": datetime.now(timezone.utc)},
    ])
    _install_fake_cursor(monkeypatch, fake_cursor)
    result = integrations.list_connected("user_1")
    assert len(result) == 1
    assert "access_token" not in result[0]
    assert "refresh_token" not in result[0]
    assert result[0]["provider"] == "google_calendar"


def test_disconnect_writes_audit_only_when_a_row_was_actually_deleted(monkeypatch):
    fake_cursor = FakeCursor(fetchone_result={"id": "row_1"})
    _install_fake_cursor(monkeypatch, fake_cursor)
    integrations.disconnect("user_1", "google_calendar")
    integrations.write_audit.assert_called_once()
    args = integrations.write_audit.call_args[0]
    assert args[1] == "integration.disconnect"


def test_disconnect_skips_audit_when_nothing_was_connected(monkeypatch):
    fake_cursor = FakeCursor(fetchone_result=None)
    _install_fake_cursor(monkeypatch, fake_cursor)
    integrations.disconnect("user_1", "google_calendar")
    integrations.write_audit.assert_not_called()


# ---------------------------------------------------------------------
# refresh_if_needed
# ---------------------------------------------------------------------

def test_refresh_returns_none_when_nothing_connected(monkeypatch):
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=None))
    assert integrations.refresh_if_needed("user_1", "google_calendar") is None


def test_refresh_returns_stored_token_unchanged_when_not_expiring(monkeypatch):
    row = _row(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))
    mock_post = MagicMock()
    monkeypatch.setattr(integrations.requests, "post", mock_post)

    token = integrations.refresh_if_needed("user_1", "google_calendar")

    assert token == "live-access-token"
    mock_post.assert_not_called()


def test_refresh_assumes_valid_when_provider_never_gave_an_expiry(monkeypatch):
    row = _row(expires_at=None)
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))
    mock_post = MagicMock()
    monkeypatch.setattr(integrations.requests, "post", mock_post)

    token = integrations.refresh_if_needed("user_1", "google_calendar")

    assert token == "live-access-token"
    mock_post.assert_not_called()


def test_refresh_returns_none_when_expired_with_no_refresh_token(monkeypatch):
    row = _row(expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
               refresh_token=None)
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))
    assert integrations.refresh_if_needed("user_1", "google_calendar") is None


def test_refresh_returns_none_for_a_provider_with_no_refresh_config(monkeypatch):
    row = _row(provider="slack", expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))
    assert integrations.refresh_if_needed("user_1", "slack") is None


def test_refresh_returns_none_when_client_credentials_env_vars_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    row = _row(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))
    assert integrations.refresh_if_needed("user_1", "google_calendar") is None


def test_refresh_calls_the_providers_token_endpoint_and_persists_new_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    row = _row(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

    fake_cursor = FakeCursor(fetchone_result=row)
    calls_log = _install_fake_cursor(monkeypatch, fake_cursor)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new-access-token",
        "expires_in": 3600,
    }
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr(integrations.requests, "post", mock_post)

    token = integrations.refresh_if_needed("user_1", "google_calendar")

    assert token == "new-access-token"
    mock_post.assert_called_once()
    post_kwargs = mock_post.call_args
    assert post_kwargs[1]["data"]["refresh_token"] == "live-refresh-token"
    assert post_kwargs[1]["data"]["grant_type"] == "refresh_token"
    # save_credentials() ran again as part of the refresh -- a second
    # insert-on-conflict call beyond the initial read's select.
    insert_calls = [q for q, _ in fake_cursor.executed if "insert into user_integrations" in q]
    assert len(insert_calls) == 1


def test_refresh_returns_none_when_provider_returns_non_200(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    row = _row(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))

    mock_response = MagicMock()
    mock_response.status_code = 401
    monkeypatch.setattr(integrations.requests, "post", MagicMock(return_value=mock_response))

    assert integrations.refresh_if_needed("user_1", "google_calendar") is None


def test_refresh_returns_none_when_response_has_no_access_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    row = _row(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"expires_in": 3600}  # no access_token
    monkeypatch.setattr(integrations.requests, "post", MagicMock(return_value=mock_response))

    assert integrations.refresh_if_needed("user_1", "google_calendar") is None


def test_refresh_preserves_old_refresh_token_when_provider_doesnt_rotate_it(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    row = _row(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    fake_cursor = FakeCursor(fetchone_result=row)
    _install_fake_cursor(monkeypatch, fake_cursor)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "new-token", "expires_in": 3600}
    monkeypatch.setattr(integrations.requests, "post", MagicMock(return_value=mock_response))

    integrations.refresh_if_needed("user_1", "google_calendar")

    insert_query, insert_params = next(
        (q, p) for q, p in fake_cursor.executed if "insert into user_integrations" in q
    )
    # save_credentials() passes refresh_token=None straight through here
    # (payload.get("refresh_token") was absent) -- the actual "don't
    # clobber it" behavior lives in the upsert's own SQL
    # (coalesce(excluded.refresh_token, user_integrations.refresh_token),
    # asserted below by string match since a fake cursor can't execute
    # real SQL), not in this module's Python.
    assert insert_params[4] is None
    assert "coalesce(excluded.refresh_token, user_integrations.refresh_token)" in insert_query
