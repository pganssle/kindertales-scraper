"""Tests for authentication and session caching."""

import io
import json
import secrets
from pathlib import Path
from unittest import mock

import pytest
from cryptography import fernet

from kindertales_scraper import auth, config, credentials


@pytest.fixture
def settings(tmp_path: Path) -> config.Config:
    """Return configuration with an isolated cache."""
    return config.Config(email="a@example.com", cache_directory=tmp_path / "cache")


def test_encrypted_cache_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Config,
) -> None:
    """Encrypted state round-trips without exposing cleartext."""
    keys: list[str] = []
    monkeypatch.setattr(
        credentials, "get_session_key", lambda: keys[-1] if keys else None
    )
    monkeypatch.setattr(credentials, "set_session_key", keys.append)
    cache = auth.SessionCache(settings)
    state = {"cookies": [{"name": "session", "value": "secret"}]}
    cache.save(state)
    assert b"secret" not in cache.path.read_bytes()
    assert cache.path.stat().st_mode & 0o777 == 0o600
    assert cache.path.parent.stat().st_mode & 0o777 == 0o700
    assert cache.load() == state
    cache.save({"cookies": []})
    assert cache.load() == {"cookies": []}


@pytest.mark.parametrize("payload", [b"tampered", fernet.Fernet.generate_key()])
def test_invalid_ciphertext_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Config,
    payload: bytes,
) -> None:
    """Invalid encrypted cache data is rejected and removed."""
    settings.cache_directory.mkdir()
    cache = auth.SessionCache(settings)
    cache.path.write_bytes(payload)
    monkeypatch.setattr(
        credentials,
        "get_session_key",
        lambda: fernet.Fernet.generate_key().decode(),
    )
    assert cache.load() is None
    assert not cache.path.exists()


def test_plaintext_cache_requires_opt_in(tmp_path: Path) -> None:
    """Plaintext mode warns, uses private permissions, and validates JSON."""
    warning = io.StringIO()
    settings = config.Config(
        email="a@example.com",
        cache_directory=tmp_path,
        allow_plaintext_session_cache=True,
    )
    cache = auth.SessionCache(settings, warning)
    cache.save({"cookies": []})
    assert json.loads(cache.path.read_text(encoding="utf-8")) == {"cookies": []}
    assert "WARNING" in warning.getvalue()
    assert cache.load() == {"cookies": []}


@pytest.mark.parametrize("payload", [b"not-json", b"[]"])
def test_invalid_plaintext_is_deleted(tmp_path: Path, payload: bytes) -> None:
    """Malformed or non-object plaintext state is invalidated."""
    settings = config.Config(
        email="a@example.com",
        cache_directory=tmp_path,
        allow_plaintext_session_cache=True,
    )
    cache = auth.SessionCache(settings)
    cache.path.write_bytes(payload)
    assert cache.load() is None
    assert not cache.path.exists()


def test_unavailable_keyring_uses_memory(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Config,
) -> None:
    """Without a keyring, state remains in memory and is never written."""

    def unavailable() -> None:
        raise credentials.CredentialStoreUnavailableError

    monkeypatch.setattr(credentials, "get_session_key", unavailable)
    cache = auth.SessionCache(settings)
    cache.save({"cookies": []})
    assert cache.load() == {"cookies": []}
    cache.delete()
    assert cache.load() is None
    assert not cache.path.exists()


def test_keyring_failing_while_storing_key_uses_memory(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Config,
) -> None:
    """A failure to persist a new encryption key leaves no unusable cache file."""
    monkeypatch.setattr(credentials, "get_session_key", lambda: None)

    def unavailable(_key: str) -> None:
        raise credentials.CredentialStoreUnavailableError

    monkeypatch.setattr(credentials, "set_session_key", unavailable)
    cache = auth.SessionCache(settings)
    cache.save({"cookies": []})
    assert cache.load() == {"cookies": []}
    assert not cache.path.exists()


@pytest.mark.parametrize("key", [None, "unavailable"])
def test_encrypted_load_without_key_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Config,
    key: str | None,
) -> None:
    """An encrypted file cannot be loaded without its keyring key."""
    settings.cache_directory.mkdir()
    cache = auth.SessionCache(settings)
    cache.path.write_bytes(b"ciphertext")
    if key == "unavailable":

        def get_key() -> None:
            raise credentials.CredentialStoreUnavailableError

        monkeypatch.setattr(credentials, "get_session_key", get_key)
    else:
        monkeypatch.setattr(credentials, "get_session_key", lambda: None)
    assert cache.load() is None


@pytest.mark.asyncio
async def test_cached_session_is_reused(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Config,
) -> None:
    """A valid cached session bypasses authentication."""
    cache = auth.SessionCache(settings)
    monkeypatch.setattr(
        credentials,
        "get_session_key",
        lambda: (_ for _ in ()).throw(credentials.CredentialStoreUnavailableError),
    )
    cache.save({"valid": True})
    manager = auth.SessionManager(cache)

    async def validate(state: auth.State) -> bool:
        return bool(state["valid"])

    async def fail() -> auth.State:
        pytest.fail("authentication should not run")

    assert await manager.state(validate, fail) == {"valid": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [None, {"valid": False}])
