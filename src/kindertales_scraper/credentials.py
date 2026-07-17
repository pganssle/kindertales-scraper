"""Credential storage through the operating-system keyring."""

import getpass
from collections.abc import Callable

import keyring
import keyring.errors

SERVICE = "kindertales-scraper"
SESSION_KEY_USER = "session-encryption-key"


class CredentialStoreUnavailable(RuntimeError):
    """Raised when a persistent credential operation requires a keyring."""


def password(email: str, prompt: Callable[[str], str] = getpass.getpass) -> tuple[str, bool]:
    """Return the account password and whether it came from persistent storage."""
    try:
        stored = keyring.get_password(SERVICE, email)
    except keyring.errors.KeyringError:
        return prompt("Kindertales password: "), False
    if stored is not None:
        return stored, True
    entered = prompt("Kindertales password: ")
    try:
        keyring.set_password(SERVICE, email, entered)
    except keyring.errors.KeyringError:
        return entered, False
    return entered, True


def set_password(email: str, value: str) -> None:
    """Store an account password."""
    try:
        keyring.set_password(SERVICE, email, value)
    except keyring.errors.KeyringError as error:
        raise CredentialStoreUnavailable from error


def get_session_key() -> str | None:
    """Read the cache encryption key."""
    try:
        return keyring.get_password(SERVICE, SESSION_KEY_USER)
    except keyring.errors.KeyringError as error:
        raise CredentialStoreUnavailable from error


def set_session_key(value: str) -> None:
    """Store the cache encryption key."""
    set_password(SESSION_KEY_USER, value)


def delete(email: str) -> None:
    """Delete the password and cache encryption key when present."""
    for username in (email, SESSION_KEY_USER):
        try:
            keyring.delete_password(SERVICE, username)
        except keyring.errors.PasswordDeleteError:
            continue
        except keyring.errors.KeyringError as error:
            raise CredentialStoreUnavailable from error
