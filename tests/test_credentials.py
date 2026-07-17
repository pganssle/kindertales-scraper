"""Tests for credential storage."""

from collections.abc import Callable

import keyring.errors
import pytest

from kindertales_scraper import credentials


def test_existing_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing password does not cause a prompt."""
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *_: "secret")
    assert credentials.password("a@example.com") == ("secret", True)


@pytest.mark.parametrize("operation", ["get", "set"])
def test_password_keyring_failure(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A keyring failure leaves a prompted password in memory only."""

    def fail(*_args: object) -> None:
        raise keyring.errors.KeyringError

    monkeypatch.setattr(
        credentials.keyring,
        "get_password",
        fail if operation == "get" else lambda *_: None,
    )
    monkeypatch.setattr(credentials.keyring, "set_password", fail)
    assert credentials.password("a@example.com", lambda _: "entered") == (
        "entered",
        False,
    )


def test_prompted_password_is_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing password is prompted for and persisted."""
    saved: list[tuple[object, ...]] = []
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *_: None)
    monkeypatch.setattr(
        credentials.keyring, "set_password", lambda *args: saved.append(args)
    )
    assert credentials.password("a@example.com", lambda _: "entered") == (
        "entered",
        True,
    )
    assert saved == [(credentials.SERVICE, "a@example.com", "entered")]


def test_key_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session key uses its reserved keyring username."""
    saved: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        credentials.keyring, "get_password", lambda *args: ":".join(args)
    )
    monkeypatch.setattr(
        credentials.keyring, "set_password", lambda *args: saved.append(args)
    )
    credentials.set_password("a@example.com", "password")
    credentials.set_session_key("key")
    assert (
        credentials.get_session_key()
        == f"{credentials.SERVICE}:{credentials.SESSION_KEY_USER}"
    )
    assert saved[-1] == (credentials.SERVICE, credentials.SESSION_KEY_USER, "key")


@pytest.mark.parametrize(
    "function", [credentials.set_password, lambda *_: credentials.get_session_key()]
)
def test_required_keyring_failure(
    monkeypatch: pytest.MonkeyPatch,
    function: Callable[..., object],
) -> None:
    """Explicit persistent operations report unavailable keyrings."""

    def fail(*_args: object) -> None:
        raise keyring.errors.KeyringError

    monkeypatch.setattr(credentials.keyring, "get_password", fail)
    monkeypatch.setattr(credentials.keyring, "set_password", fail)
    with pytest.raises(credentials.CredentialStoreUnavailableError):
        function("user", "value")


def test_delete_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deletion ignores absent values and deletes both usernames."""
    deleted: list[str] = []

    def delete(_service: str, username: str) -> None:
        deleted.append(username)
        if username == credentials.SESSION_KEY_USER:
            raise keyring.errors.PasswordDeleteError("absent")

    monkeypatch.setattr(credentials.keyring, "delete_password", delete)
    credentials.delete("a@example.com")
    assert deleted == ["a@example.com", credentials.SESSION_KEY_USER]


def test_delete_keyring_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deletion reports an unavailable keyring."""

    def fail(*_args: object) -> None:
        raise keyring.errors.KeyringError

    monkeypatch.setattr(credentials.keyring, "delete_password", fail)
    with pytest.raises(credentials.CredentialStoreUnavailableError):
        credentials.delete("a@example.com")
