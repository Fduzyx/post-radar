"""An isolated Windows vault namespace. No environment or plaintext fallback."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Protocol, cast

from post_radar.models import ExternalError

SERVICE = "PostRadar/v1"
NAMES = frozenset({"api-key", "smtp-password"})


class CredentialStore(Protocol):
    def get(self, name: str) -> str | None: ...


class Vault(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def _name(name: str) -> None:
    if name not in NAMES:
        raise ValueError("unknown_credential_name")


class WindowsCredentialStore:
    def _vault(self) -> Vault:
        if sys.platform != "win32":
            raise ExternalError("credential_store_unavailable", permanent=True)
        from keyring.backends.Windows import WinVaultKeyring

        factory = cast(Callable[[], Vault], WinVaultKeyring)
        return factory()

    def get(self, name: str) -> str | None:
        _name(name)
        try:
            return self._vault().get_password(SERVICE, name)
        except Exception:
            raise ExternalError("credential_store_unavailable", permanent=True) from None

    def set(self, name: str, value: str) -> None:
        _name(name)
        if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
            raise ValueError("invalid_credential")
        try:
            self._vault().set_password(SERVICE, name, value)
        except Exception:
            raise ExternalError("credential_store_unavailable", permanent=True) from None

    def delete(self, name: str) -> None:
        _name(name)
        try:
            self._vault().delete_password(SERVICE, name)
        except Exception:
            raise ExternalError("credential_store_unavailable", permanent=True) from None
