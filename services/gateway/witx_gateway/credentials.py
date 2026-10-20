"""Where the Gateway keeps its long-term credential .

The system key store (macOS Keychain, Windows Credential Manager, Secret Service on
Linux) when there is one; otherwise a file only the Gateway's user can read. Servers
without a desktop session usually have no key store, hence the fallback.
"""

import logging
import os
from pathlib import Path

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError

log = logging.getLogger(__name__)

SERVICE = "witx-gateway"


class CredentialStore:
    """One Gateway's credential, keyed by its journal: the credential belongs with the
    local state of the Gateway that enrolled."""

    def __init__(self, journal: Path, backend: KeyringBackend | None = None) -> None:
        self.account = str(journal.resolve())
        self.fallback = journal.with_name(journal.name + ".credential")
        self._backend = backend or keyring.get_keyring()

    def load(self) -> str | None:
        try:
            stored = self._backend.get_password(SERVICE, self.account)
        except KeyringError as error:
            log.debug("no system key store: %s", error)
            stored = None
        if stored:
            return stored
        if self.fallback.exists():
            return self.fallback.read_text().strip() or None
        return None

    def save(self, credential: str) -> None:
        """Stores the credential, replacing the previous one. Raises if it could not be
        stored anywhere: the caller must then keep using the previous credential."""
        try:
            self._backend.set_password(SERVICE, self.account, credential)
        except KeyringError as error:
            log.warning(
                "no system key store (%s); keeping the credential in %s", error, self.fallback
            )
            self._write_fallback(credential)
            return
        self.fallback.unlink(missing_ok=True)  # an older copy must not outlive this one

    def _write_fallback(self, credential: str) -> None:
        temporary = self.fallback.with_name(self.fallback.name + ".new")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write(credential)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.fallback)
