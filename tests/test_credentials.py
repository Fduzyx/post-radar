import pytest

from post_radar.credentials import SERVICE, WindowsCredentialStore
from post_radar.models import ExternalError


class FakeVault:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


def test_only_isolated_namespace_is_accessed(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = FakeVault()
    store = WindowsCredentialStore()
    monkeypatch.setattr(store, "_vault", lambda: vault)
    store.set("api-key", "synthetic-value")
    assert vault.values == {(SERVICE, "api-key"): "synthetic-value"}
    assert store.get("api-key") == "synthetic-value"
    store.delete("api-key")
    assert store.get("api-key") is None
    with pytest.raises(ValueError):
        store.get("arbitrary-service")


def test_vault_failure_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    store = WindowsCredentialStore()

    def fail() -> FakeVault:
        raise RuntimeError("SYNTHETIC-PRIVATE-DETAIL")

    monkeypatch.setattr(store, "_vault", fail)
    with pytest.raises(ExternalError) as failure:
        store.get("api-key")
    assert str(failure.value) == "credential_store_unavailable"
