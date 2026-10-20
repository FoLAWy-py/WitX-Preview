"""An in-memory system key store, so tests never touch the real one."""

from pathlib import Path

from keyring.backend import KeyringBackend
from witx_gateway.credentials import CredentialStore


class MemoryKeyring(KeyringBackend):
    priority = -1  # type: ignore[assignment]  # never picked as the default key store

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.items.pop((service, username), None)


def stored(journal: Path, credential: str) -> CredentialStore:
    """A credential store that already holds ``credential``."""
    store = CredentialStore(journal, MemoryKeyring())
    store.save(credential)
    return store
