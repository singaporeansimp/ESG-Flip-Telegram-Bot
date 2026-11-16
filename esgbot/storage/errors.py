"""What storage raises when the database cannot be used or trusted.

Everything here descends from :class:`StorageError`, so a caller that only wants
to know whether the database let it down can catch that one class. Ordinary
sqlite failures are not translated: a disk error or a constraint violation comes
out as the :class:`sqlite3.Error` it was, because there is nothing storage can
usefully add to it.
"""

from __future__ import annotations

__all__ = ["CorruptRecordError", "SchemaTooNewError", "StorageError"]


class StorageError(Exception):
    """Base class for storage problems that are not plain sqlite failures."""


class SchemaTooNewError(StorageError):
    """The database file was written by a newer version of the bot.

    Opening it would mean guessing at columns this version does not know about,
    so the open fails instead. ``found`` is the schema version in the file and
    ``supported`` is the newest this version can read.
    """

    def __init__(self, *, found: int, supported: int) -> None:
        super().__init__(
            f"database schema version {found} is newer than this version of the bot "
            f"supports ({supported}); upgrade the bot or restore an older database file"
        )
        self.found = found
        self.supported = supported


class CorruptRecordError(StorageError):
    """A stored row could not be read back as the value it is supposed to be.

    Raised when a seed, a commitment, a points fraction, a state, a timestamp or
    a stored deal does not parse, or when a player's totals add up to something
    no history could produce, such as more scoops than flips played — the shape
    of damage a hand-edited or truncated database file produces. The underlying
    :class:`ValueError` is kept as ``__cause__``, and ``record`` names the row so
    the problem can be found, for example ``"flip 42"``, ``"active flip
    'a1b2c3'"`` or ``"stats for player 11 in group -1001"``.

    This is raised rather than returned so no caller can mistake damaged data for
    a flip that really happened.
    """

    def __init__(self, record: str, reason: str) -> None:
        super().__init__(f"stored {record} is unreadable: {reason}")
        self.record = record
        self.reason = reason
