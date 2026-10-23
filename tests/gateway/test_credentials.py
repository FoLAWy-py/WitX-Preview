"""Where the Gateway keeps its credential."""

import stat
from pathlib import Path

from keyring.backends import fail
from keyring.errors import KeyringError
from witx_gateway.credentials import CredentialStore

from tests.gateway.keystore import MemoryKeyring


def test_the_credential_is_kept_in_the_system_key_store(tmp_path: Path) -> None:
    keyring = MemoryKeyring()
    store = CredentialStore(tmp_path / "gateway.sqlite3", keyring)
    assert store.load() is None
    store.save("wgw_first")
    store.save("wgw_second")
    assert store.load() == "wgw_second"
    assert list(keyring.items.values()) == ["wgw_second"]
    assert not store.fallback.exists()
    # Another Gateway on the same host (another journal) has its own credential.
    assert CredentialStore(tmp_path / "other.sqlite3", keyring).load() is None


def test_without_a_key_store_the_credential_is_in_a_file_only_its_owner_can_read(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path / "gateway.sqlite3", fail.Keyring())
    store.save("wgw_first")
    store.save("wgw_second")
    assert store.load() == "wgw_second"
    assert stat.S_IMODE(store.fallback.stat().st_mode) == 0o600


def test_a_credential_the_key_store_would_not_take_replaces_the_one_it_holds(
    tmp_path: Path,
) -> None:
    keyring = MemoryKeyring()
    store = CredentialStore(tmp_path / "gateway.sqlite3", keyring)
    store.save("wgw_first")

    def refuse(service: str, username: str, password: str) -> None:
        raise KeyringError("locked")

    keyring.set_password = refuse  # type: ignore[method-assign]
    store.save("wgw_second")  # a rotation while the key store is locked
    assert CredentialStore(tmp_path / "gateway.sqlite3", keyring).load() == "wgw_second"

    del keyring.set_password  # unlocked again: the next one goes to the key store
    store.save("wgw_third")
    assert store.load() == "wgw_third" and not store.fallback.exists()
