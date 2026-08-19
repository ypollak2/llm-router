"""Base adapter protocol for all storage backends.

Each adapter implements read/write/append operations for a specific backend
(JSON files, SQLite database, YAML configuration).
"""

from __future__ import annotations

from typing import Protocol, TypeVar

T = TypeVar("T")


class StorageAdapter(Protocol[T]):
    """Protocol all adapters must implement.

    Type parameter T is the data type the adapter persists:
    - JsonAdapter[dict] for budgets
    - SqliteAdapter[list[AuditEvent]] for audit logs
    - YamlAdapter[dict] for configuration
    """

    def read(self) -> T | None:
        """Read and return deserialized data.

        Returns:
            Deserialized data, or None if file missing/unreadable.
        """
        ...

    def write(self, data: T, atomic: bool = True) -> None:
        """Write data to storage.

        Args:
            data:   Data to persist
            atomic: If True, use temp file + rename (atomic); else direct write.

        Raises:
            OSError: On write failure
        """
        ...

    def append(self, data: T) -> None:
        """Append to log (audit/journal-style operations).

        Only used by audit log adapter. Budget/config adapters may raise NotImplementedError.

        Args:
            data: Entry to append

        Raises:
            OSError: On write failure
        """
        ...

    def verify_integrity(self) -> tuple[bool, str]:
        """Verify data hasn't been tampered with.

        Used by SQLite adapter to verify hash chain integrity.
        JSON/YAML adapters return (True, "n/a").

        Returns:
            (is_intact, explanation) tuple
        """
        ...