async def test_invalid_cache_reauthenticates_once(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Config,
    cached: auth.State | None,
) -> None:
    """Absent and rejected state trigger one fresh login."""
    cache = auth.SessionCache(settings)
    monkeypatch.setattr(
        credentials,
        "get_session_key",
        lambda: (_ for _ in ()).throw(credentials.CredentialStoreUnavailableError),
    )
    if cached is not None:
        cache.save(cached)
    manager = auth.SessionManager(cache)
    calls = 0

    async def validate(state: auth.State) -> bool:
        return bool(state["valid"])

    async def authenticate() -> auth.State:
        nonlocal calls
        calls += 1
        return {"valid": True}

    assert await manager.state(validate, authenticate) == {"valid": True}
    assert calls == 1


@pytest.mark.asyncio
async def test_rejected_fresh_session_stops(settings: config.Config) -> None:
    """A rejected fresh login does not retry indefinitely."""
    manager = auth.SessionManager(auth.SessionCache(settings))

    async def reject(_state: auth.State) -> bool:
        return False

    async def authenticate() -> auth.State:
        return {"valid": False}

    with pytest.raises(auth.AuthenticationError, match="newly authenticated"):
        await manager.state(reject, authenticate)


def test_missing_cache(settings: config.Config) -> None:
    """An absent cache is represented by None."""
    assert auth.SessionCache(settings).load() is None


@pytest.mark.asyncio
async def test_playwright_login_captures_complete_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login waits through MFA and requests IndexedDB in the captured state."""
    email_field = mock.Mock(fill=mock.AsyncMock())
    password_field = mock.Mock(fill=mock.AsyncMock())
    login_button = mock.Mock(click=mock.AsyncMock())
    page = mock.Mock(
        goto=mock.AsyncMock(),
        locator=mock.Mock(side_effect=[email_field, password_field, login_button]),
        wait_for_url=mock.AsyncMock(),
    )
    context = mock.Mock(
        new_page=mock.AsyncMock(return_value=page),
        storage_state=mock.AsyncMock(return_value={"cookies": []}),
    )
    browser = mock.Mock(
        new_context=mock.AsyncMock(return_value=context),
        close=mock.AsyncMock(),
    )
    chromium = mock.Mock(launch=mock.AsyncMock(return_value=browser))
    playwright = mock.Mock(chromium=chromium)

    class PlaywrightContext:
        async def __aenter__(self) -> mock.Mock:
            return playwright

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(auth.async_api, "async_playwright", PlaywrightContext)
    login = auth.PlaywrightLogin(
        authenticated_url_pattern="https://example.test/**",
        timeout_ms=12,
    )
    assert await login.authenticate("a@example.com", "secret", headed=True) == {
        "cookies": []
    }
    chromium.launch.assert_awaited_once_with(headless=False)
    page.goto.assert_awaited_once_with(login.login_url)
    assert page.locator.call_args_list == [
        mock.call('[data-test-id="input-identifier"]'),
        mock.call('[data-test-id="input-password"]'),
        mock.call('[data-test-id="submit-btn"]'),
    ]
    email_field.fill.assert_awaited_once_with("a@example.com")
    password_field.fill.assert_awaited_once_with("secret")
    assert login_button.click.await_count == 2
    page.wait_for_url.assert_awaited_once_with(
        "https://example.test/**",
        timeout=12,
    )
    context.storage_state.assert_awaited_once_with(indexed_db=True)
    browser.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_playwright_login_redacts_password_from_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser diagnostics never expose the submitted password."""
    secret = secrets.token_urlsafe(12)
    identifier = mock.Mock(fill=mock.AsyncMock())
    password = mock.Mock(
        fill=mock.AsyncMock(
            side_effect=auth.async_api.Error(f'fill("{secret}") timed out')
        )
    )
    submit = mock.Mock(click=mock.AsyncMock())
    page = mock.Mock(
        goto=mock.AsyncMock(),
        locator=mock.Mock(side_effect=[identifier, password, submit]),
    )
    context = mock.Mock(new_page=mock.AsyncMock(return_value=page))
    browser = mock.Mock(
        new_context=mock.AsyncMock(return_value=context),
        close=mock.AsyncMock(),
    )
    playwright = mock.Mock(
        chromium=mock.Mock(launch=mock.AsyncMock(return_value=browser))
    )

    class PlaywrightContext:
        async def __aenter__(self) -> mock.Mock:
            return playwright

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(auth.async_api, "async_playwright", PlaywrightContext)
    with pytest.raises(auth.AuthenticationError) as caught:
        await auth.PlaywrightLogin().authenticate(
            "a@example.com",
            secret,
            headed=False,
        )
    assert secret not in str(caught.value)
    assert "REDACTED" in str(caught.value)
    browser.close.assert_awaited_once_with()
