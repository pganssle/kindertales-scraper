"""Playwright authentication and protected session caching."""

import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import attrs
from cryptography import fernet
from playwright import async_api

from . import config, credentials, redaction

State = Mapping[str, Any]
Validator = Callable[[State], Awaitable[bool]]
Authenticator = Callable[[], Awaitable[State]]


class AuthenticationError(RuntimeError):
    """Raised after a fresh login does not yield a valid session."""


@attrs.define
class SessionCache:
    """Encrypted or explicitly plaintext browser session storage."""

    settings: config.Config
    warning_stream: Any = sys.stderr
    _memory: State | None = attrs.field(default=None, init=False)

    @property
    def path(self) -> Path:
        """Return the configured cache file path."""
        return self.settings.cache_directory / "session.json"

    def load(self) -> State | None:
        """Load cached state, returning no state for absent or invalid ciphertext."""
        if self._memory is not None:
            return self._memory
        if not self.path.exists():
            return None
        payload = self.path.read_bytes()
        if self.settings.allow_plaintext_session_cache:
            return self._decode(payload)
        try:
            key = credentials.get_session_key()
        except credentials.CredentialStoreUnavailableError:
            key = None
        if key is None:
            return None
        try:
            cleartext = fernet.Fernet(key.encode("ascii")).decrypt(payload)
        except (fernet.InvalidToken, ValueError):
            self.delete()
            return None
        return self._decode(cleartext)

    def save(self, state: State) -> None:
        """Persist state securely, or retain it in memory when no keyring exists."""
        payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        if self.settings.allow_plaintext_session_cache:
            print(
                "WARNING: writing plaintext browser session state; protect this file.",
                file=self.warning_stream,
            )
            self._write(payload)
            return
        try:
            key = credentials.get_session_key()
            if key is None:
                key = fernet.Fernet.generate_key().decode("ascii")
                credentials.set_session_key(key)
        except credentials.CredentialStoreUnavailableError:
            self._memory = state
            return
        self._write(fernet.Fernet(key.encode("ascii")).encrypt(payload))

    def delete(self) -> None:
        """Remove cached state from memory and disk."""
        self._memory = None
        self.path.unlink(missing_ok=True)

    def _write(self, payload: bytes) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def _decode(self, payload: bytes) -> State | None:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.delete()
            return None
        if not isinstance(value, dict):
            self.delete()
            return None
        return value


@attrs.frozen
class SessionManager:
    """Validate cached state and perform at most one fresh authentication."""

    cache: SessionCache

    async def state(self, validate: Validator, authenticate: Authenticator) -> State:
        """Return a validated session state."""
        cached = self.cache.load()
        if cached is not None and await validate(cached):
            return cached
        self.cache.delete()
        fresh = await authenticate()
        if not await validate(fresh):
            msg = "Kindertales rejected the newly authenticated session"
            raise AuthenticationError(msg)
        self.cache.save(fresh)
        return fresh


@attrs.frozen
class PlaywrightLogin:
    """Interactive Playwright login which allows the user to complete MFA."""

    login_url: str = "https://app.kindertales.com/"
    authenticated_url_pattern: str = (
        "https://app.kindertales.com/index.php?pg=dashboard*"
    )
    timeout_ms: float = 300_000

    async def authenticate(
        self,
        email: str,
        password: str,
        *,
        headed: bool,
    ) -> State:
        """Log in and capture cookies, local storage, and IndexedDB state."""
        async with async_api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not headed)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(self.login_url)
                identifier = page.locator('[data-test-id="input-identifier"]')
                password_input = page.locator('[data-test-id="input-password"]')
                submit = page.locator('[data-test-id="submit-btn"]')
                await identifier.fill(email)
                await submit.click()
                await password_input.fill(password)
                await submit.click()
                await page.wait_for_url(
                    self.authenticated_url_pattern,
                    timeout=self.timeout_ms,
                )
                return await context.storage_state(indexed_db=True)
            except async_api.Error as error:
                diagnostic = redaction.text(str(error).replace(password, "REDACTED"))
                msg = f"Kindertales browser authentication failed: {diagnostic}"
                raise AuthenticationError(msg) from None
            finally:
                await browser.close()
