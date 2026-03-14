"""Schema migration system for agent-recall databases.

Simple, file-less migration system. Each migration is a Python function
registered in MIGRATIONS. Migrations run in order, each updating the
schema version. New databases start at the latest version.

Usage::

    from agent_recall.migrations import run_migrations
    run_migrations(conn)  # called automatically by MemoryStore.__init__
"""

from __future__ import annotations

import sqlite3
from typing import Callable

# Type alias for migration functions
MigrationFn = Callable[[sqlite3.Connection], None]


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    """Create the schema_version table if it doesn't exist."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY)"
    )


def get_version(conn: sqlite3.Connection) -> int | None:
    """Get the current schema version, or None if no version is set.

    Returns None for databases that predate the migration system.
    """
    _ensure_version_table(conn)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return row[0]


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the schema version (inside an existing transaction)."""
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


# ---------------------------------------------------------------------------
# Migration functions
# ---------------------------------------------------------------------------
# Each migration receives an open sqlite3.Connection and is run inside a
# transaction. Do NOT commit or begin transactions inside migrations.
#
# Convention: migrate_NNN_short_description(conn)
# ---------------------------------------------------------------------------

def migrate_001_baseline(conn: sqlite3.Connection) -> None:
    """Baseline migration for existing databases.

    All tables already use IF NOT EXISTS, so existing databases are fine.
    This migration just establishes the version tracking baseline.
    """
    # Nothing to do -- the version bump is handled by run_migrations.
    pass


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------
# Ordered list of (version, function) tuples. Version numbers start at 1
# and must be consecutive.
MIGRATIONS: list[tuple[int, MigrationFn]] = [
    (1, migrate_001_baseline),
]

# The latest schema version (for stamping new databases).
LATEST_VERSION: int = MIGRATIONS[-1][0] if MIGRATIONS else 0


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations to the database.

    Called automatically by ``MemoryStore.__init__`` after table creation.

    For brand-new databases (no schema_version table or no version row),
    the version is stamped to ``LATEST_VERSION`` without running migrations,
    since ``_init_tables()`` already creates the current schema.

    For existing databases, migrations are applied in order. Each migration
    runs inside its own transaction for atomicity -- if a migration fails,
    only that migration is rolled back and the error propagates.

    Args:
        conn: An open SQLite connection (with ``journal_mode=WAL`` recommended).

    Returns:
        The schema version after migration.

    Raises:
        Exception: If any migration function fails. The failed migration is
            rolled back; earlier migrations that succeeded are preserved.
    """
    _ensure_version_table(conn)

    current = get_version(conn)

    if current is None:
        # New database or pre-migration database.
        # Check if this is genuinely new (no entities table data) or
        # an existing database that predates migrations.
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='entities'"
        ).fetchone()
        has_entities_table = row[0] > 0

        if has_entities_table:
            # Check if there's any data -- empty entities table means new DB
            data_row = conn.execute(
                "SELECT COUNT(*) FROM entities"
            ).fetchone()
            has_data = data_row[0] > 0
        else:
            has_data = False

        if not has_data:
            # Brand new database -- stamp to latest, no migrations needed
            _set_version(conn, LATEST_VERSION)
            conn.commit()
            return LATEST_VERSION
        else:
            # Existing database without version tracking -- start from 0
            current = 0
            _set_version(conn, 0)
            conn.commit()

    # Apply pending migrations
    for version, migrate_fn in MIGRATIONS:
        if version <= current:
            continue
        # Each migration in its own transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate_fn(conn)
            _set_version(conn, version)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        current = version

    return current
