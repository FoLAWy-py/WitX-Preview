"""Local SQLite journal: the Gateway's memory of what it was asked to do and did.

The rule that matters: ``intent`` is committed to disk *before* input is sent to the
device. After a crash, an action found in ``intent`` may or may not have happened,
so it is reported as ``unknown`` and never executed again. Results wait in the
outbox until the cloud acknowledges them, so nothing is lost across reconnects.

Keep the journal on a local disk (SQLite WAL does not work on network filesystems).

``actions`` and ``seen`` are never pruned: a row there is what makes a resent ID a repeat,
and without it only the action's deadline, read on this machine's clock, would stand
between a resend and a second input. One person's phones add a few hundred rows a day.
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

SCHEMA = """
CREATE TABLE IF NOT EXISTS epochs (
  device_id      TEXT PRIMARY KEY,
  epoch          INTEGER NOT NULL DEFAULT 0,
  stopped_epoch  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS actions (
  action_id    TEXT PRIMARY KEY,
  device_id    TEXT NOT NULL,
  digest       TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ('received', 'intent', 'done')),
  outcome      TEXT,
  reason       TEXT,
  received_at  TEXT NOT NULL,
  finished_at  TEXT
);
CREATE TABLE IF NOT EXISTS seen (
  ref_id  TEXT PRIMARY KEY            -- observation IDs already handled
);
CREATE TABLE IF NOT EXISTS outbox (
  ref_id      TEXT PRIMARY KEY,
  message     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
  native_id   TEXT PRIMARY KEY,
  identity    TEXT NOT NULL,          -- who answered first (iOS device ID, Android androidId)
  changed_to  TEXT,                   -- another device answered since: refused until forgotten
  pinned_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class JournalAction:
    action_id: UUID
    device_id: UUID
    digest: str
    state: str
    outcome: str | None
    reason: str | None


class Journal:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)

    def _tx(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(sql, params).fetchall()
                self._db.execute("COMMIT")
                return rows
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _script(self, *statements: tuple[str, tuple[object, ...]]) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in statements:
                    self._db.execute(sql, params)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------ epochs and stops

    def set_epoch(self, device_id: UUID, epoch: int) -> int:
        """Records a newer writer epoch. Epochs never go back."""
        rows = self._tx(
            """INSERT INTO epochs (device_id, epoch) VALUES (?, ?)
               ON CONFLICT (device_id) DO UPDATE SET epoch = MAX(epoch, excluded.epoch)
               RETURNING epoch""",
            str(device_id),
            epoch,
        )
        return int(rows[0][0])

    def stop(self, device_id: UUID, epoch: int) -> None:
        """Raises the stop barrier: no input for this epoch (or older) runs after this."""
        self._tx(
            """INSERT INTO epochs (device_id, epoch, stopped_epoch) VALUES (?, ?, ?)
               ON CONFLICT (device_id) DO UPDATE
               SET stopped_epoch = MAX(stopped_epoch, excluded.stopped_epoch)""",
            str(device_id),
            epoch,
            epoch,
        )

    def epochs(self, device_id: UUID) -> tuple[int, int]:
        """(current epoch, highest stopped epoch)."""
        rows = self._tx(
            "SELECT epoch, stopped_epoch FROM epochs WHERE device_id = ?", str(device_id)
        )
        return (int(rows[0][0]), int(rows[0][1])) if rows else (0, 0)

    # ------------------------------------------------------------ actions

    def receive(self, action_id: UUID, device_id: UUID, digest: str) -> JournalAction | None:
        """Records a new action. Returns the existing record if the ID was seen before."""
        inserted = self._tx(
            """INSERT INTO actions (action_id, device_id, digest, state, received_at)
               VALUES (?, ?, ?, 'received', ?)
               ON CONFLICT (action_id) DO NOTHING RETURNING action_id""",
            str(action_id),
            str(device_id),
            digest,
            _now(),
        )
        return None if inserted else self.action(action_id)

    def action(self, action_id: UUID) -> JournalAction | None:
        rows = self._tx(
            "SELECT action_id, device_id, digest, state, outcome, reason FROM actions"
            " WHERE action_id = ?",
            str(action_id),
        )
        if not rows:
            return None
        a, d, digest, state, outcome, reason = rows[0]
        return JournalAction(
            UUID(str(a)),
            UUID(str(d)),
            str(digest),
            str(state),
            outcome and str(outcome),
            reason and str(reason),
        )

    def intent(self, action_id: UUID) -> None:
        """Must be committed before the device is touched."""
        self._tx("UPDATE actions SET state = 'intent' WHERE action_id = ?", str(action_id))

    def finish(self, action_id: UUID, outcome: str, reason: str | None, message: str) -> None:
        """Stores the outcome and queues the result message in one transaction."""
        self._script(
            (
                """UPDATE actions SET state = 'done', outcome = ?, reason = ?, finished_at = ?
                   WHERE action_id = ?""",
                (outcome, reason, _now(), str(action_id)),
            ),
            (
                "INSERT OR REPLACE INTO outbox (ref_id, message, created_at) VALUES (?, ?, ?)",
                (str(action_id), message, _now()),
            ),
        )

    def interrupted(self) -> list[JournalAction]:
        """Actions left unfinished by a previous process."""
        rows = self._tx("SELECT action_id FROM actions WHERE state IN ('received', 'intent')")
        found = [self.action(UUID(str(r[0]))) for r in rows]
        return [a for a in found if a is not None]

    # ------------------------------------------------------------ observations and outbox

    # ------------------------------------------------------------ device identities

    def pinned_identity(self, native_id: str) -> str | None:
        """Who answered for this device the first time, kept across restarts."""
        rows = self._tx("SELECT identity FROM identities WHERE native_id = ?", native_id)
        return str(rows[0][0]) if rows else None

    def pin_identity(self, native_id: str, identity: str) -> None:
        self._tx(
            "INSERT OR IGNORE INTO identities (native_id, identity, pinned_at) VALUES (?, ?, ?)",
            native_id,
            identity,
            _now(),
        )

    def identity_changed(self, native_id: str, seen: str) -> None:
        self._tx("UPDATE identities SET changed_to = ? WHERE native_id = ?", seen, native_id)

    def changed_identity(self, native_id: str) -> str | None:
        rows = self._tx("SELECT changed_to FROM identities WHERE native_id = ?", native_id)
        return str(rows[0][0]) if rows and rows[0][0] else None

    def forget_identity(self, native_id: str) -> bool:
        """A person confirmed the device behind ``native_id`` is the one they mean now."""
        return bool(
            self._tx("DELETE FROM identities WHERE native_id = ? RETURNING native_id", native_id)
        )

    def mark_seen(self, ref_id: UUID) -> bool:
        """True the first time an ID is seen."""
        rows = self._tx(
            "INSERT INTO seen (ref_id) VALUES (?) ON CONFLICT DO NOTHING RETURNING ref_id",
            str(ref_id),
        )
        return bool(rows)

    def enqueue(self, ref_id: UUID, message: str) -> None:
        self._tx(
            "INSERT OR REPLACE INTO outbox (ref_id, message, created_at) VALUES (?, ?, ?)",
            str(ref_id),
            message,
            _now(),
        )

    def outbox(self) -> list[str]:
        rows = self._tx("SELECT message FROM outbox ORDER BY created_at")
        return [str(r[0]) for r in rows]

    def outbox_message(self, ref_id: UUID) -> str | None:
        rows = self._tx("SELECT message FROM outbox WHERE ref_id = ?", str(ref_id))
        return str(rows[0][0]) if rows else None

    def acknowledge(self, ref_id: UUID) -> None:
        self._tx("DELETE FROM outbox WHERE ref_id = ?", str(ref_id))
