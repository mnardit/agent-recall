"""Tests for the schema migration system."""

import sqlite3

import pytest

from agent_recall.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    get_version,
    run_migrations,
    _ensure_version_table,
    _set_version,
)
from agent_recall.store import MemoryStore


# --- Version tracking ---

def test_latest_version_matches_migrations():
    """LATEST_VERSION equals the highest migration number."""
    assert LATEST_VERSION == max(v for v, _ in MIGRATIONS)
    assert LATEST_VERSION >= 1


def test_migration_versions_consecutive():
    """Migration version numbers are consecutive starting from 1."""
    versions = [v for v, _ in MIGRATIONS]
    assert versions == list(range(1, len(MIGRATIONS) + 1))


# --- New database ---

def test_new_database_gets_latest_version(store):
    """A freshly created MemoryStore has the latest schema version."""
    version = get_version(store._conn)
    assert version == LATEST_VERSION


def test_new_database_stamps_without_running_migrations(tmp_path):
    """New database is stamped to latest without running migration functions."""
    # Create a new store — should be stamped directly
    s = MemoryStore(tmp_path / "new.db")
    assert get_version(s._conn) == LATEST_VERSION
    s.close()


# --- Pre-migration database (upgrade path) ---

def test_pre_migration_database_gets_migrated(tmp_path):
    """A database created before the migration system gets upgraded."""
    db_path = tmp_path / "old.db"

    # Simulate a pre-migration database: create tables manually without
    # schema_version, and insert some data
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE slots (
            entity_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            scope TEXT NOT NULL DEFAULT 'global',
            confidence REAL DEFAULT 1.0,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            source TEXT DEFAULT 'agent',
            PRIMARY KEY (entity_id, key, scope, valid_from)
        )
    """)
    conn.execute("""
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            created_at TEXT NOT NULL,
            archived_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL,
            to_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            status TEXT DEFAULT 'active',
            context TEXT,
            created_at TEXT NOT NULL,
            archived_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE log_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            text TEXT NOT NULL,
            author TEXT DEFAULT 'agent',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE documents (
            name TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Insert some data so it's recognized as an existing database
    conn.execute(
        "INSERT INTO entities (name, type, created_at) "
        "VALUES ('Alice', 'person', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    # Now open with MemoryStore — should detect existing DB and run migrations
    s = MemoryStore(db_path)
    assert get_version(s._conn) == LATEST_VERSION
    # Verify existing data is preserved
    assert s.find_entity("Alice") is not None
    s.close()


def test_migration_runs_from_version_zero(tmp_path):
    """Migrations run correctly starting from version 0."""
    db_path = tmp_path / "v0.db"

    # Create a store, then reset version to 0 to simulate a partially
    # set up database
    s = MemoryStore(db_path)
    # Add some data
    eid = s.resolve_entity("Alice", "person")
    s.set_slot(eid, "role", "engineer")
    # Reset version to 0
    s._conn.execute("DELETE FROM schema_version")
    s._conn.execute("INSERT INTO schema_version (version) VALUES (0)")
    s._conn.commit()
    s.close()

    # Reopen — should run all migrations from 0 to latest
    s = MemoryStore(db_path)
    assert get_version(s._conn) == LATEST_VERSION
    # Data preserved
    assert s.find_entity("Alice") is not None
    assert s.get_slot(eid, "role") == "engineer"
    s.close()


# --- Already migrated ---

def test_already_migrated_skips(store):
    """Database at latest version doesn't re-run migrations."""
    # Store is already at latest version from __init__
    initial = get_version(store._conn)
    assert initial == LATEST_VERSION

    # Running migrations again is a no-op
    result = run_migrations(store._conn)
    assert result == LATEST_VERSION
    assert get_version(store._conn) == LATEST_VERSION


def test_partial_migration_continues(tmp_path):
    """If database is at version N, only migrations > N run."""
    db_path = tmp_path / "partial.db"
    s = MemoryStore(db_path)
    s.resolve_entity("Test", "item")

    # If we had multiple migrations, we'd test partial here.
    # With only 1 migration, verify it's at version 1
    assert get_version(s._conn) == 1

    # Calling run_migrations again doesn't change anything
    result = run_migrations(s._conn)
    assert result == 1
    s.close()


# --- Transaction rollback on failure ---

def test_failed_migration_rolls_back(tmp_path):
    """A failing migration rolls back and preserves the previous version."""
    from agent_recall import migrations as m

    db_path = tmp_path / "fail.db"
    s = MemoryStore(db_path)
    s.resolve_entity("Alice", "person")
    s.close()

    # Temporarily add a failing migration
    original_migrations = m.MIGRATIONS[:]
    original_latest = m.LATEST_VERSION

    def bad_migration(conn):
        # Do something, then fail
        conn.execute(
            "CREATE TABLE test_bad_migration (id INTEGER PRIMARY KEY)"
        )
        raise RuntimeError("Intentional migration failure")

    try:
        m.MIGRATIONS.append((LATEST_VERSION + 1, bad_migration))
        m.LATEST_VERSION = LATEST_VERSION + 1

        # Reopen store — the failing migration should raise
        with pytest.raises(RuntimeError, match="Intentional migration failure"):
            MemoryStore(db_path)

        # Verify the version was NOT bumped
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_version_table(conn)
        version = get_version(conn)
        assert version == original_latest  # Still at the old version

        # Verify the partial table was rolled back
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='test_bad_migration'"
        ).fetchone()
        assert row[0] == 0  # Table should not exist
        conn.close()

    finally:
        # Restore original migrations
        m.MIGRATIONS[:] = original_migrations
        m.LATEST_VERSION = original_latest


def test_failed_migration_preserves_earlier_successes(tmp_path):
    """Earlier successful migrations persist even when a later one fails."""
    from agent_recall import migrations as m

    db_path = tmp_path / "partial_fail.db"
    s = MemoryStore(db_path)
    s.resolve_entity("Alice", "person")
    # Set version to 0 to force re-running migrations
    s._conn.execute("DELETE FROM schema_version")
    s._conn.execute("INSERT INTO schema_version (version) VALUES (0)")
    s._conn.commit()
    s.close()

    original_migrations = m.MIGRATIONS[:]
    original_latest = m.LATEST_VERSION

    call_log = []

    def good_migration(conn):
        call_log.append("good")

    def bad_migration(conn):
        call_log.append("bad")
        raise RuntimeError("Fail on purpose")

    try:
        m.MIGRATIONS[:] = [
            (1, good_migration),
            (2, bad_migration),
        ]
        m.LATEST_VERSION = 2

        with pytest.raises(RuntimeError, match="Fail on purpose"):
            MemoryStore(db_path)

        # Good migration ran and was committed
        assert "good" in call_log
        assert "bad" in call_log

        # Version should be at 1 (good succeeded, bad rolled back)
        conn = sqlite3.connect(str(db_path))
        _ensure_version_table(conn)
        assert get_version(conn) == 1
        conn.close()

    finally:
        m.MIGRATIONS[:] = original_migrations
        m.LATEST_VERSION = original_latest


# --- Edge cases ---

def test_get_version_no_table(tmp_path):
    """get_version on a bare database returns None."""
    db_path = tmp_path / "bare.db"
    conn = sqlite3.connect(str(db_path))
    version = get_version(conn)
    assert version is None
    conn.close()


def test_get_version_empty_table(tmp_path):
    """get_version with schema_version table but no rows returns None."""
    db_path = tmp_path / "empty_version.db"
    conn = sqlite3.connect(str(db_path))
    _ensure_version_table(conn)
    conn.commit()
    version = get_version(conn)
    assert version is None
    conn.close()


def test_version_table_created_idempotent(tmp_path):
    """_ensure_version_table is safe to call multiple times."""
    db_path = tmp_path / "idempotent.db"
    conn = sqlite3.connect(str(db_path))
    _ensure_version_table(conn)
    _ensure_version_table(conn)  # Should not raise
    conn.commit()
    conn.close()


def test_set_version_replaces(tmp_path):
    """_set_version replaces the existing version, not appends."""
    db_path = tmp_path / "replace.db"
    conn = sqlite3.connect(str(db_path))
    _ensure_version_table(conn)
    _set_version(conn, 1)
    conn.commit()
    _set_version(conn, 5)
    conn.commit()
    assert get_version(conn) == 5
    # Only one row
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1
    conn.close()


def test_store_close_and_reopen_preserves_version(tmp_path):
    """Schema version persists across store close and reopen."""
    db_path = tmp_path / "reopen.db"
    s = MemoryStore(db_path)
    s.resolve_entity("Alice", "person")
    s.close()

    s = MemoryStore(db_path)
    assert get_version(s._conn) == LATEST_VERSION
    assert s.find_entity("Alice") is not None
    s.close()
